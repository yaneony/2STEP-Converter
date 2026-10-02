import numpy as np
from config import Settings, _positive_number


def component_groups(labels):
    labels = np.asarray(labels, dtype=np.int64)
    if not len(labels):
        return []
    order = np.argsort(labels, kind="stable")
    breaks = np.flatnonzero(np.diff(labels[order])) + 1
    return np.split(order, breaks)


def confirmed_intersections(vertices, faces, pairs):
    pairs = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
    if not len(pairs):
        return pairs
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces)
    result = []
    for first in range(0, len(pairs), 4096):
        batch = pairs[first:first + 4096]
        triangles = vertices[faces[batch]]
        triangles = triangles - triangles[:, :1, :1]
        scale = np.max(np.abs(triangles), axis=(1, 2, 3))
        triangles = np.divide(triangles, scale[:, None, None, None],
                              out=np.zeros_like(triangles), where=scale[:, None, None, None] > 0)
        edges = np.roll(triangles, -1, axis=2) - triangles
        normals = np.cross(edges[:, :, 0], edges[:, :, 1])
        cross_edges = np.cross(edges[:, 0, :, None], edges[:, 1, None, :]).reshape(-1, 9, 3)
        coplanar = np.cross(normals[:, :, None], edges).reshape(-1, 6, 3)
        axes = np.concatenate((normals, cross_edges, coplanar), axis=1)
        lengths = np.linalg.norm(axes, axis=2)
        axes = np.divide(axes, lengths[:, :, None], out=np.zeros_like(axes), where=lengths[:, :, None] > 0)
        left = np.einsum('bij,bkj->bik', triangles[:, 0], axes)
        right = np.einsum('bij,bkj->bik', triangles[:, 1], axes)
        slack = np.finfo(np.float64).eps * 64.0
        separated = np.any((left.max(axis=1) < right.min(axis=1) - slack)
                           | (right.max(axis=1) < left.min(axis=1) - slack), axis=1)
        result.append(batch[~separated])
    return np.concatenate(result) if result else np.empty((0, 2), dtype=np.int64)


def component_measurements(vertices, faces):
    import open3d as o3d
    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(vertices)
    mesh.triangles = o3d.utility.Vector3iVector(faces)
    labels, _, _ = mesh.cluster_connected_triangles()
    result = []
    for indices in component_groups(labels):
        triangles = faces[indices]
        points = vertices[np.unique(triangles)]
        edges = np.unique(np.sort(np.vstack((triangles[:, [0, 1]],
                                            triangles[:, [1, 2]],
                                            triangles[:, [2, 0]])), axis=1), axis=0)
        euler = len(points) - len(edges) + len(triangles)
        origin = points.mean(axis=0)
        p0, p1, p2 = (vertices[triangles[:, k]] - origin for k in range(3))
        volume = abs(float(np.einsum("ij,ij->i", p0, np.cross(p1, p2)).sum() / 6.0))
        result.append((np.concatenate((points.min(axis=0), points.max(axis=0))), euler, volume))
    return result


def surface_deviation(vertices, faces, other_vertices, other_faces):
    import open3d as o3d
    origin = np.minimum(vertices.min(axis=0), other_vertices.min(axis=0))
    high = np.maximum(vertices.max(axis=0), other_vertices.max(axis=0))
    scale = float(np.linalg.norm(high - origin))
    if scale <= 0:
        raise ValueError("mesh has zero size")
    origin = origin + (high - origin) * 0.5
    left = (vertices - origin) / scale
    right = (other_vertices - origin) / scale

    def directed(points, triangles, target_points, target_triangles):
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.core.Tensor(target_points.astype(np.float32)),
                            o3d.core.Tensor(target_triangles.astype(np.uint32)))
        worst = 0.0
        for first in range(0, len(triangles), 8192):
            samples = points[triangles[first:first + 8192]]
            queries = np.concatenate((samples.reshape(-1, 3), samples.mean(axis=1),
                (samples[:, 0] + samples[:, 1]) * 0.5,
                (samples[:, 1] + samples[:, 2]) * 0.5,
                (samples[:, 2] + samples[:, 0]) * 0.5))
            distances = scene.compute_distance(o3d.core.Tensor(queries.astype(np.float32))).numpy()
            if not np.isfinite(distances).all():
                raise ValueError("surface distance calculation failed")
            worst = max(worst, float(distances.max()))
        return worst

    return max(directed(left, faces, right, other_faces),
               directed(right, other_faces, left, faces)) * scale


