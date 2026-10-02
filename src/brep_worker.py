from OCC.Core.BRep import BRep_Builder, BRep_Tool
from OCC.Core.BRepTools import breptools
from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Sewing
from OCC.Core.BRepCheck import BRepCheck_Analyzer
from OCC.Core.ShapeFix import ShapeFix_Shape
from OCC.Core.TopAbs import TopAbs_SHELL
from OCC.Core.TopoDS import TopoDS_Shape, topods
from occ_geometry import (
    combine, parts, solidify as build_solids, geometry_change_error,
    _count_topo, _count_surface_type,
)


def read_shape(path):
    shape = TopoDS_Shape()
    if not breptools.Read(shape, path, BRep_Builder()) or shape.IsNull():
        raise ValueError('could not read the input B-Rep')
    return shape


def write_shape(shape, path):
    if shape.IsNull() or not breptools.Write(shape, path):
        raise ValueError('could not write the result B-Rep')


def fix(input_path, output_path, settings):
    fixer = ShapeFix_Shape(read_shape(input_path))
    fixer.Perform()
    write_shape(fixer.Shape(), output_path)


def sew(input_path, output_path, settings):
    from OCC.Core.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID
    from OCC.Core.TopoDS import TopoDS_Iterator
    shape = read_shape(input_path)
    children = []
    iterator = TopoDS_Iterator(shape)
    while iterator.More():
        children.append(iterator.Value())
        iterator.Next()
    groups = children if (
        settings['SEW_PARTS_SEPARATELY']
        and children
        and shape.ShapeType() == TopAbs_COMPOUND
        and all(part.ShapeType() in (TopAbs_COMPOUND, TopAbs_SHELL, TopAbs_SOLID) for part in children)
    ) else [shape]
    result = []
    for part in groups:
        sewing = BRepBuilderAPI_Sewing(settings['tolerance'])
        sewing.Add(part)
        sewing.Perform()
        candidate = sewing.SewedShape()
        if candidate.IsNull():
            raise ValueError('sewing produced an empty shape')
        result.append(candidate)
    write_shape(combine(result), output_path)


def solidify(input_path, output_path, settings):
    write_shape(build_solids(read_shape(input_path)), output_path)


def repair_mesh(input_path, output_path, settings):
    import numpy as np
    from pathlib import Path
    from config import Settings
    from model_repair import repair_volume
    geometry_settings = Settings.from_mapping({key: value for key, value in settings.items() if key != 'mesh_path'})
    with np.load(settings['mesh_path'], allow_pickle=False) as mesh:
        shape = read_shape(input_path) if Path(input_path).is_file() else None
        result = repair_volume(shape, mesh['vertices'], mesh['faces'], settings=geometry_settings,
                               max_attached_area_ratio=geometry_settings.max_repair_attached_triangle_area_ratio)
    write_shape(result, output_path)


def refine(input_path, output_path, settings):
    from OCC.Core.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCC.Core.TopAbs import TopAbs_FACE
    results = []
    for part in parts(read_shape(input_path)):
        selected = part
        for angular in (settings['PLANAR_MERGE_ANGLE_RADIANS'], 0.0):
            unify = ShapeUpgrade_UnifySameDomain(part, True, True, True)
            unify.SetLinearTolerance(settings['tolerance'])
            unify.SetAngularTolerance(angular)
            unify.Build()
            candidate = unify.Shape()
            if candidate.IsNull():
                continue
            fixer = ShapeFix_Shape(candidate)
            fixer.Perform()
            candidate = fixer.Shape()
            if candidate.IsNull() or not BRepCheck_Analyzer(candidate).IsValid():
                continue
            if geometry_change_error(part, candidate, settings['tolerance']):
                continue
            if _count_topo(candidate, TopAbs_FACE) <= _count_topo(part, TopAbs_FACE):
                selected = candidate
                break
        results.append(selected)
    write_shape(combine(results), output_path)


