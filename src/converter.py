import sys
import os
import json
import ctypes
import traceback
import argparse
import subprocess
import zipfile
import struct
import tempfile
import textwrap
import time
import threading
import math
import posixpath
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

VERSION = "3.0.0"

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_DIR / "data"
_DATA_DIR.mkdir(exist_ok=True)

STD_OUTPUT_HANDLE  = -11
CONSOLE_MODE_FLAGS = 7
BYTES_PER_KB       = 1024
NAME_TRIM_WIDTH    = 62

_CONFIG_DEFAULTS = {
    "SEWING_TOLERANCE": 0.01,
    "DEFAULT_REDUCTION_PERCENT": 0,
    "AUTO_REDUCTION_ENABLED": True,
    "AUTO_REDUCTION_TARGET_TRIANGLES": 50000,
    "ASK_FOR_REDUCTION": True,
    "SKIP_UP_TO_DATE_OUTPUTS": True,
    "PLANAR_MERGE_ANGLE_RADIANS": 0.01,
    "SEWING_TIMEOUT_SECONDS": 1800,
    "SEW_PARTS_SEPARATELY": True,
    "DEFAULT_STEP_FORMAT": "ap203",
    "GENERATE_PNG_PREVIEW": True,
    "INPUT_FOLDER_NAME": "models",
    "CHECK_MESH_QUALITY": True,
    "REPAIR_MESH_BEFORE_CONVERSION": True,
    "VERTEX_MERGE_DISTANCE": 0.0,
    "FIX_TRIANGLE_ORIENTATION": True,
    "REMOVE_NON_MANIFOLD_TRIANGLES": False,
    "REJECT_NON_MANIFOLD_MESH": False,
    "FILL_SMALL_MESH_HOLES":   False,
    "FILL_SMALL_PLANAR_BREP_GAPS":    True,
    "MAX_BREP_GAP_EDGE_COUNT": 8,
    "MAX_BREP_GAP_AREA_RATIO": 0.005,
    "CHECK_SELF_INTERSECTIONS": True,
    "SELF_INTERSECTION_CHECK_MAX_TRIANGLES": 50000,
    "REJECT_SELF_INTERSECTING_MESH": False,
    "USE_SCALE_AWARE_SEWING_TOLERANCE": True,
    "SCALE_AWARE_SEWING_TOLERANCE_RATIO": 1e-6,
    "REQUIRE_SOLID_OUTPUT": True,
    "VALIDATE_STEP_AFTER_WRITING": True,
    "PRESERVE_BOUNDARIES_DURING_REDUCTION": True,
    "REDUCTION_BOUNDARY_WEIGHT": 10.0,
    "MAX_REDUCTION_SIZE_CHANGE_PERCENT": 0.5,
    "MAX_REDUCTION_VOLUME_CHANGE_PERCENT": 2.0,
    "EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION": False,
    "EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO": 0.0005,
    "EXPERIMENTAL_PARAMETRIC_MAX_VOLUME_CHANGE_PERCENT": 0.1,
    "RECONSTRUCT_ANALYTIC_PRIMITIVES": True,
    "ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO": 0.001,
    "ANALYTIC_PRIMITIVE_MIN_TRIANGLES": 32,
    "RECONSTRUCT_ANALYTIC_THROUGH_HOLES": True,
    "RECONSTRUCT_ANALYTIC_BLIND_HOLES": True,
    "ANALYTIC_HOLE_FIT_ERROR_RATIO": 0.002,
    "ANALYTIC_HOLE_MIN_SIDES": 12,
    "ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO": 0.002,
    "ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS": 0.005,
    "ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT": 0.1,
    "STL_FILE_EXTENSION": ".stl",
    "THREE_MF_FILE_EXTENSION": ".3mf",
    "OBJ_FILE_EXTENSION": ".obj",
    "IGES_FILE_EXTENSION": ".igs",
    "AMF_FILE_EXTENSION": ".amf",
    "STEP_FILE_EXTENSION": ".stp",
}

_CONFIG_PATH = _DATA_DIR / "config.json"
_cfg_warnings: list[str] = []


def _valid_reduce_config(value) -> bool:
    values = value.split(",") if isinstance(value, str) else [value]
    if not values:
        return False
    for item in values:
        if isinstance(item, bool):
            return False
        if isinstance(item, str):
            item = item.strip().rstrip("%")
        try:
            pct = float(item)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(pct) or not 0 <= pct < 100:
            return False
    return True


def _positive_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _nonnegative_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        _CONFIG_PATH.write_text(
            json.dumps(_CONFIG_DEFAULTS, indent=4),
            encoding="utf-8",
        )
        raw = {}
    else:
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-level value must be an object")
        except Exception as exc:
            _cfg_warnings.append(f"config: could not read {_CONFIG_PATH.name}: {exc}; using defaults")
            raw = {}

    cfg = dict(_CONFIG_DEFAULTS)
    validators = {
        "SEWING_TOLERANCE": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
                                       and math.isfinite(v) and v > 0,
        "PLANAR_MERGE_ANGLE_RADIANS": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
                                       and math.isfinite(v) and v > 0,
        "SEWING_TIMEOUT_SECONDS": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
                                 and math.isfinite(v) and v > 0,
        "SEW_PARTS_SEPARATELY": lambda v: isinstance(v, bool),
        "DEFAULT_REDUCTION_PERCENT": _valid_reduce_config,
        "AUTO_REDUCTION_ENABLED": lambda v: isinstance(v, bool),
        "AUTO_REDUCTION_TARGET_TRIANGLES": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v >= 4),
        "DEFAULT_STEP_FORMAT": lambda v: v in ("ap203", "ap214", "ap242"),
        "ASK_FOR_REDUCTION": lambda v: isinstance(v, bool),
        "SKIP_UP_TO_DATE_OUTPUTS": lambda v: isinstance(v, bool),
        "GENERATE_PNG_PREVIEW": lambda v: isinstance(v, bool),
        "CHECK_MESH_QUALITY": lambda v: isinstance(v, bool),
        "REPAIR_MESH_BEFORE_CONVERSION": lambda v: isinstance(v, bool),
        "VERTEX_MERGE_DISTANCE": lambda v: _nonnegative_number(v),
        "FIX_TRIANGLE_ORIENTATION": lambda v: isinstance(v, bool),
        "REMOVE_NON_MANIFOLD_TRIANGLES": lambda v: isinstance(v, bool),
        "REJECT_NON_MANIFOLD_MESH": lambda v: isinstance(v, bool),
        "FILL_SMALL_MESH_HOLES": lambda v: isinstance(v, bool),
        "FILL_SMALL_PLANAR_BREP_GAPS": lambda v: isinstance(v, bool),
        "MAX_BREP_GAP_EDGE_COUNT": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v >= 3),
        "MAX_BREP_GAP_AREA_RATIO": lambda v: _nonnegative_number(v),
        "CHECK_SELF_INTERSECTIONS": lambda v: isinstance(v, bool),
        "SELF_INTERSECTION_CHECK_MAX_TRIANGLES": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        "REJECT_SELF_INTERSECTING_MESH": lambda v: isinstance(v, bool),
        "USE_SCALE_AWARE_SEWING_TOLERANCE": lambda v: isinstance(v, bool),
        "SCALE_AWARE_SEWING_TOLERANCE_RATIO": lambda v: _positive_number(v),
        "REQUIRE_SOLID_OUTPUT": lambda v: isinstance(v, bool),
        "VALIDATE_STEP_AFTER_WRITING": lambda v: isinstance(v, bool),
        "PRESERVE_BOUNDARIES_DURING_REDUCTION": lambda v: isinstance(v, bool),
        "REDUCTION_BOUNDARY_WEIGHT": lambda v: _positive_number(v),
        "MAX_REDUCTION_SIZE_CHANGE_PERCENT": lambda v: _nonnegative_number(v),
        "MAX_REDUCTION_VOLUME_CHANGE_PERCENT": lambda v: _nonnegative_number(v),
        "EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION": lambda v: isinstance(v, bool),
        "EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO": lambda v: _positive_number(v),
        "EXPERIMENTAL_PARAMETRIC_MAX_VOLUME_CHANGE_PERCENT": lambda v: (
            _nonnegative_number(v)),
        "RECONSTRUCT_ANALYTIC_PRIMITIVES": lambda v: isinstance(v, bool),
        "ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO": lambda v: _positive_number(v),
        "ANALYTIC_PRIMITIVE_MIN_TRIANGLES": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v >= 4),
        "RECONSTRUCT_ANALYTIC_THROUGH_HOLES": lambda v: isinstance(v, bool),
        "RECONSTRUCT_ANALYTIC_BLIND_HOLES": lambda v: isinstance(v, bool),
        "ANALYTIC_HOLE_FIT_ERROR_RATIO": lambda v: _positive_number(v),
        "ANALYTIC_HOLE_MIN_SIDES": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v >= 8),
        "ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO": lambda v: _positive_number(v),
        "ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS": lambda v: _positive_number(v),
        "ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT": lambda v: (
            _nonnegative_number(v)),
        "INPUT_FOLDER_NAME": lambda v: (isinstance(v, str) and bool(v.strip())
                                      and not Path(v).is_absolute()
                                      and ".." not in Path(v).parts),
        "STL_FILE_EXTENSION": lambda v: isinstance(v, str) and v.startswith(".") and len(v) > 1,
        "THREE_MF_FILE_EXTENSION": lambda v: isinstance(v, str) and v.startswith(".") and len(v) > 1,
        "OBJ_FILE_EXTENSION": lambda v: isinstance(v, str) and v.startswith(".") and len(v) > 1,
        "IGES_FILE_EXTENSION": lambda v: isinstance(v, str) and v.startswith(".") and len(v) > 1,
        "AMF_FILE_EXTENSION": lambda v: isinstance(v, str) and v.startswith(".") and len(v) > 1,
        "STEP_FILE_EXTENSION": lambda v: isinstance(v, str) and v.startswith(".") and len(v) > 1,
    }
    for key in raw:
        if key not in _CONFIG_DEFAULTS:
            _cfg_warnings.append(
                f"config: unknown setting {key}; setting ignored")

    for key, default in _CONFIG_DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]
        if validators[key](value):
            cfg[key] = (
                value.lower()
                if key.endswith("_FILE_EXTENSION")
                else value
            )
        else:
            _cfg_warnings.append(
                f"config: invalid {key}={value!r}; "
                f"using default {default!r}")
    input_extensions = {
        cfg["STL_FILE_EXTENSION"], cfg["THREE_MF_FILE_EXTENSION"], cfg["OBJ_FILE_EXTENSION"],
        cfg["IGES_FILE_EXTENSION"], cfg["AMF_FILE_EXTENSION"], ".iges",
    }
    if cfg["STEP_FILE_EXTENSION"] in input_extensions:
        _cfg_warnings.append(
            "config: STEP_FILE_EXTENSION="
            f"{cfg['STEP_FILE_EXTENSION']!r} conflicts with an input extension; "
            f"using default {_CONFIG_DEFAULTS['STEP_FILE_EXTENSION']!r}")
        cfg["STEP_FILE_EXTENSION"] = _CONFIG_DEFAULTS["STEP_FILE_EXTENSION"]
    return cfg


_cfg = _load_config()
globals().update(_cfg)

try:
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE), CONSOLE_MODE_FLAGS)
except Exception:
    pass

_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
G   = '\033[92m' if _USE_COLOR else ''
R   = '\033[91m' if _USE_COLOR else ''
Y   = '\033[93m' if _USE_COLOR else ''
C   = '\033[96m' if _USE_COLOR else ''
DIM = '\033[2m'  if _USE_COLOR else ''
B   = '\033[1m'  if _USE_COLOR else ''
X   = '\033[0m'  if _USE_COLOR else ''
_PAUSE_MODE = None

_real_stdout_fd = os.dup(1)


@contextmanager
def quiet():
    sys.stdout.flush()
    sys.stderr.flush()
    fd1, fd2 = os.dup(1), os.dup(2)
    nul = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(nul, 1)
        os.dup2(nul, 2)
    finally:
        os.close(nul)
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(fd1, 1)
        os.close(fd1)
        os.dup2(fd2, 2)
        os.close(fd2)


try:
    with quiet():
        from OCC.Core.StlAPI import StlAPI_Reader
        from OCC.Core.BRep import BRep_Builder
        from OCC.Core.BRepTools import breptools
        from OCC.Core.STEPControl import (
            STEPControl_Reader, STEPControl_Writer, STEPControl_AsIs)
        from OCC.Core.BRepCheck import BRepCheck_Analyzer
        from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
        from OCC.Core.BRepPrimAPI import (
            BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder,
            BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere)
        from OCC.Core.BRepBuilderAPI import (
            BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon)
        from OCC.Core.BRepGProp import brepgprop
        from OCC.Core.GProp import GProp_GProps
        from OCC.Core.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
        from OCC.Core.Interface import Interface_Static
        from OCC.Core.IGESControl import IGESControl_Reader
        from OCC.Core.IFSelect import IFSelect_RetDone
        from OCC.Core.TopoDS import (
            TopoDS_Compound, TopoDS_Shape, TopoDS_Iterator, topods)
        from OCC.Core.TopExp import TopExp_Explorer, topexp
        from OCC.Core.TopTools import TopTools_IndexedMapOfShape
        from OCC.Core.TopAbs import (
            TopAbs_FACE, TopAbs_EDGE, TopAbs_SHELL, TopAbs_SOLID,
            TopAbs_COMPOUND, TopAbs_VERTEX, TopAbs_WIRE)
        from OCC.Core.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
except Exception as e:
    print(f"\n  {R}[ERROR]{X} Failed to load OpenCASCADE: {e}\n")
    traceback.print_exc()
    if sys.stdin.isatty():
        input("\n  Press Enter to exit...")
    sys.exit(1)


_STL_TRI = struct.Struct("<12fH")
_IGES_EXTS = {IGES_FILE_EXTENSION, ".iges"}
_SUPPORTED_EXTS = {STL_FILE_EXTENSION, THREE_MF_FILE_EXTENSION, OBJ_FILE_EXTENSION, AMF_FILE_EXTENSION, *_IGES_EXTS}

_BOX_CONTENT = 72
_BOX_LABEL   = 13
_BOX_STATUS  = 7
_BOX_TIME    = 8
_BOX_DETAIL  = _BOX_CONTENT - _BOX_LABEL - _BOX_STATUS - _BOX_TIME


def _pause(prompt: str = "  Press Enter to exit...") -> None:
    should_pause = _PAUSE_MODE is True or (_PAUSE_MODE is None and sys.stdin.isatty())
    if should_pause:
        try:
            input(prompt)
        except EOFError:
            pass


def _mesh_to_shape_single(verts, tris):
    if len(tris) == 0:
        raise ValueError("no triangle data found")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".stl")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            buf = bytearray(80 + 4 + len(tris) * _STL_TRI.size)
            struct.pack_into("<I", buf, 80, len(tris))
            for i, t in enumerate(tris):
                v0, v1, v2 = verts[t[0]], verts[t[1]], verts[t[2]]
                _STL_TRI.pack_into(buf, 84 + i * _STL_TRI.size, 0.0, 0.0, 0.0, *v0, *v1, *v2, 0)
            f.write(buf)
        shape = TopoDS_Shape()
        with quiet():
            StlAPI_Reader().Read(shape, tmp_path.replace("\\", "/"))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return shape


def _mesh_to_shape(verts, tris):
    if not SEW_PARTS_SEPARATELY:
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
        for component_id in range(component_count):
            component_faces = faces[cluster_ids == component_id]
            used_vertices, remapped = np.unique(
                component_faces, return_inverse=True)
            component_vertices = vertices[used_vertices]
            component_faces = remapped.reshape(-1, 3)
            part = _mesh_to_shape_single(
                component_vertices.tolist(), component_faces.tolist())
            if not part.IsNull():
                builder.Add(compound, part)
        return compound
    except Exception:
        return _mesh_to_shape_single(verts, tris)


def _clean_mesh_arrays(verts, tris):
    import numpy as np
    verts = np.asarray(verts, dtype=np.float64)
    tris = np.asarray(tris, dtype=np.int32)
    if len(tris) == 0:
        return verts, tris
    if verts.ndim != 2 or verts.shape[1] != 3:
        raise ValueError("vertices must be an Nx3 array")
    if tris.ndim != 2 or tris.shape[1] != 3:
        raise ValueError("triangles must be an Nx3 array")
    if not np.isfinite(verts).all():
        raise ValueError("mesh contains non-finite vertex coordinates")
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
        area_sq = np.einsum(
            "ij,ij->i", np.cross(p1 - p0, p2 - p0),
            np.cross(p1 - p0, p2 - p0))
        new_faces = new_faces[area_sq > 0]
    if len(new_faces):
        _, first = np.unique(
            np.sort(new_faces, axis=1), axis=0, return_index=True)
        new_faces = new_faces[np.sort(first)]
    return unique_v.astype(np.float64), new_faces.astype(np.int32)


