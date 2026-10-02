import numpy as np
from config import Settings


def repair_precision(vertices):
    diagonal = float(np.linalg.norm(np.ptp(vertices, axis=0)))
    return max(1e-7, min(diagonal * 1e-9, 1e-5))


def _repair_source_faces(vertices, faces, max_attached_area_ratio):
    if not np.isfinite(max_attached_area_ratio) or not 0 <= max_attached_area_ratio <= 1:
        raise ValueError('attached triangle area limit must be between zero and one')
    edges = np.sort(np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])), axis=1)
    _, inverse, counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
    odd = counts % 2 != 0
    measurements = {'removed_attached_triangles': 0, 'removed_attached_area': 0.0,
                    'removed_attached_area_ratio': 0.0}
    if not np.any(odd):
        return faces, measurements
    face_edges = inverse.reshape(3, len(faces)).T
    triangles = vertices[faces]
    areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0],
                                    triangles[:, 2] - triangles[:, 0]), axis=1) * 0.5
    total_area = float(areas.sum())
    candidates = _attached_patch_candidates(face_edges, counts, areas, total_area * max_attached_area_ratio)
    claimed = np.bincount(face_edges[candidates].ravel(), minlength=len(counts))
    if not np.all(claimed[odd] == 1) or np.any(claimed[~odd] % 2) or np.any(claimed > counts):
        raise ValueError('repair source has unmatched surfaces without a unique attached-patch correction')
    repaired_faces = faces[~candidates]
    if not np.array_equal(np.unique(faces), np.unique(repaired_faces)):
        raise ValueError('attached patch correction would remove source vertices')
    removed_area = float(areas[candidates].sum())
    ratio = removed_area / total_area if total_area > 0 else float('inf')
    if not np.isfinite(ratio) or not np.isfinite(removed_area) or ratio > max_attached_area_ratio:
        raise ValueError('attached patch correction exceeds the allowed source surface area change')
    measurements.update(removed_attached_triangles=int(candidates.sum()),
                        removed_attached_area=removed_area, removed_attached_area_ratio=ratio)
    return repaired_faces, measurements


def _attached_patch_candidates(face_edges, counts, areas, area_limit):
    from mesh_geometry import component_groups
    parent = np.arange(len(face_edges), dtype=np.int64)

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    order = np.argsort(face_edges.ravel(), kind='stable')
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    for start in starts[counts == 2]:
        left, right = order[start:start + 2] // 3
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left
    labels = np.array([find(index) for index in range(len(parent))])
    odd = counts % 2 != 0
    candidates = np.zeros(len(face_edges), dtype=bool)
    for group in component_groups(labels):
        if float(areas[group].sum()) > area_limit:
            continue
        edges, claims = np.unique(face_edges[group], return_counts=True)
        if (np.any(odd[edges]) and np.all(claims[odd[edges]] == 1)
                and np.all(claims[~odd[edges]] == 2)):
            candidates[group] = True
    return candidates


