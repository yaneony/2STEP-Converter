import math

from config import Settings
from mesh_geometry import component_groups
from OCC.Core.BRep import BRep_Builder, BRep_Tool
from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.BRepBndLib import brepbndlib
from OCC.Core.BRepBuilderAPI import (
    BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace,
)
from OCC.Core.BRepCheck import BRepCheck_Analyzer
from OCC.Core.BRepGProp import brepgprop
from OCC.Core.BRepTools import breptools
from OCC.Core.Bnd import Bnd_Box
from OCC.Core.GeomAbs import GeomAbs_Plane
from OCC.Core.GProp import GProp_GProps
from OCC.Core.TopAbs import (
    TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE, TopAbs_EDGE, TopAbs_WIRE, TopAbs_VERTEX,
)
from OCC.Core.TopExp import TopExp_Explorer, topexp
from OCC.Core.TopTools import TopTools_IndexedMapOfShape
from OCC.Core.TopoDS import TopoDS_Compound, TopoDS_Iterator, TopoDS_Shell, topods
from OCC.Core.gp import gp_Pnt


def parts(shape):
    if shape.ShapeType() in (TopAbs_SOLID, TopAbs_SHELL):
        yield shape
        return
    from OCC.Core.TopAbs import TopAbs_COMPOUND, TopAbs_COMPSOLID
    if shape.ShapeType() not in (TopAbs_COMPOUND, TopAbs_COMPSOLID):
        yield shape
        return
    iterator = TopoDS_Iterator(shape)
    while iterator.More():
        yield from parts(iterator.Value())
        iterator.Next()


def combine(shapes):
    shapes = list(shapes)
    if len(shapes) == 1:
        return shapes[0]
    result = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(result)
    for shape in shapes:
        builder.Add(result, shape)
    return result


def volume(shape):
    from OCC.Core.TopExp import TopExp_Explorer
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    total = 0.0
    while explorer.More():
        props = GProp_GProps()
        brepgprop.VolumeProperties(explorer.Current(), props)
        total += abs(float(props.Mass()))
        explorer.Next()
    return total


def bounds(shape):
    box = Bnd_Box()

    brepbndlib.AddOptimal(shape, box, False, False)
    if box.IsVoid():
        raise ValueError("shape has no geometric bounds")
    return box.Get()


def solid_signature(shape):
    return sorted((entry[0], entry[1]) for entry in _solid_measurements(shape))


def _solid_measurements(shape):
    import numpy as np
    from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCC.Core.gp import gp_Trsf, gp_Vec
    from OCC.Core.TopExp import TopExp_Explorer
    result = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        solid = explorer.Current()
        box = bounds(solid)
        diagonal = math.dist(box[:3], box[3:])
        reference = np.asarray(box[:3]) + (np.asarray(box[3:]) - box[:3]) * 0.5
        offset = np.zeros(3)
        measured = solid
        if np.linalg.norm(reference) > diagonal * 8:
            offset = reference
            translation = gp_Trsf()
            translation.SetTranslation(gp_Vec(*(-offset)))
            measured = BRepBuilderAPI_Transform(solid, translation, True).Shape()
        props = GProp_GProps(gp_Pnt(*(reference - offset)))
        integration_error = brepgprop.VolumeProperties(measured, props, 1e-10)
        if not math.isfinite(integration_error) or not 0 <= integration_error <= 1e-7:
            raise ValueError('solid mass integration did not reach sufficient accuracy')
        mass = abs(float(props.Mass()))
        center = tuple(np.asarray(props.CentreOfMass().Coord()) + offset)
        matrix = props.MatrixOfInertia()
        denominator = mass * diagonal * diagonal
        if not math.isfinite(denominator) or denominator <= 0:
            raise ValueError('solid has invalid mass properties')
        inertia = tuple(matrix.Value(i, j) / denominator for i in range(1, 4) for j in range(1, 4))
        if not all(math.isfinite(value) for value in (*center, *inertia)):
            raise ValueError('solid has non-finite mass properties')
        if np.linalg.eigvalsh(np.array(inertia).reshape(3, 3))[0] < -1e-10:
            raise ValueError('solid has invalid inertia properties')
        result.append((box, mass, center, inertia))
        explorer.Next()
    return sorted(result)