def _mesh_quality_report(verts, tris, check_self_intersection=True):
    import numpy as np
    verts, tris = _clean_mesh_arrays(verts, tris)
    if not len(tris):
        return {
            "vertices": len(verts), "triangles": 0, "boundary_edges": 0,
            "non_manifold_edges": 0, "components": 0,
            "self_intersections": None,
            "internal_self_intersections": None,
            "cross_component_intersections": None,
            "watertight": False,
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
    signed_face_volumes = np.einsum(
        "ij,ij->i",
        verts[tris[:, 0]],
        np.cross(verts[tris[:, 1]], verts[tris[:, 2]]),
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
        cluster_ids, _, _ = mesh.cluster_connected_triangles()
        cluster_ids = np.asarray(cluster_ids)
        components = int(cluster_ids.max()) + 1 if len(cluster_ids) else 0
        volume = sum(
            abs(float(signed_face_volumes[cluster_ids == component].sum()))
            for component in range(components)
        )
        if check_self_intersection:
            intersection_pairs = np.asarray(
                mesh.get_self_intersecting_triangles(), dtype=np.int64)
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
    except Exception:
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
        "watertight": boundary_edges == 0 and non_manifold_edges == 0,
        "volume": volume,
        "dimensions": dimensions,
        "diagonal": diagonal,
        "median_edge": float(np.median(edge_lengths)) if len(edge_lengths) else 0.0,
    }


def _effective_tolerance(verts, requested):
    if not USE_SCALE_AWARE_SEWING_TOLERANCE or verts is None or not len(verts):
        return requested
    import numpy as np
    dimensions = np.ptp(np.asarray(verts, dtype=np.float64), axis=0)
    diagonal = float(np.linalg.norm(dimensions))
    if diagonal <= 0:
        return requested
    adaptive = max(diagonal * SCALE_AWARE_SEWING_TOLERANCE_RATIO, 1e-9)
    return min(float(requested), adaptive)


def _should_check_self_intersections(tris):
    return (
        CHECK_SELF_INTERSECTIONS
        and (
            SELF_INTERSECTION_CHECK_MAX_TRIANGLES == 0
            or len(tris) <= SELF_INTERSECTION_CHECK_MAX_TRIANGLES
        )
    )


def _repair_mesh_arrays(verts, tris):
    import numpy as np
    verts, tris = _clean_mesh_arrays(verts, tris)
    if not REPAIR_MESH_BEFORE_CONVERSION:
        return verts, tris
    try:
        import open3d as o3d
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(verts)
        o3d_mesh.triangles = o3d.utility.Vector3iVector(tris)
        dimensions = np.ptp(verts, axis=0) if len(verts) else np.zeros(3)
        diagonal = float(np.linalg.norm(dimensions))
        merge_tolerance = (
            VERTEX_MERGE_DISTANCE
            if VERTEX_MERGE_DISTANCE > 0
            else max(diagonal * 1e-9, 1e-12)
        )
        o3d_mesh.merge_close_vertices(merge_tolerance)
        o3d_mesh.remove_duplicated_vertices()
        o3d_mesh.remove_duplicated_triangles()
        o3d_mesh.remove_degenerate_triangles()
        o3d_mesh.remove_unreferenced_vertices()
        if REMOVE_NON_MANIFOLD_TRIANGLES:
            o3d_mesh.remove_non_manifold_edges()
        if FIX_TRIANGLE_ORIENTATION and o3d_mesh.is_orientable():
            o3d_mesh.orient_triangles()
        verts = np.asarray(o3d_mesh.vertices, dtype=np.float64)
        tris = np.asarray(o3d_mesh.triangles, dtype=np.int32)
    except Exception as exc:
        raise RuntimeError(f"Open3D mesh repair failed: {exc}") from exc

    if FILL_SMALL_MESH_HOLES:
        try:
            import trimesh
            mesh = trimesh.Trimesh(vertices=verts, faces=tris, process=False)
            trimesh.repair.fill_holes(mesh)
            if FIX_TRIANGLE_ORIENTATION:
                trimesh.repair.fix_normals(mesh, multibody=True)
            verts = np.asarray(mesh.vertices, dtype=np.float64)
            tris = np.asarray(mesh.faces, dtype=np.int32)
        except Exception as exc:
            raise RuntimeError(f"hole filling failed: {exc}") from exc
    return _clean_mesh_arrays(verts, tris)


def _fit_sphere(verts, tris, relative_tolerance):
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    matrix = np.column_stack((2.0 * points, np.ones(len(points))))
    rhs = np.einsum("ij,ij->i", points, points)
    solution, _, _, _ = np.linalg.lstsq(matrix, rhs, rcond=None)
    center = solution[:3]
    radius_sq = solution[3] + float(np.dot(center, center))
    if radius_sq <= 0:
        return None
    radius = math.sqrt(radius_sq)
    distances = np.linalg.norm(points - center, axis=1)
    error = float(np.max(np.abs(distances - radius)))
    if error > max(radius * relative_tolerance, 1e-9):
        return None
    centroids, normals = _face_geometry(points, tris)
    radial = centroids - center
    radial_length = np.linalg.norm(radial, axis=1)
    valid = radial_length > 1e-15
    alignment = np.abs(np.einsum(
        "ij,ij->i",
        normals[valid],
        radial[valid] / radial_length[valid, None],
    ))
    if not len(alignment) or float(np.quantile(alignment, 0.1)) < 0.98:
        return None
    return center, radius, error


def _face_geometry(verts, tris):
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    faces = np.asarray(tris, dtype=np.int32)
    p0, p1, p2 = points[faces[:, 0]], points[faces[:, 1]], points[faces[:, 2]]
    normals = np.cross(p1 - p0, p2 - p0)
    lengths = np.linalg.norm(normals, axis=1)
    good = lengths > 1e-15
    normals[good] /= lengths[good, None]
    return (p0 + p1 + p2) / 3.0, normals


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
    boundary_edges = {
        edge for edge, count in edge_counts.items() if count == 1
    }
    if not boundary_edges:
        return []

    adjacency = {}
    for first, second in boundary_edges:
        adjacency.setdefault(first, []).append(second)
        adjacency.setdefault(second, []).append(first)
    if any(len(neighbors) != 2 for neighbors in adjacency.values()):
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
            candidates = [
                neighbor for neighbor in adjacency[current]
                if neighbor != previous
            ]
            if len(candidates) != 1:
                return []
            next_vertex = candidates[0]
            edge = (min(current, next_vertex), max(current, next_vertex))
            if next_vertex != start and edge not in unused:
                return []
            unused.discard(edge)
            previous, current = current, next_vertex
            if len(cycle) > len(boundary_edges):
                return []
        if len(cycle) < 3:
            return []
        cycles.append(cycle)
    return cycles


def _profile_basis(axis):
    import numpy as np
    reference = np.array(
        [1.0, 0.0, 0.0]
        if abs(float(axis[0])) < 0.9
        else [0.0, 1.0, 0.0],
        dtype=np.float64,
    )
    first = np.cross(reference, axis)
    first /= np.linalg.norm(first)
    second = np.cross(axis, first)
    second /= np.linalg.norm(second)
    return first, second


def _profile_segments(points_2d, cycles):
    return [
        (points_2d[cycle[index]], points_2d[cycle[(index + 1) % len(cycle)]])
        for cycle in cycles
        for index in range(len(cycle))
    ]


def _point_to_profile_distance(point, segments):
    import numpy as np
    best = float("inf")
    for start, end in segments:
        direction = end - start
        length_sq = float(np.dot(direction, direction))
        if length_sq <= 1e-30:
            distance = float(np.linalg.norm(point - start))
        else:
            fraction = min(
                1.0,
                max(0.0, float(np.dot(point - start, direction)) / length_sq),
            )
            distance = float(np.linalg.norm(point - (start + fraction * direction)))
        best = min(best, distance)
    return best


def _profile_area(points_2d, cycle):
    import numpy as np
    values = points_2d[cycle]
    return 0.5 * float(
        (values[:, 0] * np.roll(values[:, 1], -1)).sum()
        - (values[:, 1] * np.roll(values[:, 0], -1)).sum()
    )


def _point_in_profile(point, polygon):
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            crossing = (
                (previous[0] - current[0])
                * (point[1] - current[1])
                / (previous[1] - current[1])
                + current[0]
            )
            if point[0] < crossing:
                inside = not inside
        previous = current
    return inside


def _shape_volume(shape) -> float:
    properties = GProp_GProps()
    brepgprop.VolumeProperties(shape, properties)
    return abs(float(properties.Mass()))


def _reconstruct_experimental_extrusion(verts, tris, report):
    if not EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION:
        return None

    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    faces = np.asarray(tris, dtype=np.int32)
    if len(points) < 4 or len(faces) < 4:
        return None

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
    direction_tolerance = max(
        1e-6, EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO * 2.0)
    for normal, weight in zip(normals, weights):
        canonical = normal.copy()
        dominant = int(np.argmax(np.abs(canonical)))
        if canonical[dominant] < 0:
            canonical = -canonical
        matched = False
        for cluster in clusters:
            alignment = float(np.dot(cluster["axis"], canonical))
            if alignment >= 1.0 - direction_tolerance:
                combined = (
                    cluster["axis"] * cluster["weight"]
                    + canonical * float(weight)
                )
                cluster["weight"] += float(weight)
                cluster["axis"] = combined / np.linalg.norm(combined)
                matched = True
                break
        if not matched:
            clusters.append({"axis": canonical, "weight": float(weight)})

    diagonal = max(float(np.linalg.norm(np.ptp(points, axis=0))), 1e-12)
    distance_tolerance = max(
        diagonal * EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO, 1e-9)
    mesh_volume = float(report["volume"])
    if mesh_volume <= 1e-15:
        return None

    for cluster in sorted(
        clusters, key=lambda item: item["weight"], reverse=True
    )[:12]:
        axis = cluster["axis"]
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
        if np.any(
            np.abs(_face_geometry(points, faces[side_faces])[1] @ axis)
            > max(0.02, direction_tolerance * 10.0)
        ):
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
            max(
                _point_to_profile_distance(points_2d[index], top_segments)
                for cycle in bottom_cycles for index in cycle
            ),
            max(
                _point_to_profile_distance(points_2d[index], bottom_segments)
                for cycle in top_cycles for index in cycle
            ),
        )
        if profile_error > distance_tolerance:
            continue
        middle_vertices = ~(bottom_vertices | top_vertices)
        if np.any(middle_vertices):
            side_error = max(
                _point_to_profile_distance(point, bottom_segments)
                for point in points_2d[middle_vertices]
            )
            if side_error > distance_tolerance:
                continue

        areas = [
            _profile_area(points_2d, cycle) for cycle in bottom_cycles
        ]
        outer_index = int(np.argmax(np.abs(areas)))
        if abs(areas[outer_index]) <= distance_tolerance ** 2:
            continue
        outer_polygon = points_2d[bottom_cycles[outer_index]]
        if any(
            not _point_in_profile(points_2d[cycle[0]], outer_polygon)
            for index, cycle in enumerate(bottom_cycles)
            if index != outer_index
        ):
            continue

        ordered_cycles = [bottom_cycles[outer_index]] + [
            cycle for index, cycle in enumerate(bottom_cycles)
            if index != outer_index
        ]
        ordered_areas = [areas[outer_index]] + [
            area for index, area in enumerate(areas) if index != outer_index
        ]
        wires = []
        wire_failed = False
        for index, (cycle, area) in enumerate(zip(ordered_cycles, ordered_areas)):
            desired_positive = index == 0
            if (area > 0) != desired_positive:
                cycle = list(reversed(cycle))
            polygon = BRepBuilderAPI_MakePolygon()
            for vertex_index in cycle:
                polygon.Add(gp_Pnt(*map(float, points[vertex_index])))
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
        prism = BRepPrimAPI_MakePrism(
            face_builder.Face(),
            gp_Vec(*map(float, axis * height)),
            True,
        ).Shape()
        valid, _ = _validate_occ_shape(prism, require_solid=True)
        if not valid or _count_topo(prism, TopAbs_SOLID) != 1:
            continue
        volume_change = abs(_shape_volume(prism) - mesh_volume) / mesh_volume * 100.0
        if volume_change > EXPERIMENTAL_PARAMETRIC_MAX_VOLUME_CHANGE_PERCENT:
            continue
        if _count_topo(prism, TopAbs_FACE) >= len(faces):
            continue
        return prism
    return None


def _fit_revolved_primitive(verts, tris, relative_tolerance, cone=False):
    import numpy as np
    points = np.asarray(verts, dtype=np.float64)
    center = points.mean(axis=0)
    _, axes = np.linalg.eigh(np.cov((points - center).T))
    centroids, normals = _face_geometry(points, tris)
    diagonal = max(float(np.linalg.norm(np.ptp(points, axis=0))), 1e-12)
    tolerance = max(diagonal * relative_tolerance, 1e-9)
    best = None

    for axis in axes.T:
        axis = axis / np.linalg.norm(axis)
        face_axial = np.abs(normals @ axis)
        side_mask = face_axial < 0.95
        cap_mask = face_axial >= 0.95
        if np.count_nonzero(side_mask) < 4 or np.count_nonzero(cap_mask) < 2:
            continue
        side_faces = np.asarray(tris, dtype=np.int32)[side_mask]
        side_indices = np.unique(side_faces)
        side_points = points[side_indices]
        side_delta = side_points - center
        side_z = side_delta @ axis
        side_radial_vec = side_delta - np.outer(side_z, axis)
        side_radius = np.linalg.norm(side_radial_vec, axis=1)
        all_z = (points - center) @ axis
        z_min, z_max = float(all_z.min()), float(all_z.max())
        height = z_max - z_min
        if height <= tolerance:
            continue

        side_centers = centroids[side_mask] - center
        center_z = side_centers @ axis
        center_radial = side_centers - np.outer(center_z, axis)
        center_radius = np.linalg.norm(center_radial, axis=1)
        valid_center = center_radius > 1e-15
        projected_normals = normals[side_mask] - np.outer(
            normals[side_mask] @ axis, axis)
        projected_length = np.linalg.norm(projected_normals, axis=1)
        valid_alignment = valid_center & (projected_length > 1e-15)
        if not np.any(valid_alignment):
            continue
        alignment = np.abs(np.einsum(
            "ij,ij->i",
            center_radial[valid_alignment] / center_radius[valid_alignment, None],
            projected_normals[valid_alignment]
            / projected_length[valid_alignment, None],
        ))
        if float(np.quantile(alignment, 0.1)) < 0.98:
            continue

        if cone:
            design = np.column_stack((side_z, np.ones(len(side_z))))
            slope, intercept = np.linalg.lstsq(
                design, side_radius, rcond=None)[0]
            predicted = slope * side_z + intercept
            residual = float(np.max(np.abs(side_radius - predicted)))
            r1 = float(slope * z_min + intercept)
            r2 = float(slope * z_max + intercept)
            if abs(r1 - r2) <= tolerance or min(r1, r2) < -tolerance:
                continue
            r1, r2 = max(0.0, r1), max(0.0, r2)
        else:
            radius = float(np.mean(side_radius))
            residual = float(np.max(np.abs(side_radius - radius)))
            r1 = r2 = radius
        if residual > tolerance or max(r1, r2) <= tolerance:
            continue

        cap_z = (centroids[cap_mask] - center) @ axis
        cap_distance = np.minimum(np.abs(cap_z - z_min), np.abs(cap_z - z_max))
        if float(np.max(cap_distance)) > tolerance:
            continue
        candidate = {
            "axis": axis, "origin": center + axis * z_min,
            "height": height, "r1": r1, "r2": r2,
            "error": residual,
        }
        if best is None or candidate["error"] < best["error"]:
            best = candidate
    return best


def _reconstruct_analytic_shape(verts, tris):
    if not (
        RECONSTRUCT_ANALYTIC_PRIMITIVES
        or EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION
    ):
        return None, None
    report = _mesh_quality_report(
        verts, tris, check_self_intersection=False)
    if not report["watertight"] or report["components"] != 1:
        return None, None

    if (
        RECONSTRUCT_ANALYTIC_PRIMITIVES
        and len(tris) >= ANALYTIC_PRIMITIVE_MIN_TRIANGLES
    ):
        sphere = _fit_sphere(verts, tris, ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO)
        if sphere is not None:
            center, radius, _ = sphere
            shape = BRepPrimAPI_MakeSphere(
                gp_Pnt(*map(float, center)), float(radius)).Shape()
            valid, _ = _validate_occ_shape(shape, require_solid=True)
            if valid:
                return shape, "sphere"

        cylinder = _fit_revolved_primitive(
            verts, tris, ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO, cone=False)
        if cylinder is not None:
            axis = cylinder["axis"]
            ax2 = gp_Ax2(
                gp_Pnt(*map(float, cylinder["origin"])),
                gp_Dir(*map(float, axis)),
            )
            shape = BRepPrimAPI_MakeCylinder(
                ax2, cylinder["r1"], cylinder["height"]).Shape()
            valid, _ = _validate_occ_shape(shape, require_solid=True)
            if valid:
                return shape, "cylinder"

        cone = _fit_revolved_primitive(
            verts, tris, ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO, cone=True)
        if cone is not None:
            axis = cone["axis"]
            ax2 = gp_Ax2(
                gp_Pnt(*map(float, cone["origin"])),
                gp_Dir(*map(float, axis)),
            )
            shape = BRepPrimAPI_MakeCone(
                ax2, cone["r1"], cone["r2"], cone["height"]).Shape()
            valid, _ = _validate_occ_shape(shape, require_solid=True)
            if valid:
                return shape, "cone"

    try:
        extrusion = _reconstruct_experimental_extrusion(verts, tris, report)
    except Exception:
        extrusion = None
    if extrusion is not None:
        return extrusion, "linear extrusion"
    return None, None


def _find_3mf_model(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    rels_path = "_rels/.rels"
    if rels_path in names:
        with zf.open(rels_path) as f:
            root = ET.parse(f).getroot()
        rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        for rel in root.findall(f"{{{rels_ns}}}Relationship"):
            if "3dmanufacturing" in rel.get("Type", ""):
                target = rel.get("Target", "").lstrip("/")
                if target:
                    return target
    for name in names:
        if name.endswith(".model"):
            return name
    raise ValueError("could not find 3D model document in 3MF archive")


def _read_iges_shape(path: Path):
    reader = IGESControl_Reader()
    with quiet():
        status = reader.ReadFile(path.as_posix())
        if status == IFSelect_RetDone:
            reader.TransferRoots()
    if status != IFSelect_RetDone:
        raise ValueError(f"IGES reader failed with status {status}")
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError("IGES file produced an empty shape")
    return shape, None, None


def _stl_tri_count(path: Path):
    try:
        with open(path, "rb") as f:
            header = f.read(84)
        if len(header) < 84:
            return None
        n = struct.unpack_from("<I", header, 80)[0]
        binary_size = 84 + n * 50
        if (
            binary_size <= path.stat().st_size
            and (n > 0 or binary_size == path.stat().st_size)
        ):
            return n
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.lstrip().startswith("facet normal"))
    except Exception:
        return None


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


def _has_candidate_hole_wires(shape) -> bool:
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
                    and _count_topo(wire, TopAbs_VERTEX)
                    >= ANALYTIC_HOLE_MIN_SIDES
                ):
                    return True
                wires.Next()
        faces.Next()
    return False


def _free_topology_counts(shape) -> dict:
    counts = {
        "shell": 0,
        "face": 0,
        "wire": 0,
        "edge": 0,
        "vertex": 0,
    }

    def visit(part):
        shape_type = part.ShapeType()
        if shape_type == TopAbs_SOLID:
            return
        if shape_type == TopAbs_SHELL:
            counts["shell"] += 1
            return
        if shape_type == TopAbs_FACE:
            counts["face"] += 1
            return
        if shape_type == TopAbs_WIRE:
            counts["wire"] += 1
            return
        if shape_type == TopAbs_EDGE:
            counts["edge"] += 1
            return
        if shape_type == TopAbs_VERTEX:
            counts["vertex"] += 1
            return
        iterator = TopoDS_Iterator(part)
        while iterator.More():
            visit(iterator.Value())
            iterator.Next()

    visit(shape)
    return counts


def _count_free_shells(shape) -> int:
    counts = _free_topology_counts(shape)
    return counts["shell"] + counts["face"]


def _validate_occ_shape(shape, require_solid=False):
    if shape is None or shape.IsNull():
        return False, "shape is null"
    if not BRepCheck_Analyzer(shape).IsValid():
        return False, "OpenCASCADE reports invalid topology"
    solids = _count_topo(shape, TopAbs_SOLID)
    if require_solid and solids == 0:
        return False, "shape contains no valid solid"
    free_counts = _free_topology_counts(shape)
    if require_solid and any(free_counts.values()):
        details = []
        for name, count in free_counts.items():
            if count:
                details.append(
                    f"{count} {name}{'' if count == 1 else 's'}")
        return False, (
            "shape contains topology outside valid solids: "
            + ", ".join(details))
    return True, None


def _first_error_line(stderr_bytes):
    if not stderr_bytes:
        return None
    text = stderr_bytes.decode('utf-8', errors='replace')
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:200] if lines else None


