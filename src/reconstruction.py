import math
from config import Settings
from mesh_geometry import _mesh_quality_report
from occ_geometry import _validate_occ_shape, _count_topo, volume as _solid_volume
from OCC.Core.BRepPrimAPI import (BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere)
from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace
from OCC.Core.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCC.Core.TopAbs import TopAbs_SOLID, TopAbs_FACE


def _fit_sphere(verts, tris, relative_tolerance):
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    origin = points.mean(axis=0)
    scale = float(np.linalg.norm(np.ptp(points, axis=0)))
    if scale <= 0:
        return None
    local = (points - origin) / scale
    matrix = np.column_stack((2.0 * local, np.ones(len(points))))
    rhs = np.einsum('ij,ij->i', local, local)
    solution, _, rank, singular = np.linalg.lstsq(matrix, rhs, rcond=None)
    if rank != 4 or singular[-1] <= singular[0] * 1e-10:
        return None
    local_center = solution[:3]
    radius_sq = solution[3] + float(np.dot(local_center, local_center))
    if radius_sq <= 0:
        return None
    radius = math.sqrt(radius_sq) * scale
    center = origin + local_center * scale
    distances = np.linalg.norm(points - center, axis=1)
    error = float(np.max(np.abs(distances - radius)))
    if error > max(radius * relative_tolerance, 1e-09):
        return None
    triangles = local[np.asarray(tris, dtype=np.int32)]
    lower, upper = _triangle_radius_bounds(triangles - local_center)
    surface_error = max(float(np.max(np.abs(lower - radius / scale))),
                        float(np.max(np.abs(upper - radius / scale)))) * scale
    if not math.isfinite(surface_error) or surface_error > max(radius * relative_tolerance, 1e-09):
        return None
    centroids, normals = _face_geometry(local, tris)
    radial = centroids - local_center
    radial_length = np.linalg.norm(radial, axis=1)
    valid = radial_length > 1e-15
    alignment = np.abs(np.einsum('ij,ij->i', normals[valid], radial[valid] / radial_length[valid, None]))
    if not len(alignment) or float(np.quantile(alignment, 0.1)) < 0.98:
        return None
    return (center, radius, max(error, surface_error))


def _triangle_radius_bounds(triangles):
    import numpy as np
    triangles = np.asarray(triangles, dtype=np.float64)
    scale = np.max(np.abs(triangles), axis=(1, 2))
    triangles = np.divide(triangles, scale[:, None, None], out=np.zeros_like(triangles), where=scale[:, None, None] > 0)
    squared = np.einsum('ijk,ijk->ij', triangles, triangles)
    closest = squared.min(axis=1)
    first = triangles[:, 1] - triangles[:, 0]
    second = triangles[:, 2] - triangles[:, 0]
    normal = np.cross(first, second)
    normal_sq = np.einsum('ij,ij->i', normal, normal)
    plane = np.einsum('ij,ij->i', triangles[:, 0], normal)
    factor = np.divide(plane, normal_sq, out=np.zeros_like(plane), where=normal_sq > 0)
    projection = normal * factor[:, None]
    inside = normal_sq > 0
    for start, end in ((0, 1), (1, 2), (2, 0)):
        point = triangles[:, start]
        edge = triangles[:, end] - point
        length_sq = np.einsum('ij,ij->i', edge, edge)
        t = np.divide(-np.einsum('ij,ij->i', point, edge), length_sq,
                      out=np.zeros_like(length_sq), where=length_sq > 0)
        nearest = point + np.clip(t, 0.0, 1.0)[:, None] * edge
        closest = np.minimum(closest, np.einsum('ij,ij->i', nearest, nearest))
        side = np.einsum('ij,ij->i', np.cross(edge, projection - point), normal)
        inside &= side >= -normal_sq * 1e-12
    plane_sq = np.divide(plane * plane, normal_sq, out=np.full_like(plane, np.inf), where=normal_sq > 0)
    closest = np.where(inside, np.minimum(closest, plane_sq), closest)
    return np.sqrt(np.maximum(0.0, closest)) * scale, np.sqrt(squared.max(axis=1)) * scale