def fill_gaps(input_path, output_path, settings):
    from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCC.Core.BRepGProp import brepgprop
    from OCC.Core.GProp import GProp_GProps
    from OCC.Core.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    results = []
    for part in parts(read_shape(input_path)):
        selected = part
        if part.ShapeType() == TopAbs_SHELL and (not BRep_Tool.IsClosed(topods.Shell(part))):
            free_bounds = ShapeAnalysis_FreeBounds(part, settings['tolerance'], True, True)
            allowed = not TopExp_Explorer(free_bounds.GetOpenWires(), TopAbs_WIRE).More()
            explorer = TopExp_Explorer(free_bounds.GetClosedWires(), TopAbs_WIRE)
            fills, fill_area = ([], 0.0)
            while allowed and explorer.More():
                wire = topods.Wire(explorer.Current())
                edges = TopExp_Explorer(wire, TopAbs_EDGE)
                count = 0
                while edges.More():
                    count += 1
                    edges.Next()
                if not 3 <= count <= settings['MAX_BREP_GAP_EDGE_COUNT']:
                    allowed = False
                    break
                maker = BRepBuilderAPI_MakeFace(wire, True)
                if not maker.IsDone():
                    allowed = False
                    break
                face = maker.Face()
                props = GProp_GProps()
                brepgprop.SurfaceProperties(face, props)
                fill_area += props.Mass()
                fills.append(face)
                explorer.Next()
            props = GProp_GProps()
            brepgprop.SurfaceProperties(part, props)
            if allowed and fills and (0 < fill_area <= props.Mass() * settings['MAX_BREP_GAP_AREA_RATIO']):
                sewing = BRepBuilderAPI_Sewing(settings['tolerance'])
                sewing.Add(combine([part, *fills]))
                sewing.Perform()
                closed = sewing.SewedShape()
                closed_parts = list(parts(closed)) if not closed.IsNull() else []
                if (
                    len(closed_parts) == 1
                    and closed_parts[0].ShapeType() == TopAbs_SHELL
                    and BRep_Tool.IsClosed(topods.Shell(closed_parts[0]))
                    and BRepCheck_Analyzer(closed).IsValid()
                ):
                    selected = closed
        results.append(selected)
    write_shape(combine(results), output_path)