def _run_brep_subprocess(shape, make_script, timeout=300, fallback=None):
    fd_in,  in_path  = tempfile.mkstemp(suffix='.brep')
    fd_out, out_path = tempfile.mkstemp(suffix='.brep')
    os.close(fd_in)
    os.close(fd_out)
    in_fwd  = in_path.replace('\\', '/')
    out_fwd = out_path.replace('\\', '/')
    err_msg = None
    try:
        breptools.Write(shape, in_fwd)
        proc = subprocess.run(
            [sys.executable, '-c', make_script(in_fwd, out_fwd)],
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode == 0 and os.path.getsize(out_path) > 0:
            result = TopoDS_Shape()
            breptools.Read(result, out_fwd, BRep_Builder())
            if not result.IsNull():
                return result, None
            err_msg = "subprocess produced a null shape"
        else:
            err_msg = _first_error_line(proc.stderr) or f"subprocess exited with code {proc.returncode}"
    except subprocess.TimeoutExpired:
        err_msg = f"subprocess timed out after {timeout}s"
    except Exception as e:
        s = str(e)
        err_msg = s.splitlines()[0][:200] if s else type(e).__name__
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass
    return (shape if fallback is None else fallback), err_msg


def _parallel_fix(shape):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools;"
            "from OCC.Core.BRep import BRep_Builder;"
            "from OCC.Core.TopoDS import TopoDS_Shape;"
            "from OCC.Core.ShapeFix import ShapeFix_Shape;"
            "b=BRep_Builder();s=TopoDS_Shape();"
            f"breptools.Read(s,{in_fwd!r},b);"
            "f=ShapeFix_Shape(s);f.Perform();r=f.Shape();"
            f"breptools.Write(r if not r.IsNull() else s,{out_fwd!r})"
        )
    return _run_brep_subprocess(shape, make_script)


def _parallel_refine(shape, tolerance):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools\n"
            "from OCC.Core.BRep import BRep_Builder\n"
            "from OCC.Core.TopoDS import TopoDS_Shape,TopoDS_Compound,TopoDS_Iterator\n"
            "from OCC.Core.ShapeUpgrade import ShapeUpgrade_UnifySameDomain\n"
            "from OCC.Core.ShapeFix import ShapeFix_Shape\n"
            "from OCC.Core.BRepCheck import BRepCheck_Analyzer\n"
            "from OCC.Core.TopExp import TopExp_Explorer\n"
            "from OCC.Core.TopAbs import TopAbs_FACE,TopAbs_SOLID,TopAbs_SHELL\n"
            "b=BRep_Builder();s=TopoDS_Shape()\n"
            f"breptools.Read(s,{in_fwd!r},b)\n"
            "def count(sh,kind):\n"
            "  e=TopExp_Explorer(sh,kind);n=0\n"
            "  while e.More():n+=1;e.Next()\n"
            "  return n\n"
            "def collect(sh,out):\n"
            "  if sh.ShapeType() in (TopAbs_SOLID,TopAbs_SHELL):out.append(sh);return\n"
            "  it=TopoDS_Iterator(sh);had=False\n"
            "  while it.More():had=True;collect(it.Value(),out);it.Next()\n"
            "  if not had:out.append(sh)\n"
            "def refine(part):\n"
            "  before=count(part,TopAbs_FACE);solid_count=count(part,TopAbs_SOLID)\n"
            "  for angular in (" + repr(float(PLANAR_MERGE_ANGLE_RADIANS)) + ",0.0):\n"
            "    u=ShapeUpgrade_UnifySameDomain(part,True,True,True)\n"
            f"    u.SetLinearTolerance({tolerance})\n"
            "    u.SetAngularTolerance(angular);u.Build();candidate=u.Shape()\n"
            "    if candidate.IsNull():continue\n"
            "    fix=ShapeFix_Shape(candidate);fix.Perform();candidate=fix.Shape()\n"
            "    if candidate.IsNull() or not BRepCheck_Analyzer(candidate).IsValid():continue\n"
            "    if solid_count and count(candidate,TopAbs_SOLID)!=solid_count:continue\n"
            "    if count(candidate,TopAbs_FACE)<=before:return candidate\n"
            "  return part\n"
            "parts=[];collect(s,parts);results=[refine(part) for part in parts]\n"
            "if len(results)==1:r=results[0]\n"
            "else:\n"
            "  r=TopoDS_Compound();b.MakeCompound(r)\n"
            "  for part in results:b.Add(r,part)\n"
            f"breptools.Write(r,{out_fwd!r})\n"
        )
    return _run_brep_subprocess(shape, make_script)


def _parallel_reconstruct_holes(shape, tolerance):
    def make_script(in_fwd, out_fwd):
        return (
            "import math\n"
            "import numpy as np\n"
            "from OCC.Core.Bnd import Bnd_Box\n"
            "from OCC.Core.BRepTools import breptools\n"
            "from OCC.Core.BRep import BRep_Builder,BRep_Tool\n"
            "from OCC.Core.BRepAdaptor import BRepAdaptor_Surface\n"
            "from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Cut\n"
            "from OCC.Core.BRepBndLib import brepbndlib\n"
            "from OCC.Core.BRepClass3d import BRepClass3d_SolidClassifier\n"
            "from OCC.Core.BRepCheck import BRepCheck_Analyzer\n"
            "from OCC.Core.BRepGProp import brepgprop\n"
            "from OCC.Core.BRepPrimAPI import BRepPrimAPI_MakeCylinder\n"
            "from OCC.Core.GProp import GProp_GProps\n"
            "from OCC.Core.GeomAbs import GeomAbs_Cylinder,GeomAbs_Plane\n"
            "from OCC.Core.ShapeFix import ShapeFix_Shape\n"
            "from OCC.Core.TopExp import TopExp_Explorer\n"
            "from OCC.Core.TopAbs import TopAbs_FACE,TopAbs_IN,TopAbs_OUT,TopAbs_SOLID,TopAbs_SHELL,TopAbs_VERTEX,TopAbs_WIRE\n"
            "from OCC.Core.TopoDS import TopoDS_Shape,TopoDS_Compound,TopoDS_Iterator,topods\n"
            "from OCC.Core.gp import gp_Ax2,gp_Dir,gp_Pnt\n"
            "b=BRep_Builder();s=TopoDS_Shape()\n"
            f"breptools.Read(s,{in_fwd!r},b)\n"
            "def count(sh,kind):\n"
            "  e=TopExp_Explorer(sh,kind);n=0\n"
            "  while e.More():n+=1;e.Next()\n"
            "  return n\n"
            "def volume(sh):\n"
            "  p=GProp_GProps();brepgprop.VolumeProperties(sh,p);return abs(p.Mass())\n"
            "def surface_count(sh,kind):\n"
            "  e=TopExp_Explorer(sh,TopAbs_FACE);n=0\n"
            "  while e.More():\n"
            "    if BRepAdaptor_Surface(topods.Face(e.Current()),True).GetType()==kind:n+=1\n"
            "    e.Next()\n"
            "  return n\n"
            "def point_state(classifier,point):\n"
            f"  classifier.Perform(gp_Pnt(*point),max({tolerance}*0.01,1e-8))\n"
            "  return classifier.State()\n"
            "def collect(sh,out):\n"
            "  if sh.ShapeType() in (TopAbs_SOLID,TopAbs_SHELL):out.append(sh);return\n"
            "  it=TopoDS_Iterator(sh);had=False\n"
            "  while it.More():had=True;collect(it.Value(),out);it.Next()\n"
            "  if not had:out.append(sh)\n"
            "def wire_points(wire):\n"
            "  values=[];e=TopExp_Explorer(wire,TopAbs_VERTEX)\n"
            "  while e.More():\n"
            "    p=BRep_Tool.Pnt(topods.Vertex(e.Current()));point=np.array((p.X(),p.Y(),p.Z()),dtype=float)\n"
            f"    if not any(np.linalg.norm(point-old)<=max({tolerance}*0.01,1e-9) for old in values):values.append(point)\n"
            "    e.Next()\n"
            "  return np.asarray(values,dtype=float)\n"
            "def fit_circle(points,normal):\n"
            "  normal=np.asarray(normal,dtype=float);normal/=np.linalg.norm(normal)\n"
            "  ref=np.array((1.0,0.0,0.0)) if abs(normal[0])<0.8 else np.array((0.0,1.0,0.0))\n"
            "  u=np.cross(normal,ref);u/=np.linalg.norm(u);v=np.cross(normal,u)\n"
            "  origin=points.mean(axis=0);delta=points-origin;x=delta@u;y=delta@v\n"
            "  matrix=np.column_stack((2.0*x,2.0*y,np.ones(len(points))));rhs=x*x+y*y\n"
            "  cx,cy,c=np.linalg.lstsq(matrix,rhs,rcond=None)[0]\n"
            "  radius_sq=c+cx*cx+cy*cy\n"
            "  if radius_sq<=0:return None\n"
            "  center=origin+cx*u+cy*v;radius=math.sqrt(radius_sq)\n"
            "  radial=np.sqrt((x-cx)**2+(y-cy)**2)\n"
            "  error=float(np.max(np.abs(radial-radius))/radius)\n"
            "  coords=sorted((float(px),float(py)) for px,py in zip(x,y))\n"
            "  def cross(o,a,b):return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])\n"
            "  lower=[]\n"
            "  for point in coords:\n"
            "    while len(lower)>=2 and cross(lower[-2],lower[-1],point)<=0:lower.pop()\n"
            "    lower.append(point)\n"
            "  upper=[]\n"
            "  for point in reversed(coords):\n"
            "    while len(upper)>=2 and cross(upper[-2],upper[-1],point)<=0:upper.pop()\n"
            "    upper.append(point)\n"
            "  hull=lower[:-1]+upper[:-1]\n"
            "  if len(hull)!=len(coords):return None\n"
            "  area=abs(sum(hull[i][0]*hull[(i+1)%len(hull)][1]-hull[(i+1)%len(hull)][0]*hull[i][1] for i in range(len(hull))))*0.5\n"
            "  if area<=0:return None\n"
            "  return center,radius,float(np.max(radial)),error,normal,area\n"
            "def openings(solid):\n"
            "  result=[];faces=TopExp_Explorer(solid,TopAbs_FACE)\n"
            "  while faces.More():\n"
            "    face=topods.Face(faces.Current());surface=BRepAdaptor_Surface(face,True)\n"
            "    if surface.GetType()==GeomAbs_Plane:\n"
            "      d=surface.Plane().Axis().Direction();normal=(d.X(),d.Y(),d.Z())\n"
            "      outer=breptools.OuterWire(face);wires=TopExp_Explorer(face,TopAbs_WIRE)\n"
            "      while wires.More():\n"
            "        wire=topods.Wire(wires.Current())\n"
            "        if not wire.IsSame(outer):\n"
            "          points=wire_points(wire)\n"
            f"          if len(points)>={ANALYTIC_HOLE_MIN_SIDES}:\n"
            "            fitted=fit_circle(points,normal)\n"
            "            if fitted is not None:\n"
            "              center,radius,max_radius,error,normal,area=fitted\n"
            f"              if error<={ANALYTIC_HOLE_FIT_ERROR_RATIO}:result.append((center,radius,max_radius,error,normal,len(points),area))\n"
            "        wires.Next()\n"
            "    faces.Next()\n"
            "  return result\n"
            "def reconstruct(solid):\n"
            "  found=openings(solid)\n"
            "  if not found:return solid\n"
            "  box=Bnd_Box();brepbndlib.Add(solid,box);bounds=box.Get()\n"
            "  diagonal=float(np.linalg.norm(np.asarray(bounds[3:])-np.asarray(bounds[:3])))\n"
            f"  axis_cos=math.cos({ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS})\n"
            "  options=[]\n"
            f"  if {RECONSTRUCT_ANALYTIC_THROUGH_HOLES!r}:\n"
            "    for left in range(len(found)):\n"
            "      for right in range(left+1,len(found)):\n"
            "        c1,r1,m1,e1,n1,k1,a1=found[left];c2,r2,m2,e2,n2,k2,a2=found[right]\n"
            "        delta=c2-c1;length=float(np.linalg.norm(delta))\n"
            f"        if length<=max({tolerance},1e-9):continue\n"
            "        axis=delta/length;n1=n1/np.linalg.norm(n1);n2=n2/np.linalg.norm(n2)\n"
            "        radius_error=abs(r1-r2)/max(r1,r2)\n"
            "        area_error=abs(a1-a2)/max(a1,a2)\n"
            "        lateral=float(np.linalg.norm(delta-n1*np.dot(delta,n1)))\n"
            f"        lateral_limit=max({tolerance}*5.0,max(r1,r2)*{ANALYTIC_HOLE_FIT_ERROR_RATIO})\n"
            f"        if radius_error>{ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO}:continue\n"
            f"        if area_error>max({ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO}*2.0,{ANALYTIC_HOLE_FIT_ERROR_RATIO}*2.0):continue\n"
            "        if abs(float(np.dot(n1,n2)))<axis_cos or abs(float(np.dot(n1,axis)))<axis_cos:continue\n"
            "        if lateral>lateral_limit:continue\n"
            "        options.append((length,left,right,axis))\n"
            "  options.sort(key=lambda item:item[0]);used=set();current=solid\n"
            "  classifier=BRepClass3d_SolidClassifier(current)\n"
            "  for length,left,right,axis in options:\n"
            "    if left in used or right in used:continue\n"
            "    c1,r1,m1,e1,n1,k1,a1=found[left];c2,r2,m2,e2,n2,k2,a2=found[right]\n"
            "    fitted_radius=(r1+r2)*0.5\n"
            f"    clearance=max(fitted_radius*1e-6,{tolerance}*0.01,1e-9)\n"
            "    cut_radius=max(m1,m2)+clearance\n"
            "    ref=np.array((1.0,0.0,0.0)) if abs(axis[0])<0.8 else np.array((0.0,1.0,0.0))\n"
            "    u=np.cross(axis,ref);u/=np.linalg.norm(u);v=np.cross(axis,u)\n"
            f"    wall_radius=max(m1,m2)+max(fitted_radius*0.05,{tolerance}*5.0,1e-6)\n"
            "    tunnel_clear=True\n"
            "    for fraction in (0.02,0.1,0.25,0.5,0.75,0.9,0.98):\n"
            "      base=c1+axis*(length*fraction)\n"
            "      if point_state(classifier,base)!=TopAbs_OUT:tunnel_clear=False;break\n"
            "      for angle in (0.0,math.pi*0.25,math.pi*0.5,math.pi*0.75,math.pi,math.pi*1.25,math.pi*1.5,math.pi*1.75):\n"
            "        radial=u*math.cos(angle)+v*math.sin(angle)\n"
            "        if point_state(classifier,base+radial*(fitted_radius*0.8))!=TopAbs_OUT or point_state(classifier,base+radial*wall_radius)!=TopAbs_IN:\n"
            "          tunnel_clear=False;break\n"
            "      if not tunnel_clear:break\n"
            "    if not tunnel_clear:continue\n"
            f"    margin=max(cut_radius*0.05,length*0.05,{tolerance}*10.0,1e-7)\n"
            "    start=c1-axis*margin\n"
            "    try:\n"
            "      tool=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start),gp_Dir(*axis)),cut_radius,length+2.0*margin).Shape()\n"
            "      before_volume=volume(current);before_faces=count(current,TopAbs_FACE);before_cylinders=surface_count(current,GeomAbs_Cylinder)\n"
            "      cut=BRepAlgoAPI_Cut(current,tool);cut.Build();candidate=cut.Shape()\n"
            "      if not cut.IsDone() or candidate.IsNull():continue\n"
            "      fixer=ShapeFix_Shape(candidate);fixer.Perform();candidate=fixer.Shape()\n"
            "      after_volume=volume(candidate);after_faces=count(candidate,TopAbs_FACE);after_cylinders=surface_count(candidate,GeomAbs_Cylinder)\n"
            "      removed_volume=before_volume-after_volume\n"
            "      opening_area=(a1+a2)*0.5\n"
            "      expected_removed=max(0.0,(math.pi*cut_radius*cut_radius-opening_area)*length)\n"
            f"      removal_slack=max(expected_removed*0.002,math.pi*cut_radius*cut_radius*length*1e-7,{tolerance}*cut_radius*length*0.01,1e-9)\n"
            "      volume_change=(before_volume-after_volume)/before_volume*100.0 if before_volume>0 else float('inf')\n"
            "      valid=(not candidate.IsNull() and BRepCheck_Analyzer(candidate).IsValid() and count(candidate,TopAbs_SOLID)==1 and after_volume>0 and after_volume<=before_volume and "
            f"removed_volume<=expected_removed+removal_slack and volume_change<={ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT} and after_faces<before_faces and after_cylinders>before_cylinders)\n"
            "      if valid:current=candidate;used.add(left);used.add(right);classifier=BRepClass3d_SolidClassifier(current)\n"
            "    except Exception:\n"
            "      continue\n"
            f"  if not {RECONSTRUCT_ANALYTIC_BLIND_HOLES!r}:return current\n"
            "  classifier=BRepClass3d_SolidClassifier(current)\n"
            "  for index,(center,radius,max_radius,error,normal,sides,area) in enumerate(found):\n"
            "    if index in used:continue\n"
            "    normal=normal/np.linalg.norm(normal)\n"
            "    ref=np.array((1.0,0.0,0.0)) if abs(normal[0])<0.8 else np.array((0.0,1.0,0.0))\n"
            "    u=np.cross(normal,ref);u/=np.linalg.norm(u);v=np.cross(normal,u)\n"
            f"    probe=max(radius*1e-3,{tolerance}*2.0,1e-6)\n"
            f"    wall_radius=max_radius+max(radius*0.05,{tolerance}*5.0,1e-6)\n"
            "    directions=[]\n"
            "    for sign in (-1.0,1.0):\n"
            "      axis=normal*sign;inside=center+axis*probe\n"
            "      if point_state(classifier,inside)!=TopAbs_OUT:continue\n"
            "      ring=[]\n"
            "      for angle in (0.0,math.pi*0.5,math.pi,math.pi*1.5):\n"
            "        radial=u*math.cos(angle)+v*math.sin(angle)\n"
            "        ring.append(point_state(classifier,inside+radial*wall_radius)==TopAbs_IN)\n"
            "      if all(ring):directions.append(axis)\n"
            "    if len(directions)!=1:continue\n"
            "    axis=directions[0]\n"
            "    step=max(radius*0.25,probe*2.0,diagonal/512.0)\n"
            "    max_depth=max(diagonal*1.05,step)\n"
            "    low=probe;high=None;depth=low+step\n"
            "    while depth<=max_depth:\n"
            "      state=point_state(classifier,center+axis*depth)\n"
            "      if state==TopAbs_IN:high=depth;break\n"
            "      if state!=TopAbs_OUT:break\n"
            "      low=depth;depth+=step\n"
            "    if high is None:continue\n"
            "    for iteration in range(28):\n"
            "      middle=(low+high)*0.5\n"
            "      if point_state(classifier,center+axis*middle)==TopAbs_IN:high=middle\n"
            "      else:low=middle\n"
            "    hole_depth=(low+high)*0.5\n"
            "    if hole_depth<=probe*4.0:continue\n"
            "    verified=True\n"
            "    for fraction in (0.02,0.1,0.25,0.5,0.75,0.9,0.98):\n"
            "      base=center+axis*(hole_depth*fraction)\n"
            "      if point_state(classifier,base)!=TopAbs_OUT:verified=False;break\n"
            "      for angle in (0.0,math.pi*0.25,math.pi*0.5,math.pi*0.75,math.pi,math.pi*1.25,math.pi*1.5,math.pi*1.75):\n"
            "        radial=u*math.cos(angle)+v*math.sin(angle)\n"
            "        if point_state(classifier,base+radial*(radius*0.8))!=TopAbs_OUT or point_state(classifier,base+radial*wall_radius)!=TopAbs_IN:\n"
            "          verified=False;break\n"
            "      if not verified:break\n"
            "    bottom_probe=max(probe,min(radius*0.01,hole_depth*0.01))\n"
            "    if verified:\n"
            "      before_bottom=center+axis*(hole_depth-bottom_probe)\n"
            "      after_bottom=center+axis*(hole_depth+bottom_probe)\n"
            "      for radial_scale in (0.0,0.45,0.8):\n"
            "        for radial in (u,v):\n"
            "          if point_state(classifier,before_bottom+radial*(radius*radial_scale))!=TopAbs_OUT or point_state(classifier,after_bottom+radial*(radius*radial_scale))!=TopAbs_IN:\n"
            "            verified=False;break\n"
            "        if not verified:break\n"
            "    if not verified:continue\n"
            f"    clearance=max(radius*1e-6,{tolerance}*0.01,1e-9)\n"
            "    cut_radius=max_radius+clearance\n"
            f"    margin=max(cut_radius*0.05,{tolerance}*10.0,1e-7)\n"
            "    end_clearance=clearance\n"
            "    start=center-axis*margin\n"
            "    try:\n"
            "      tool=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start),gp_Dir(*axis)),cut_radius,hole_depth+margin+end_clearance).Shape()\n"
            "      before_volume=volume(current);before_faces=count(current,TopAbs_FACE);before_cylinders=surface_count(current,GeomAbs_Cylinder)\n"
            "      cut=BRepAlgoAPI_Cut(current,tool);cut.Build();candidate=cut.Shape()\n"
            "      if not cut.IsDone() or candidate.IsNull():continue\n"
            "      fixer=ShapeFix_Shape(candidate);fixer.Perform();candidate=fixer.Shape()\n"
            "      after_volume=volume(candidate);after_faces=count(candidate,TopAbs_FACE);after_cylinders=surface_count(candidate,GeomAbs_Cylinder)\n"
            "      removed_volume=before_volume-after_volume\n"
            "      expected_removed=max(0.0,(math.pi*cut_radius*cut_radius-area)*hole_depth+math.pi*cut_radius*cut_radius*end_clearance)\n"
            f"      removal_slack=max(expected_removed*0.002,math.pi*cut_radius*cut_radius*hole_depth*1e-7,{tolerance}*cut_radius*hole_depth*0.01,1e-9)\n"
            "      volume_change=(before_volume-after_volume)/before_volume*100.0 if before_volume>0 else float('inf')\n"
            "      valid=(not candidate.IsNull() and BRepCheck_Analyzer(candidate).IsValid() and count(candidate,TopAbs_SOLID)==1 and after_volume>0 and after_volume<=before_volume and "
            f"abs(removed_volume-expected_removed)<=removal_slack and volume_change<={ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT} and after_faces<before_faces and after_cylinders>before_cylinders)\n"
            "      if valid:current=candidate;used.add(index);classifier=BRepClass3d_SolidClassifier(current)\n"
            "    except Exception:\n"
            "      continue\n"
            "  return current\n"
            "parts=[];collect(s,parts);results=[]\n"
            "for part in parts:\n"
            "  results.append(reconstruct(topods.Solid(part)) if part.ShapeType()==TopAbs_SOLID else part)\n"
            "if len(results)==1:r=results[0]\n"
            "else:\n"
            "  r=TopoDS_Compound();b.MakeCompound(r)\n"
            "  for part in results:b.Add(r,part)\n"
            f"breptools.Write(r,{out_fwd!r})\n"
        )
    return _run_brep_subprocess(
        shape, make_script, timeout=SEWING_TIMEOUT_SECONDS)