class _TriangleRayIndex:
    def __init__(self, triangles):
        slack = 1e-9
        padding = 2 * slack * (np.abs(triangles[:, 1] - triangles[:, 0])
                               + np.abs(triangles[:, 2] - triangles[:, 0]))
        padding += np.finfo(np.float64).eps * 64 * np.maximum(1, np.max(np.abs(triangles), axis=1))
        self.minimum = triangles.min(axis=1) - padding
        self.maximum = triangles.max(axis=1) + padding
        centers = self.minimum + (self.maximum - self.minimum) * 0.5
        self.order = np.arange(len(triangles), dtype=np.int64)
        nodes = []
        pending = [(0, len(triangles), -1, -1)] if len(triangles) else []
        while pending:
            first, last, parent, child = pending.pop()
            indices = self.order[first:last]
            low = self.minimum[indices].min(axis=0)
            high = self.maximum[indices].max(axis=0)
            node = len(nodes)
            nodes.append([low, high, -1, -1, first, last])
            if parent >= 0:
                nodes[parent][child] = node
            if last - first > 64:
                axis = int(np.argmax(np.ptp(centers[indices], axis=0)))
                middle = (last - first) // 2
                self.order[first:last] = indices[np.argpartition(centers[indices, axis], middle)]
                pending.append((first + middle, last, node, 3))
                pending.append((first, first + middle, node, 2))
        self.low = np.asarray([node[0] for node in nodes]).reshape(-1, 3)
        self.high = np.asarray([node[1] for node in nodes]).reshape(-1, 3)
        self.children = np.asarray([node[2:4] for node in nodes], dtype=np.int64).reshape(-1, 2)
        self.ranges = np.asarray([node[4:] for node in nodes], dtype=np.int64).reshape(-1, 2)

    def candidates(self, point, direction):
        def intersects(low, high):
            first = (low - point) / direction
            last = (high - point) / direction
            near = np.minimum(first, last).max(axis=1)
            far = np.maximum(first, last).min(axis=1)
            return (far >= np.maximum(near, -1e-9))

        if len(self.low) == 1:
            return np.flatnonzero(intersects(self.minimum, self.maximum))
        pending = np.array([0], dtype=np.int64) if len(self.low) else np.empty(0, dtype=np.int64)
        leaves = []
        while len(pending):
            pending = pending[intersects(self.low[pending], self.high[pending])]
            terminal = self.children[pending, 0] < 0
            leaves.extend(pending[terminal])
            pending = self.children[pending[~terminal]].ravel()
        if not leaves:
            return np.empty(0, dtype=np.int64)
        indices = np.concatenate([self.order[first:last] for first, last in self.ranges[leaves]])
        return indices[intersects(self.minimum[indices], self.maximum[indices])]


class _MaterialClassifier:
    def __init__(self, triangles):
        triangles = np.asarray(triangles, dtype=np.float64)
        self.starts = triangles[:, 0]
        self.edges1 = triangles[:, 1] - self.starts
        self.edges2 = triangles[:, 2] - self.starts
        self.normals = np.cross(self.edges1, self.edges2)
        self.normal_lengths = np.linalg.norm(self.normals, axis=1)
        self.edge_scale = np.linalg.norm(self.edges1, axis=1) * np.linalg.norm(self.edges2, axis=1)
        self.index = _TriangleRayIndex(triangles)
        self.rays = np.array([
            [1, .371, .529], [.253, 1, .617], [.419, .283, 1],
            [-1, .713, .231], [.711, -1, .389], [.437, .619, -1],
            [1, 1.213, 1.731],
        ])
        self.rays /= np.linalg.norm(self.rays, axis=1)[:, None]

    def __call__(self, point):
        point = np.asarray(point, dtype=np.float64)
        if point.shape != (3,) or not np.isfinite(point).all():
            return None
        votes = []
        slack = 1e-9
        for direction in self.rays:
            indices = self.index.candidates(point, direction)
            origins = point - self.starts[indices]
            edges1, edges2 = self.edges1[indices], self.edges2[indices]
            cross = np.cross(direction, edges2)
            determinant = np.einsum('ij,ij->i', edges1, cross)
            usable = np.abs(determinant) > self.edge_scale[indices] * 1e-12
            plane_distance = np.abs(np.einsum('ij,ij->i', origins, self.normals[indices]))
            if np.any((~usable) & (plane_distance <= self.normal_lengths[indices] * slack)):
                continue
            inverse = np.zeros_like(determinant)
            inverse[usable] = 1.0 / determinant[usable]
            q = np.cross(origins, edges1)
            depth_numerator = np.einsum('ij,ij->i', edges2, q)
            u = np.einsum('ij,ij->i', origins, cross) * inverse
            v = (q @ direction) * inverse
            distance = depth_numerator * inverse
            hits = usable & (u >= -slack) & (v >= -slack) & (u + v <= 1 + slack)
            if np.any(hits & (np.abs(distance) <= slack)):
                continue
            hits &= distance > slack
            if np.any(hits & ((u <= slack) | (v <= slack) | (u + v >= 1 - slack))):
                continue
            votes.append(int(np.count_nonzero(hits)) % 2)
        if len(votes) < 3 or len(set(votes)) != 1:
            return None
        return bool(votes[0])