def reduction_geometry_error(vertices, faces, new_vertices, new_faces,
                             max_surface_percent, preserve_topology=True,
                             max_size_percent=0.5, max_volume_percent=2.0):
    before = component_measurements(vertices, faces)
    pending = component_measurements(new_vertices, new_faces)
    if len(before) != len(pending):
        return "reduction changed connected component count"
    for old_bounds, old_euler, old_volume in before:
        index = min(range(len(pending)), key=lambda i: np.linalg.norm(old_bounds - pending[i][0]))
        new_bounds, new_euler, new_volume = pending.pop(index)
        diagonal = max(float(np.linalg.norm(old_bounds[3:] - old_bounds[:3])), 1e-12)
        if preserve_topology and old_euler != new_euler:
            return "reduction changed component topology (a hole or handle was lost)"
        if np.max(np.abs(new_bounds - old_bounds)) / diagonal * 100.0 > max_size_percent:
            return "reduction changed a component's position or size"
        if (
            old_volume > diagonal ** 3 * 1e-12
            and abs(new_volume - old_volume) / old_volume * 100 > max_volume_percent
        ):
            return "reduction changed a component's volume"
    diagonal = float(np.linalg.norm(np.ptp(vertices, axis=0)))
    deviation = surface_deviation(vertices, faces, new_vertices, new_faces)
    limit = max(diagonal * max_surface_percent / 100.0, diagonal * 2e-7)
    if deviation > limit:
        return f"reduction moved the surface by {deviation:.6g} (limit {limit:.6g})"
    return None


def _clean_mesh_arrays(verts, tris):
    verts = np.asarray(verts, dtype=np.float64)
    tris = np.asarray(tris)
    if verts.size == 0:
        verts = verts.reshape(0, 3)
    if tris.size == 0:
        tris = tris.reshape(0, 3)
    if verts.ndim != 2 or verts.shape[1] != 3:
        raise ValueError("vertices must be an Nx3 array")
    if tris.ndim != 2 or tris.shape[1] != 3:
        raise ValueError("triangles must be an Nx3 array")
    if not np.isfinite(verts).all():
        raise ValueError("mesh contains non-finite vertex coordinates")
    if (
        not (np.issubdtype(tris.dtype, np.integer) or np.issubdtype(tris.dtype, np.floating))
        or not np.isfinite(tris).all()
    ):
        raise ValueError("triangle indices must be finite integers")
    if not np.equal(tris, np.floor(tris)).all():
        raise ValueError("triangle indices must be integers")
    if tris.min(initial=0) < 0 or tris.max(initial=-1) >= len(verts):
        raise ValueError("mesh contains out-of-range triangle indices")
    unique_v, inv = np.unique(verts, axis=0, return_inverse=True)
    new_faces = inv[tris.astype(np.int64)]
    good = ((new_faces[:, 0] != new_faces[:, 1]) &
            (new_faces[:, 1] != new_faces[:, 2]) &
            (new_faces[:, 0] != new_faces[:, 2]))
    new_faces = new_faces[good]
    if len(new_faces):
        p0 = unique_v[new_faces[:, 0]]
        p1 = unique_v[new_faces[:, 1]]
        p2 = unique_v[new_faces[:, 2]]
        normals = np.cross(p1 - p0, p2 - p0)
        area_sq = np.einsum("ij,ij->i", normals, normals)
        new_faces = new_faces[area_sq > 0]
    if len(new_faces):
        _, first = np.unique(
            np.sort(new_faces, axis=1), axis=0, return_index=True)
        new_faces = new_faces[np.sort(first)]
    used, remapped = np.unique(new_faces, return_inverse=True)
    return unique_v[used], remapped.reshape(-1, 3).astype(np.int32)