def _parallel_fill_brep_holes(shape, tolerance):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools\n"
            "from OCC.Core.BRep import BRep_Builder,BRep_Tool\n"
            "from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeFace,BRepBuilderAPI_Sewing\n"
            "from OCC.Core.BRepCheck import BRepCheck_Analyzer\n"
            "from OCC.Core.BRepGProp import brepgprop\n"
            "from OCC.Core.GProp import GProp_GProps\n"
            "from OCC.Core.ShapeAnalysis import ShapeAnalysis_FreeBounds\n"
            "from OCC.Core.TopExp import TopExp_Explorer\n"
            "from OCC.Core.TopAbs import TopAbs_EDGE,TopAbs_SHELL,TopAbs_WIRE\n"
            "from OCC.Core.TopoDS import TopoDS_Shape,TopoDS_Compound,topods\n"
            "b=BRep_Builder();s=TopoDS_Shape()\n"
            f"breptools.Read(s,{in_fwd!r},b)\n"
            "parts=[];e=TopExp_Explorer(s,TopAbs_SHELL)\n"
            "while e.More():\n"
            "  sh=topods.Shell(e.Current());candidate=sh\n"
            "  if not BRep_Tool.IsClosed(sh):\n"
            f"    fb=ShapeAnalysis_FreeBounds(sh,{tolerance},True,True)\n"
            "    open_exp=TopExp_Explorer(fb.GetOpenWires(),TopAbs_WIRE)\n"
            "    wire_exp=TopExp_Explorer(fb.GetClosedWires(),TopAbs_WIRE)\n"
            "    fills=[];fill_area=0.0;allowed=not open_exp.More()\n"
            "    while allowed and wire_exp.More():\n"
            "      wire=topods.Wire(wire_exp.Current());edge_exp=TopExp_Explorer(wire,TopAbs_EDGE);edge_count=0\n"
            "      while edge_exp.More():edge_count+=1;edge_exp.Next()\n"
            f"      if edge_count<3 or edge_count>{MAX_BREP_GAP_EDGE_COUNT}:allowed=False;break\n"
            "      maker=BRepBuilderAPI_MakeFace(wire,True)\n"
            "      if not maker.IsDone():allowed=False;break\n"
            "      face=maker.Face();props=GProp_GProps();brepgprop.SurfaceProperties(face,props)\n"
            "      fill_area+=props.Mass();fills.append(face);wire_exp.Next()\n"
            "    shell_props=GProp_GProps();brepgprop.SurfaceProperties(sh,shell_props)\n"
            f"    allowed=allowed and fills and shell_props.Mass()>0 and fill_area<=shell_props.Mass()*{MAX_BREP_GAP_AREA_RATIO}\n"
            "    if allowed:\n"
            "      work=TopoDS_Compound();b.MakeCompound(work);b.Add(work,sh)\n"
            "      for face in fills:b.Add(work,face)\n"
            f"      sew=BRepBuilderAPI_Sewing({tolerance});sew.Add(work);sew.Perform();closed=sew.SewedShape()\n"
            "      shell_exp=TopExp_Explorer(closed,TopAbs_SHELL);found=False;all_closed=True\n"
            "      while shell_exp.More():found=True;all_closed=all_closed and BRep_Tool.IsClosed(topods.Shell(shell_exp.Current()));shell_exp.Next()\n"
            "      if found and all_closed and BRepCheck_Analyzer(closed).IsValid():candidate=closed\n"
            "  parts.append(candidate);e.Next()\n"
            "if len(parts)==1:r=parts[0]\n"
            "elif parts:\n"
            "  r=TopoDS_Compound();b.MakeCompound(r)\n"
            "  for part in parts:b.Add(r,part)\n"
            "else:r=s\n"
            f"breptools.Write(r,{out_fwd!r})\n"
        )
    return _run_brep_subprocess(shape, make_script)


def _parallel_solidify(shape):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools\n"
            "from OCC.Core.BRep import BRep_Builder, BRep_Tool\n"
            "from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeSolid\n"
            "from OCC.Core.BRepCheck import BRepCheck_Analyzer\n"
            "from OCC.Core.TopExp import TopExp_Explorer\n"
            "from OCC.Core.TopAbs import TopAbs_SOLID, TopAbs_SHELL\n"
            "from OCC.Core.TopoDS import TopoDS_Shape, TopoDS_Compound, topods\n"
            "b=BRep_Builder();s=TopoDS_Shape()\n"
            f"breptools.Read(s,{in_fwd!r},b)\n"
            "se=TopExp_Explorer(s,TopAbs_SOLID)\n"
            "if se.More():\n"
            "  r=s\n"
            "else:\n"
            "  made=[];open_shells=[];e=TopExp_Explorer(s,TopAbs_SHELL)\n"
            "  while e.More():\n"
            "    sh=topods.Shell(e.Current())\n"
            "    if BRep_Tool.IsClosed(sh):\n"
            "      mk=BRepBuilderAPI_MakeSolid(sh)\n"
            "      sol=mk.Solid()\n"
            "      if not sol.IsNull() and BRepCheck_Analyzer(sol).IsValid(): made.append(sol)\n"
            "      else: open_shells.append(sh)\n"
            "    else: open_shells.append(sh)\n"
            "    e.Next()\n"
            "  parts=made+open_shells\n"
            "  if len(parts)==1: r=parts[0]\n"
            "  elif parts:\n"
            "    r=TopoDS_Compound();b.MakeCompound(r)\n"
            "    for p in parts: b.Add(r,p)\n"
            "  else: r=s\n"
            f"breptools.Write(r,{out_fwd!r})\n"
        )
    return _run_brep_subprocess(shape, make_script)


def _subprocess_sew(shape, tolerance):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools\n"
            "from OCC.Core.BRep import BRep_Builder\n"
            "from OCC.Core.TopoDS import TopoDS_Shape,TopoDS_Compound,TopoDS_Iterator\n"
            "from OCC.Core.TopAbs import TopAbs_COMPOUND\n"
            "from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Sewing\n"
            "b=BRep_Builder();s=TopoDS_Shape()\n"
            f"breptools.Read(s,{in_fwd!r},b)\n"
            "children=[];it=TopoDS_Iterator(s)\n"
            "while it.More():\n"
            "  children.append(it.Value());it.Next()\n"
            "parts=children if children and all(p.ShapeType()==TopAbs_COMPOUND for p in children) else [s]\n"
            "results=[]\n"
            "for part in parts:\n"
            f"  sew=BRepBuilderAPI_Sewing({tolerance})\n"
            "  sew.Add(part);sew.Perform();n=sew.SewedShape()\n"
            "  results.append(n if not n.IsNull() else part)\n"
            "if len(results)==1:r=results[0]\n"
            "else:\n"
            "  r=TopoDS_Compound();b.MakeCompound(r)\n"
            "  for part in results:b.Add(r,part)\n"
            f"breptools.Write(r,{out_fwd!r})\n"
        )
    return _run_brep_subprocess(shape, make_script, timeout=SEWING_TIMEOUT_SECONDS, fallback=TopoDS_Shape())


_step_open   = [False]
_timer_label = [""]
_timer_t0    = [0.0]
_timer_stop  = threading.Event()
_timer_th    = [None]
_TIMER_PAD   = _BOX_STATUS + _BOX_DETAIL + _BOX_TIME


def _box_top():
    print(f"  {DIM}+{'-' * (_BOX_CONTENT + 4)}+{X}")


def _box_sep():
    print(f"  {DIM}+{'-' * (_BOX_CONTENT + 4)}+{X}")


def _box_bot():
    print(f"  {DIM}+{'-' * (_BOX_CONTENT + 4)}+{X}")


def _box_row(left: str, right: str = "", lc: str = "", rc: str = "") -> None:
    max_left = _BOX_CONTENT - len(right) - (1 if right else 0)
    if len(left) > max_left:
        left = left[:max_left - 3] + "..."
    gap = _BOX_CONTENT - len(left) - len(right)
    print(f"  {DIM}|{X}  {lc}{left}{X}{' ' * gap}{rc}{right}{X}  {DIM}|{X}")


def _box_file_row(status: str, name: str, details: str = "",
                  color: str = "") -> None:
    prefix = f"OUTPUT  {status:<6}"
    width = _BOX_CONTENT - len(prefix) - len(details) - (1 if details else 0)
    _box_row(f"{prefix}{_trim(name, width)}", details, lc=color, rc=color)


def _box_input_row(progress: str, name: str, size: str) -> None:
    prefix = f"INPUT   {progress:<10}"
    width = _BOX_CONTENT - len(prefix) - len(size) - 1
    _box_row(f"{prefix}{_trim(name, width)}", size, lc=B, rc=DIM)


def _result_details(info: dict) -> str:
    parts = []
    solids = info.get("solids", 0)
    open_shells = info.get("open_shells", 0)
    if solids:
        parts.append(f"{solids:,} solid{'s' if solids != 1 else ''}")
    elif open_shells:
        parts.append(f"{open_shells:,} open shell{'s' if open_shells != 1 else ''}")
    if info.get("faces") is not None:
        parts.append(f"{info['faces']:,} faces")
    if info.get("schema"):
        parts.append(info["schema"])
    return " | ".join(parts)


def _box_success_row(name: str, info: dict, elapsed: float) -> None:
    _box_file_row(
        "OK", name, f"{info['kb']:,} KB | {_fmt_time(elapsed)}", f"{G}{B}")
    details = _result_details(info)
    if details:
        _box_row(f"DETAILS       {details}", lc=C)


def _stop_timer() -> None:
    _timer_stop.set()
    t = _timer_th[0]
    if t is not None:
        t.join(timeout=1.0)
        _timer_th[0] = None


def _step_start(label: str) -> float:
    _step_open[0]   = True
    _timer_label[0] = label.upper()
    t0 = time.perf_counter()
    _timer_t0[0]    = t0
    _timer_stop.clear()
    label = _timer_label[0]
    print(f"  {DIM}|{X}  {DIM}{label:<{_BOX_LABEL}}", end="", flush=True)
    if not sys.stdout.isatty():
        _timer_th[0] = None
        return t0

    def _tick():
        while not _timer_stop.wait(0.5):
            t_str = _fmt_time(time.perf_counter() - _timer_t0[0])
            line = f"\r  {DIM}|{X}  {DIM}{label:<{_BOX_LABEL}}{DIM}{t_str:>{_TIMER_PAD}}{X}\033[K"
            try:
                os.write(_real_stdout_fd, line.encode())
            except Exception:
                pass

    _timer_th[0] = threading.Thread(target=_tick, daemon=True)
    _timer_th[0].start()
    return t0


def _step_end(t0: float, detail: str = "") -> None:
    _stop_timer()
    _step_open[0] = False
    elapsed = time.perf_counter() - t0
    if len(detail) > _BOX_DETAIL:
        detail = detail[:_BOX_DETAIL - 3] + "..."
    time_str = _fmt_time(elapsed)
    print(
        f"\r  {DIM}|{X}  "
        f"{DIM}{_timer_label[0]:<{_BOX_LABEL}}{X}"
        f"{G}{'OK':<{_BOX_STATUS}}{X}"
        f"{C}{detail:<{_BOX_DETAIL}}{X}"
        f"{Y}{time_str:>{_BOX_TIME}}{X}"
        f"  {DIM}|{X}"
    )


def _step_fail() -> None:
    if _step_open[0]:
        _stop_timer()
        _step_open[0] = False
        print(
            f"\r  {DIM}|{X}  "
            f"{DIM}{_timer_label[0]:<{_BOX_LABEL}}{X}"
            f"{R}{'FAIL':<{_BOX_STATUS}}{'step failed':<{_BOX_DETAIL}}"
            f"{'-':>{_BOX_TIME}}{X}"
            f"  {DIM}|{X}"
        )


def _trim(name: str, width: int = NAME_TRIM_WIDTH) -> str:
    return name if len(name) <= width else name[:width - 3] + "..."


_EST_HISTORY = _DATA_DIR / "estimator.json"
_EST_MIN     = 5
_EST_POWERS  = (2, 1, 0)