def geometry_change_error(before, after, tolerance, max_volume_percent=0.001):
    left, right = _solid_measurements(before), _solid_measurements(after)
    if len(left) != len(right):
        return "operation changed solid count"
    if not left:
        left, right = [(bounds(before), 0.0, (), ())], [(bounds(after), 0.0, (), ())]

    pending = list(right)
    for old_bounds, old_volume, old_center, old_inertia in left:
        diagonal = math.dist(old_bounds[:3], old_bounds[3:])
        index = min(range(len(pending)), key=lambda i: (
            max(abs(a - b) for a, b in zip(old_bounds, pending[i][0]))
            + math.dist(old_center, pending[i][2])
            + diagonal * max((abs(a - b) for a, b in zip(old_inertia, pending[i][3])), default=0)
        ))
        new_bounds, new_volume, new_center, new_inertia = pending.pop(index)
        numerical = max(abs(v) for v in old_bounds) * 2e-14
        limit = max(tolerance, numerical, 1e-7)
        if max(abs(a - b) for a, b in zip(old_bounds, new_bounds)) > limit:
            return "operation changed a component's position or bounds"
        if old_volume > 0:
            change = abs(new_volume - old_volume) / old_volume * 100.0
            if not math.isfinite(change) or change > max_volume_percent:
                return f"operation changed a component's volume by {change:.6g}%"
            relative_volume = max_volume_percent / 100.0
            if math.dist(old_center, new_center) > max(2 * limit, 2 * diagonal * relative_volume):
                return "operation changed a component's center of mass"
            inertia_limit = max(8 * limit / diagonal, 4 * relative_volume, 1e-12)
            if max(abs(a - b) for a, b in zip(old_inertia, new_inertia)) > inertia_limit:
                return "operation changed a component's material distribution"
    return None


def mesh_conversion_error(vertices, faces, shape, tolerance,
                          fit_error_ratio=0.0, max_volume_percent=0.001):
    import numpy as np
    from OCC.Core.TopExp import TopExp_Explorer
    from mesh_geometry import component_measurements

    source = component_measurements(vertices, faces)
    pending = []
    explorer = TopExp_Explorer(shape, TopAbs_SHELL)
    while explorer.More():
        shell = explorer.Current()
        props = GProp_GProps()
        brepgprop.VolumeProperties(shell, props)
        pending.append((bounds(shell), abs(float(props.Mass()))))
        explorer.Next()
    if len(source) != len(pending):
        return "conversion changed the source shell/component count"
    for old_bounds, _, old_volume in source:
        match = min(range(len(pending)), key=lambda i: np.linalg.norm(old_bounds - pending[i][0]))
        new_bounds, new_volume = pending.pop(match)
        diagonal = float(np.linalg.norm(old_bounds[3:] - old_bounds[:3]))
        limit = max(tolerance, max(abs(old_bounds)) * 2e-14, 1e-7, diagonal * fit_error_ratio)
        if np.max(np.abs(old_bounds - new_bounds)) > limit:
            return "conversion changed a source component's position or bounds"
        if old_volume > 0 and abs(old_volume - new_volume) / old_volume * 100 > max_volume_percent:
            return "conversion changed a source component's volume"
    return None