def _triangle_cone_residual_bounds(triangles, radius, slope):
    import numpy as np
    triangles = np.asarray(triangles, dtype=np.float64)
    scale = np.maximum(np.max(np.abs(triangles), axis=(1, 2)), abs(radius))
    triangles = np.divide(triangles, scale[:, None, None], out=np.zeros_like(triangles), where=scale[:, None, None] > 0)
    radii = np.divide(radius, scale, out=np.zeros_like(scale), where=scale > 0)

    def residual(points):
        return np.hypot(points[:, 0], points[:, 1]) - radii - slope * points[:, 2]

    values = np.hypot(triangles[:, :, 0], triangles[:, :, 1]) - radii[:, None] - slope * triangles[:, :, 2]
    lower = values.min(axis=1)
    upper = values.max(axis=1)
    for start, end in ((0, 1), (1, 2), (2, 0)):
        point = triangles[:, start]
        edge = triangles[:, end] - point
        squared = np.einsum('ij,ij->i', edge[:, :2], edge[:, :2])
        product = np.einsum('ij,ij->i', point[:, :2], edge[:, :2])
        nearest = np.divide(-product, squared, out=np.zeros_like(squared), where=squared > 0)
        radial_point = point[:, :2] + nearest[:, None] * edge[:, :2]
        height = np.hypot(radial_point[:, 0], radial_point[:, 1])
        axial = slope * edge[:, 2]
        stationary = (squared > 0) & (axial * axial < squared)
        denominator = np.sqrt(squared) * np.sqrt(np.maximum(squared - axial * axial, 0))
        correction = np.divide(axial * height, denominator, out=np.zeros_like(height), where=stationary & (denominator > 0))
        fraction = np.clip(nearest + correction, 0.0, 1.0)
        candidate = residual(point + fraction[:, None] * edge)
        lower = np.where(stationary, np.minimum(lower, candidate), lower)
    first = triangles[:, 1] - triangles[:, 0]
    second = triangles[:, 2] - triangles[:, 0]
    determinant = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
    u = np.divide(second[:, 0] * triangles[:, 0, 1] - second[:, 1] * triangles[:, 0, 0],
                  determinant, out=np.zeros_like(determinant), where=determinant != 0)
    v = np.divide(first[:, 1] * triangles[:, 0, 0] - first[:, 0] * triangles[:, 0, 1],
                  determinant, out=np.zeros_like(determinant), where=determinant != 0)
    inside = (determinant != 0) & (u >= 0) & (v >= 0) & (u + v <= 1)
    axis_z = triangles[:, 0, 2] + u * first[:, 2] + v * second[:, 2]
    lower = np.where(inside, np.minimum(lower, -radii - slope * axis_z), lower)
    return lower * scale, upper * scale


def _fit_circle(points):
    import numpy as np
    if len(points) < 3:
        return None
    origin = points.mean(axis=0)
    scale = float(np.linalg.norm(np.ptp(points, axis=0)))
    if scale <= 0:
        return None
    local = (points - origin) / scale
    matrix = np.column_stack((2.0 * local, np.ones(len(local))))
    solution, _, rank, singular = np.linalg.lstsq(matrix, np.einsum('ij,ij->i', local, local), rcond=None)
    if rank != 3 or singular[-1] <= singular[0] * 1e-10:
        return None
    radius_sq = solution[2] + float(solution[:2] @ solution[:2])
    if radius_sq <= 0:
        return None
    return origin + solution[:2] * scale, math.sqrt(radius_sq) * scale


def _primitive_axes(points, normals, areas):
    import numpy as np
    axes = list(np.linalg.eigh(np.cov(points.T))[1].T)
    axes.extend(np.linalg.eigh((normals.T * areas) @ normals)[1].T)
    canonical = normals.copy()
    dominant = np.argmax(np.abs(canonical), axis=1)
    canonical *= np.where(canonical[np.arange(len(canonical)), dominant] < 0, -1.0, 1.0)[:, None]
    _, labels = np.unique(np.round(canonical, 8), axis=0, return_inverse=True)
    weights = np.bincount(labels, weights=areas)
    directions = np.zeros((len(weights), 3))
    np.add.at(directions, labels, canonical * areas[:, None])
    axes.extend(directions[index] for index in np.argsort(weights)[-6:][::-1])
    result = []
    for axis in axes:
        length = float(np.linalg.norm(axis))
        if length <= 0:
            continue
        axis = axis / length
        if not any(abs(float(axis @ previous)) > 1.0 - 1e-10 for previous in result):
            result.append(axis)
    return result