def _mesh_quality_report(verts, tris, check_self_intersection=True):
    verts, tris = _clean_mesh_arrays(verts, tris)
    if not len(tris):
        return {
            "vertices": len(verts), "triangles": 0, "boundary_edges": 0,
            "non_manifold_edges": 0, "components": 0,
            "self_intersections": None,
            "internal_self_intersections": None,
            "cross_component_intersections": None,
            "watertight": False,
            "vertex_manifold": False, "orientable": False,
            "volume": 0.0, "dimensions": np.zeros(3), "diagonal": 0.0,
            "median_edge": 0.0,
        }

    edges = np.vstack((
        tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]],
    )).astype(np.int64)
    edge_lengths = np.linalg.norm(verts[edges[:, 0]] - verts[edges[:, 1]], axis=1)
    sorted_edges = np.sort(edges, axis=1)
    _, edge_counts = np.unique(sorted_edges, axis=0, return_counts=True)

    dimensions = np.ptp(verts, axis=0)
    diagonal = float(np.linalg.norm(dimensions))
    centered = verts - verts.mean(axis=0)
    signed_face_volumes = np.einsum(
        "ij,ij->i",
        centered[tris[:, 0]],
        np.cross(centered[tris[:, 1]], centered[tris[:, 2]]),
    ) / 6.0
    volume = abs(float(signed_face_volumes.sum()))
    self_intersections = None
    internal_self_intersections = None
    cross_component_intersections = None
    components = 1
    try:
        import open3d as o3d
        mesh = o3d.geometry.TriangleMesh()
        mesh.vertices = o3d.utility.Vector3dVector(verts)
        mesh.triangles = o3d.utility.Vector3iVector(tris)
        vertex_manifold = mesh.is_vertex_manifold()
        orientable = mesh.is_orientable()
        cluster_ids, _, _ = mesh.cluster_connected_triangles()
        cluster_ids = np.asarray(cluster_ids)
        components = int(cluster_ids.max()) + 1 if len(cluster_ids) else 0
        volume = 0.0
        for indices in component_groups(cluster_ids):
            component = tris[indices]
            origin = verts[np.unique(component)].mean(axis=0)
            p0, p1, p2 = (verts[component[:, k]] - origin for k in range(3))
            volume += abs(float(np.einsum("ij,ij->i", p0, np.cross(p1, p2)).sum() / 6.0))
        if check_self_intersection:
            intersection_pairs = np.asarray(
                mesh.get_self_intersecting_triangles(), dtype=np.int64)
            intersection_pairs = confirmed_intersections(verts, tris, intersection_pairs)
            self_intersections = len(intersection_pairs)
            if len(intersection_pairs):
                same_component = (
                    cluster_ids[intersection_pairs[:, 0]]
                    == cluster_ids[intersection_pairs[:, 1]]
                )
                internal_self_intersections = int(
                    np.count_nonzero(same_component))
                cross_component_intersections = int(
                    len(intersection_pairs) - internal_self_intersections)
            else:
                internal_self_intersections = 0
                cross_component_intersections = 0
    except ImportError:
        vertex_manifold = orientable = None
        parent = np.arange(len(verts), dtype=np.int64)

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for left, right in sorted_edges:
            root_left, root_right = find(int(left)), find(int(right))
            if root_left != root_right:
                parent[root_right] = root_left
        used = np.unique(tris)
        component_roots = {find(int(index)) for index in used}
        components = len(component_roots)
        triangle_roots = np.asarray(
            [find(int(face[0])) for face in tris], dtype=np.int64)
        volume = sum(
            abs(float(signed_face_volumes[triangle_roots == root].sum()))
            for root in component_roots
        )

    boundary_edges = int(np.count_nonzero(edge_counts == 1))
    non_manifold_edges = int(np.count_nonzero(edge_counts > 2))
    return {
        "vertices": len(verts),
        "triangles": len(tris),
        "boundary_edges": boundary_edges,
        "non_manifold_edges": non_manifold_edges,
        "components": components,
        "self_intersections": self_intersections,
        "internal_self_intersections": internal_self_intersections,
        "cross_component_intersections": cross_component_intersections,
        "watertight": (boundary_edges == 0 and non_manifold_edges == 0
                       and vertex_manifold is True and orientable is True),
        "vertex_manifold": vertex_manifold,
        "orientable": orientable,
        "volume": volume,
        "dimensions": dimensions,
        "diagonal": diagonal,
        "median_edge": float(np.median(edge_lengths)) if len(edge_lengths) else 0.0,
    }