def reconstruct_holes(input_path, output_path, settings):
    import math
    import numpy as np
    from reconstruction import _fit_circle
    from occ_geometry import volume
    from OCC.Core.Bnd import Bnd_Box
    from OCC.Core.BRepTools import breptools
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
    from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCC.Core.BRepBndLib import brepbndlib
    from OCC.Core.BRepClass3d import BRepClass3d_SolidClassifier
    from OCC.Core.BRepCheck import BRepCheck_Analyzer
    from OCC.Core.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCC.Core.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
    from OCC.Core.ShapeFix import ShapeFix_Shape
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_OUT, TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE
    from OCC.Core.TopoDS import topods
    from OCC.Core.gp import gp_Ax2, gp_Dir, gp_Pnt
    s = read_shape(input_path)

    def point_state(classifier, point):
        classifier.Perform(gp_Pnt(*point), max(settings['tolerance'] * 0.01, 1e-08))
        return classifier.State()


    def wire_points(wire):
        values = []
        e = TopExp_Explorer(wire, TopAbs_VERTEX)
        while e.More():
            p = BRep_Tool.Pnt(topods.Vertex(e.Current()))
            point = np.array((p.X(), p.Y(), p.Z()), dtype=float)
            if not any((np.linalg.norm(point - old) <= max(
                settings['tolerance'] * 0.01,
                1e-09,
            ) for old in values)):
                values.append(point)
            e.Next()
        return np.asarray(values, dtype=float)

    def fit_circle(points, normal):
        normal = np.asarray(normal, dtype=float)
        normal /= np.linalg.norm(normal)
        ref = np.array((1.0, 0.0, 0.0)) if abs(normal[0]) < 0.8 else np.array((0.0, 1.0, 0.0))
        u = np.cross(normal, ref)
        u /= np.linalg.norm(u)
        v = np.cross(normal, u)
        origin = points.mean(axis=0)
        delta = points - origin
        x = delta @ u
        y = delta @ v
        fitted = _fit_circle(np.column_stack((x, y)))
        if fitted is None:
            return None
        (cx, cy), radius = fitted
        center = origin + cx * u + cy * v
        radial = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        error = float(np.max(np.abs(radial - radius)) / radius)
        coords = sorted(((float(px), float(py)) for px, py in zip(x, y)))

        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
        lower = []
        for point in coords:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
                lower.pop()
            lower.append(point)
        upper = []
        for point in reversed(coords):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
                upper.pop()
            upper.append(point)
        hull = lower[:-1] + upper[:-1]
        if len(hull) != len(coords):
            return None
        area = abs(sum((hull[i][0] * hull[(i + 1) % len(hull)][1] - hull[(i + 1) % len(hull)][0] * hull[i][1] for i in range(len(hull))))) * 0.5
        if area <= 0:
            return None
        return (center, radius, float(np.max(radial)), error, normal, area)

    def openings(solid):
        result = []
        faces = TopExp_Explorer(solid, TopAbs_FACE)
        while faces.More():
            face = topods.Face(faces.Current())
            surface = BRepAdaptor_Surface(face, True)
            if surface.GetType() == GeomAbs_Plane:
                d = surface.Plane().Axis().Direction()
                normal = (d.X(), d.Y(), d.Z())
                outer = breptools.OuterWire(face)
                wires = TopExp_Explorer(face, TopAbs_WIRE)
                while wires.More():
                    wire = topods.Wire(wires.Current())
                    if not wire.IsSame(outer):
                        points = wire_points(wire)
                        if len(points) >= settings['ANALYTIC_HOLE_MIN_SIDES']:
                            fitted = fit_circle(points, normal)
                            if fitted is not None:
                                center, radius, max_radius, error, normal, area = fitted
                                if error <= settings['ANALYTIC_HOLE_FIT_ERROR_RATIO']:
                                    result.append((
                                        center,
                                        radius,
                                        max_radius,
                                        error,
                                        normal,
                                        len(points),
                                        area,
                                    ))
                    wires.Next()
            faces.Next()
        return result

    def reconstruct(solid):
        found = openings(solid)
        if not found:
            return solid
        box = Bnd_Box()
        brepbndlib.Add(solid, box)
        bounds = box.Get()
        diagonal = float(np.linalg.norm(np.asarray(bounds[3:]) - np.asarray(bounds[:3])))
        axis_cos = math.cos(settings['ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS'])
        options = []
        if settings['RECONSTRUCT_ANALYTIC_THROUGH_HOLES']:
            for left in range(len(found)):
                for right in range(left + 1, len(found)):
                    c1, r1, m1, e1, n1, k1, a1 = found[left]
                    c2, r2, m2, e2, n2, k2, a2 = found[right]
                    delta = c2 - c1
                    length = float(np.linalg.norm(delta))
                    if length <= max(settings['tolerance'], 1e-09):
                        continue
                    axis = delta / length
                    n1 = n1 / np.linalg.norm(n1)
                    n2 = n2 / np.linalg.norm(n2)
                    radius_error = abs(r1 - r2) / max(r1, r2)
                    area_error = abs(a1 - a2) / max(a1, a2)
                    lateral = float(np.linalg.norm(delta - n1 * np.dot(delta, n1)))
                    lateral_limit = max(
                        settings['tolerance'] * 5.0,
                        max(r1, r2) * settings['ANALYTIC_HOLE_FIT_ERROR_RATIO'],
                    )
                    if radius_error > settings['ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO']:
                        continue
                    if area_error > max(
                        settings['ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO'] * 2.0,
                        settings['ANALYTIC_HOLE_FIT_ERROR_RATIO'] * 2.0,
                    ):
                        continue
                    if abs(float(np.dot(n1, n2))) < axis_cos or abs(float(np.dot(n1, axis))) < axis_cos:
                        continue
                    if lateral > lateral_limit:
                        continue
                    options.append((length, left, right, axis))
        options.sort(key=lambda item: item[0])
        used = set()
        current = solid
        classifier = BRepClass3d_SolidClassifier(current)
        for length, left, right, axis in options:
            if left in used or right in used:
                continue
            c1, r1, m1, e1, n1, k1, a1 = found[left]
            c2, r2, m2, e2, n2, k2, a2 = found[right]
            fitted_radius = (r1 + r2) * 0.5
            clearance = max(fitted_radius * 1e-06, settings['tolerance'] * 0.01, 1e-09)
            cut_radius = max(m1, m2) + clearance
            ref = np.array((1.0, 0.0, 0.0)) if abs(axis[0]) < 0.8 else np.array((0.0, 1.0, 0.0))
            u = np.cross(axis, ref)
            u /= np.linalg.norm(u)
            v = np.cross(axis, u)
            wall_radius = max(m1, m2) + max(fitted_radius * 0.05, settings['tolerance'] * 5.0, 1e-06)
            tunnel_clear = True
            for fraction in (0.02, 0.1, 0.25, 0.5, 0.75, 0.9, 0.98):
                base = c1 + axis * (length * fraction)
                if point_state(classifier, base) != TopAbs_OUT:
                    tunnel_clear = False
                    break
                for angle in (
                    0.0,
                    math.pi * 0.25,
                    math.pi * 0.5,
                    math.pi * 0.75,
                    math.pi,
                    math.pi * 1.25,
                    math.pi * 1.5,
                    math.pi * 1.75,
                ):
                    radial = u * math.cos(angle) + v * math.sin(angle)
                    if (
                        point_state(classifier, base + radial * (fitted_radius * 0.8)) != TopAbs_OUT
                        or point_state(classifier, base + radial * wall_radius) != TopAbs_IN
                    ):
                        tunnel_clear = False
                        break
                if not tunnel_clear:
                    break
            if not tunnel_clear:
                continue
            margin = max(cut_radius * 0.05, length * 0.05, settings['tolerance'] * 10.0, 1e-07)
            start = c1 - axis * margin
            try:
                tool = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(gp_Pnt(*start), gp_Dir(*axis)),
                    cut_radius,
                    length + 2.0 * margin,
                ).Shape()
                before_volume = volume(current)
                before_faces = _count_topo(current, TopAbs_FACE)
                before_cylinders = _count_surface_type(current, GeomAbs_Cylinder)
                cut = BRepAlgoAPI_Cut(current, tool)
                cut.Build()
                candidate = cut.Shape()
                if not cut.IsDone() or candidate.IsNull():
                    continue
                fixer = ShapeFix_Shape(candidate)
                fixer.Perform()
                candidate = fixer.Shape()
                after_volume = volume(candidate)
                after_faces = _count_topo(candidate, TopAbs_FACE)
                after_cylinders = _count_surface_type(candidate, GeomAbs_Cylinder)
                removed_volume = before_volume - after_volume
                opening_area = (a1 + a2) * 0.5
                expected_removed = max(0.0, (math.pi * cut_radius * cut_radius - opening_area) * length)
                removal_slack = max(
                    expected_removed * 0.002,
                    math.pi * cut_radius * cut_radius * length * 1e-07,
                    settings['tolerance'] * cut_radius * length * 0.01,
                    1e-09,
                )
                volume_change = (before_volume - after_volume) / before_volume * 100.0 if before_volume > 0 else float('inf')
                valid = (
                    not candidate.IsNull()
                    and BRepCheck_Analyzer(candidate).IsValid()
                    and _count_topo(candidate, TopAbs_SOLID) == 1
                    and after_volume > 0
                    and after_volume <= before_volume
                    and removed_volume <= expected_removed + removal_slack
                    and volume_change <= settings['ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT']
                    and after_faces < before_faces
                    and after_cylinders > before_cylinders
                )
                if valid:
                    current = candidate
                    used.add(left)
                    used.add(right)
                    classifier = BRepClass3d_SolidClassifier(current)
            except Exception:
                continue
        if not settings['RECONSTRUCT_ANALYTIC_BLIND_HOLES']:
            return current
        classifier = BRepClass3d_SolidClassifier(current)
        for index, (center, radius, max_radius, error, normal, sides, area) in enumerate(found):
            if index in used:
                continue
            normal = normal / np.linalg.norm(normal)
            ref = np.array((1.0, 0.0, 0.0)) if abs(normal[0]) < 0.8 else np.array((0.0, 1.0, 0.0))
            u = np.cross(normal, ref)
            u /= np.linalg.norm(u)
            v = np.cross(normal, u)
            probe = max(radius * 0.001, settings['tolerance'] * 2.0, 1e-06)
            wall_radius = max_radius + max(radius * 0.05, settings['tolerance'] * 5.0, 1e-06)
            directions = []
            for sign in (-1.0, 1.0):
                axis = normal * sign
                inside = center + axis * probe
                if point_state(classifier, inside) != TopAbs_OUT:
                    continue
                ring = []
                for angle in (0.0, math.pi * 0.5, math.pi, math.pi * 1.5):
                    radial = u * math.cos(angle) + v * math.sin(angle)
                    ring.append(point_state(classifier, inside + radial * wall_radius) == TopAbs_IN)
                if all(ring):
                    directions.append(axis)
            if len(directions) != 1:
                continue
            axis = directions[0]
            step = max(radius * 0.25, probe * 2.0, diagonal / 512.0)
            max_depth = max(diagonal * 1.05, step)
            low = probe
            high = None
            depth = low + step
            while depth <= max_depth:
                state = point_state(classifier, center + axis * depth)
                if state == TopAbs_IN:
                    high = depth
                    break
                if state != TopAbs_OUT:
                    break
                low = depth
                depth += step
            if high is None:
                continue
            for iteration in range(28):
                middle = (low + high) * 0.5
                if point_state(classifier, center + axis * middle) == TopAbs_IN:
                    high = middle
                else:
                    low = middle
            hole_depth = (low + high) * 0.5
            if hole_depth <= probe * 4.0:
                continue
            verified = True
            for fraction in (0.02, 0.1, 0.25, 0.5, 0.75, 0.9, 0.98):
                base = center + axis * (hole_depth * fraction)
                if point_state(classifier, base) != TopAbs_OUT:
                    verified = False
                    break
                for angle in (
                    0.0,
                    math.pi * 0.25,
                    math.pi * 0.5,
                    math.pi * 0.75,
                    math.pi,
                    math.pi * 1.25,
                    math.pi * 1.5,
                    math.pi * 1.75,
                ):
                    radial = u * math.cos(angle) + v * math.sin(angle)
                    if (
                        point_state(classifier, base + radial * (radius * 0.8)) != TopAbs_OUT
                        or point_state(classifier, base + radial * wall_radius) != TopAbs_IN
                    ):
                        verified = False
                        break
                if not verified:
                    break
            bottom_probe = max(probe, min(radius * 0.01, hole_depth * 0.01))
            if verified:
                before_bottom = center + axis * (hole_depth - bottom_probe)
                after_bottom = center + axis * (hole_depth + bottom_probe)
                for radial_scale in (0.0, 0.45, 0.8):
                    for radial in (u, v):
                        if (
                            point_state(
                                classifier,
                                before_bottom + radial * (radius * radial_scale),
                            ) != TopAbs_OUT
                            or point_state(
                                classifier,
                                after_bottom + radial * (radius * radial_scale),
                            ) != TopAbs_IN
                        ):
                            verified = False
                            break
                    if not verified:
                        break
            if not verified:
                continue
            clearance = max(radius * 1e-06, settings['tolerance'] * 0.01, 1e-09)
            cut_radius = max_radius + clearance
            margin = max(cut_radius * 0.05, settings['tolerance'] * 10.0, 1e-07)
            end_clearance = clearance
            start = center - axis * margin
            try:
                tool = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(gp_Pnt(*start), gp_Dir(*axis)),
                    cut_radius,
                    hole_depth + margin + end_clearance,
                ).Shape()
                before_volume = volume(current)
                before_faces = _count_topo(current, TopAbs_FACE)
                before_cylinders = _count_surface_type(current, GeomAbs_Cylinder)
                cut = BRepAlgoAPI_Cut(current, tool)
                cut.Build()
                candidate = cut.Shape()
                if not cut.IsDone() or candidate.IsNull():
                    continue
                fixer = ShapeFix_Shape(candidate)
                fixer.Perform()
                candidate = fixer.Shape()
                after_volume = volume(candidate)
                after_faces = _count_topo(candidate, TopAbs_FACE)
                after_cylinders = _count_surface_type(candidate, GeomAbs_Cylinder)
                removed_volume = before_volume - after_volume
                expected_removed = max(
                    0.0,
                    (math.pi * cut_radius * cut_radius - area) * hole_depth + math.pi * cut_radius * cut_radius * end_clearance,
                )
                removal_slack = max(
                    expected_removed * 0.002,
                    math.pi * cut_radius * cut_radius * hole_depth * 1e-07,
                    settings['tolerance'] * cut_radius * hole_depth * 0.01,
                    1e-09,
                )
                volume_change = (before_volume - after_volume) / before_volume * 100.0 if before_volume > 0 else float('inf')
                valid = (
                    not candidate.IsNull()
                    and BRepCheck_Analyzer(candidate).IsValid()
                    and _count_topo(candidate, TopAbs_SOLID) == 1
                    and after_volume > 0
                    and after_volume <= before_volume
                    and abs(removed_volume - expected_removed) <= removal_slack
                    and volume_change <= settings['ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT']
                    and after_faces < before_faces
                    and after_cylinders > before_cylinders
                )
                if valid:
                    current = candidate
                    used.add(index)
                    classifier = BRepClass3d_SolidClassifier(current)
            except Exception:
                continue
        return current
    results = []
    for part in parts(s):
        results.append(reconstruct(topods.Solid(part)) if part.ShapeType() == TopAbs_SOLID else part)
    write_shape(combine(results), output_path)


def main():
    import json
    import sys
    operations = {
        'fix': fix,
        'sew': sew,
        'solidify': solidify,
        'repair_mesh': repair_mesh,
        'refine': refine,
        'fill_gaps': fill_gaps,
        'reconstruct_holes': reconstruct_holes,
    }
    operation, input_path, output_path, raw_settings = sys.argv[1:]
    operations[operation](input_path, output_path, json.loads(raw_settings))

if __name__ == '__main__':
    main()