def _face_geometry(verts, tris):
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    faces = np.asarray(tris, dtype=np.int32)
    p0, p1, p2 = (points[faces[:, 0]], points[faces[:, 1]], points[faces[:, 2]])
    normals = np.cross(p1 - p0, p2 - p0)
    lengths = np.linalg.norm(normals, axis=1)
    good = lengths > 1e-15
    normals[good] /= lengths[good, None]
    return ((p0 + p1 + p2) / 3.0, normals)


def _cap_boundary_cycles(faces):
    edge_counts = {}
    for triangle in faces:
        for first, second in (
            (int(triangle[0]), int(triangle[1])),
            (int(triangle[1]), int(triangle[2])),
            (int(triangle[2]), int(triangle[0])),
        ):
            edge = (min(first, second), max(first, second))
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
    boundary_edges = {edge for edge, count in edge_counts.items() if count == 1}
    if not boundary_edges:
        return []
    adjacency = {}
    for first, second in boundary_edges:
        adjacency.setdefault(first, []).append(second)
        adjacency.setdefault(second, []).append(first)
    if any((len(neighbors) != 2 for neighbors in adjacency.values())):
        return []
    unused = set(boundary_edges)
    cycles = []
    while unused:
        first_edge = min(unused)
        start, current = first_edge
        previous = start
        cycle = [start]
        unused.remove(first_edge)
        while current != start:
            cycle.append(current)
            candidates = [neighbor for neighbor in adjacency[current] if neighbor != previous]
            if len(candidates) != 1:
                return []
            next_vertex = candidates[0]
            edge = (min(current, next_vertex), max(current, next_vertex))
            if next_vertex != start and edge not in unused:
                return []
            unused.discard(edge)
            previous, current = (current, next_vertex)
            if len(cycle) > len(boundary_edges):
                return []
        if len(cycle) < 3:
            return []
        cycles.append(cycle)
    return cycles


def _profile_basis(axis):
    import numpy as np
    reference = np.array([1.0, 0.0, 0.0] if abs(float(axis[0])) < 0.9 else [0.0, 1.0, 0.0], dtype=np.float64)
    first = np.cross(reference, axis)
    first /= np.linalg.norm(first)
    second = np.cross(axis, first)
    second /= np.linalg.norm(second)
    return (first, second)


def _profile_segments(points_2d, cycles):
    return [(
        points_2d[cycle[index]],
        points_2d[cycle[(index + 1) % len(cycle)]],
    ) for cycle in cycles for index in range(len(cycle))]


def _point_to_profile_distance(point, segments):
    import numpy as np
    best = float('inf')
    for start, end in segments:
        direction = end - start
        length_sq = float(np.dot(direction, direction))
        if length_sq <= 1e-30:
            distance = float(np.linalg.norm(point - start))
        else:
            fraction = min(1.0, max(0.0, float(np.dot(point - start, direction)) / length_sq))
            distance = float(np.linalg.norm(point - (start + fraction * direction)))
        best = min(best, distance)
    return best


def _profile_area(points_2d, cycle):
    import numpy as np
    values = points_2d[cycle]
    return 0.5 * float((values[:, 0] * np.roll(
        values[:, 1],
        -1,
    )).sum() - (values[:, 1] * np.roll(values[:, 0], -1)).sum())


def _point_in_profile(point, polygon):
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            crossing = (previous[0] - current[0]) * (point[1] - current[1]) / (previous[1] - current[1]) + current[0]
            if point[0] < crossing:
                inside = not inside
        previous = current
    return inside