def _face_material_probes(face, precision):
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
    from OCC.Core.BRepClass import BRepClass_FaceClassifier
    from OCC.Core.BRepTools import breptools
    from OCC.Core.TopAbs import TopAbs_IN
    from OCC.Core.TopLoc import TopLoc_Location
    from OCC.Core.gp import gp_Pnt, gp_Pnt2d, gp_Vec
    location = TopLoc_Location()
    mesh = BRep_Tool.Triangulation(face, location)
    if mesh is not None:
        for triangle_index in range(1, mesh.NbTriangles() + 1):
            points = np.array([
                mesh.Node(node).Transformed(location.Transformation()).Coord()
                for node in mesh.Triangle(triangle_index).Get()
            ])
            normal = np.cross(points[1] - points[0], points[2] - points[0])
            length = float(np.linalg.norm(normal))
            if length > 0 and np.isfinite(length):
                center = points[0] + np.mean(points - points[0], axis=0)
                yield center, normal / length
    low_u, high_u, low_v, high_v = breptools.UVBounds(face)
    if not np.isfinite([low_u, high_u, low_v, high_v]).all():
        return
    surface = BRepAdaptor_Surface(face, True)
    for fraction_u, fraction_v in ((.5, .5), (.25, .25), (.25, .75), (.75, .25),
                                  (.75, .75), (.25, .5), (.75, .5), (.5, .25), (.5, .75)):
        u = low_u + fraction_u * (high_u - low_u)
        v = low_v + fraction_v * (high_v - low_v)
        if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), precision).State() != TopAbs_IN:
            continue
        point, tangent_u, tangent_v = gp_Pnt(), gp_Vec(), gp_Vec()
        surface.D1(u, v, point, tangent_u, tangent_v)
        normal = np.cross(tangent_u.Coord(), tangent_v.Coord())
        length = float(np.linalg.norm(normal))
        if length > 0 and np.isfinite(length):
            yield np.asarray(point.Coord()), normal / length


def _cell_material(solid, source_material, origin, scale, precision):
    from OCC.Core.BRepClass3d import BRepClass3d_SolidClassifier
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_IN
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopoDS import topods
    from OCC.Core.gp import gp_Pnt
    BRepMesh_IncrementalMesh(solid, max(precision, scale * 1e-5))
    classifier = BRepClass3d_SolidClassifier(solid)
    explorer = TopExp_Explorer(solid, TopAbs_FACE)
    faces = []
    while explorer.More():
        faces.append(topods.Face(explorer.Current()))
        explorer.Next()
    decisions = []
    for index, face in enumerate(faces):
        for center, normal in _face_material_probes(face, precision):
            decision = None
            for offset in (precision * 20, precision * 100, scale * 1e-5, precision * 4, precision * 2):
                for sign in (-1, 1):
                    point = center + normal * (sign * offset)
                    classifier.Perform(gp_Pnt(*point), precision)
                    if classifier.State() == TopAbs_IN:
                        decision = source_material((point - origin) / scale)
                        if decision is not None:
                            break
                if decision is not None:
                    break
            if decision is not None:
                decisions.append(decision)
                break
        else:
            raise ValueError(f'repair cannot classify material near cell face {index + 1} of {len(faces)}')
        if len(set(decisions)) != 1:
            raise ValueError('repair cell contains conflicting material and cavity probes')
    if len(decisions) < 3 or len(set(decisions)) != 1:
        raise ValueError('repair cannot distinguish material from cavities consistently')
    return decisions[0]


