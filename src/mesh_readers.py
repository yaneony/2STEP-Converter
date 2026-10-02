import math
import posixpath
import re
import struct
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from config import Settings
from kernel_io import quiet


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    signature: tuple
    started_ns: int

    @staticmethod
    def _signature(path):
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    @classmethod
    def capture(cls, path):
        path = Path(path).absolute()
        started_ns = time.time_ns()
        signature = cls._signature(path)
        return cls(path, signature, max(started_ns, signature[3]))

    def validate(self):
        try:
            unchanged = self._signature(self.path) == self.signature
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError('input changed during conversion; retry after the source file is stable')


def _clean_mesh_arrays(vertices, triangles):
    from mesh_geometry import _clean_mesh_arrays as clean
    return clean(vertices, triangles)


_UNIT_TO_MM = {
    'micron': 0.001,
    'micrometer': 0.001,
    'millimeter': 1.0,
    'centimeter': 10.0,
    'meter': 1000.0,
    'inch': 25.4,
    'foot': 304.8,
    'feet': 304.8,
}


def _find_3mf_model(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    rels_path = '_rels/.rels'
    if rels_path in names:
        with zf.open(rels_path) as f:
            root = ET.parse(f).getroot()
        rels_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
        for rel in root.findall(f'{{{rels_ns}}}Relationship'):
            if '3dmanufacturing' in rel.get('Type', ''):
                target = rel.get('Target', '').lstrip('/')
                if target:
                    return target
    for name in names:
        if name.endswith('.model'):
            return name
    raise ValueError('could not find 3D model document in 3MF archive')


def _stl_tri_count(path: Path):
    try:
        with open(path, 'rb') as f:
            header = f.read(84)
        if len(header) < 84:
            return None
        n = struct.unpack_from('<I', header, 80)[0]
        binary_size = 84 + n * 50
        if binary_size <= path.stat().st_size and (n > 0 or binary_size == path.stat().st_size):
            return n
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return sum((1 for line in f if line.lstrip().startswith('facet normal')))
    except Exception:
        return None


def _unit_scale_mm(value: str | None, default: str='millimeter') -> float:
    unit = (value or default).strip().lower()
    if unit not in _UNIT_TO_MM:
        raise ValueError(f'unsupported model unit: {unit}')
    return _UNIT_TO_MM[unit]


def _xml_local_name(element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def _xml_children(element, name: str):
    return [child for child in list(element) if _xml_local_name(child) == name]


def _xml_child(element, name: str):
    return next((child for child in list(element) if _xml_local_name(child) == name), None)


def _xml_attr(element, name: str):
    return next((value for key, value in element.attrib.items() if key.rsplit('}', 1)[-1] == name), None)


def _xml_float(element, name: str, default: float=0.0) -> float:
    child = _xml_child(element, name)
    return float(child.text) if child is not None and child.text else default


def _xml_int(element, name: str) -> int:
    child = _xml_child(element, name)
    if child is None or child.text is None:
        raise ValueError(f'missing integer element: {name}')
    return int(child.text)


def _transform_3mf(value: str | None):
    import numpy as np
    if not value:
        return np.eye(4, dtype=np.float64)
    parts = [float(part) for part in value.split()]
    if len(parts) != 12 or not np.isfinite(parts).all():
        raise ValueError(f'invalid 3MF transform: {value!r}')
    return np.array(
        [
            [parts[0], parts[3], parts[6], parts[9]],
            [parts[1], parts[4], parts[7], parts[10]],
            [parts[2], parts[5], parts[8], parts[11]],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _apply_transform(verts, transform):
    import numpy as np
    verts = np.asarray(verts, dtype=np.float64)
    if len(verts) == 0:
        return verts.reshape(0, 3)
    homogeneous = np.column_stack((verts, np.ones(len(verts), dtype=np.float64)))
    return (transform @ homogeneous.T).T[:, :3]


def _scale_3mf_transform(transform, unit_scale: float):
    scaled = transform.copy()
    scaled[:3, 3] *= unit_scale
    return scaled


def _resolve_3mf_part(zf: zipfile.ZipFile, target: str) -> str:
    normalized = posixpath.normpath(target.replace('\\', '/').lstrip('/'))
    if normalized in ('', '.') or normalized == '..' or normalized.startswith('../'):
        raise ValueError(f'invalid 3MF model path: {target!r}')
    matches = {name.casefold(): name for name in zf.namelist()}
    resolved = matches.get(normalized.casefold())
    if resolved is None:
        raise ValueError(f'3MF references missing model document {target}')
    return resolved


def _parse_3mf_model(model_file):
    raw = model_file.read()
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        if 'unbound prefix' not in str(exc):
            raise
        root = re.search(br'<model\b(?:[^>"\x27]|"[^"]*"|\x27[^\x27]*\x27)*>', raw)
        if root is None or re.search(br'\bxmlns:p\s*=', root.group()):
            raise
        if not re.search(br'\bp:(?:path|UUID)\s*=', raw) or re.search(br'</?p:', raw):
            raise
        declaration = b' xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
        corrected = raw[:root.end() - 1] + declaration + raw[root.end() - 1:]
        return ET.fromstring(corrected)


def _load_3mf_arrays(path: Path):
    import numpy as np
    with zipfile.ZipFile(str(path)) as zf:
        root_document = _resolve_3mf_part(zf, _find_3mf_model(zf))
        documents = {}

        def load_document(document_path):
            if document_path in documents:
                return documents[document_path]
            with zf.open(document_path) as model_file:
                model_root = _parse_3mf_model(model_file)
            resources = _xml_child(model_root, 'resources')
            if resources is None:
                raise ValueError(f'3MF model document has no resources: {document_path}')
            unit_scale = _unit_scale_mm(model_root.get('unit'))
            objects = {}
            referenced = set()
            for obj in _xml_children(resources, 'object'):
                obj_id = obj.get('id')
                if not obj_id:
                    continue
                mesh_el = _xml_child(obj, 'mesh')
                mesh = None
                if mesh_el is not None:
                    vertices_el = _xml_child(mesh_el, 'vertices')
                    triangles_el = _xml_child(mesh_el, 'triangles')
                    if vertices_el is not None and triangles_el is not None:
                        verts = [(
                            float(v.get('x')),
                            float(v.get('y')),
                            float(v.get('z')),
                        ) for v in _xml_children(vertices_el, 'vertex')]
                        tris = [(
                            int(t.get('v1')),
                            int(t.get('v2')),
                            int(t.get('v3')),
                        ) for t in _xml_children(triangles_el, 'triangle')]
                        mesh = (
                            np.asarray(verts, dtype=np.float64).reshape(-1, 3) * unit_scale,
                            np.asarray(tris, dtype=np.int32).reshape(-1, 3),
                        )
                        mesh = _clean_mesh_arrays(*mesh)
                components_el = _xml_child(obj, 'components')
                components = []
                if components_el is not None:
                    for component in _xml_children(components_el, 'component'):
                        child_id = component.get('objectid')
                        if not child_id:
                            continue
                        target = _xml_attr(component, 'path')
                        if target and document_path != root_document:
                            raise ValueError('3MF external component paths are only valid in the root model document')
                        child_document = _resolve_3mf_part(zf, target) if target else document_path
                        child_transform = _scale_3mf_transform(
                            _transform_3mf(component.get('transform')),
                            unit_scale,
                        )
                        components.append((child_document, child_id, child_transform))
                        if child_document == document_path:
                            referenced.add(child_id)
                objects[obj_id] = (mesh, components)
            document = (model_root, unit_scale, objects, referenced)
            documents[document_path] = document
            return document
        root, root_scale, root_objects, root_referenced = load_document(root_document)
        verts_all, tris_all = ([], [])
        vertex_count = 0

        def emit(document_path, obj_id, transform, stack):
            nonlocal vertex_count
            key = (document_path, obj_id)
            if key in stack:
                raise ValueError(f'cyclic 3MF component reference involving object {obj_id}')
            _, _, objects, _ = load_document(document_path)
            if obj_id not in objects:
                raise ValueError(f'3MF references missing object {obj_id} in {document_path}')
            mesh, components = objects[obj_id]
            if mesh is not None:
                verts, tris = mesh
                verts_all.append(_apply_transform(verts, transform))
                if np.linalg.det(transform[:3, :3]) < 0:
                    tris = tris[:, [0, 2, 1]]
                tris_all.append(tris + vertex_count)
                vertex_count += len(verts)
            for child_document, child_id, child_transform in components:
                emit(child_document, child_id, transform @ child_transform, stack | {key})
        build = _xml_child(root, 'build')
        items = _xml_children(build, 'item') if build is not None else []
        if items:
            for item in items:
                if item.get('printable', '1').lower() in ('0', 'false'):
                    continue
                obj_id = item.get('objectid')
                if not obj_id:
                    continue
                target = _xml_attr(item, 'path')
                document_path = _resolve_3mf_part(zf, target) if target else root_document
                item_transform = _scale_3mf_transform(_transform_3mf(item.get('transform')), root_scale)
                emit(document_path, obj_id, item_transform, set())
        else:
            roots = [obj_id for obj_id in root_objects if obj_id not in root_referenced]
            for obj_id in roots:
                emit(root_document, obj_id, np.eye(4), set())
    if not verts_all or not tris_all:
        raise ValueError('3MF model contains no triangle geometry')
    return (np.vstack(verts_all), np.vstack(tris_all))


def _amf_instance_transform(instance):
    import numpy as np
    tx = _xml_float(instance, 'deltax')
    ty = _xml_float(instance, 'deltay')
    tz = _xml_float(instance, 'deltaz')
    rx = math.radians(_xml_float(instance, 'rx'))
    ry = math.radians(_xml_float(instance, 'ry'))
    rz = math.radians(_xml_float(instance, 'rz'))
    sx, cx = (math.sin(rx), math.cos(rx))
    sy, cy = (math.sin(ry), math.cos(ry))
    sz, cz = (math.sin(rz), math.cos(rz))
    mx = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]], dtype=float)
    my = np.array([[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]], dtype=float)
    mz = np.array([[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
    mt = np.eye(4, dtype=float)
    mt[:3, 3] = (tx, ty, tz)
    return mt @ mz @ my @ mx


def _load_amf_arrays(path: Path):
    import numpy as np
    raw = path.read_bytes()
    if raw[:2] == b'PK':
        with zipfile.ZipFile(str(path)) as zf:
            names = zf.namelist()
            target = next((n for n in names if n.lower().endswith('.amf')), None)
            if target is None:
                raise ValueError('compressed AMF archive contains no .amf document')
            with zf.open(target) as f:
                root = ET.parse(f).getroot()
    else:
        root = ET.fromstring(raw)
    objects = {}
    for obj in _xml_children(root, 'object'):
        obj_id = obj.get('id')
        mesh_el = _xml_child(obj, 'mesh')
        if not obj_id or mesh_el is None:
            continue
        vertices_el = _xml_child(mesh_el, 'vertices')
        if vertices_el is None:
            continue
        verts = []
        for vertex in _xml_children(vertices_el, 'vertex'):
            coords = _xml_child(vertex, 'coordinates')
            if coords is not None:
                verts.append([_xml_float(coords, 'x'), _xml_float(coords, 'y'), _xml_float(coords, 'z')])
        tris = []
        for volume in _xml_children(mesh_el, 'volume'):
            for tri in _xml_children(volume, 'triangle'):
                tris.append([_xml_int(tri, 'v1'), _xml_int(tri, 'v2'), _xml_int(tri, 'v3')])
        objects[obj_id] = (
            np.asarray(verts, dtype=np.float64).reshape(-1, 3),
            np.asarray(tris, dtype=np.int32).reshape(-1, 3),
        )
        objects[obj_id] = _clean_mesh_arrays(*objects[obj_id])
    scale = _unit_scale_mm(root.get('unit'))
    scale_transform = np.diag([scale, scale, scale, 1.0])
    instances = []
    for constellation in _xml_children(root, 'constellation'):
        for instance in _xml_children(constellation, 'instance'):
            obj_id = instance.get('objectid')
            if obj_id:
                instances.append((obj_id, _amf_instance_transform(instance)))
    if not instances:
        instances = [(obj_id, np.eye(4, dtype=float)) for obj_id in objects]
    verts_all, tris_all, offset = ([], [], 0)
    for obj_id, transform in instances:
        if obj_id not in objects:
            raise ValueError(f'AMF references missing object {obj_id}')
        verts, tris = objects[obj_id]
        verts_all.append(_apply_transform(verts, scale_transform @ transform))
        tris_all.append(tris + offset)
        offset += len(verts)
    if not verts_all or not tris_all:
        raise ValueError('AMF model contains no triangle geometry')
    return (np.vstack(verts_all), np.vstack(tris_all))


def _triangulate_polygon(points, indices):
    import numpy as np
    polygon = []
    for index in indices:
        if not polygon or index != polygon[-1]:
            polygon.append(index)
    if len(polygon) > 1 and polygon[0] == polygon[-1]:
        polygon.pop()
    if len(polygon) < 3:
        raise ValueError('OBJ face has fewer than three distinct vertices')
    coordinates = np.asarray([points[index] for index in polygon], dtype=np.float64)
    if not np.isfinite(coordinates).all():
        raise ValueError('OBJ face contains non-finite coordinates')
    coordinates = coordinates - coordinates[0]
    scale = float(np.max(np.abs(coordinates)))
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError('OBJ face is degenerate')
    coordinates /= scale
    normal = np.zeros(3, dtype=np.float64)
    for current, following in zip(coordinates, np.roll(coordinates, -1, axis=0)):
        normal += np.array([
            (current[1] - following[1]) * (current[2] + following[2]),
            (current[2] - following[2]) * (current[0] + following[0]),
            (current[0] - following[0]) * (current[1] + following[1]),
        ])
    drop_axis = int(np.argmax(np.abs(normal)))
    if abs(normal[drop_axis]) <= 1e-15:
        raise ValueError('OBJ face is degenerate')
    projected = np.delete(coordinates, drop_axis, axis=1)
    epsilon = float(np.ptp(projected, axis=0).max()) ** 2 * 1e-12

    def cross_2d(left, middle, right):
        first = middle - left
        second = right - middle
        return first[0] * second[1] - first[1] * second[0]
    following = np.roll(projected, -1, axis=0)
    area2 = float(np.sum(projected[:, 0] * following[:, 1] - following[:, 0] * projected[:, 1]))
    if abs(area2) <= epsilon:
        raise ValueError('OBJ face has zero projected area')
    orientation = 1.0 if area2 > 0 else -1.0
    remaining = list(range(len(polygon)))
    changed = True
    while changed and len(remaining) > 3:
        changed = False
        for position in range(len(remaining)):
            left = remaining[position - 1]
            middle = remaining[position]
            right = remaining[(position + 1) % len(remaining)]
            if abs(cross_2d(projected[left], projected[middle], projected[right])) <= epsilon:
                remaining.pop(position)
                changed = True
                break
    if len(remaining) < 3:
        raise ValueError('OBJ face is degenerate after removing collinear vertices')

    def point_in_triangle(point, left, middle, right):
        values = (cross_2d(left, middle, point), cross_2d(middle, right, point), cross_2d(right, left, point))
        return all((orientation * value >= -epsilon for value in values))
    triangles = []
    while len(remaining) > 3:
        ear_found = False
        for position in range(len(remaining)):
            left = remaining[position - 1]
            middle = remaining[position]
            right = remaining[(position + 1) % len(remaining)]
            if orientation * cross_2d(projected[left], projected[middle], projected[right]) <= epsilon:
                continue
            if any((point_in_triangle(
                projected[candidate],
                projected[left],
                projected[middle],
                projected[right],
            ) for candidate in remaining if candidate not in (left, middle, right))):
                continue
            triangles.append([polygon[left], polygon[middle], polygon[right]])
            remaining.pop(position)
            ear_found = True
            break
        if not ear_found:
            raise ValueError('OBJ face is self-intersecting or cannot be triangulated')
    triangles.append([polygon[index] for index in remaining])
    return triangles


def _load_mesh_arrays(path: Path, *, settings: Settings=Settings()):
    import numpy as np
    ext = path.suffix.lower()
    if ext == settings.stl_file_extension:
        with open(path, 'rb') as f:
            f.seek(80)
            count_bytes = f.read(4)
            n = struct.unpack('<I', count_bytes)[0] if len(count_bytes) == 4 else 0
            data = f.read()
        binary_data_size = n * 50
        if (
            len(count_bytes) == 4
            and binary_data_size <= len(data)
            and (n > 0 or binary_data_size == len(data))
        ):
            stl_dt = np.dtype([
                ('n', np.float32, (3,)),
                ('v0', np.float32, (3,)),
                ('v1', np.float32, (3,)),
                ('v2', np.float32, (3,)),
                ('attr', np.uint16),
            ])
            tris = np.frombuffer(data[:binary_data_size], dtype=stl_dt)
            verts = np.stack([tris['v0'], tris['v1'], tris['v2']], axis=1).reshape(-1, 3)
            return (verts.astype(np.float64), np.arange(n * 3, dtype=np.int32).reshape(n, 3))
        verts = []
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.split()
                if p and p[0] == 'vertex':
                    if len(p) != 4:
                        raise ValueError('invalid ASCII STL vertex')
                    verts.append([float(p[1]), float(p[2]), float(p[3])])
        if not verts or len(verts) % 3:
            raise ValueError('STL contains no complete triangle geometry')
        v = np.array(verts, dtype=np.float64).reshape(-1, 3)
        return (v, np.arange(len(v), dtype=np.int32).reshape(-1, 3))
    if ext == settings.three_mf_file_extension:
        return _load_3mf_arrays(path)
    if ext == settings.obj_file_extension:
        verts, tris = ([], [])
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.partition('#')[0].split()
                if not p:
                    continue
                if p[0] == 'v':
                    verts.append([float(p[1]), float(p[2]), float(p[3])])
                elif p[0] == 'f':
                    idx = []
                    for x in p[1:]:
                        raw = int(x.split('/')[0])
                        if raw == 0:
                            raise ValueError('OBJ vertex indices cannot be zero')
                        idx.append(len(verts) + raw if raw < 0 else raw - 1)
                    if any((index < 0 or index >= len(verts) for index in idx)):
                        raise ValueError('OBJ face references a missing vertex')
                    tris.extend(_triangulate_polygon(verts, idx))
        return (
            np.array(verts, dtype=np.float64).reshape(-1, 3),
            np.array(tris, dtype=np.int32).reshape(-1, 3),
        )
    if ext == settings.amf_file_extension:
        return _load_amf_arrays(path)
    raise ValueError(f'unsupported format for reduction: {path.suffix}')


def _quick_3mf_tri_count(path: Path):
    with zipfile.ZipFile(str(path)) as zf:
        root_document = _resolve_3mf_part(zf, _find_3mf_model(zf))
        documents = {}

        def load_document(document_path):
            if document_path in documents:
                return documents[document_path]
            with zf.open(document_path) as model_file:
                root = _parse_3mf_model(model_file)
            resources = _xml_child(root, 'resources')
            if resources is None:
                raise ValueError(f'3MF model document has no resources: {document_path}')
            objects = {}
            referenced = set()
            for obj in _xml_children(resources, 'object'):
                obj_id = obj.get('id')
                if not obj_id:
                    continue
                mesh = _xml_child(obj, 'mesh')
                triangles = _xml_child(mesh, 'triangles') if mesh is not None else None
                triangle_count = len(_xml_children(triangles, 'triangle')) if triangles is not None else 0
                components = []
                components_element = _xml_child(obj, 'components')
                if components_element is not None:
                    for component in _xml_children(components_element, 'component'):
                        child_id = component.get('objectid')
                        if not child_id:
                            continue
                        target = _xml_attr(component, 'path')
                        if target and document_path != root_document:
                            raise ValueError('3MF external component paths are only valid in the root model document')
                        child_document = _resolve_3mf_part(zf, target) if target else document_path
                        components.append((child_document, child_id))
                        if child_document == document_path:
                            referenced.add(child_id)
                objects[obj_id] = (triangle_count, components)
            document = (root, objects, referenced)
            documents[document_path] = document
            return document
        root, root_objects, root_referenced = load_document(root_document)

        def count_object(document_path, object_id, stack):
            key = (document_path, object_id)
            if key in stack:
                raise ValueError(f'cyclic 3MF component reference involving object {object_id}')
            _, objects, _ = load_document(document_path)
            if object_id not in objects:
                raise ValueError(f'3MF references missing object {object_id} in {document_path}')
            triangle_count, components = objects[object_id]
            return triangle_count + sum((count_object(
                child_document,
                child_id,
                stack | {key},
            ) for child_document, child_id in components))
        build = _xml_child(root, 'build')
        items = _xml_children(build, 'item') if build is not None else []
        if items:
            total = 0
            for item in items:
                if item.get('printable', '1').lower() in ('0', 'false'):
                    continue
                object_id = item.get('objectid')
                if not object_id:
                    continue
                target = _xml_attr(item, 'path')
                document_path = _resolve_3mf_part(zf, target) if target else root_document
                total += count_object(document_path, object_id, set())
            return total or None
        roots = [object_id for object_id in root_objects if object_id not in root_referenced]
        total = sum((count_object(root_document, object_id, set()) for object_id in roots))
        return total or None


def _quick_amf_tri_count(path: Path):
    raw = path.read_bytes()
    if raw[:2] == b'PK':
        with zipfile.ZipFile(str(path)) as zf:
            target = next((name for name in zf.namelist() if name.lower().endswith('.amf')), None)
            if target is None:
                raise ValueError('compressed AMF archive contains no .amf document')
            with zf.open(target) as model_file:
                root = ET.parse(model_file).getroot()
    else:
        root = ET.fromstring(raw)
    object_counts = {}
    for obj in _xml_children(root, 'object'):
        object_id = obj.get('id')
        mesh = _xml_child(obj, 'mesh')
        if not object_id or mesh is None:
            continue
        object_counts[object_id] = sum((len(_xml_children(
            volume,
            'triangle',
        )) for volume in _xml_children(mesh, 'volume')))
    instances = [instance.get('objectid') for constellation in _xml_children(
        root,
        'constellation',
    ) for instance in _xml_children(
        constellation,
        'instance',
    ) if instance.get('objectid')]
    if instances:
        total = 0
        for object_id in instances:
            if object_id not in object_counts:
                raise ValueError(f'AMF references missing object {object_id}')
            total += object_counts[object_id]
    else:
        total = sum(object_counts.values())
    return total or None


def _quick_tri_count(path: Path, *, settings: Settings=Settings()):
    ext = path.suffix.lower()
    if ext == settings.stl_file_extension:
        return _stl_tri_count(path)
    if ext == settings.three_mf_file_extension:
        try:
            return _quick_3mf_tri_count(path)
        except Exception:
            return None
    if ext == settings.obj_file_extension:
        try:
            count = 0
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    parts = line.partition('#')[0].split()
                    if parts and parts[0] == 'f':
                        if len(parts) < 4:
                            return None
                        count += len(parts) - 3
            return count or None
        except Exception:
            return None
    if ext == settings.amf_file_extension:
        try:
            return _quick_amf_tri_count(path)
        except Exception:
            return None
    return None


def _read_iges_shape(path: Path):
    from OCC.Core.IGESControl import IGESControl_Reader
    from OCC.Core.IFSelect import IFSelect_RetDone
    reader = IGESControl_Reader()
    with quiet():
        status = reader.ReadFile(path.as_posix())
        if status == IFSelect_RetDone:
            reader.TransferRoots()
    if status != IFSelect_RetDone:
        raise ValueError(f'IGES reader failed with status {status}')
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError('IGES file produced an empty shape')
    return (shape, None, None)