def _reconstruct_experimental_extrusion(verts, tris, report, *, settings: Settings=Settings()):
    if not settings.experimental_parametric_reconstruction:
        return None
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    faces = np.asarray(tris, dtype=np.int32)
    if len(points) < 4 or len(faces) < 4:
        return None
    origin = points.mean(axis=0)
    points = points - origin
    p0 = points[faces[:, 0]]
    p1 = points[faces[:, 1]]
    p2 = points[faces[:, 2]]
    raw_normals = np.cross(p1 - p0, p2 - p0)
    doubled_areas = np.linalg.norm(raw_normals, axis=1)
    valid_faces = doubled_areas > 1e-15
    if not np.any(valid_faces):
        return None
    normals = raw_normals[valid_faces] / doubled_areas[valid_faces, None]
    weights = doubled_areas[valid_faces]
    clusters = []
    direction_tolerance = max(1e-06, settings.experimental_parametric_fit_error_ratio * 2.0)
    for normal, weight in zip(normals, weights):
        canonical = normal.copy()
        dominant = int(np.argmax(np.abs(canonical)))
        if canonical[dominant] < 0:
            canonical = -canonical
        matched = False
        for cluster in clusters:
            alignment = float(np.dot(cluster['axis'], canonical))
            if alignment >= 1.0 - direction_tolerance:
                combined = cluster['axis'] * cluster['weight'] + canonical * float(weight)
                cluster['weight'] += float(weight)
                cluster['axis'] = combined / np.linalg.norm(combined)
                matched = True
                break
        if not matched:
            clusters.append({'axis': canonical, 'weight': float(weight)})
    diagonal = max(float(np.linalg.norm(np.ptp(points, axis=0))), 1e-12)
    distance_tolerance = max(diagonal * settings.experimental_parametric_fit_error_ratio, 1e-09)
    mesh_volume = float(report['volume'])
    if mesh_volume <= 1e-15:
        return None
    for cluster in sorted(clusters, key=lambda item: item['weight'], reverse=True)[:12]:
        axis = cluster['axis']
        axial = points @ axis
        minimum = float(axial.min())
        maximum = float(axial.max())
        height = maximum - minimum
        if height <= distance_tolerance:
            continue
        bottom_vertices = np.abs(axial - minimum) <= distance_tolerance
        top_vertices = np.abs(axial - maximum) <= distance_tolerance
        bottom_faces = np.all(bottom_vertices[faces], axis=1)
        top_faces = np.all(top_vertices[faces], axis=1)
        if not np.any(bottom_faces) or not np.any(top_faces):
            continue
        side_faces = ~(bottom_faces | top_faces)
        if np.any(np.abs(_face_geometry(
            points,
            faces[side_faces],
        )[1] @ axis) > max(0.02, direction_tolerance * 10.0)):
            continue
        bottom_cycles = _cap_boundary_cycles(faces[bottom_faces])
        top_cycles = _cap_boundary_cycles(faces[top_faces])
        if not bottom_cycles or not top_cycles:
            continue
        first_basis, second_basis = _profile_basis(axis)
        points_2d = np.column_stack((points @ first_basis, points @ second_basis))
        bottom_segments = _profile_segments(points_2d, bottom_cycles)
        top_segments = _profile_segments(points_2d, top_cycles)
        if len(bottom_segments) != len(top_segments):
            continue
        profile_error = max(
            max((_point_to_profile_distance(
                points_2d[index],
                top_segments,
            ) for cycle in bottom_cycles for index in cycle)),
            max((_point_to_profile_distance(
                points_2d[index],
                bottom_segments,
            ) for cycle in top_cycles for index in cycle)),
        )
        if profile_error > distance_tolerance:
            continue
        middle_vertices = ~(bottom_vertices | top_vertices)
        if np.any(middle_vertices):
            side_error = max((_point_to_profile_distance(
                point,
                bottom_segments,
            ) for point in points_2d[middle_vertices]))
            if side_error > distance_tolerance:
                continue
        areas = [_profile_area(points_2d, cycle) for cycle in bottom_cycles]
        outer_index = int(np.argmax(np.abs(areas)))
        if abs(areas[outer_index]) <= distance_tolerance ** 2:
            continue
        outer_polygon = points_2d[bottom_cycles[outer_index]]
        if any((not _point_in_profile(
            points_2d[cycle[0]],
            outer_polygon,
        ) for index, cycle in enumerate(bottom_cycles) if index != outer_index)):
            continue
        ordered_cycles = [bottom_cycles[outer_index]] + [cycle for (
            index,
            cycle,
        ) in enumerate(bottom_cycles) if index != outer_index]
        ordered_areas = [areas[outer_index]] + [area for (
            index,
            area,
        ) in enumerate(areas) if index != outer_index]
        wires = []
        wire_failed = False
        for index, (cycle, area) in enumerate(zip(ordered_cycles, ordered_areas)):
            desired_positive = index == 0
            if (area > 0) != desired_positive:
                cycle = list(reversed(cycle))
            polygon = BRepBuilderAPI_MakePolygon()
            for vertex_index in cycle:
                polygon.Add(gp_Pnt(*map(float, points[vertex_index] + origin)))
            polygon.Close()
            if not polygon.IsDone():
                wire_failed = True
                break
            wires.append(polygon.Wire())
        if wire_failed or not wires:
            continue
        face_builder = BRepBuilderAPI_MakeFace(wires[0], True)
        for inner_wire in wires[1:]:
            face_builder.Add(inner_wire)
        if not face_builder.IsDone():
            continue
        prism = BRepPrimAPI_MakePrism(face_builder.Face(), gp_Vec(*map(float, axis * height)), True).Shape()
        valid, _ = _validate_occ_shape(prism, require_solid=True)
        if not valid or _count_topo(prism, TopAbs_SOLID) != 1:
            continue
        volume_change = abs(_solid_volume(prism) - mesh_volume) / mesh_volume * 100.0
        if volume_change > settings.experimental_parametric_max_volume_change_percent:
            continue
        if _count_topo(prism, TopAbs_FACE) >= len(faces):
            continue
        return prism
    return None