def repair_volume(shape, vertices, faces, *, max_attached_area_ratio=0.005, settings: Settings=Settings()):
    from OCC.Core.BOPAlgo import BOPAlgo_MakerVolume
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopTools import TopTools_ListOfShape
    from OCC.Core.TopoDS import topods
    from occ_geometry import combine, _validate_occ_shape, _mesh_to_shape
    from mesh_geometry import _mesh_quality_report
    if _mesh_quality_report(vertices, faces, False)['boundary_edges']:
        raise ValueError('volume repair requires closed source boundaries')
    repaired_faces, correction = _repair_source_faces(vertices, faces, max_attached_area_ratio)
    if shape is None or correction['removed_attached_triangles']:
        shape = _mesh_to_shape(vertices, repaired_faces, settings=settings)
    origin = np.min(vertices, axis=0)
    scale = float(np.linalg.norm(np.ptp(vertices, axis=0)))
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError('repair requires finite non-degenerate geometry')
    triangles = (vertices[repaired_faces] - origin) / scale
    source_material = _MaterialClassifier(triangles)
    precision = repair_precision(vertices)
    arguments = TopTools_ListOfShape()
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        arguments.Append(explorer.Current())
        explorer.Next()
    maker = BOPAlgo_MakerVolume()
    maker.SetArguments(arguments)
    maker.SetIntersect(True)
    maker.SetAvoidInternalShapes(True)
    maker.SetNonDestructive(True)
    maker.SetFuzzyValue(precision)
    maker.Perform()
    if maker.HasErrors() or maker.Shape().IsNull():
        raise ValueError('OpenCASCADE could not reconstruct closed volumes')
    solids = []
    explorer = TopExp_Explorer(maker.Shape(), TopAbs_SOLID)
    while explorer.More():
        solid = topods.Solid(explorer.Current())
        valid, error = _validate_occ_shape(solid, require_solid=True)
        if not valid:
            raise ValueError(f'repair produced an invalid cell: {error}')
        if _cell_material(solid, source_material, origin, scale, precision):
            solids.append(solid)
        explorer.Next()
    if not solids:
        raise ValueError('repair found no unambiguous closed material volume')
    result = combine(solids)
    valid, error = _validate_occ_shape(result, require_solid=True)
    if not valid:
        raise ValueError(f'repair produced invalid solids: {error}')
    repair_metrics(vertices, faces, result, max_attached_area_ratio=max_attached_area_ratio)
    return result


def repair_metrics(vertices, faces, shape, *, max_attached_area_ratio=0.005):
    from occ_geometry import bounds, volume, _count_topo
    from OCC.Core.TopAbs import TopAbs_SOLID
    old_bounds = np.concatenate((np.min(vertices, axis=0), np.max(vertices, axis=0)))
    deviation = float(np.max(np.abs(old_bounds - bounds(shape))))
    precision = repair_precision(vertices)
    numerical = float(np.max(np.abs(vertices))) * 2e-14
    if not np.isfinite(deviation) or deviation > max(precision * 10, numerical):
        raise ValueError('repair changed the source position or outer bounds')
    origin = vertices[0] + np.mean(vertices - vertices[0], axis=0)
    triangles = vertices[faces] - origin
    source_volume = abs(float(np.sum(np.einsum(
        'ij,ij->i', triangles[:, 0], np.cross(triangles[:, 1], triangles[:, 2]),
    )) / 6))
    repaired_volume = volume(shape)
    if not np.isfinite(source_volume) or not np.isfinite(repaired_volume):
        raise ValueError('repair volume measurements are not finite')
    change = (repaired_volume - source_volume) / source_volume * 100 if source_volume > 0 else None
    if change is not None and not np.isfinite(change):
        change = None
    _, correction = _repair_source_faces(vertices, faces, max_attached_area_ratio)
    return {
        **correction,
        'source_signed_volume_magnitude': source_volume,
        'source_volume_reference': 'signed sum about the mean source vertex; invalid input reference only',
        'volume': repaired_volume,
        'volume_change_percent': change,
        'bounds_change': deviation,
        'solids': _count_topo(shape, TopAbs_SOLID),
        'material_rule': 'even-odd',
    }