def solidify(shape):
    from OCC.Core.TopTools import TopTools_IndexedMapOfShape
    from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCC.Core.BRepClass3d import BRepClass3d_SolidClassifier
    from OCC.Core.BRepLib import breplib
    from OCC.Core.TopAbs import TopAbs_IN, TopAbs_OUT, TopAbs_EDGE, TopAbs_VERTEX
    from OCC.Core.TopExp import TopExp_Explorer, topexp
    from OCC.Core.TopoDS import topods

    preserved, closed = [], []
    for part in parts(shape):
        if part.ShapeType() != TopAbs_SHELL or not BRep_Tool.IsClosed(topods.Shell(part)):
            preserved.append(part)
            continue
        solid = BRepBuilderAPI_MakeSolid(topods.Shell(part)).Solid()
        if not breplib.OrientClosedSolid(solid) or not BRepCheck_Analyzer(solid).IsValid():
            preserved.append(part)
            continue
        shell_exp = TopExp_Explorer(solid, TopAbs_SHELL)
        closed.append((topods.Shell(shell_exp.Current()), solid, bounds(solid), volume(solid)))

    containers = [[] for _ in closed]
    for i, (_, inner, inner_bounds, _) in enumerate(closed):
        for j, (_, outer, outer_bounds, _) in enumerate(closed):
            if i == j or not all(
                outer_bounds[k] < inner_bounds[k] and inner_bounds[k + 3] < outer_bounds[k + 3]
                for k in range(3)
            ):
                continue
            classifier = BRepClass3d_SolidClassifier(outer)
            states = []
            vertices = TopTools_IndexedMapOfShape()
            topexp.MapShapes(inner, TopAbs_VERTEX, vertices)
            for vertex_index in range(1, vertices.Size() + 1):
                classifier.Perform(BRep_Tool.Pnt(topods.Vertex(vertices.FindKey(vertex_index))), 1e-7)
                states.append(classifier.State())
            if states and any(state == TopAbs_OUT for state in states):
                continue
            if not states or not all(state == TopAbs_IN for state in states):
                raise ValueError("ambiguous or intersecting nested shells")
            section = BRepAlgoAPI_Section(inner, outer, False)
            section.Build()
            if not section.IsDone():
                raise ValueError("could not verify nested shell boundaries")
            if (
                TopExp_Explorer(section.Shape(), TopAbs_EDGE).More()
                or TopExp_Explorer(section.Shape(), TopAbs_VERTEX).More()
            ):
                raise ValueError("nested shell boundaries intersect or touch")
            containers[i].append(j)

    for i, (outer_shell, _, _, _) in enumerate(closed):
        if len(containers[i]) % 2:
            continue
        maker = BRepBuilderAPI_MakeSolid(outer_shell)
        for j, (inner_shell, _, _, _) in enumerate(closed):
            if i in containers[j] and len(containers[j]) == len(containers[i]) + 1:
                maker.Add(topods.Shell(inner_shell.Reversed()))
        solid = maker.Solid()
        if not breplib.OrientClosedSolid(solid) or not BRepCheck_Analyzer(solid).IsValid():
            raise ValueError("failed to build a valid solid with its cavities")
        preserved.append(solid)
    return combine(preserved)


def _mesh_to_shape_single(verts, tris):
    if len(tris) == 0:
        raise ValueError('no triangle data found')
    shape = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shape)
    vertices = [BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, vertex))).Vertex() for vertex in verts]
    edges, counts = ({}, {})
    for triangle in tris:
        wire = BRepBuilderAPI_MakeWire()
        for first, second in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            key = (min(first, second), max(first, second))
            if key not in edges:
                maker = BRepBuilderAPI_MakeEdge(vertices[key[0]], vertices[key[1]])
                if not maker.IsDone():
                    raise ValueError('triangle edge is below CAD kernel precision')
                edges[key] = maker.Edge()
            edge = edges[key] if first < second else topods.Edge(edges[key].Reversed())
            wire.Add(edge)
            counts[key] = counts.get(key, 0) + 1
        if not wire.IsDone():
            raise ValueError('triangle cannot be represented at CAD kernel precision')
        face = BRepBuilderAPI_MakeFace(wire.Wire(), True)
        if not face.IsDone():
            raise ValueError('failed to construct a triangle face')
        builder.Add(shape, face.Face())
    shape.Closed(all((count == 2 for count in counts.values())))
    return shape


def _mesh_to_shape(verts, tris, *, settings: Settings=Settings()):
    if not settings.sew_parts_separately:
        return _mesh_to_shape_single(verts, tris)
    try:
        import numpy as np
        import open3d as o3d
        vertices = np.asarray(verts, dtype=np.float64)
        faces = np.asarray(tris, dtype=np.int32)
        mesh = o3d.geometry.TriangleMesh()
        mesh.vertices = o3d.utility.Vector3dVector(vertices)
        mesh.triangles = o3d.utility.Vector3iVector(faces)
        cluster_ids, _, _ = mesh.cluster_connected_triangles()
        cluster_ids = np.asarray(cluster_ids)
        component_count = int(cluster_ids.max()) + 1 if len(cluster_ids) else 0
        if component_count <= 1:
            return _mesh_to_shape_single(verts, tris)
        compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(compound)
        for indices in component_groups(cluster_ids):
            component_faces = faces[indices]
            used_vertices, remapped = np.unique(component_faces, return_inverse=True)
            component_vertices = vertices[used_vertices]
            component_faces = remapped.reshape(-1, 3)
            part = _mesh_to_shape_single(component_vertices.tolist(), component_faces.tolist())
            if not part.IsNull():
                builder.Add(compound, part)
        return compound
    except ImportError as exc:
        raise RuntimeError('Open3D is required to separate mesh components') from exc