def _fit_revolved_primitive(verts, tris, relative_tolerance, cone=False):
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    center = points.mean(axis=0)
    points = points - center
    faces = np.asarray(tris, dtype=np.int32)
    centroids, normals = _face_geometry(points, faces)
    triangles = points[faces]
    areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0],
                                   triangles[:, 2] - triangles[:, 0]), axis=1)
    axes = _primitive_axes(points, normals, areas)
    diagonal = max(float(np.linalg.norm(np.ptp(points, axis=0))), 1e-12)
    tolerance = max(diagonal * relative_tolerance, 1e-09)
    best = None
    for axis in axes:
        face_axial = np.abs(normals @ axis)
        side_mask = face_axial < 0.95
        cap_mask = face_axial >= 0.95
        if np.count_nonzero(side_mask) < 4 or np.count_nonzero(cap_mask) < 2:
            continue
        side_faces = faces[side_mask]
        side_indices = np.unique(side_faces)
        side_points = points[side_indices]
        side_z = side_points @ axis
        all_z = points @ axis
        z_min, z_max = (float(all_z.min()), float(all_z.max()))
        height = z_max - z_min
        if height <= tolerance:
            continue
        basis = np.column_stack(_profile_basis(axis))
        projected = side_points @ basis
        if cone:
            circles = []
            for level in (z_min, z_max):
                ring = projected[np.abs(side_z - level) <= tolerance]
                circle = (ring[0], 0.0) if len(ring) == 1 else _fit_circle(ring)
                if circle is None:
                    break
                circles.append(circle)
            if len(circles) != 2 or np.linalg.norm(circles[0][0] - circles[1][0]) > tolerance:
                continue
            radial_center = (circles[0][0] + circles[1][0]) * 0.5
            r1, r2 = float(circles[0][1]), float(circles[1][1])
            if abs(r1 - r2) <= tolerance:
                continue
            slope = (r2 - r1) / height
        else:
            circle = _fit_circle(projected)
            if circle is None:
                continue
            radial_center, radius = circle
            r1 = r2 = float(radius)
            slope = 0.0
        offset = basis @ radial_center
        side_centers = centroids[side_mask] - offset
        center_z = side_centers @ axis
        center_radial = side_centers - np.outer(center_z, axis)
        center_radius = np.linalg.norm(center_radial, axis=1)
        valid_center = center_radius > 1e-15
        projected_normals = normals[side_mask] - np.outer(normals[side_mask] @ axis, axis)
        projected_length = np.linalg.norm(projected_normals, axis=1)
        valid_alignment = valid_center & (projected_length > 1e-15)
        if not np.any(valid_alignment):
            continue
        alignment = np.abs(np.einsum(
            'ij,ij->i',
            center_radial[valid_alignment] / center_radius[valid_alignment, None],
            projected_normals[valid_alignment] / projected_length[valid_alignment, None],
        ))
        if float(np.quantile(alignment, 0.1)) < 0.98:
            continue
        side_triangles = triangles[side_mask]
        if cone:
            local_triangles = np.empty_like(side_triangles)
            local_triangles[:, :, :2] = side_triangles @ basis - radial_center
            local_triangles[:, :, 2] = side_triangles @ axis - z_min
            lower, upper = _triangle_cone_residual_bounds(local_triangles, r1, slope)
            residual = max(float(np.max(np.abs(lower))), float(np.max(np.abs(upper))))
        else:
            radial_triangles = side_triangles - offset - (side_triangles @ axis)[:, :, None] * axis
            lower, upper = _triangle_radius_bounds(radial_triangles)
            residual = max(float(np.max(np.abs(lower - r1))), float(np.max(np.abs(upper - r1))))
        if not math.isfinite(residual) or residual > tolerance or max(r1, r2) <= tolerance:
            continue
        cap_points = points[np.unique(faces[cap_mask])]
        cap_z = cap_points @ axis
        cap_distance = np.minimum(np.abs(cap_z - z_min), np.abs(cap_z - z_max))
        if float(np.max(cap_distance)) > tolerance:
            continue
        cap_radius = np.linalg.norm(cap_points - offset - np.outer(cap_z, axis), axis=1)
        if np.any(cap_radius > r1 + slope * (cap_z - z_min) + tolerance):
            continue
        candidate = {
            'axis': axis,
            'origin': center + offset + axis * z_min,
            'height': height,
            'r1': r1,
            'r2': r2,
            'error': residual,
        }
        if best is None or candidate['error'] < best['error']:
            best = candidate
    return best