def _est_load():
    if _EST_HISTORY.exists():
        try:
            data = json.loads(_EST_HISTORY.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _est_save(data):
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{_EST_HISTORY.name}.", suffix=".tmp", dir=_EST_HISTORY.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, _EST_HISTORY)
    except OSError:
        return
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _bucket_add(bucket, x, y):
    scale  = bucket.get("scale",  x) or 1.0
    XtX    = bucket.get("XtX",    [[0.0] * 3 for _ in range(3)])
    Xty    = bucket.get("Xty",    [0.0] * 3)
    sum_y2 = bucket.get("sum_y2", 0.0)
    if x > scale:
        r   = scale / x
        XtX = [[XtX[i][j] * r ** (_EST_POWERS[i] + _EST_POWERS[j])
                for j in range(3)] for i in range(3)]
        Xty = [Xty[i] * r ** _EST_POWERS[i] for i in range(3)]
        scale = x
    xn  = x / scale
    row = (xn * xn, xn, 1.0)
    for i in range(3):
        Xty[i] += row[i] * y
        for j in range(3):
            XtX[i][j] += row[i] * row[j]
    return {"scale": scale, "XtX": XtX, "Xty": Xty, "sum_y2": sum_y2 + y * y}


def _bucket_predict(bucket, x):
    XtX    = bucket.get("XtX",    [[0.0] * 3 for _ in range(3)])
    Xty    = bucket.get("Xty",    [0.0] * 3)
    sum_y2 = bucket.get("sum_y2", 0.0)
    scale  = bucket.get("scale",  1.0) or 1.0
    n = int(round(XtX[2][2]))
    if n < _EST_MIN:
        return None, n, 0.0
    cs = _solve3(XtX, Xty)
    if cs is None:
        return None, n, 0.0
    a2, a1, a0 = cs
    xn   = x / scale
    pred   = max(0.5, a2 * xn * xn + a1 * xn + a0)
    sum_y  = Xty[2]
    ss_tot = sum_y2 - sum_y * sum_y / n
    ss_res = (sum_y2
              - 2 * (a2 * Xty[0] + a1 * Xty[1] + a0 * Xty[2])
              + a2 * a2 * XtX[0][0] + a1 * a1 * XtX[1][1] + a0 * a0 * XtX[2][2]
              + 2 * a2 * a1 * XtX[0][1] + 2 * a2 * a0 * XtX[0][2] + 2 * a1 * a0 * XtX[1][2])
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-10 else 1.0
    return pred, n, r2


def _solve3(A, b):
    M = [A[i][:] + [b[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            return None
        M[col], M[pivot] = M[pivot], M[col]
        inv = 1.0 / M[col][col]
        M[col] = [v * inv for v in M[col]]
        for row in range(3):
            if row != col:
                f = M[row][col]
                M[row] = [M[row][j] - f * M[col][j] for j in range(4)]
    return [M[i][3] for i in range(3)]


def _rec_post_sew(fmt: str, n_faces: int, post_sew_seconds: float):
    data = _est_load()
    x, y = float(n_faces), float(post_sew_seconds)
    data[f"f:{fmt}"]  = _bucket_add(data.get(f"f:{fmt}",  {}), x, y)
    data["f:_all"]    = _bucket_add(data.get("f:_all",    {}), x, y)
    _est_save(data)


def _rec_total(fmt: str, n_triangles: int, total_seconds: float):
    data = _est_load()
    x, y = float(n_triangles), float(total_seconds)
    data[f"t:{fmt}"] = _bucket_add(data.get(f"t:{fmt}", {}), x, y)
    data["t:_all"] = _bucket_add(data.get("t:_all", {}), x, y)
    _est_save(data)


def _est_time_metric(prefix: str, value: int, fmt=None):
    data = _est_load()
    x = float(value)
    all_key = f"{prefix}:_all"
    n_all = int(round(
        data.get(all_key, {}).get(
            "XtX", [[0] * 3 for _ in range(3)])[2][2]))
    if fmt:
        key = f"{prefix}:{fmt}"
        if key in data:
            pred, n, r2 = _bucket_predict(data[key], x)
            if pred is not None:
                return pred, n, r2
    if all_key in data:
        pred, n, r2 = _bucket_predict(data[all_key], x)
        if pred is not None:
            return pred, n, r2
    return None, n_all, 0.0


def _est_time_faces(n_faces: int, fmt=None):
    return _est_time_metric("f", n_faces, fmt=fmt)


def _est_time_triangles(n_triangles: int, fmt=None):
    return _est_time_metric("t", n_triangles, fmt=fmt)


def _fmt_time(seconds: float) -> str:
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _show_post_sew_estimate(n_faces: int, fmt: str = None) -> None:
    est, n_samples, r2 = _est_time_faces(n_faces, fmt=fmt)
    _box_sep()
    if est is not None:
        _box_row(
            f"ETA     ~{_fmt_time(est)} remaining",
            f"updated | {n_samples} records | {int(r2 * 100)}%",
            lc=Y, rc=DIM,
        )
    else:
        _box_row(
            f"ETA     learning...  {n_samples} of {_EST_MIN} conversions",
            "updated estimate",
            lc=DIM, rc=DIM,
        )
    _box_sep()


def _show_early_estimate(
        n_triangles: int, fmt: str = None, elapsed: float = 0.0) -> None:
    est, n_samples, r2 = _est_time_triangles(n_triangles, fmt=fmt)
    estimate_kind = "early"
    if est is not None:
        est = max(0.5, est - elapsed)
    if est is None:
        fallback, fallback_samples, fallback_r2 = _est_time_faces(
            n_triangles, fmt=fmt)
        if fallback is not None:
            est = fallback * 2.0
            n_samples = fallback_samples
            r2 = fallback_r2
            estimate_kind = "provisional"
    _box_sep()
    if est is not None:
        _box_row(
            f"ETA     ~{_fmt_time(est)} remaining",
            f"{estimate_kind} | {n_samples} records | {int(r2 * 100)}%",
            lc=Y, rc=DIM,
        )
    else:
        _box_row(
            f"ETA     learning...  {n_samples} of {_EST_MIN} conversions",
            "early estimate",
            lc=DIM, rc=DIM,
        )
    _box_sep()


def _reduce_prompt(default_fractions, n_tris=None, batch=False):
    if isinstance(default_fractions, float):
        default_fractions = [default_fractions]
    if default_fractions:
        pct_str = ",".join(_reduction_label(f) for f in default_fractions)
    else:
        pct_str = "0"
    hint = ""
    if n_tris is not None and default_fractions and len(default_fractions) == 1 and default_fractions[0] is not None:
        hint = f"  {DIM}({n_tris:,} -> ~{max(1, int(n_tris * default_fractions[0])):,}){X}"
    batch_hint = f"  {DIM}[!N,N = all files]{X}" if batch else ""
    try:
        raw = input(f"  {DIM}|{X}  {DIM}{'reduce':<{_BOX_LABEL}}{X}{Y}{pct_str}%{X}{hint}{batch_hint}  ").strip()
    except EOFError:
        raw = ""
    _box_sep()
    lock_all = raw.startswith("!")
    if lock_all:
        raw = raw[1:].strip()
    try:
        fractions = _parse_reduction(raw, strict=True) if raw else default_fractions
    except ValueError as exc:
        _box_row(f"invalid reduction: {exc}", lc=R)
        _box_sep()
        return _reduce_prompt(default_fractions, n_tris=n_tris, batch=batch)
    return fractions, lock_all


def _parse_reduction(value: str, *, strict: bool = False):
    if value is None:
        return None
    if not str(value).strip():
        if strict:
            raise ValueError("reduction percentage cannot be empty")
        return None
    results = []
    seen = set()
    for part in str(value).split(','):
        part = part.strip().rstrip('%')
        if not part:
            if strict:
                raise ValueError("empty reduction percentage")
            continue
        try:
            pct = float(part)
        except ValueError as exc:
            if strict:
                raise ValueError(f"invalid reduction percentage: {part!r}") from exc
            continue
        if not math.isfinite(pct) or not 0 <= pct < 100:
            if strict:
                raise ValueError(f"reduction percentage must be at least 0 and less than 100: {part!r}")
            continue
        key = round(pct, 9)
        if key in seen:
            continue
        seen.add(key)
        results.append(None if key == 0 else (100.0 - key) / 100.0)
    results.sort(key=lambda f: 0.0 if f is None else (1.0 - f) * 100.0)
    if strict and not results:
        raise ValueError("no reduction percentages were provided")
    return results if results else None


def _arg_reduction(value: str):
    try:
        return _parse_reduction(value, strict=True)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _arg_positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("value must be a finite number greater than zero")
    return number


def _reduction_pct(fraction) -> float:
    return 0.0 if fraction is None else (1.0 - fraction) * 100.0


def _reduction_label(fraction) -> str:
    pct = _reduction_pct(fraction)
    if math.isclose(
            pct, round(pct), rel_tol=0.0, abs_tol=5e-10):
        return str(int(round(pct)))
    return f"{pct:.9f}".rstrip("0").rstrip(".")


_UNIT_TO_MM = {
    "micron": 0.001,
    "micrometer": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "meter": 1000.0,
    "inch": 25.4,
    "foot": 304.8,
    "feet": 304.8,
}


def _unit_scale_mm(value: str | None, default: str = "millimeter") -> float:
    unit = (value or default).strip().lower()
    if unit not in _UNIT_TO_MM:
        raise ValueError(f"unsupported model unit: {unit}")
    return _UNIT_TO_MM[unit]


def _xml_local_name(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _xml_children(element, name: str):
    return [child for child in list(element) if _xml_local_name(child) == name]


def _xml_child(element, name: str):
    return next((child for child in list(element) if _xml_local_name(child) == name), None)


def _xml_attr(element, name: str):
    return next((
        value for key, value in element.attrib.items()
        if key.rsplit("}", 1)[-1] == name
    ), None)


def _xml_float(element, name: str, default: float = 0.0) -> float:
    child = _xml_child(element, name)
    return float(child.text) if child is not None and child.text else default


def _xml_int(element, name: str) -> int:
    child = _xml_child(element, name)
    if child is None or child.text is None:
        raise ValueError(f"missing integer element: {name}")
    return int(child.text)


def _transform_3mf(value: str | None):
    import numpy as np
    if not value:
        return np.eye(4, dtype=np.float64)
    parts = [float(part) for part in value.split()]
    if len(parts) != 12 or not np.isfinite(parts).all():
        raise ValueError(f"invalid 3MF transform: {value!r}")
    return np.array([
        [parts[0], parts[3], parts[6],  parts[9]],
        [parts[1], parts[4], parts[7], parts[10]],
        [parts[2], parts[5], parts[8], parts[11]],
        [0.0,      0.0,      0.0,      1.0],
    ], dtype=np.float64)


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
    normalized = posixpath.normpath(target.replace("\\", "/").lstrip("/"))
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"invalid 3MF model path: {target!r}")
    matches = {name.casefold(): name for name in zf.namelist()}
    resolved = matches.get(normalized.casefold())
    if resolved is None:
        raise ValueError(f"3MF references missing model document {target}")
    return resolved


def _load_3mf_arrays(path: Path):
    import numpy as np
    with zipfile.ZipFile(str(path)) as zf:
        root_document = _resolve_3mf_part(zf, _find_3mf_model(zf))
        documents = {}

        def load_document(document_path):
            if document_path in documents:
                return documents[document_path]
            with zf.open(document_path) as model_file:
                model_root = ET.parse(model_file).getroot()
            resources = _xml_child(model_root, "resources")
            if resources is None:
                raise ValueError(
                    f"3MF model document has no resources: {document_path}")

            unit_scale = _unit_scale_mm(model_root.get("unit"))
            objects = {}
            referenced = set()
            for obj in _xml_children(resources, "object"):
                obj_id = obj.get("id")
                if not obj_id:
                    continue
                mesh_el = _xml_child(obj, "mesh")
                mesh = None
                if mesh_el is not None:
                    vertices_el = _xml_child(mesh_el, "vertices")
                    triangles_el = _xml_child(mesh_el, "triangles")
                    if vertices_el is not None and triangles_el is not None:
                        verts = [
                            (float(v.get("x")), float(v.get("y")),
                             float(v.get("z")))
                            for v in _xml_children(vertices_el, "vertex")
                        ]
                        tris = [
                            (int(t.get("v1")), int(t.get("v2")),
                             int(t.get("v3")))
                            for t in _xml_children(triangles_el, "triangle")
                        ]
                        mesh = (
                            np.asarray(
                                verts, dtype=np.float64).reshape(-1, 3)
                            * unit_scale,
                            np.asarray(
                                tris, dtype=np.int32).reshape(-1, 3),
                        )

                components_el = _xml_child(obj, "components")
                components = []
                if components_el is not None:
                    for component in _xml_children(
                            components_el, "component"):
                        child_id = component.get("objectid")
                        if not child_id:
                            continue
                        target = _xml_attr(component, "path")
                        if target and document_path != root_document:
                            raise ValueError(
                                "3MF external component paths are only "
                                "valid in the root model document")
                        child_document = (
                            _resolve_3mf_part(zf, target)
                            if target else document_path)
                        child_transform = _scale_3mf_transform(
                            _transform_3mf(component.get("transform")),
                            unit_scale,
                        )
                        components.append(
                            (child_document, child_id, child_transform))
                        if child_document == document_path:
                            referenced.add(child_id)
                objects[obj_id] = (mesh, components)

            document = (model_root, unit_scale, objects, referenced)
            documents[document_path] = document
            return document

        root, root_scale, root_objects, root_referenced = load_document(
            root_document)
        verts_all, tris_all = [], []
        vertex_count = 0

        def emit(document_path, obj_id, transform, stack):
            nonlocal vertex_count
            key = (document_path, obj_id)
            if key in stack:
                raise ValueError(
                    f"cyclic 3MF component reference involving object {obj_id}")
            _, _, objects, _ = load_document(document_path)
            if obj_id not in objects:
                raise ValueError(
                    f"3MF references missing object {obj_id} "
                    f"in {document_path}")
            mesh, components = objects[obj_id]
            if mesh is not None:
                verts, tris = mesh
                verts_all.append(_apply_transform(verts, transform))
                tris_all.append(tris + vertex_count)
                vertex_count += len(verts)
            for child_document, child_id, child_transform in components:
                emit(
                    child_document,
                    child_id,
                    transform @ child_transform,
                    stack | {key},
                )

        build = _xml_child(root, "build")
        items = _xml_children(build, "item") if build is not None else []
        if items:
            for item in items:
                obj_id = item.get("objectid")
                if not obj_id:
                    continue
                target = _xml_attr(item, "path")
                document_path = (
                    _resolve_3mf_part(zf, target)
                    if target else root_document)
                item_transform = _scale_3mf_transform(
                    _transform_3mf(item.get("transform")), root_scale)
                emit(document_path, obj_id, item_transform, set())
        else:
            roots = [
                obj_id for obj_id in root_objects
                if obj_id not in root_referenced
            ]
            for obj_id in roots:
                emit(root_document, obj_id, np.eye(4), set())

    if not verts_all or not tris_all:
        raise ValueError("3MF model contains no triangle geometry")
    return np.vstack(verts_all), np.vstack(tris_all)


def _amf_instance_transform(instance):
    import numpy as np
    tx = _xml_float(instance, "deltax")
    ty = _xml_float(instance, "deltay")
    tz = _xml_float(instance, "deltaz")
    rx = math.radians(_xml_float(instance, "rx"))
    ry = math.radians(_xml_float(instance, "ry"))
    rz = math.radians(_xml_float(instance, "rz"))
    sx, cx = math.sin(rx), math.cos(rx)
    sy, cy = math.sin(ry), math.cos(ry)
    sz, cz = math.sin(rz), math.cos(rz)
    mx = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]], dtype=float)
    my = np.array([[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]], dtype=float)
    mz = np.array([[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
    mt = np.eye(4, dtype=float)
    mt[:3, 3] = (tx, ty, tz)
    return mt @ mz @ my @ mx


def _load_amf_arrays(path: Path):
    import numpy as np
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(str(path)) as zf:
            names = zf.namelist()
            target = next((n for n in names if n.lower().endswith(".amf")), None)
            if target is None:
                raise ValueError("compressed AMF archive contains no .amf document")
            with zf.open(target) as f:
                root = ET.parse(f).getroot()
    else:
        root = ET.fromstring(raw)

    objects = {}
    for obj in _xml_children(root, "object"):
        obj_id = obj.get("id")
        mesh_el = _xml_child(obj, "mesh")
        if not obj_id or mesh_el is None:
            continue
        vertices_el = _xml_child(mesh_el, "vertices")
        if vertices_el is None:
            continue
        verts = []
        for vertex in _xml_children(vertices_el, "vertex"):
            coords = _xml_child(vertex, "coordinates")
            if coords is not None:
                verts.append([
                    _xml_float(coords, "x"),
                    _xml_float(coords, "y"),
                    _xml_float(coords, "z"),
                ])
        tris = []
        for volume in _xml_children(mesh_el, "volume"):
            for tri in _xml_children(volume, "triangle"):
                tris.append([
                    _xml_int(tri, "v1"),
                    _xml_int(tri, "v2"),
                    _xml_int(tri, "v3"),
                ])
        objects[obj_id] = (
            np.asarray(verts, dtype=np.float64).reshape(-1, 3),
            np.asarray(tris, dtype=np.int32).reshape(-1, 3),
        )

    scale = _unit_scale_mm(root.get("unit"))
    scale_transform = np.diag([scale, scale, scale, 1.0])
    instances = []
    for constellation in _xml_children(root, "constellation"):
        for instance in _xml_children(constellation, "instance"):
            obj_id = instance.get("objectid")
            if obj_id:
                instances.append((obj_id, _amf_instance_transform(instance)))
    if not instances:
        instances = [(obj_id, np.eye(4, dtype=float)) for obj_id in objects]

    verts_all, tris_all, offset = [], [], 0
    for obj_id, transform in instances:
        if obj_id not in objects:
            raise ValueError(f"AMF references missing object {obj_id}")
        verts, tris = objects[obj_id]
        verts_all.append(_apply_transform(verts, scale_transform @ transform))
        tris_all.append(tris + offset)
        offset += len(verts)
    if not verts_all or not tris_all:
        raise ValueError("AMF model contains no triangle geometry")
    return np.vstack(verts_all), np.vstack(tris_all)


def _triangulate_polygon(points, indices):
    import numpy as np

    polygon = []
    for index in indices:
        if not polygon or index != polygon[-1]:
            polygon.append(index)
    if len(polygon) > 1 and polygon[0] == polygon[-1]:
        polygon.pop()
    if len(polygon) < 3:
        raise ValueError("OBJ face has fewer than three distinct vertices")

    coordinates = np.asarray([points[index] for index in polygon], dtype=np.float64)
    normal = np.zeros(3, dtype=np.float64)
    for current, following in zip(coordinates, np.roll(coordinates, -1, axis=0)):
        normal += np.array([
            (current[1] - following[1]) * (current[2] + following[2]),
            (current[2] - following[2]) * (current[0] + following[0]),
            (current[0] - following[0]) * (current[1] + following[1]),
        ])
    drop_axis = int(np.argmax(np.abs(normal)))
    if abs(normal[drop_axis]) <= 1e-15:
        raise ValueError("OBJ face is degenerate")
    projected = np.delete(coordinates, drop_axis, axis=1)
    scale = max(float(np.ptp(projected, axis=0).max()), 1.0)
    epsilon = scale * scale * 1e-12

    def cross_2d(left, middle, right):
        first = middle - left
        second = right - middle
        return first[0] * second[1] - first[1] * second[0]

    area2 = sum(
        projected[index, 0] * projected[(index + 1) % len(projected), 1]
        - projected[(index + 1) % len(projected), 0] * projected[index, 1]
        for index in range(len(projected))
    )
    if abs(area2) <= epsilon:
        raise ValueError("OBJ face has zero projected area")
    orientation = 1.0 if area2 > 0 else -1.0

    remaining = list(range(len(polygon)))
    changed = True
    while changed and len(remaining) > 3:
        changed = False
        for position in range(len(remaining)):
            left = remaining[position - 1]
            middle = remaining[position]
            right = remaining[(position + 1) % len(remaining)]
            if abs(cross_2d(
                    projected[left], projected[middle], projected[right])) <= epsilon:
                remaining.pop(position)
                changed = True
                break
    if len(remaining) < 3:
        raise ValueError("OBJ face is degenerate after removing collinear vertices")

    def point_in_triangle(point, left, middle, right):
        values = (
            cross_2d(left, middle, point),
            cross_2d(middle, right, point),
            cross_2d(right, left, point),
        )
        return all(orientation * value >= -epsilon for value in values)

    triangles = []
    while len(remaining) > 3:
        ear_found = False
        for position in range(len(remaining)):
            left = remaining[position - 1]
            middle = remaining[position]
            right = remaining[(position + 1) % len(remaining)]
            if orientation * cross_2d(
                    projected[left], projected[middle], projected[right]) <= epsilon:
                continue
            if any(
                point_in_triangle(
                    projected[candidate],
                    projected[left],
                    projected[middle],
                    projected[right],
                )
                for candidate in remaining
                if candidate not in (left, middle, right)
            ):
                continue
            triangles.append([
                polygon[left], polygon[middle], polygon[right]])
            remaining.pop(position)
            ear_found = True
            break
        if not ear_found:
            raise ValueError(
                "OBJ face is self-intersecting or cannot be triangulated")
    triangles.append([polygon[index] for index in remaining])
    return triangles


def _load_mesh_arrays(path: Path):
    import numpy as np
    ext = path.suffix.lower()

    if ext == STL_FILE_EXTENSION:
        with open(path, 'rb') as f:
            f.seek(80)
            n = struct.unpack('<I', f.read(4))[0]
            data = f.read()
        binary_data_size = n * 50
        if (
            binary_data_size <= len(data)
            and (n > 0 or binary_data_size == len(data))
        ):
            stl_dt = np.dtype([('n', np.float32, (3,)), ('v0', np.float32, (3,)),
                                ('v1', np.float32, (3,)), ('v2', np.float32, (3,)),
                                ('attr', np.uint16)])
            tris = np.frombuffer(
                data[:binary_data_size], dtype=stl_dt)
            verts = np.stack([tris['v0'], tris['v1'], tris['v2']], axis=1).reshape(-1, 3)
            return verts.astype(np.float64), np.arange(n * 3, dtype=np.int32).reshape(n, 3)
        verts = []
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.split()
                if p and p[0] == 'vertex':
                    verts.append([float(p[1]), float(p[2]), float(p[3])])
        v = np.array(verts, dtype=np.float64)
        return v, np.arange(len(v), dtype=np.int32).reshape(-1, 3)

    if ext == THREE_MF_FILE_EXTENSION:
        return _load_3mf_arrays(path)

    if ext == OBJ_FILE_EXTENSION:
        verts, tris = [], []
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.split()
                if not p:
                    continue
                if p[0] == 'v':
                    verts.append([float(p[1]), float(p[2]), float(p[3])])
                elif p[0] == 'f':
                    idx = []
                    for x in p[1:]:
                        raw = int(x.split('/')[0])
                        idx.append(len(verts) + raw if raw < 0 else raw - 1)
                    if any(index < 0 or index >= len(verts) for index in idx):
                        raise ValueError("OBJ face references a missing vertex")
                    tris.extend(_triangulate_polygon(verts, idx))
        return np.array(verts, dtype=np.float64), np.array(tris, dtype=np.int32)

    if ext == AMF_FILE_EXTENSION:
        return _load_amf_arrays(path)

    raise ValueError(f"unsupported format for reduction: {path.suffix}")


def _reduce_mesh_arrays(verts, faces, keep_fraction):
    import numpy as np
    n_before = len(faces)
    if n_before == 0:
        raise ValueError("mesh is empty")

    verts, faces = _clean_mesh_arrays(verts, faces)
    if len(faces) == 0:
        raise ValueError("mesh has no valid faces after vertex merging")

    target_count = max(4, int(n_before * keep_fraction))
    verts_out = faces_out = None
    reducer_errors = []

    try:
        import open3d as o3d
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(verts)
        o3d_mesh.triangles = o3d.utility.Vector3iVector(faces)
        simplified = o3d_mesh.simplify_quadric_decimation(
            target_count,
            boundary_weight=(
                REDUCTION_BOUNDARY_WEIGHT if PRESERVE_BOUNDARIES_DURING_REDUCTION else 1.0),
        )
        verts_out = np.asarray(simplified.vertices, dtype=np.float64)
        faces_out = np.asarray(simplified.triangles, dtype=np.int32)
        if len(faces_out) == 0:
            raise ValueError("empty")
    except Exception as exc:
        reducer_errors.append(f"Open3D: {exc}")

    if verts_out is None or len(faces_out) == 0:
        try:
            import trimesh
            import trimesh.simplify
            mesh_t = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
            result = trimesh.simplify.simplify_quadric_decimation(mesh_t, target_count)
            if result is not None and len(result.faces) > 0:
                verts_out = np.array(result.vertices, dtype=np.float64)
                faces_out = np.array(result.faces, dtype=np.int32)
        except Exception as exc:
            reducer_errors.append(f"trimesh: {exc}")

    if verts_out is None or len(faces_out) == 0:
        try:
            import fast_simplification
            target_reduction = max(0.01, min(0.99, 1.0 - keep_fraction))
            verts_out, faces_out = fast_simplification.simplify(
                verts, faces, target_reduction, agg=2
            )
        except Exception as exc:
            reducer_errors.append(f"fast-simplification: {exc}")
            raise RuntimeError("; ".join(reducer_errors)) from exc

    verts_out, faces_out = _clean_mesh_arrays(verts_out, faces_out)
    if len(faces_out) == 0:
        raise ValueError("simplified mesh contains no valid faces")
    before = _mesh_quality_report(
        verts, faces, check_self_intersection=False)
    after = _mesh_quality_report(
        verts_out, faces_out,
        check_self_intersection=(
            CHECK_MESH_QUALITY
            and _should_check_self_intersections(faces_out)),
    )
    diagonal = max(before["diagonal"], 1e-12)
    dimension_error_pct = (
        float(np.linalg.norm(after["dimensions"] - before["dimensions"]))
        / diagonal * 100.0
    )
    if dimension_error_pct > MAX_REDUCTION_SIZE_CHANGE_PERCENT:
        raise ValueError(
            "reduction changed model dimensions by "
            f"{dimension_error_pct:.3g}%")
    if (
        before["watertight"]
        and PRESERVE_BOUNDARIES_DURING_REDUCTION
        and not after["watertight"]
    ):
        raise ValueError("reduction broke mesh watertightness")
    if after["non_manifold_edges"] > before["non_manifold_edges"]:
        raise ValueError("reduction introduced non-manifold edges")
    if (
        PRESERVE_BOUNDARIES_DURING_REDUCTION
        and after["components"] != before["components"]
    ):
        raise ValueError(
            "reduction changed connected component count from "
            f"{before['components']:,} to {after['components']:,}")
    if (
        REJECT_SELF_INTERSECTING_MESH
        and after["internal_self_intersections"]
    ):
        raise ValueError(
            "reduction introduced "
            f"{after['internal_self_intersections']:,} "
            "internal self-intersections")
    if (
        before["watertight"]
        and after["watertight"]
        and before["volume"] > 1e-12
    ):
        volume_error_pct = (
            abs(after["volume"] - before["volume"])
            / before["volume"] * 100.0
        )
        if volume_error_pct > MAX_REDUCTION_VOLUME_CHANGE_PERCENT:
            raise ValueError(
                "reduction changed volume by "
                f"{volume_error_pct:.3g}%")
    return verts_out, faces_out, n_before, len(faces_out)


_STEP_SCHEMAS = {"ap203": "AP203", "ap214": "AP214IS", "ap242": "AP242DIS"}


def _preview_topology_text(shape) -> str:
    n_solids = _count_topo_unique(shape, TopAbs_SOLID)
    n_faces = _count_topo_unique(shape, TopAbs_FACE)
    n_edges = _count_topo_unique(shape, TopAbs_EDGE)
    solid_label = "solid" if n_solids == 1 else "solids"
    return (
        f"{n_solids:,} {solid_label} | "
        f"{n_faces:,} faces | {n_edges:,} edges"
    )


def _render_preview(step_path: Path, png_path: Path, duration: float = None,
                    display_name: str = None, reduction_pct: int = None, shape=None):
    try:
        import numpy as np
        import math as _math

        if shape is None:
            from OCC.Core.STEPControl import STEPControl_Reader
            with quiet():
                reader = STEPControl_Reader()
                status = reader.ReadFile(step_path.as_posix())
                if status != IFSelect_RetDone:
                    return f"STEP read failed (status {status})"
                reader.TransferRoots()
                shape = reader.OneShape()
            if shape.IsNull():
                return "empty shape from STEP"

        _OUT_SIZE = 1200

        _er, _ar = _math.radians(20), _math.radians(315)
        _ffx = -_math.cos(_er) * _math.cos(_ar)
        _ffy = -_math.cos(_er) * _math.sin(_ar)
        _ffz = -_math.sin(_er)
        _rm  = _math.hypot(_ffy, _ffx)
        _rx, _ry = _ffy / _rm, -_ffx / _rm
        _ux = _ry * _ffz;  _uy = -_rx * _ffz;  _uz = _rx * _ffy - _ry * _ffx
        _um = _math.sqrt(_ux*_ux + _uy*_uy + _uz*_uz)
        _ux, _uy, _uz = _ux/_um, _uy/_um, _uz/_um

        mpl_arr = None
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from mpl_toolkits.mplot3d.art3d import Line3DCollection
            try:
                from OCC.Core.TopoDS import topods_Edge as _cast_edge
            except ImportError:
                _cast_edge = lambda s: s
            from OCC.Core.BRepAdaptor import BRepAdaptor_Curve
            from OCC.Core.GCPnts import GCPnts_TangentialDeflection

            segments, raw_pts = [], []
            exp = TopExp_Explorer(shape, TopAbs_EDGE)
            while exp.More():
                try:
                    edge  = _cast_edge(exp.Current())
                    curve = BRepAdaptor_Curve(edge)
                    disc  = GCPnts_TangentialDeflection(curve, 0.3, 0.05)
                    pts   = []
                    for i in range(1, disc.NbPoints() + 1):
                        p = disc.Value(i)
                        pts.append((p.X(), p.Y(), p.Z()))
                        raw_pts.append(pts[-1])
                    for j in range(len(pts) - 1):
                        segments.append([pts[j], pts[j + 1]])
                except Exception:
                    pass
                exp.Next()

            if segments:
                bg_hex = "#16213e"
                bg_rgb = np.array([0x16, 0x21, 0x3e], dtype=np.uint8)
                arr_pts = np.array(raw_pts, dtype=np.float32)
                mins, maxs = arr_pts.min(axis=0), arr_pts.max(axis=0)
                mid  = ((mins + maxs) / 2).tolist()
                half = float((maxs - mins).max()) / 2 * 1.05 or 1.0

                fig = plt.figure(figsize=(10, 10), dpi=200, facecolor=bg_hex)
                ax  = fig.add_subplot(111, projection="3d", facecolor=bg_hex)
                ax.add_collection3d(Line3DCollection(segments, linewidths=0.4, colors="#ffffff", alpha=0.9))
                ax.set_xlim(mid[0] - half, mid[0] + half)
                ax.set_ylim(mid[1] - half, mid[1] + half)
                ax.set_zlim(mid[2] - half, mid[2] + half)
                ax.set_axis_off()
                ax.view_init(elev=20, azim=315)
                plt.tight_layout(pad=0)
                fig.canvas.draw()
                mpl_arr = np.frombuffer(fig.canvas.buffer_rgba(), dtype=np.uint8).reshape(
                    fig.canvas.get_width_height()[::-1] + (4,))
                plt.close(fig)

                non_bg = ~np.all(mpl_arr[:, :, :3] == bg_rgb, axis=2)
                rows = np.where(np.any(non_bg, axis=1))[0]
                cols = np.where(np.any(non_bg, axis=0))[0]
                if len(rows) and len(cols):
                    pad = 30
                    mpl_arr = mpl_arr[
                        max(0, rows[0]-pad):min(mpl_arr.shape[0], rows[-1]+pad+1),
                        max(0, cols[0]-pad):min(mpl_arr.shape[1], cols[-1]+pad+1)]
                bg_mask = np.all(mpl_arr[:, :, :3] == bg_rgb, axis=2)
                mpl_arr[bg_mask, 3] = 0
        except Exception:
            mpl_arr = None

        if mpl_arr is None:
            return "preview render failed"

        try:
            from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
            _resample = getattr(getattr(_PIL_Image, "Resampling", _PIL_Image), "LANCZOS")

            canvas   = _PIL_Image.new("RGBA", (_OUT_SIZE, _OUT_SIZE), (0x16, 0x21, 0x3e, 255))
            _draw_bg = _PIL_Draw.Draw(canvas)
            _gc      = (0x33, 0x3d, 0x55, 255)
            _gs      = 60
            _half    = _OUT_SIZE // 2
            _diag    = int(_math.sqrt(2) * _OUT_SIZE) + _gs
            _nl      = _diag // _gs + 2
            _gdx     = np.array([_rx, -_ux]); _gdx /= np.hypot(*_gdx)
            _gdy     = np.array([_ry, -_uy]); _gdy /= np.hypot(*_gdy)
            for _gd, _gp in [(_gdx, np.array([-_gdx[1], _gdx[0]])),
                              (_gdy, np.array([-_gdy[1], _gdy[0]]))]:
                for _ni in range(-_nl, _nl + 1):
                    _lox = _half + _ni * _gs * _gp[0]
                    _loy = _half + _ni * _gs * _gp[1]
                    _draw_bg.line(
                        [(int(_lox - _gd[0]*_diag), int(_loy - _gd[1]*_diag)),
                         (int(_lox + _gd[0]*_diag), int(_loy + _gd[1]*_diag))],
                        fill=_gc, width=1)

            img = _PIL_Image.fromarray(mpl_arr)
            img.thumbnail((_OUT_SIZE, _OUT_SIZE), _resample)
            canvas.paste(img, ((_OUT_SIZE - img.width) // 2,
                               (_OUT_SIZE - img.height) // 2), img)

            out_kb  = step_path.stat().st_size // 1024
            lines   = [
                (display_name or step_path.stem,                                         (255, 255, 255, 220)),
                (f"reduction {reduction_pct or 0}%",                                       (180, 190, 220, 155)),
                (_preview_topology_text(shape),                                           (180, 190, 220, 170)),
                (f"{out_kb:,} KB" + (f"  |  {_fmt_time(duration)}" if duration else ""), (180, 190, 220, 130)),
            ]
            lines = [(t, c) for t, c in lines if t]
            _font_sz = 18
            try:
                import matplotlib as _mpl
                _fp      = str(Path(_mpl.__file__).parent / "mpl-data" / "fonts" / "ttf" / "DejaVuSans.ttf")
                _font    = _PIL_Font.truetype(_fp, _font_sz)
                _font_ax = _PIL_Font.truetype(_fp, 14)
            except Exception:
                _font = _font_ax = _PIL_Font.load_default()
            draw     = _PIL_Draw.Draw(canvas)
            margin, line_h = 20, _font_sz + 5
            y0 = _OUT_SIZE - margin - len(lines) * line_h
            for i, (text, color) in enumerate(lines):
                draw.text((margin, y0 + i * line_h),
                          text if i == 0 else text[:1].upper() + text[1:],
                          fill=color, font=_font)

            _axis_dirs = {}
            for _n, _wv in [("X",(1.,0.,0.)), ("Y",(0.,1.,0.)), ("Z",(0.,0.,1.))]:
                _sx = _wv[0]*_rx + _wv[1]*_ry
                _sy = _wv[0]*_ux + _wv[1]*_uy + _wv[2]*_uz
                _d  = np.array([_sx, -_sy])
                _mag = float(np.hypot(*_d))
                _axis_dirs[_n] = _d / _mag if _mag > 0 else _d
            _arrow, _ix, _iy = 60, _OUT_SIZE - 90, _OUT_SIZE - 90
            _ax_cols = {"X": (255, 85, 85, 230), "Y": (85, 204, 85, 230), "Z": (85, 136, 255, 230)}
            for _n, _col in _ax_cols.items():
                _d = _axis_dirs[_n]
                _ex, _ey = int(_ix + _d[0] * _arrow), int(_iy + _d[1] * _arrow)
                draw.line([(_ix, _iy), (_ex, _ey)], fill=_col, width=2)
                draw.text((_ex + int(_d[0] * 10), _ey + int(_d[1] * 10) - 7),
                          _n, fill=_col, font=_font_ax)

            canvas.save(str(png_path))

        except ImportError:
            import matplotlib.image as mpimg
            mpimg.imsave(str(png_path), mpl_arr)

        return None

    except Exception as e:
        return str(e).splitlines()[0][:50]


def _render_preview_atomic(step_path: Path, png_path: Path, **kwargs):
    temp_png = None
    try:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        fd_png, temp_png_name = tempfile.mkstemp(
            prefix=f".{png_path.name}.", suffix=".tmp.png", dir=png_path.parent)
        os.close(fd_png)
        temp_png = Path(temp_png_name)
        error = _render_preview(step_path, temp_png, **kwargs)
        if error is None and temp_png.stat().st_size > 0:
            os.replace(temp_png, png_path)
            return None
        png_path.unlink(missing_ok=True)
        return error or "preview output is missing or empty"
    except Exception as exc:
        try:
            png_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"preview commit failed: {exc}"
    finally:
        if temp_png is not None:
            try:
                temp_png.unlink(missing_ok=True)
            except OSError:
                pass


def _write_step_atomic(shape, output_path: Path, step_schema: str) -> int:
    Interface_Static.SetCVal("write.step.schema", step_schema)
    Interface_Static.SetCVal("write.step.product.name", "")
    Interface_Static.SetCVal("write.step.assembly", "0")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = STEPControl_Writer()
    if shape.ShapeType() == TopAbs_COMPOUND:
        transfer_shapes = []
        iterator = TopoDS_Iterator(shape)
        while iterator.More():
            transfer_shapes.append(iterator.Value())
            iterator.Next()
        if not transfer_shapes:
            transfer_shapes = [shape]
    else:
        transfer_shapes = [shape]

    fd_tmp, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp.stp", dir=output_path.parent)
    os.close(fd_tmp)
    tmp_output = Path(tmp_name)
    try:
        with quiet():
            for subshape in transfer_shapes:
                status = writer.Transfer(subshape, STEPControl_AsIs)
                if status != IFSelect_RetDone:
                    raise RuntimeError(f"STEP transfer failed with status {status}")
            status = writer.Write(tmp_output.as_posix())
        if status != IFSelect_RetDone:
            raise RuntimeError(f"STEP writer failed with status {status}")
        if not tmp_output.is_file() or tmp_output.stat().st_size == 0:
            raise RuntimeError("temporary STEP output is missing or empty")
        if VALIDATE_STEP_AFTER_WRITING:
            reader = STEPControl_Reader()
            with quiet():
                read_status = reader.ReadFile(tmp_output.as_posix())
                if read_status == IFSelect_RetDone:
                    reader.TransferRoots()
            if read_status != IFSelect_RetDone:
                raise RuntimeError(
                    f"STEP readback failed with status {read_status}")
            read_shape = reader.OneShape()
            valid, validation_error = _validate_occ_shape(
                read_shape, require_solid=REQUIRE_SOLID_OUTPUT)
            if not valid:
                raise RuntimeError(
                    f"STEP readback validation failed: {validation_error}")
        os.replace(tmp_output, output_path)
        return output_path.stat().st_size // BYTES_PER_KB
    finally:
        try:
            tmp_output.unlink(missing_ok=True)
        except OSError:
            pass


def convert(input_path: Path, output_path: Path, tolerance: float = SEWING_TOLERANCE,
            reduce_fraction=None, step_schema: str = "AP203",
            _suppress_read_step: bool = False,
            _mesh_data=None):
    n_verts = n_tris = None
    verts_np = tris_np = None
    primitive_name = None
    shape = None
    _t_convert = time.perf_counter()

    try:
        original_ext = input_path.suffix.lower()

        if not _suppress_read_step:
            t = _step_start("reading")
        if original_ext in _IGES_EXTS:
            shape, n_verts, n_tris = _read_iges_shape(input_path)
            if shape.IsNull():
                if not _suppress_read_step:
                    _step_fail()
                return False, "input produced an empty shape"
            if not _suppress_read_step:
                read_parts = []
                if n_verts is not None:
                    read_parts.append(f"{n_verts:,} vertices")
                if n_tris is not None:
                    read_parts.append(f"{n_tris:,} triangles")
                if not read_parts:
                    read_parts.append(f"{_count_topo(shape, TopAbs_FACE):,} faces")
                _step_end(t, " | ".join(read_parts))
        else:
            if _mesh_data is not None:
                verts_np, tris_np = _mesh_data
                n_verts = len(verts_np)
                n_tris = len(tris_np)
            else:
                verts_np, tris_np = _load_mesh_arrays(input_path)
                verts_np, tris_np = _repair_mesh_arrays(verts_np, tris_np)
                n_verts = len(verts_np)
                n_tris = len(tris_np)
            if not _suppress_read_step:
                _step_end(t, f"{n_verts:,} vertices | {n_tris:,} triangles")

        if (
            not _suppress_read_step
            and n_tris is not None
            and original_ext not in _IGES_EXTS
        ):
            estimated_triangles = (
                max(4, int(n_tris * reduce_fraction))
                if reduce_fraction is not None else n_tris
            )
            _show_early_estimate(
                estimated_triangles, fmt=original_ext,
                elapsed=time.perf_counter() - _t_convert)

        effective_tolerance = _effective_tolerance(verts_np, tolerance)
        if CHECK_MESH_QUALITY and verts_np is not None:
            t = _step_start("quality")
            quality_report = _mesh_quality_report(
                verts_np, tris_np,
                check_self_intersection=_should_check_self_intersections(tris_np))
            detail_parts = [
                "watertight" if quality_report["watertight"] else
                f"{quality_report['boundary_edges']:,} boundary edges",
                f"{quality_report['components']:,} "
                f"part{'s' if quality_report['components'] != 1 else ''}",
            ]
            if quality_report["non_manifold_edges"]:
                detail_parts.append(
                    f"{quality_report['non_manifold_edges']:,} non-manifold")
            if quality_report["internal_self_intersections"]:
                detail_parts.append(
                    f"{quality_report['internal_self_intersections']:,} intersections")
            if quality_report["cross_component_intersections"]:
                detail_parts.append(
                    f"{quality_report['cross_component_intersections']:,} "
                    "part overlaps")
            if (
                CHECK_SELF_INTERSECTIONS
                and quality_report["self_intersections"] is None
            ):
                detail_parts.append("intersection scan skipped")
            if (
                REJECT_SELF_INTERSECTING_MESH
                and quality_report["internal_self_intersections"]
            ):
                _step_fail()
                return False, (
                    "mesh contains "
                    f"{quality_report['internal_self_intersections']:,} "
                    "internal self-intersections")
            if (
                REJECT_NON_MANIFOLD_MESH
                and quality_report["non_manifold_edges"]
            ):
                _step_fail()
                return False, (
                    "mesh contains "
                    f"{quality_report['non_manifold_edges']:,} non-manifold edges")
            _step_end(t, " | ".join(detail_parts))

        if reduce_fraction is not None and original_ext not in _IGES_EXTS:
            t = _step_start("reducing")
            try:
                s_verts, s_tris, n_before, n_after = _reduce_mesh_arrays(
                    verts_np, tris_np, reduce_fraction)
                red_pct = int(round((1.0 - n_after / n_before) * 100))
                _step_end(t, f"{n_before:,} -> {n_after:,} triangles | {red_pct}% removed")
                verts_np, tris_np = s_verts, s_tris
                n_tris = n_after
            except Exception as e:
                _step_fail()
                return False, f"mesh reduction failed: {e}"

        if shape is None and verts_np is not None:
            t = _step_start("fitting")
            shape, primitive_name = _reconstruct_analytic_shape(
                verts_np, tris_np)
            if shape is None:
                shape = _mesh_to_shape(verts_np.tolist(), tris_np.tolist())
                _step_end(t, "triangle B-Rep | planar merge enabled")
            elif primitive_name == "linear extrusion":
                _step_end(t, "experimental linear extrusion | exact profile prism")
            else:
                _step_end(t, f"analytic {primitive_name} | exact CAD surfaces")
        if shape is None or shape.IsNull():
            return False, "input produced an empty shape"

        if primitive_name is not None:
            refined = shape
            n_faces_after = _count_topo(refined, TopAbs_FACE)
            n_solids = _count_topo(refined, TopAbs_SOLID)
            free_shells = 0
            n_faces_sewn = n_faces_after
            t_post_sew = None
        else:
            t = _step_start("sewing")
            with quiet():
                sewn, sew_err = _subprocess_sew(shape, effective_tolerance)
            if sewn.IsNull():
                _step_fail()
                detail = f": {sew_err}" if sew_err else ""
                return False, f"sewing failed{detail}"
            n_shells = _count_topo(sewn, TopAbs_SHELL)
            n_faces_sewn = _count_topo(sewn, TopAbs_FACE)
            _step_end(
                t,
                f"{n_shells:,} shell{'s' if n_shells != 1 else ''} | "
                f"{n_faces_sewn:,} faces",
            )

            t_post_sew = time.perf_counter()
            _show_post_sew_estimate(n_faces_sewn, fmt=original_ext)

            t = _step_start("fixing")
            with quiet():
                fixed, fix_err = _parallel_fix(sewn)
            if fix_err:
                _step_fail()
                return False, f"shape fixing failed: {fix_err}"
            n_faces_out = _count_topo(fixed, TopAbs_FACE)
            _step_end(t, f"{n_faces_sewn:,} -> {n_faces_out:,} faces")

            closed_shape = fixed
            if FILL_SMALL_PLANAR_BREP_GAPS:
                t = _step_start("closing")
                with quiet():
                    candidate, close_err = _parallel_fill_brep_holes(
                        fixed, effective_tolerance)
                candidate_valid, _ = _validate_occ_shape(
                    candidate, require_solid=False)
                if close_err or not candidate_valid:
                    detail = close_err or "invalid result"
                    _step_end(t, f"kept original | {detail}")
                else:
                    closed_shape = candidate
                    n_faces_closed = _count_topo(
                        closed_shape, TopAbs_FACE)
                    _step_end(
                        t, f"{n_faces_out:,} -> {n_faces_closed:,} faces")

            t = _step_start("solidifying")
            with quiet():
                final_shape, solid_err = _parallel_solidify(closed_shape)
            n_solids = _count_topo(final_shape, TopAbs_SOLID)
            free_shells = _count_free_shells(final_shape)
            valid, validation_error = _validate_occ_shape(
                final_shape, require_solid=REQUIRE_SOLID_OUTPUT)
            if not valid:
                _step_fail()
                return False, f"solid validation failed: {validation_error}"
            if solid_err:
                _step_end(t, f"{n_solids:,} solids | fallback: {solid_err}")
            elif n_solids:
                detail = (
                    f"{n_solids:,} solid{'s' if n_solids != 1 else ''}")
                if free_shells:
                    detail += (
                        f" | {free_shells:,} free "
                        f"shell{'s' if free_shells != 1 else ''}")
                _step_end(t, detail)
            else:
                _step_end(
                    t,
                    f"0 solids | {free_shells:,} open "
                    f"shell{'s' if free_shells != 1 else ''}",
                )

            n_faces_solid = _count_topo(final_shape, TopAbs_FACE)
            t = _step_start("refining")
            with quiet():
                refined, refine_err = _parallel_refine(
                    final_shape, effective_tolerance)
            if refine_err:
                refined = final_shape
                n_faces_after = n_faces_solid
                _step_end(t, f"kept original | {refine_err}")
            else:
                refined_valid, _ = _validate_occ_shape(
                    refined, require_solid=REQUIRE_SOLID_OUTPUT)
                if not refined_valid:
                    refined = final_shape
                    n_faces_after = n_faces_solid
                    _step_end(t, "invalid result | kept original")
                else:
                    n_faces_after = _count_topo(refined, TopAbs_FACE)
                    _step_end(
                        t, f"{n_faces_solid:,} -> {n_faces_after:,} faces")

            n_solids = _count_topo(refined, TopAbs_SOLID)
            free_shells = _count_free_shells(refined)

        if (
            (
                RECONSTRUCT_ANALYTIC_THROUGH_HOLES
                or RECONSTRUCT_ANALYTIC_BLIND_HOLES
            )
            and original_ext not in _IGES_EXTS
            and _has_candidate_hole_wires(refined)
        ):
            t = _step_start("hole fitting")
            faces_before_holes = _count_topo(refined, TopAbs_FACE)
            cylinders_before = _count_surface_type(
                refined, GeomAbs_Cylinder)
            with quiet():
                hole_shape, hole_err = _parallel_reconstruct_holes(
                    refined, effective_tolerance)
            hole_valid, _ = _validate_occ_shape(
                hole_shape, require_solid=REQUIRE_SOLID_OUTPUT)
            cylinders_after = _count_surface_type(
                hole_shape, GeomAbs_Cylinder)
            reconstructed_holes = max(
                0, cylinders_after - cylinders_before)
            if hole_err or not hole_valid or not reconstructed_holes:
                detail = hole_err or "no safe matches"
                _step_end(t, f"kept faceted | {detail}")
            else:
                refined = hole_shape
                n_faces_after = _count_topo(refined, TopAbs_FACE)
                n_solids = _count_topo(refined, TopAbs_SOLID)
                free_shells = _count_free_shells(refined)
                label = (
                    "hole" if reconstructed_holes == 1 else "holes")
                _step_end(
                    t,
                    f"{reconstructed_holes:,} {label} | "
                    f"{faces_before_holes:,} -> "
                    f"{n_faces_after:,} faces",
                )

        t = _step_start("writing")
        try:
            out_kb = _write_step_atomic(refined, output_path, step_schema)
        except Exception as exc:
            _step_fail()
            return False, str(exc)
        _step_end(t, f"{step_schema} | {out_kb:,} KB")

        if GENERATE_PNG_PREVIEW:
            t = _step_start("preview")
            png_path = output_path.with_suffix(".png")
            _red_pct = _reduction_label(reduce_fraction)
            err = _render_preview_atomic(
                output_path, png_path,
                duration=time.perf_counter() - _t_convert,
                display_name=input_path.stem,
                reduction_pct=_red_pct)
            _step_end(t, png_path.name if err is None else f"not created | {err}")

        if t_post_sew is not None:
            _rec_post_sew(
                original_ext, n_faces_sewn,
                time.perf_counter() - t_post_sew)
        if n_tris is not None:
            _rec_total(
                original_ext, n_tris,
                time.perf_counter() - _t_convert)
        return True, {
            "kb": out_kb,
            "faces": n_faces_after,
            "solids": n_solids,
            "open_shells": free_shells,
            "schema": step_schema,
            "tolerance": effective_tolerance,
        }

    except Exception:
        _step_fail()
        return False, traceback.format_exc().strip()


def models_dir() -> Path:
    return _PROJECT_DIR / INPUT_FOLDER_NAME


def _err_line(tb: str) -> str:
    lines = [l for l in tb.splitlines() if l.strip()]
    return lines[-1] if lines else tb


def _box_err(info: str) -> None:
    msg = _err_line(info)
    prefix = "ERROR   FAIL  "
    width = _BOX_CONTENT - len(prefix)
    parts = textwrap.wrap(msg, width=width, break_long_words=True, break_on_hyphens=False) or [""]
    _box_row(f"{prefix}{parts[0]}", lc=R)
    for line in parts[1:]:
        _box_row(f"{' ' * len(prefix)}{line}", lc=R)


def _quick_3mf_tri_count(path: Path):
    with zipfile.ZipFile(str(path)) as zf:
        root_document = _resolve_3mf_part(zf, _find_3mf_model(zf))
        documents = {}

        def load_document(document_path):
            if document_path in documents:
                return documents[document_path]
            with zf.open(document_path) as model_file:
                root = ET.parse(model_file).getroot()
            resources = _xml_child(root, "resources")
            if resources is None:
                raise ValueError(
                    f"3MF model document has no resources: {document_path}")
            objects = {}
            referenced = set()
            for obj in _xml_children(resources, "object"):
                obj_id = obj.get("id")
                if not obj_id:
                    continue
                mesh = _xml_child(obj, "mesh")
                triangles = _xml_child(mesh, "triangles") if mesh is not None else None
                triangle_count = (
                    len(_xml_children(triangles, "triangle"))
                    if triangles is not None else 0
                )
                components = []
                components_element = _xml_child(obj, "components")
                if components_element is not None:
                    for component in _xml_children(
                            components_element, "component"):
                        child_id = component.get("objectid")
                        if not child_id:
                            continue
                        target = _xml_attr(component, "path")
                        if target and document_path != root_document:
                            raise ValueError(
                                "3MF external component paths are only "
                                "valid in the root model document")
                        child_document = (
                            _resolve_3mf_part(zf, target)
                            if target else document_path)
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
                raise ValueError(
                    f"cyclic 3MF component reference involving object {object_id}")
            _, objects, _ = load_document(document_path)
            if object_id not in objects:
                raise ValueError(
                    f"3MF references missing object {object_id} "
                    f"in {document_path}")
            triangle_count, components = objects[object_id]
            return triangle_count + sum(
                count_object(
                    child_document, child_id, stack | {key})
                for child_document, child_id in components
            )

        build = _xml_child(root, "build")
        items = _xml_children(build, "item") if build is not None else []
        if items:
            total = 0
            for item in items:
                object_id = item.get("objectid")
                if not object_id:
                    continue
                target = _xml_attr(item, "path")
                document_path = (
                    _resolve_3mf_part(zf, target)
                    if target else root_document)
                total += count_object(document_path, object_id, set())
            return total or None

        roots = [
            object_id for object_id in root_objects
            if object_id not in root_referenced
        ]
        total = sum(
            count_object(root_document, object_id, set())
            for object_id in roots
        )
        return total or None


def _quick_amf_tri_count(path: Path):
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(str(path)) as zf:
            target = next(
                (name for name in zf.namelist()
                 if name.lower().endswith(".amf")),
                None,
            )
            if target is None:
                raise ValueError(
                    "compressed AMF archive contains no .amf document")
            with zf.open(target) as model_file:
                root = ET.parse(model_file).getroot()
    else:
        root = ET.fromstring(raw)

    object_counts = {}
    for obj in _xml_children(root, "object"):
        object_id = obj.get("id")
        mesh = _xml_child(obj, "mesh")
        if not object_id or mesh is None:
            continue
        object_counts[object_id] = sum(
            len(_xml_children(volume, "triangle"))
            for volume in _xml_children(mesh, "volume")
        )
    instances = [
        instance.get("objectid")
        for constellation in _xml_children(root, "constellation")
        for instance in _xml_children(constellation, "instance")
        if instance.get("objectid")
    ]
    if instances:
        total = 0
        for object_id in instances:
            if object_id not in object_counts:
                raise ValueError(
                    f"AMF references missing object {object_id}")
            total += object_counts[object_id]
    else:
        total = sum(object_counts.values())
    return total or None


def _quick_tri_count(path: Path):
    ext = path.suffix.lower()
    if ext == STL_FILE_EXTENSION:
        return _stl_tri_count(path)
    if ext == THREE_MF_FILE_EXTENSION:
        try:
            return _quick_3mf_tri_count(path)
        except Exception:
            return None
    if ext == OBJ_FILE_EXTENSION:
        try:
            count = 0
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("f ") or line.startswith("f\t"):
                        count += max(1, len(line.split()) - 3)
            return count or None
        except Exception:
            return None
    if ext == AMF_FILE_EXTENSION:
        try:
            return _quick_amf_tri_count(path)
        except Exception:
            return None
    return None


def _automatic_reduction_fractions(path: Path, fractions, triangle_count=None):
    if (
        fractions
        or not AUTO_REDUCTION_ENABLED
        or path.suffix.lower() in _IGES_EXTS
    ):
        return fractions
    count = triangle_count if triangle_count is not None else _quick_tri_count(path)
    if count is None or count <= AUTO_REDUCTION_TARGET_TRIANGLES:
        return fractions
    keep_fraction = AUTO_REDUCTION_TARGET_TRIANGLES / count
    return [max(0.01, min(0.99, keep_fraction))]


def _make_output_path(src: Path, base_dir: Path, fraction) -> Path:
    return base_dir / (
        f"{src.stem} [{_reduction_label(fraction)}]"
        f"{STEP_FILE_EXTENSION}"
    )


def _is_up_to_date(src: Path, dst: Path, force: bool) -> bool:
    if not SKIP_UP_TO_DATE_OUTPUTS or force:
        return False
    try:
        source_stat = src.stat()
        output_stat = dst.stat()
    except OSError:
        return False
    if not dst.is_file() or output_stat.st_size == 0:
        return False
    dependency_paths = (Path(__file__).resolve(), _CONFIG_PATH)
    newest_input_mtime = source_stat.st_mtime_ns
    for dependency in dependency_paths:
        try:
            newest_input_mtime = max(
                newest_input_mtime,
                dependency.stat().st_mtime_ns,
            )
        except OSError:
            return False
    if output_stat.st_mtime_ns < newest_input_mtime:
        return False
    if GENERATE_PNG_PREVIEW:
        preview = dst.with_suffix(".png")
        try:
            preview_stat = preview.stat()
        except OSError:
            return False
        if (
            not preview.is_file()
            or preview_stat.st_size == 0
            or preview_stat.st_mtime_ns < output_stat.st_mtime_ns
        ):
            return False
    return True


def _file_signature(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _scan_supported_files(folder: Path):
    result = {}
    try:
        entries = list(folder.iterdir())
    except OSError:
        return result
    for entry in entries:
        try:
            if entry.is_file() and entry.suffix.lower() in _SUPPORTED_EXTS:
                result[entry.name] = _file_signature(entry)
        except OSError:
            continue
    return result


def _wait_for_stable_file(path: Path, checks: int = 3, interval: float = 0.5,
                          timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        signature = _file_signature(path)
        if signature == previous:
            stable += 1
            if stable >= checks:
                return signature
        else:
            previous = signature
            stable = 0
        time.sleep(interval)
    raise TimeoutError(f"file did not become stable within {timeout:g}s")


def _preload_mesh(src: Path):
    if src.suffix.lower() in _IGES_EXTS:
        return None
    try:
        verts, tris = _load_mesh_arrays(src)
        verts, tris = _repair_mesh_arrays(verts, tris)
        return (verts, tris)
    except Exception:
        return None


def _run_batch(files, n, out_dir, args, reduce_fractions, step_schema):
    ok_n = fail_n = skip_n = 0
    failed_sources = set()
    _lock_fracs = None
    _locked = False
    claimed_outputs = {}

    def claim_output(source, output):
        key = os.path.normcase(str(output.resolve()))
        previous = claimed_outputs.get(key)
        if previous is None:
            claimed_outputs[key] = source
        return key, previous

    def release_output(key, source):
        if claimed_outputs.get(key) == source:
            claimed_outputs.pop(key, None)

    for i, src_file in enumerate(files):
        base_dir = out_dir or src_file.parent
        is_iges = src_file.suffix.lower() in _IGES_EXTS
        _is_interactive = (ASK_FOR_REDUCTION and args.reduce is None
                           and sys.stdin.isatty() and not _locked and not is_iges)
        eff_fractions = None if is_iges else (_lock_fracs if _locked else reduce_fractions)
        eff_fractions = _automatic_reduction_fractions(
            src_file, eff_fractions)

        src_kb = src_file.stat().st_size // BYTES_PER_KB
        size_str = f"{src_kb:,} KB"
        progress = f"[{i+1}/{n}]"
        if is_iges and reduce_fractions:
            print(f"  {Y}!  {_trim(src_file.name)}: reduction ignored for IGES input{X}")

        if _is_interactive and not args.dry_run:
            _box_top()
            _box_input_row(progress, src_file.name, size_str)
            _box_sep()
            _t_read = _step_start("reading")
            n_tris_preview = _quick_tri_count(src_file)
            _read_detail = f"{n_tris_preview:,} triangles" if n_tris_preview is not None else ""
            _step_end(_t_read, _read_detail)
            if n_tris_preview is not None:
                estimate_fraction = (
                    eff_fractions[0] if eff_fractions else None)
                estimate_triangles = (
                    max(4, int(n_tris_preview * estimate_fraction))
                    if estimate_fraction is not None else n_tris_preview)
                _show_early_estimate(
                    estimate_triangles, fmt=src_file.suffix.lower())
            else:
                _box_sep()
            chosen_fracs, lock_all = _reduce_prompt(eff_fractions, n_tris=n_tris_preview, batch=True)
            if lock_all:
                _lock_fracs = chosen_fracs
                _locked = True
            _inter_fracs = chosen_fracs or [None]
            _inter_mesh = _preload_mesh(src_file) if len(_inter_fracs) > 1 else None
            for _i_cfrac, _cfrac in enumerate(_inter_fracs):
                out_file = _make_output_path(
                    src_file, base_dir, _cfrac)
                _claim_key, _claimed_by = claim_output(
                    src_file, out_file)
                _up_to_date = _is_up_to_date(
                    src_file, out_file, args.force)
                if _i_cfrac > 0:
                    _box_sep()
                if _claimed_by is not None:
                    _box_file_row(
                        "SKIP", out_file.name,
                        f"same output as {_claimed_by.name}", DIM)
                    skip_n += 1
                    continue
                if _up_to_date:
                    _skip_kb = out_file.stat().st_size // BYTES_PER_KB
                    _box_file_row(
                        "SKIP", out_file.name,
                        f"up to date | {_skip_kb:,} KB",
                        DIM)
                    skip_n += 1
                    continue
                _t0 = time.perf_counter()
                success, info = convert(src_file, out_file, args.tolerance, _cfrac,
                                        step_schema=step_schema, _suppress_read_step=True,
                                        _mesh_data=_inter_mesh)
                _elapsed = time.perf_counter() - _t0
                _box_sep()
                if success:
                    _box_success_row(out_file.name, info, _elapsed)
                    ok_n += 1
                else:
                    release_output(_claim_key, src_file)
                    failed_sources.add(src_file)
                    _box_err(info)
                    fail_n += 1
            _box_bot()
            print()
            continue

        fracs = list(eff_fractions) if eff_fractions else [None]

        if args.dry_run:
            for _efrac in fracs:
                out_file = _make_output_path(
                    src_file, base_dir, _efrac)
                _claim_key, _claimed_by = claim_output(
                    src_file, out_file)
                if _claimed_by is not None:
                    print(
                        f"  {DIM}-  {_trim(src_file.name)}  "
                        f"same output as {_claimed_by.name}{X}")
                    skip_n += 1
                    continue
                _up_to_date = _is_up_to_date(
                    src_file, out_file, args.force)
                if _up_to_date:
                    print(
                        f"  {DIM}-  {_trim(src_file.name)}  "
                        f"up to date{X}")
                    skip_n += 1
                else:
                    print(f"  {C}->  {_trim(src_file.name)}  {src_kb:,} KB -> {out_file.name}{X}")
                    ok_n += 1
            continue

        _preloaded_mesh = _preload_mesh(src_file) if len(fracs) > 1 else None

        _box_top()
        _box_input_row(progress, src_file.name, size_str)
        _box_sep()

        for _i_efrac, _efrac in enumerate(fracs):
            out_file = _make_output_path(
                src_file, base_dir, _efrac)
            _claim_key, _claimed_by = claim_output(
                src_file, out_file)
            _up_to_date = _is_up_to_date(
                src_file, out_file, args.force)

            if _i_efrac > 0:
                _box_sep()

            if _claimed_by is not None:
                _box_file_row(
                    "SKIP", out_file.name,
                    f"same output as {_claimed_by.name}", DIM)
                skip_n += 1
                continue

            if _up_to_date:
                _skip_kb = out_file.stat().st_size // BYTES_PER_KB
                _box_file_row(
                    "SKIP", out_file.name,
                    f"up to date | {_skip_kb:,} KB",
                    DIM)
                skip_n += 1
                continue

            _t0 = time.perf_counter()
            success, info = convert(src_file, out_file, args.tolerance, _efrac,
                                    step_schema=step_schema, _mesh_data=_preloaded_mesh)
            _elapsed = time.perf_counter() - _t0
            _box_sep()
            if success:
                _box_success_row(out_file.name, info, _elapsed)
                ok_n += 1
            else:
                release_output(_claim_key, src_file)
                failed_sources.add(src_file)
                _box_err(info)
                fail_n += 1

        _box_bot()
        print()

    return ok_n, fail_n, skip_n, failed_sources


def _print_summary(ok_n, fail_n, skip_n, dry_run=False):
    total = ok_n + fail_n + skip_n
    title = "DRY RUN SUMMARY" if dry_run else "CONVERSION SUMMARY"
    ok_label = "WOULD CONVERT" if dry_run else "CONVERTED"
    _box_top()
    _box_row(title, f"{total:,} item{'s' if total != 1 else ''}", lc=B, rc=DIM)
    _box_sep()
    _box_row(ok_label, f"{ok_n:,}", lc=G, rc=G)
    _box_row("SKIPPED", f"{skip_n:,}", lc=DIM, rc=DIM)
    _box_row("FAILED", f"{fail_n:,}", lc=R if fail_n else DIM, rc=R if fail_n else DIM)
    _box_bot()


def main():
    global GENERATE_PNG_PREVIEW, EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION, _PAUSE_MODE
    parser = argparse.ArgumentParser(description="Convert to STEP.")
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument("input", nargs="*",
        help="input file(s); omit to convert everything in the models/ folder")
    parser.add_argument("--output", "-o", metavar="FILE",
        help="output path (single-file mode only)")
    parser.add_argument("--output-dir", "-d", metavar="DIR",
        help="write output files to this directory instead of alongside the source")
    parser.add_argument("--tolerance", "-t", type=_arg_positive_float,
        default=None,
        help=f"sewing tolerance (default: {SEWING_TOLERANCE})")
    parser.add_argument("--reduce", "-r", metavar="PCT", type=_arg_reduction,
        help="reduce mesh by this %% of triangles before converting (0 = off)")
    parser.add_argument("--format", metavar="SCHEMA", default=None,
        choices=["ap203", "ap214", "ap242"],
        help=f"STEP schema: ap203, ap214, ap242 (default: {DEFAULT_STEP_FORMAT})")
    parser.add_argument("--force", "-f", action="store_true",
        help="re-convert files even if the output is already up-to-date")
    parser.add_argument("--dry-run", "--dry", action="store_true",
        help="show what would be converted without actually converting")
    parser.add_argument("--watch", "-w", action="store_true",
        help="after batch conversion, watch the folder and convert new or changed files")
    parser.add_argument("--preview", action=argparse.BooleanOptionalAction, default=GENERATE_PNG_PREVIEW,
        help=f"generate a .png preview alongside each .stp (default: {GENERATE_PNG_PREVIEW})")
    parser.add_argument(
        "--experimental-parametric",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "try experimental exact linear-extrusion reconstruction "
            f"(default: {EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION})"
        ),
    )
    parser.add_argument("--pause", action=argparse.BooleanOptionalAction, default=None,
        help="pause before exiting (default: only in an interactive terminal)")
    args = parser.parse_args()
    explicit_geometry_options = (
        args.tolerance is not None
        or args.format is not None
        or args.experimental_parametric is not None
    )
    if args.tolerance is None:
        args.tolerance = SEWING_TOLERANCE
    selected_format = args.format or DEFAULT_STEP_FORMAT
    if explicit_geometry_options:
        args.force = True
    GENERATE_PNG_PREVIEW = args.preview
    if args.experimental_parametric is not None:
        EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION = args.experimental_parametric
    _PAUSE_MODE = args.pause

    if args.output and len(args.input) != 1:
        parser.error("--output requires exactly one input file")
    if args.output and args.output_dir:
        parser.error("--output and --output-dir cannot be used together")
    if args.watch and args.input:
        parser.error("--watch operates on the models folder and cannot be combined with input files")
    if args.watch and args.dry_run:
        parser.error("--watch cannot be combined with --dry-run")
    if args.output and args.reduce is not None and len(args.reduce) > 1:
        parser.error("--output cannot be combined with multiple reduction percentages")

    if args.reduce is not None:
        reduce_fractions = args.reduce
    elif isinstance(DEFAULT_REDUCTION_PERCENT, str):
        reduce_fractions = _parse_reduction(DEFAULT_REDUCTION_PERCENT, strict=True)
    elif 0 < DEFAULT_REDUCTION_PERCENT < 100:
        reduce_fractions = [((100.0 - DEFAULT_REDUCTION_PERCENT) / 100.0)]
    else:
        reduce_fractions = None
    if args.output and reduce_fractions and len(reduce_fractions) > 1:
        parser.error("--output cannot be combined with multiple default reductions")
    step_schema = _STEP_SCHEMAS[selected_format]

    print()
    _w = _BOX_CONTENT + 4
    print(f"  {C}{B}+{'=' * _w}+{X}")
    print(f"  {C}{B}|{f'2STEP-Converter {VERSION}':^{_w}}|{X}")
    print(f"  {C}{B}+{'=' * _w}+{X}")
    print()

    if _cfg_warnings:
        for _w_msg in _cfg_warnings:
            print(f"  {Y}!  {_w_msg}{X}")
        print()

    out_dir = Path(args.output_dir).resolve() if args.output_dir else None

    if len(args.input) > 1:
        files = []
        invalid = []
        for p in args.input:
            fp = Path(p).resolve()
            if not fp.is_file():
                invalid.append(f"file not found: {fp}")
            elif fp.suffix.lower() not in _SUPPORTED_EXTS:
                invalid.append(f"unsupported format: {fp.name}")
            else:
                files.append(fp)
        if invalid:
            parser.error("; ".join(invalid))
        n = len(files)
        print(f"  {n} file{'s' if n > 1 else ''} to convert\n")
        ok_n, fail_n, skip_n, _ = _run_batch(
            files, n, out_dir, args, reduce_fractions, step_schema)
        _print_summary(ok_n, fail_n, skip_n, dry_run=args.dry_run)
        print()
        _pause()
        sys.exit(0 if fail_n == 0 else 1)

    if len(args.input) == 1:
        input_path = Path(args.input[0]).resolve()
        if not input_path.is_file():
            print(f"  {R}[ERROR]{X} File not found: {input_path}\n")
            _pause()
            sys.exit(1)
        if input_path.suffix.lower() not in _SUPPORTED_EXTS:
            parser.error(f"unsupported format: {input_path.name}")
        if args.output and Path(args.output).resolve() == input_path:
            parser.error("--output must not overwrite the input file")

        _single_interactive = (ASK_FOR_REDUCTION and args.reduce is None
                               and sys.stdin.isatty()
                               and input_path.suffix.lower() not in _IGES_EXTS)
        _single_base_dir = out_dir or input_path.parent
        src_kb = input_path.stat().st_size // BYTES_PER_KB
        size_str = f"{src_kb:,} KB"
        single_fractions = _automatic_reduction_fractions(
            input_path, reduce_fractions)
        if input_path.suffix.lower() in _IGES_EXTS:
            if reduce_fractions:
                print(f"  {Y}!  reduction ignored for IGES input{X}\n")
            fractions = [None]
        else:
            fractions = list(single_fractions) if single_fractions else [None]

        if args.dry_run:
            for _dfrac in fractions:
                _dout = (Path(args.output).resolve() if args.output
                         else _make_output_path(
                             input_path, _single_base_dir, _dfrac))
                if _is_up_to_date(
                        input_path, _dout, args.force):
                    print(
                        f"  {DIM}-  {_trim(input_path.name)}  "
                        f"up to date{X}\n")
                else:
                    print(f"  {C}->  {_trim(input_path.name)}  {src_kb:,} KB -> {_dout.name}{X}\n")
            _pause()
            sys.exit(0)

        _box_top()
        _box_input_row("[1/1]", input_path.name, size_str)
        _box_sep()
        if _single_interactive:
            _t_read = _step_start("reading")
            n_tris_preview = _quick_tri_count(input_path)
            _step_end(_t_read, f"{n_tris_preview:,} triangles" if n_tris_preview is not None else "")
            if n_tris_preview is not None:
                estimate_fraction = (
                    single_fractions[0] if single_fractions else None)
                estimate_triangles = (
                    max(4, int(n_tris_preview * estimate_fraction))
                    if estimate_fraction is not None else n_tris_preview)
                _show_early_estimate(
                    estimate_triangles, fmt=input_path.suffix.lower())
            else:
                _box_sep()
            chosen, _ = _reduce_prompt(
                single_fractions, n_tris=n_tris_preview)
            fractions = list(chosen) if chosen else [None]

        if args.output and len(fractions) > 1:
            _box_err("--output cannot be used with multiple interactive reductions")
            _box_bot()
            print()
            _pause()
            sys.exit(2)

        outputs = [
            Path(args.output).resolve() if args.output
            else _make_output_path(
                input_path, _single_base_dir, fraction)
            for fraction in fractions
        ]
        pending = [
            (fraction, output)
            for fraction, output in zip(fractions, outputs)
            if not _is_up_to_date(
                input_path, output, args.force)
        ]
        preloaded_mesh = (
            _preload_mesh(input_path)
            if len(pending) > 1 and input_path.suffix.lower() not in _IGES_EXTS
            else None
        )

        any_fail = False
        success_count = skip_count = fail_count = 0
        for index, (fraction, output_path) in enumerate(zip(fractions, outputs)):
            if index > 0:
                _box_sep()
            if _is_up_to_date(
                    input_path, output_path, args.force):
                out_kb = output_path.stat().st_size // BYTES_PER_KB
                _box_file_row(
                    "SKIP", output_path.name,
                    f"up to date | {out_kb:,} KB",
                    DIM)
                skip_count += 1
                continue
            started = time.perf_counter()
            success, info = convert(
                input_path, output_path, args.tolerance, fraction,
                step_schema=step_schema, _mesh_data=preloaded_mesh)
            elapsed = time.perf_counter() - started
            _box_sep()
            if success:
                _box_success_row(output_path.name, info, elapsed)
                success_count += 1
            else:
                _box_err(info)
                any_fail = True
                fail_count += 1

        _box_bot()
        print()
        _print_summary(success_count, fail_count, skip_count)
        print()
        _pause()
        sys.exit(1 if any_fail else 0)

    folder = models_dir()
    folder.mkdir(parents=True, exist_ok=True)

    files = sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in _SUPPORTED_EXTS)
    if not files:
        print(f"  No supported files found in {INPUT_FOLDER_NAME}\\\n")
        ok_n = fail_n = skip_n = 0
        initial_failed_sources = set()
        if not args.watch:
            _pause()
            sys.exit(0)
    else:
        n = len(files)
        print(
            f"  {n} file{'s' if n > 1 else ''} found in "
            f"{C}{INPUT_FOLDER_NAME}\\{X}\n")
        (
            ok_n,
            fail_n,
            skip_n,
            initial_failed_sources,
        ) = _run_batch(
            files, n, out_dir, args, reduce_fractions, step_schema)
        _print_summary(ok_n, fail_n, skip_n, dry_run=args.dry_run)
        print()

    if args.watch and not args.dry_run:
        known = _scan_supported_files(folder)
        unresolved_failures = {
            path.name for path in initial_failed_sources}
        for failed_name in unresolved_failures:
            known.pop(failed_name, None)
        print(f"  {DIM}Watching {C}{INPUT_FOLDER_NAME}\\{X}{DIM}  Ctrl+C to stop{X}\n")
        try:
            while True:
                time.sleep(2)
                current = _scan_supported_files(folder)
                changed_names = [
                    name for name, signature in current.items()
                    if known.get(name) != signature
                ]
                missing_names = (
                    set(known) | unresolved_failures
                ) - set(current)
                for missing_name in missing_names:
                    known.pop(missing_name, None)
                    unresolved_failures.discard(missing_name)
                for name in sorted(changed_names):
                    src = folder / name
                    try:
                        stable_signature = _wait_for_stable_file(src)
                    except (OSError, TimeoutError) as exc:
                        print(f"  {R}X  {name}: {exc}{X}")
                        unresolved_failures.add(name)
                        continue
                    src_kb = stable_signature[0] // BYTES_PER_KB
                    size_str = f"{src_kb:,} KB"
                    watch_fractions = _automatic_reduction_fractions(
                        src, reduce_fractions)
                    _wfracs = (
                        [None] if src.suffix.lower() in _IGES_EXTS
                        else (
                            list(watch_fractions)
                            if watch_fractions else [None]
                        )
                    )
                    _watch_mesh = _preload_mesh(src) if len(_wfracs) > 1 else None
                    all_ok = True
                    _box_top()
                    _box_input_row("[changed]", src.name, size_str)
                    _box_sep()
                    for _i_wfrac, _wfrac in enumerate(_wfracs):
                        dst = _make_output_path(
                            src, out_dir or folder, _wfrac)
                        if _i_wfrac > 0:
                            _box_sep()
                        if _is_up_to_date(
                                src, dst, args.force):
                            out_kb = dst.stat().st_size // BYTES_PER_KB
                            _box_file_row(
                                "SKIP", dst.name,
                                f"up to date | {out_kb:,} KB",
                                DIM)
                            continue
                        _t0 = time.perf_counter()
                        success, info = convert(src, dst, args.tolerance, _wfrac,
                                                step_schema=step_schema,
                                                _mesh_data=_watch_mesh)
                        elapsed = time.perf_counter() - _t0
                        _box_sep()
                        if success:
                            _box_success_row(dst.name, info, elapsed)
                        else:
                            _box_err(info)
                            all_ok = False
                            unresolved_failures.add(name)
                    _box_bot()
                    print()
                    if all_ok:
                        known[name] = stable_signature
                        unresolved_failures.discard(name)
        except KeyboardInterrupt:
            print(f"\n  {DIM}Watch stopped.{X}\n")
        sys.exit(0 if not unresolved_failures else 1)
    else:
        _pause()
        sys.exit(0 if fail_n == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        _pause("\n  Press Enter to exit...")
        sys.exit(1)