def _count_topo(shape, kind) -> int:
    exp = TopExp_Explorer(shape, kind)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


def _count_topo_unique(shape, kind) -> int:
    indexed = TopTools_IndexedMapOfShape()
    topexp.MapShapes(shape, kind, indexed)
    return indexed.Size()


def _count_surface_type(shape, surface_type) -> int:
    count = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = topods.Face(exp.Current())
        if BRepAdaptor_Surface(face, True).GetType() == surface_type:
            count += 1
        exp.Next()
    return count


def _has_candidate_hole_wires(shape, *, settings: Settings=Settings()) -> bool:
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = topods.Face(faces.Current())
        surface = BRepAdaptor_Surface(face, True)
        if surface.GetType() == GeomAbs_Plane:
            outer = breptools.OuterWire(face)
            wires = TopExp_Explorer(face, TopAbs_WIRE)
            while wires.More():
                wire = topods.Wire(wires.Current())
                if (
                    not wire.IsSame(outer)
                    and _count_topo_unique(wire, TopAbs_VERTEX) >= settings.analytic_hole_min_sides
                ):
                    return True
                wires.Next()
        faces.Next()
    return False


def _free_topology_counts(shape) -> dict:
    counts = {'shell': 0, 'face': 0, 'wire': 0, 'edge': 0, 'vertex': 0}

    def visit(part):
        shape_type = part.ShapeType()
        if shape_type == TopAbs_SOLID:
            return
        if shape_type == TopAbs_SHELL:
            counts['shell'] += 1
            return
        if shape_type == TopAbs_FACE:
            counts['face'] += 1
            return
        if shape_type == TopAbs_WIRE:
            counts['wire'] += 1
            return
        if shape_type == TopAbs_EDGE:
            counts['edge'] += 1
            return
        if shape_type == TopAbs_VERTEX:
            counts['vertex'] += 1
            return
        iterator = TopoDS_Iterator(part)
        while iterator.More():
            visit(iterator.Value())
            iterator.Next()
    visit(shape)
    return counts


def _count_free_shells(shape) -> int:
    counts = _free_topology_counts(shape)
    return counts['shell'] + counts['face']


def _validate_occ_shape(shape, require_solid=False):
    if shape is None or shape.IsNull():
        return (False, 'shape is null')
    if not BRepCheck_Analyzer(shape).IsValid():
        return (False, 'OpenCASCADE reports invalid topology')
    solids = _count_topo(shape, TopAbs_SOLID)
    if require_solid and solids == 0:
        return (False, 'shape contains no valid solid')
    free_counts = _free_topology_counts(shape)
    if require_solid and any(free_counts.values()):
        details = []
        for name, count in free_counts.items():
            if count:
                details.append(f"{count} {name}{('' if count == 1 else 's')}")
        return (False, 'shape contains topology outside valid solids: ' + ', '.join(details))
    if require_solid:
        from OCC.Core.BRepClass3d import BRepClass3d_SolidClassifier
        from OCC.Core.TopAbs import TopAbs_OUT
        explorer = TopExp_Explorer(shape, TopAbs_SOLID)
        while explorer.More():
            solid = topods.Solid(explorer.Current())
            shells = TopExp_Explorer(solid, TopAbs_SHELL)
            while shells.More():
                if not BRep_Tool.IsClosed(topods.Shell(shells.Current())):
                    return (False, 'solid contains an open shell')
                shells.Next()
            properties = GProp_GProps()
            brepgprop.VolumeProperties(solid, properties)
            if not math.isfinite(properties.Mass()) or properties.Mass() <= 0:
                return (False, 'solid has a non-positive or non-finite volume')
            classifier = BRepClass3d_SolidClassifier(solid)
            classifier.PerformInfinitePoint(1e-07)
            if classifier.State() != TopAbs_OUT:
                return (False, 'solid orientation describes unbounded material')
            explorer.Next()
    return (True, None)