def _reconstruct_analytic_shape(verts, tris, report=None, *, settings: Settings=Settings()):
    if not (settings.reconstruct_analytic_primitives or settings.experimental_parametric_reconstruction):
        return (None, None)
    if report is None:
        report = _mesh_quality_report(verts, tris, check_self_intersection=False)
    if not report['watertight'] or report['components'] != 1:
        return (None, None)

    def safe_primitive(shape):
        valid, _ = _validate_occ_shape(shape, require_solid=True)
        source_volume = report['volume']
        if not valid or source_volume <= 0:
            return False
        change = abs(_solid_volume(shape) - source_volume) / source_volume * 100.0
        return change <= settings.analytic_primitive_max_volume_change_percent
    if settings.reconstruct_analytic_primitives and len(tris) >= settings.analytic_primitive_min_triangles:
        sphere = _fit_sphere(verts, tris, settings.analytic_primitive_fit_error_ratio)
        if sphere is not None:
            center, radius, _ = sphere
            shape = BRepPrimAPI_MakeSphere(gp_Pnt(*map(float, center)), float(radius)).Shape()
            if safe_primitive(shape):
                return (shape, 'sphere')
        cylinder = _fit_revolved_primitive(
            verts,
            tris,
            settings.analytic_primitive_fit_error_ratio,
            cone=False,
        )
        if cylinder is not None:
            axis = cylinder['axis']
            ax2 = gp_Ax2(gp_Pnt(*map(float, cylinder['origin'])), gp_Dir(*map(float, axis)))
            shape = BRepPrimAPI_MakeCylinder(ax2, cylinder['r1'], cylinder['height']).Shape()
            if safe_primitive(shape):
                return (shape, 'cylinder')
        cone = _fit_revolved_primitive(verts, tris, settings.analytic_primitive_fit_error_ratio, cone=True)
        if cone is not None:
            axis = cone['axis']
            ax2 = gp_Ax2(gp_Pnt(*map(float, cone['origin'])), gp_Dir(*map(float, axis)))
            shape = BRepPrimAPI_MakeCone(ax2, cone['r1'], cone['r2'], cone['height']).Shape()
            if safe_primitive(shape):
                return (shape, 'cone')
    try:
        extrusion = _reconstruct_experimental_extrusion(verts, tris, report, settings=settings)
    except Exception:
        extrusion = None
    if extrusion is not None:
        return (extrusion, 'linear extrusion')
    return (None, None)