def _needs_volume_repair(report):
    return bool(report and (
        report.get('internal_self_intersections')
        or report.get('non_manifold_edges')
        or report.get('orientable') is False
        or report.get('vertex_manifold') is False
    ))


def _conversion_quality_report(vertices, faces, *, settings: Settings=Settings()):
    check_intersections = (
        (settings.check_self_intersections or settings.repair_intersecting_mesh)
        and (settings.self_intersection_check_max_triangles <= 0
             or len(faces) <= settings.self_intersection_check_max_triangles)
    )
    return _mesh_quality_report(vertices, faces, check_intersections)


def _effective_tolerance(verts, requested, *, settings: Settings=Settings()):
    if not settings.use_scale_aware_sewing_tolerance or verts is None or (not len(verts)):
        return requested
    dimensions = np.ptp(np.asarray(verts, dtype=np.float64), axis=0)
    diagonal = float(np.linalg.norm(dimensions))
    if diagonal <= 0:
        return requested
    adaptive = max(diagonal * settings.scale_aware_sewing_tolerance_ratio, 1e-09)
    return min(float(requested), adaptive)


def _repair_mesh_arrays(verts, tris, *, settings: Settings=Settings()):
    verts, tris = _clean_mesh_arrays(verts, tris)
    if not settings.repair_mesh_before_conversion:
        return (verts, tris)
    try:
        import open3d as o3d
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(verts)
        o3d_mesh.triangles = o3d.utility.Vector3iVector(tris)
        dimensions = np.ptp(verts, axis=0) if len(verts) else np.zeros(3)
        diagonal = float(np.linalg.norm(dimensions))
        merge_tolerance = settings.vertex_merge_distance if settings.vertex_merge_distance > 0 else max(
            diagonal * 1e-09,
            1e-12,
        )
        o3d_mesh.merge_close_vertices(merge_tolerance)
        o3d_mesh.remove_duplicated_vertices()
        o3d_mesh.remove_duplicated_triangles()
        o3d_mesh.remove_degenerate_triangles()
        o3d_mesh.remove_unreferenced_vertices()
        if settings.remove_non_manifold_triangles:
            o3d_mesh.remove_non_manifold_edges()
        if settings.fix_triangle_orientation and o3d_mesh.is_orientable():
            o3d_mesh.orient_triangles()
        verts = np.asarray(o3d_mesh.vertices, dtype=np.float64)
        tris = np.asarray(o3d_mesh.triangles, dtype=np.int32)
    except Exception as exc:
        raise RuntimeError(f'Open3D mesh repair failed: {exc}') from exc
    if settings.fill_small_mesh_holes:
        try:
            import trimesh
            mesh = trimesh.Trimesh(vertices=verts, faces=tris, process=False)
            trimesh.repair.fill_holes(mesh)
            if settings.fix_triangle_orientation:
                trimesh.repair.fix_normals(mesh, multibody=True)
            verts = np.asarray(mesh.vertices, dtype=np.float64)
            tris = np.asarray(mesh.faces, dtype=np.int32)
        except Exception as exc:
            raise RuntimeError(f'hole filling failed: {exc}') from exc
    return _clean_mesh_arrays(verts, tris)


def _reduce_mesh_arrays(verts, faces, keep_fraction, *, settings: Settings=Settings()):
    if not _positive_number(keep_fraction) or keep_fraction > 1:
        raise ValueError('keep fraction must be greater than zero and at most one')
    verts, faces = _clean_mesh_arrays(verts, faces)
    n_before = len(faces)
    if not n_before:
        raise ValueError('mesh has no valid faces after vertex merging')
    target_count = max(4, int(n_before * keep_fraction))
    before = _mesh_quality_report(verts, faces, check_self_intersection=False)
    reducer_errors = []
    for reducer in ('Open3D', 'fast-simplification', 'fast-simplification conservative'):
        try:
            if reducer == 'Open3D':
                import open3d as o3d
                mesh = o3d.geometry.TriangleMesh()
                mesh.vertices = o3d.utility.Vector3dVector(verts)
                mesh.triangles = o3d.utility.Vector3iVector(faces)
                simplified = mesh.simplify_quadric_decimation(
                    target_count,
                    boundary_weight=settings.reduction_boundary_weight if settings.preserve_boundaries_during_reduction else 1.0,
                )
                verts_out = np.asarray(simplified.vertices, dtype=np.float64)
                faces_out = np.asarray(simplified.triangles, dtype=np.int32)
            else:
                import fast_simplification
                verts_out, faces_out = fast_simplification.simplify(
                    verts, faces, target_count=target_count,
                    agg=2 if reducer.endswith('conservative') else 7,
                )
            if len(faces_out) > target_count + max(2, target_count // 100):
                raise ValueError(f'reducer could only reach {len(faces_out):,} triangles (target {target_count:,})')
            return _validated_reduction(verts, faces, verts_out, faces_out, before, settings=settings)
        except Exception as exc:
            detail = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            reducer_errors.append(f'{reducer}: {detail}')
    raise ValueError('; '.join(reducer_errors))


def _validated_reduction(verts, faces, verts_out, faces_out, before, *, settings):
    verts_out, faces_out = _clean_mesh_arrays(verts_out, faces_out)
    if len(faces_out) == 0:
        raise ValueError('simplified mesh contains no valid faces')
    after = _conversion_quality_report(verts_out, faces_out, settings=settings)
    diagonal = max(before['diagonal'], 1e-12)
    dimension_error_pct = float(np.linalg.norm(after['dimensions'] - before['dimensions'])) / diagonal * 100.0
    if dimension_error_pct > settings.max_reduction_size_change_percent:
        raise ValueError(f'reduction changed model dimensions by {dimension_error_pct:.3g}%')
    if before['watertight'] and settings.preserve_boundaries_during_reduction and (not after['watertight']):
        raise ValueError('reduction broke mesh watertightness')
    if after['non_manifold_edges'] > before['non_manifold_edges']:
        raise ValueError('reduction introduced non-manifold edges')
    if settings.preserve_boundaries_during_reduction and after['components'] != before['components']:
        raise ValueError(f"reduction changed connected component count from {before['components']:,} to {after['components']:,}")
    if (
        (settings.reject_self_intersecting_mesh or settings.require_solid_output)
        and after['internal_self_intersections']
    ):
        raise ValueError(f"reduction result contains {after['internal_self_intersections']:,} internal self-intersections")
    if before['watertight'] and after['watertight'] and (before['volume'] > 1e-12):
        volume_error_pct = abs(after['volume'] - before['volume']) / before['volume'] * 100.0
        if volume_error_pct > settings.max_reduction_volume_change_percent:
            raise ValueError(f'reduction changed volume by {volume_error_pct:.3g}%')
    error = reduction_geometry_error(
        verts,
        faces,
        verts_out,
        faces_out,
        settings.max_reduction_surface_deviation_percent,
        preserve_topology=settings.preserve_boundaries_during_reduction,
        max_size_percent=settings.max_reduction_size_change_percent,
        max_volume_percent=settings.max_reduction_volume_change_percent,
    )
    if error:
        raise ValueError(error)
    return (verts_out, faces_out, len(faces), len(faces_out))

