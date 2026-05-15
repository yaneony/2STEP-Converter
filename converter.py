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
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)

STD_OUTPUT_HANDLE  = -11
CONSOLE_MODE_FLAGS = 7
BYTES_PER_KB       = 1024
NAME_TRIM_WIDTH    = 62
SEPARATOR_WIDTH    = 32

_CONFIG_DEFAULTS = {
    "DEFAULT_TOLERANCE":  0.01,
    "DEFAULT_REDUCE":     0,
    "REDUCE_INTERACTIVE": True,
    "SKIP_EXISTING":      True,
    "ANGULAR_TOLERANCE":  0.01,
    "SEW_TIMEOUT":        1800,
    "DEFAULT_FORMAT":     "ap203",
    "GENERATE_PREVIEW":   True,
    "MODELS_DIR_NAME":    "models",
    "STL_EXT":            ".stl",
    "TMF_EXT":            ".3mf",
    "OBJ_EXT":            ".obj",
    "IGS_EXT":            ".igs",
    "AMF_EXT":            ".amf",
    "STP_EXT":            ".stp",
}

_CONFIG_PATH = _DATA_DIR / "config.json"
if not _CONFIG_PATH.exists():
    _CONFIG_PATH.write_text(json.dumps(_CONFIG_DEFAULTS, indent=4), encoding="utf-8")

_cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
for _k, _default in _CONFIG_DEFAULTS.items():
    globals()[_k] = _cfg.get(_k, _default)

_cfg_warnings: list[str] = []
for _k, _lo, _hi in [
    ("DEFAULT_TOLERANCE",  0.000000001, None),
    ("ANGULAR_TOLERANCE",  0.000000001, None),
]:
    _v = _cfg.get(_k)
    if _v is not None and not isinstance(_v, (int, float)):
        _cfg_warnings.append(f"config: {_k} must be a number (got {type(_v).__name__!r})")
    elif _v is not None:
        if _lo is not None and _v < _lo:
            _cfg_warnings.append(f"config: {_k} must be ≥ {_lo} (got {_v})")
        if _hi is not None and _v > _hi:
            _cfg_warnings.append(f"config: {_k} must be ≤ {_hi} (got {_v})")
_dr = _cfg.get("DEFAULT_REDUCE")
if _dr is not None:
    if isinstance(_dr, str):
        if not any(True for _p in _dr.split(',') if _p.strip().rstrip('%').replace('.', '', 1).isdigit()):
            _cfg_warnings.append(f"config: DEFAULT_REDUCE string contains no valid percentages (got {_dr!r})")
    elif isinstance(_dr, (int, float)):
        if _dr < 0 or _dr > 100:
            _cfg_warnings.append(f"config: DEFAULT_REDUCE must be 0-100 (got {_dr})")
    else:
        _cfg_warnings.append(f"config: DEFAULT_REDUCE must be a number or string (got {type(_dr).__name__!r})")
for _bool_k in ("REDUCE_INTERACTIVE", "SKIP_EXISTING", "GENERATE_PREVIEW"):
    _b = _cfg.get(_bool_k)
    if _b is not None and not isinstance(_b, bool):
        _cfg_warnings.append(f"config: {_bool_k} must be true or false (got {_b!r})")
_st = _cfg.get("SEW_TIMEOUT")
if _st is not None:
    if not isinstance(_st, (int, float)) or isinstance(_st, bool):
        _cfg_warnings.append(f"config: SEW_TIMEOUT must be a positive number of seconds (got {_st!r})")
        SEW_TIMEOUT = 1800
    elif _st <= 0:
        _cfg_warnings.append(f"config: SEW_TIMEOUT must be > 0 (got {_st})")
        SEW_TIMEOUT = 1800
_df = _cfg.get("DEFAULT_FORMAT")
if _df is not None and _df not in ("ap203", "ap214", "ap242"):
    _cfg_warnings.append(f"config: DEFAULT_FORMAT must be ap203, ap214, or ap242 (got {_df!r})")
    DEFAULT_FORMAT = "ap203"
for _ext_k in ("STL_EXT", "TMF_EXT", "OBJ_EXT", "IGS_EXT", "AMF_EXT", "STP_EXT"):
    _ev = _cfg.get(_ext_k)
    if _ev is not None and (not isinstance(_ev, str) or not _ev.startswith(".")):
        _cfg_warnings.append(f"config: {_ext_k} must start with '.' (got {_ev!r})")

try:
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE), CONSOLE_MODE_FLAGS)
except Exception:
    pass

G   = '\033[92m'
R   = '\033[91m'
Y   = '\033[93m'
C   = '\033[96m'
DIM = '\033[2m'
B   = '\033[1m'
X   = '\033[0m'

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
        from OCC.Core.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCC.Core.Interface import Interface_Static
        from OCC.Core.IGESControl import IGESControl_Reader
        from OCC.Core.IFSelect import IFSelect_RetDone, IFSelect_RetError, IFSelect_RetFail
        from OCC.Core.TopoDS import TopoDS_Shape
        from OCC.Core.TopExp import TopExp_Explorer
        from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_SHELL
except Exception as e:
    print(f"\n  {R}[ERROR]{X} Failed to load OpenCASCADE: {e}\n")
    traceback.print_exc()
    input("\n  Press Enter to exit...")
    sys.exit(1)


_3MF_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_STL_TRI = struct.Struct("<12fH")
_IGES_EXTS = {IGS_EXT, ".iges"}
_SUPPORTED_EXTS = {STL_EXT, TMF_EXT, OBJ_EXT, AMF_EXT, *_IGES_EXTS}

_BOX_CONTENT = 72
_BOX_LABEL   = 12
_BOX_TIME    = 6
_BOX_DETAIL  = _BOX_CONTENT - _BOX_LABEL - 2 - _BOX_TIME


def _mesh_to_shape(verts, tris):
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


def _repair_mesh_arrays(verts, tris):
    import numpy as np
    try:
        import open3d as o3d
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(np.array(verts, dtype=np.float64))
        o3d_mesh.triangles = o3d.utility.Vector3iVector(np.array(tris, dtype=np.int32))
        o3d_mesh.remove_duplicated_vertices()
        o3d_mesh.remove_duplicated_triangles()
        o3d_mesh.remove_degenerate_triangles()
        return (np.asarray(o3d_mesh.vertices, dtype=np.float32),
                np.asarray(o3d_mesh.triangles, dtype=np.int32))
    except Exception:
        return np.array(verts, dtype=np.float32), np.array(tris, dtype=np.int32)


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
        if 84 + n * 50 == path.stat().st_size:
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
            f"breptools.Read(s,'{in_fwd}',b);"
            "f=ShapeFix_Shape(s);f.Perform();r=f.Shape();"
            f"breptools.Write(r if not r.IsNull() else s,'{out_fwd}')"
        )
    return _run_brep_subprocess(shape, make_script)


def _parallel_refine(shape, tolerance):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools\n"
            "from OCC.Core.BRep import BRep_Builder\n"
            "from OCC.Core.TopoDS import TopoDS_Shape\n"
            "from OCC.Core.ShapeUpgrade import ShapeUpgrade_UnifySameDomain\n"
            "b=BRep_Builder();s=TopoDS_Shape()\n"
            f"breptools.Read(s,'{in_fwd}',b)\n"
            "r=s\n"
            "for _ in range(2):\n"
            f"  u=ShapeUpgrade_UnifySameDomain(r,True,True,True)\n"
            f"  u.SetLinearTolerance({tolerance})\n"
            f"  u.SetAngularTolerance({ANGULAR_TOLERANCE})\n"
            "  u.Build()\n"
            "  n=u.Shape()\n"
            "  r=n if not n.IsNull() else r\n"
            f"breptools.Write(r,'{out_fwd}')\n"
        )
    return _run_brep_subprocess(shape, make_script)


def _subprocess_sew(shape, tolerance):
    def make_script(in_fwd, out_fwd):
        return (
            "from OCC.Core.BRepTools import breptools;"
            "from OCC.Core.BRep import BRep_Builder;"
            "from OCC.Core.TopoDS import TopoDS_Shape;"
            "from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Sewing;"
            "b=BRep_Builder();s=TopoDS_Shape();"
            f"breptools.Read(s,'{in_fwd}',b);"
            f"sew=BRepBuilderAPI_Sewing({tolerance});"
            "sew.Add(s);sew.Perform();"
            "r=sew.SewedShape();"
            f"breptools.Write(r if not r.IsNull() else s,'{out_fwd}')"
        )
    return _run_brep_subprocess(shape, make_script, timeout=SEW_TIMEOUT, fallback=TopoDS_Shape())


_step_open   = [False]
_timer_label = [""]
_timer_t0    = [0.0]
_timer_stop  = threading.Event()
_timer_th    = [None]
_TIMER_PAD   = _BOX_DETAIL + _BOX_TIME + 2


def _box_top():
    print(f"  {DIM}┌{'─' * (_BOX_CONTENT + 4)}┐{X}")


def _box_sep():
    print(f"  {DIM}├{'─' * (_BOX_CONTENT + 4)}┤{X}")


def _box_bot():
    print(f"  {DIM}└{'─' * (_BOX_CONTENT + 4)}┘{X}")


def _box_row(left: str, right: str = "", lc: str = "", rc: str = "") -> None:
    max_left = _BOX_CONTENT - len(right) - (1 if right else 0)
    if len(left) > max_left:
        left = left[:max_left - 3] + "..."
    gap = _BOX_CONTENT - len(left) - len(right)
    print(f"  {DIM}│{X}  {lc}{left}{X}{' ' * gap}{rc}{right}{X}  {DIM}│{X}")


def _stop_timer() -> None:
    _timer_stop.set()
    t = _timer_th[0]
    if t is not None:
        t.join(timeout=1.0)
        _timer_th[0] = None


def _step_start(label: str) -> float:
    _step_open[0]   = True
    _timer_label[0] = label
    t0 = time.perf_counter()
    _timer_t0[0]    = t0
    _timer_stop.clear()
    print(f"  {DIM}│{X}  {DIM}{label:<{_BOX_LABEL}}", end="", flush=True)

    def _tick():
        while not _timer_stop.wait(0.5):
            t_str = _fmt_time(time.perf_counter() - _timer_t0[0])
            line = f"\r  {DIM}│{X}  {DIM}{label:<{_BOX_LABEL}}{DIM}{t_str:>{_TIMER_PAD}}{X}\033[K"
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
    print(f"\r  {DIM}│{X}  {DIM}{_timer_label[0]:<{_BOX_LABEL}}{X}{C}{detail:<{_BOX_DETAIL}}{X}  {Y}{time_str:>{_BOX_TIME}}{X}  {DIM}│{X}")


def _step_fail() -> None:
    if _step_open[0]:
        _stop_timer()
        _step_open[0] = False
        print(f"\r  {DIM}│{X}  {DIM}{_timer_label[0]:<{_BOX_LABEL}}{X}{R}{'error':<{_BOX_DETAIL}}{X}  {R}{'failed':>{_BOX_TIME}}{X}  {DIM}│{X}")


def _trim(name: str, width: int = NAME_TRIM_WIDTH) -> str:
    return name if len(name) <= width else name[:width - 3] + "..."


_EST_HISTORY = _DATA_DIR / "estimator.json"
_old_est = Path(__file__).parent / "estimator.json"
if not _EST_HISTORY.exists() and _old_est.exists():
    _EST_HISTORY.write_bytes(_old_est.read_bytes())
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
    try:
        _EST_HISTORY.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
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


def _est_time_faces(n_faces: int, fmt=None):
    data  = _est_load()
    x     = float(n_faces)
    n_all = int(round(data.get("f:_all", {}).get("XtX", [[0]*3 for _ in range(3)])[2][2]))
    if fmt:
        key = f"f:{fmt}"
        if key in data:
            pred, n, r2 = _bucket_predict(data[key], x)
            if pred is not None:
                return pred, n, r2
    if "f:_all" in data:
        pred, n, r2 = _bucket_predict(data["f:_all"], x)
        if pred is not None:
            return pred, n, r2
    return None, n_all, 0.0


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
            f"~{_fmt_time(est)}  estimated",
            f"{n_samples} records · {int(r2 * 100)}%",
            lc=Y, rc=DIM,
        )
    else:
        _box_row(
            f"learning...  {n_samples} of {_EST_MIN} conversions recorded",
            lc=DIM,
        )
    _box_sep()


def _reduce_prompt(default_fractions, n_tris=None, batch=False):
    if isinstance(default_fractions, float):
        default_fractions = [default_fractions]
    if default_fractions:
        pct_str = ",".join(str(int(round((1.0 - f) * 100))) if f is not None else "0" for f in default_fractions)
    else:
        pct_str = "0"
    hint = ""
    if n_tris is not None and default_fractions and len(default_fractions) == 1 and default_fractions[0] is not None:
        hint = f"  {DIM}({n_tris:,} → ~{max(1, int(n_tris * default_fractions[0])):,}){X}"
    batch_hint = f"  {DIM}[!N,N = all files]{X}" if batch else ""
    raw = input(f"  {DIM}│{X}  {DIM}{'reduce':<{_BOX_LABEL}}{X}{Y}{pct_str}%{X}{hint}{batch_hint}  ").strip()
    _box_sep()
    lock_all = raw.startswith("!")
    if lock_all:
        raw = raw[1:].strip()
    fractions = _parse_reduction(raw) if raw else default_fractions
    return fractions, lock_all


def _parse_reduction(value: str):
    if not value:
        return None
    results = []
    seen = set()
    for part in value.split(','):
        part = part.strip().rstrip('%')
        if not part:
            continue
        try:
            pct = float(part)
            if pct == 0:
                if None not in seen:
                    seen.add(None)
                    results.append(None)
            elif 0 < pct < 100:
                frac = (100.0 - pct) / 100.0
                key = int(round(pct))
                if key not in seen:
                    seen.add(key)
                    results.append(frac)
            elif pct == 100:
                if 100 not in seen:
                    seen.add(100)
                    results.append(0.001)
        except ValueError:
            pass
    results.sort(key=lambda f: 0 if f is None else int(round((1.0 - f) * 100)))
    return results if results else None


def _load_mesh_arrays(path: Path):
    import numpy as np
    ext = path.suffix.lower()

    if ext == STL_EXT:
        with open(path, 'rb') as f:
            f.seek(80)
            n = struct.unpack('<I', f.read(4))[0]
            data = f.read()
        if len(data) == n * 50:
            stl_dt = np.dtype([('n', np.float32, (3,)), ('v0', np.float32, (3,)),
                                ('v1', np.float32, (3,)), ('v2', np.float32, (3,)),
                                ('attr', np.uint16)])
            tris = np.frombuffer(data, dtype=stl_dt)
            verts = np.stack([tris['v0'], tris['v1'], tris['v2']], axis=1).reshape(-1, 3)
            return verts.astype(np.float32), np.arange(n * 3, dtype=np.int32).reshape(n, 3)
        verts = []
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.split()
                if p and p[0] == 'vertex':
                    verts.append([float(p[1]), float(p[2]), float(p[3])])
        v = np.array(verts, dtype=np.float32)
        return v, np.arange(len(v), dtype=np.int32).reshape(-1, 3)

    if ext == TMF_EXT:
        with zipfile.ZipFile(str(path)) as zf:
            with zf.open(_find_3mf_model(zf)) as f:
                root = ET.parse(f).getroot()
        resources = root.find(f"{{{_3MF_NS}}}resources")
        verts_all, tris_all, offset = [], [], 0
        for obj in resources.findall(f"{{{_3MF_NS}}}object"):
            mesh_el = obj.find(f"{{{_3MF_NS}}}mesh")
            if mesh_el is None:
                continue
            ve = mesh_el.find(f"{{{_3MF_NS}}}vertices")
            te = mesh_el.find(f"{{{_3MF_NS}}}triangles")
            if ve is None or te is None:
                continue
            verts = [(float(v.get('x')), float(v.get('y')), float(v.get('z')))
                     for v in ve.findall(f"{{{_3MF_NS}}}vertex")]
            tris_all += [(int(t.get('v1')) + offset, int(t.get('v2')) + offset,
                          int(t.get('v3')) + offset)
                         for t in te.findall(f"{{{_3MF_NS}}}triangle")]
            verts_all += verts
            offset += len(verts)
        return np.array(verts_all, dtype=np.float32), np.array(tris_all, dtype=np.int32)

    if ext == OBJ_EXT:
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
                    for i in range(1, len(idx) - 1):
                        tris.append([idx[0], idx[i], idx[i + 1]])
        return np.array(verts, dtype=np.float32), np.array(tris, dtype=np.int32)

    if ext == AMF_EXT:
        raw = path.read_bytes()
        if raw[:2] == b'PK':
            with zipfile.ZipFile(str(path)) as zf:
                names = zf.namelist()
                target = next((n for n in names if n.endswith('.amf')), names[0])
                with zf.open(target) as f:
                    root = ET.parse(f).getroot()
        else:
            root = ET.fromstring(raw)
        verts_all, tris_all, offset = [], [], 0
        for obj in root.findall('object'):
            mesh_el = obj.find('mesh')
            if mesh_el is None:
                continue
            ve = mesh_el.find('vertices')
            if ve is None:
                continue
            verts = []
            for vertex in ve.findall('vertex'):
                coords = vertex.find('coordinates')
                if coords is None:
                    continue
                verts.append([float(coords.findtext('x', '0')),
                               float(coords.findtext('y', '0')),
                               float(coords.findtext('z', '0'))])
            for volume in mesh_el.findall('volume'):
                for tri in volume.findall('triangle'):
                    tris_all.append([int(tri.findtext('v1')) + offset,
                                     int(tri.findtext('v2')) + offset,
                                     int(tri.findtext('v3')) + offset])
            verts_all += verts
            offset += len(verts)
        return np.array(verts_all, dtype=np.float32), np.array(tris_all, dtype=np.int32)

    raise ValueError(f"unsupported format for reduction: {path.suffix}")


def _reduce_mesh_arrays(verts, faces, keep_fraction):
    import numpy as np
    n_before = len(faces)
    if n_before == 0:
        raise ValueError("mesh is empty")

    unique_v, inv = np.unique(np.round(verts, 6), axis=0, return_inverse=True)
    new_faces = inv[faces.astype(np.int64)]
    good = ((new_faces[:, 0] != new_faces[:, 1]) &
            (new_faces[:, 1] != new_faces[:, 2]) &
            (new_faces[:, 0] != new_faces[:, 2]))
    verts = unique_v.astype(np.float32)
    faces = new_faces[good].astype(np.int32)
    if len(faces) == 0:
        raise ValueError("mesh has no valid faces after vertex merging")

    target_count = max(4, int(n_before * keep_fraction))
    verts_out = faces_out = None

    try:
        import open3d as o3d
        o3d_mesh = o3d.geometry.TriangleMesh()
        o3d_mesh.vertices = o3d.utility.Vector3dVector(verts)
        o3d_mesh.triangles = o3d.utility.Vector3iVector(faces)
        simplified = o3d_mesh.simplify_quadric_decimation(target_count)
        verts_out = np.asarray(simplified.vertices, dtype=np.float32)
        faces_out = np.asarray(simplified.triangles, dtype=np.int32)
        if len(faces_out) == 0:
            raise ValueError("empty")
    except Exception:
        pass

    if verts_out is None or len(faces_out) == 0:
        try:
            import trimesh
            import trimesh.simplify
            mesh_t = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
            result = trimesh.simplify.simplify_quadric_decimation(mesh_t, target_count)
            if result is not None and len(result.faces) > 0:
                verts_out = np.array(result.vertices, dtype=np.float32)
                faces_out = np.array(result.faces, dtype=np.int32)
        except Exception:
            pass

    if verts_out is None or len(faces_out) == 0:
        import fast_simplification
        target_reduction = max(0.01, min(0.99, 1.0 - keep_fraction))
        verts_out, faces_out = fast_simplification.simplify(
            verts, faces, target_reduction, agg=2
        )

    return verts_out, faces_out, n_before, len(faces_out)


_STEP_SCHEMAS = {"ap203": "AP203", "ap214": "AP214IS", "ap242": "AP242DIS"}


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

            n_faces = _count_topo(shape, TopAbs_FACE)
            n_edges = _count_topo(shape, TopAbs_EDGE)
            out_kb  = step_path.stat().st_size // 1024
            lines   = [
                (display_name or step_path.stem,                                         (255, 255, 255, 220)),
                (f"reduction {reduction_pct or 0}%",                                       (180, 190, 220, 155)),
                (f"{n_faces:,} faces  ·  {n_edges:,} edges",                            (180, 190, 220, 170)),
                (f"{out_kb:,} KB" + (f"  ·  {_fmt_time(duration)}" if duration else ""), (180, 190, 220, 130)),
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


def convert(input_path: Path, output_path: Path, tolerance: float = DEFAULT_TOLERANCE,
            reduce_fraction=None, interactive: bool = False,
            batch: bool = False, _chosen_out: dict = None,
            step_schema: str = "AP203", _suppress_read_step: bool = False,
            _mesh_data=None):
    n_verts = n_tris = None
    verts_np = tris_np = None
    shape = None
    _t_convert = time.perf_counter()

    try:
        ext = input_path.suffix.lower()
        original_ext = ext

        if not _suppress_read_step:
            t = _step_start("reading")
        if ext in _IGES_EXTS:
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
                _step_end(t, " · ".join(read_parts))
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
            if _chosen_out is not None:
                _chosen_out['mesh_data'] = (verts_np, tris_np)
            if reduce_fraction is None:
                shape = _mesh_to_shape(verts_np.tolist(), tris_np.tolist())
                if shape.IsNull():
                    if not _suppress_read_step:
                        _step_fail()
                    return False, "input produced an empty shape"
            if not _suppress_read_step:
                _step_end(t, f"{n_verts:,} vertices · {n_tris:,} triangles")

        if interactive and original_ext not in _IGES_EXTS:
            _box_sep()
            _default = [reduce_fraction] if reduce_fraction is not None else None
            _fracs, _lock_all = _reduce_prompt(_default, n_tris=n_tris, batch=batch)
            reduce_fraction = _fracs[0] if _fracs else None
            if _chosen_out is not None:
                _chosen_out['fraction'] = reduce_fraction
                _chosen_out['fractions'] = _fracs

        if reduce_fraction is not None and original_ext not in _IGES_EXTS:
            t = _step_start("reducing")
            try:
                s_verts, s_tris, n_before, n_after = _reduce_mesh_arrays(
                    verts_np, tris_np, reduce_fraction)
                red_pct = int(round((1.0 - n_after / n_before) * 100))
                _new_shape = _mesh_to_shape(s_verts.tolist(), s_tris.tolist())
                if _new_shape.IsNull():
                    raise ValueError("reduced mesh produced an empty shape")
                _step_end(t, f"{n_before:,} → {n_after:,} triangles  (-{red_pct}%)")
                shape = _new_shape
                n_tris = n_after
            except Exception as e:
                _step_end(t, f"skipped  ({e})")
                if shape is None:
                    shape = _mesh_to_shape(verts_np.tolist(), tris_np.tolist())

        if shape is None and verts_np is not None:
            shape = _mesh_to_shape(verts_np.tolist(), tris_np.tolist())
        if shape is None or shape.IsNull():
            return False, "input produced an empty shape"

        t = _step_start("sewing")
        with quiet():
            sewn, sew_err = _subprocess_sew(shape, tolerance)
        if sewn.IsNull():
            _step_fail()
            detail = f": {sew_err}" if sew_err else ""
            return False, f"sewing failed{detail}"
        n_shells     = _count_topo(sewn, TopAbs_SHELL)
        n_faces_sewn = _count_topo(sewn, TopAbs_FACE)
        _step_end(t, f"{n_shells:,} shell{'s' if n_shells != 1 else ''}")

        t_post_sew = time.perf_counter()
        _show_post_sew_estimate(n_faces_sewn, fmt=original_ext)

        t = _step_start("fixing")
        with quiet():
            fixed, _ = _parallel_fix(sewn)
        n_faces_out = _count_topo(fixed, TopAbs_FACE)
        _step_end(t, f"{n_faces_sewn:,} to {n_faces_out:,} faces")

        t = _step_start("refining")
        with quiet():
            refined, _ = _parallel_refine(fixed, tolerance)
        n_faces_after = _count_topo(refined, TopAbs_FACE)
        _step_end(t, f"{n_faces_out:,} to {n_faces_after:,} faces")

        t = _step_start("writing")
        Interface_Static.SetCVal("write.step.schema", step_schema)
        Interface_Static.SetCVal("write.step.product.name", "")
        Interface_Static.SetCVal("write.step.assembly", "0")
        writer = STEPControl_Writer()
        with quiet():
            ts = writer.Transfer(refined, STEPControl_AsIs)
            ws = writer.Write(output_path.as_posix())
        if ts in (IFSelect_RetError, IFSelect_RetFail) or \
           ws in (IFSelect_RetError, IFSelect_RetFail):
            _step_fail()
            return False, "STEP writer failed"
        if not output_path.exists() or output_path.stat().st_size == 0:
            _step_fail()
            return False, "output file is missing or empty"
        out_kb = output_path.stat().st_size // BYTES_PER_KB
        _step_end(t, f"{out_kb:,} KB")

        if GENERATE_PREVIEW:
            t = _step_start("preview")
            png_path = output_path.with_suffix(".png")
            _red_pct = int(round((1.0 - reduce_fraction) * 100)) if reduce_fraction is not None else 0
            err = _render_preview(output_path, png_path,
                                  duration=time.perf_counter() - _t_convert,
                                  display_name=input_path.stem,
                                  reduction_pct=_red_pct,
                                  shape=refined)
            _step_end(t, png_path.name if err is None else f"skipped ({err})")

        _rec_post_sew(original_ext, n_faces_sewn, time.perf_counter() - t_post_sew)
        return True, {"kb": out_kb}

    except Exception:
        _step_fail()
        return False, traceback.format_exc().strip()


def models_dir() -> Path:
    return Path(__file__).parent / MODELS_DIR_NAME


def _err_line(tb: str) -> str:
    lines = [l for l in tb.splitlines() if l.strip()]
    return lines[-1] if lines else tb


def _box_err(info: str) -> None:
    msg = _err_line(info)
    prefix = "✗  "
    width = _BOX_CONTENT - len(prefix)
    parts = textwrap.wrap(msg, width=width, break_long_words=True, break_on_hyphens=False) or [""]
    _box_row(f"{prefix}{parts[0]}", lc=R)
    for line in parts[1:]:
        _box_row(f"   {line}", lc=R)


def _quick_tri_count(path: Path):
    ext = path.suffix.lower()
    if ext == STL_EXT:
        return _stl_tri_count(path)
    if ext == TMF_EXT:
        try:
            with zipfile.ZipFile(str(path)) as zf:
                model_file = _find_3mf_model(zf)
                with zf.open(model_file) as f:
                    root = ET.parse(f).getroot()
            return sum(1 for _ in root.iter(f"{{{_3MF_NS}}}triangle")) or None
        except Exception:
            return None
    if ext == OBJ_EXT:
        try:
            count = 0
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("f ") or line.startswith("f\t"):
                        count += max(1, len(line.split()) - 3)
            return count or None
        except Exception:
            return None
    if ext == AMF_EXT:
        try:
            raw = path.read_bytes()
            if raw[:2] == b"PK":
                with zipfile.ZipFile(str(path)) as zf:
                    names = zf.namelist()
                    target = next((n for n in names if n.endswith(".amf")), names[0])
                    with zf.open(target) as f:
                        root = ET.parse(f).getroot()
            else:
                root = ET.fromstring(raw)
            return sum(1 for _ in root.iter("triangle")) or None
        except Exception:
            return None
    return None


def _make_output_path(src: Path, base_dir: Path, fraction, ext: str) -> Path:
    reduction_pct = int(round((1.0 - fraction) * 100)) if fraction is not None else 0
    return base_dir / (f"{src.stem} [{reduction_pct}]{ext}")


def _is_up_to_date(src: Path, dst: Path, force: bool) -> bool:
    return SKIP_EXISTING and not force and dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime


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
    _lock_fracs = None
    _locked = False

    for i, src_file in enumerate(files):
        base_dir = out_dir or src_file.parent
        _is_interactive = REDUCE_INTERACTIVE and not args.reduce and not _locked
        eff_fractions = _lock_fracs if _locked else reduce_fractions

        src_kb = src_file.stat().st_size // BYTES_PER_KB
        size_str = f"{src_kb:,} KB"
        prefix = f"[{i+1}/{n}]  "
        name_trim = _BOX_CONTENT - len(size_str) - len(prefix) - 1

        if _is_interactive and not args.dry_run:
            _box_top()
            _box_row(f"{prefix}{_trim(src_file.name, name_trim)}", size_str, lc=B, rc=DIM)
            _box_sep()
            _t_read = _step_start("reading")
            n_tris_preview = _quick_tri_count(src_file)
            _read_detail = f"{n_tris_preview:,} triangles" if n_tris_preview is not None else ""
            _step_end(_t_read, _read_detail)
            _box_sep()
            chosen_fracs, lock_all = _reduce_prompt(eff_fractions, n_tris=n_tris_preview, batch=True)
            if lock_all:
                _lock_fracs = chosen_fracs
                _locked = True
            _inter_fracs = chosen_fracs or [None]
            _inter_mesh = _preload_mesh(src_file) if len(_inter_fracs) > 1 else None
            for _i_cfrac, _cfrac in enumerate(_inter_fracs):
                out_file = _make_output_path(src_file, base_dir, _cfrac, STP_EXT)
                _up_to_date = _is_up_to_date(src_file, out_file, args.force)
                if _i_cfrac > 0:
                    _box_sep()
                if _up_to_date:
                    _skip_kb = out_file.stat().st_size // BYTES_PER_KB
                    _skip_str = f"{_skip_kb:,} KB"
                    _box_row(f"✓  {_trim(out_file.name, _BOX_CONTENT - len(_skip_str) - 3)}", _skip_str, lc=f"{G}{B}", rc=G)
                    skip_n += 1
                    continue
                _t0 = time.perf_counter()
                success, info = convert(src_file, out_file, args.tolerance, _cfrac,
                                        step_schema=step_schema, _suppress_read_step=True,
                                        _mesh_data=_inter_mesh)
                _elapsed = time.perf_counter() - _t0
                _box_sep()
                if success:
                    out_str = f"{info['kb']:,} KB · {_fmt_time(_elapsed)}"
                    _box_row(f"✓  {_trim(out_file.name, _BOX_CONTENT - len(out_str) - 3)}", out_str, lc=f"{G}{B}", rc=G)
                    ok_n += 1
                else:
                    _box_err(info)
                    fail_n += 1
            _box_bot()
            print()
            continue

        fracs = list(eff_fractions) if eff_fractions else [None]

        _preloaded_mesh = _preload_mesh(src_file) if len(fracs) > 1 else None

        if args.dry_run:
            for _efrac in fracs:
                out_file = _make_output_path(src_file, base_dir, _efrac, STP_EXT)
                _up_to_date = _is_up_to_date(src_file, out_file, args.force)
                if _up_to_date:
                    print(f"  {DIM}↷  {_trim(src_file.name)}  up-to-date{X}")
                    skip_n += 1
                else:
                    print(f"  {C}→  {_trim(src_file.name)}  {src_kb:,} KB → {out_file.name}{X}")
                    ok_n += 1
            continue

        _box_top()
        _box_row(f"{prefix}{_trim(src_file.name, name_trim)}", size_str, lc=B, rc=DIM)
        _box_sep()

        for _i_efrac, _efrac in enumerate(fracs):
            out_file = _make_output_path(src_file, base_dir, _efrac, STP_EXT)
            _up_to_date = _is_up_to_date(src_file, out_file, args.force)

            if _i_efrac > 0:
                _box_sep()

            if _up_to_date:
                _skip_kb = out_file.stat().st_size // BYTES_PER_KB
                _skip_str = f"{_skip_kb:,} KB"
                _box_row(f"✓  {_trim(out_file.name, _BOX_CONTENT - len(_skip_str) - 3)}", _skip_str, lc=f"{G}{B}", rc=G)
                skip_n += 1
                continue

            _t0 = time.perf_counter()
            success, info = convert(src_file, out_file, args.tolerance, _efrac,
                                    step_schema=step_schema, _mesh_data=_preloaded_mesh)
            _elapsed = time.perf_counter() - _t0
            _box_sep()
            if success:
                out_str = f"{info['kb']:,} KB · {_fmt_time(_elapsed)}"
                _box_row(f"✓  {_trim(out_file.name, _BOX_CONTENT - len(out_str) - 3)}", out_str, lc=f"{G}{B}", rc=G)
                ok_n += 1
            else:
                _box_err(info)
                fail_n += 1

        _box_bot()
        print()

    return ok_n, fail_n, skip_n


def _print_summary(ok_n, fail_n, skip_n, dry_run=False):
    print(f"  {DIM}{'─' * SEPARATOR_WIDTH}{X}")
    if fail_n == 0 and skip_n == 0:
        verb = "would convert" if dry_run else "converted"
        print(f"  {G}{B}✓  All {ok_n} file{'s' if ok_n > 1 else ''} {verb} successfully{X}")
    else:
        _parts = []
        if ok_n:   _parts.append(f"{G}✓  {ok_n} {'would convert' if dry_run else 'converted'}{X}")
        if skip_n: _parts.append(f"{DIM}↷  {skip_n} skipped{X}")
        if fail_n: _parts.append(f"{R}✗  {fail_n} failed{X}")
        print(f"  {'    '.join(_parts)}")


def main():
    global GENERATE_PREVIEW
    parser = argparse.ArgumentParser(description="Convert to STEP.")
    parser.add_argument("input", nargs="*",
        help="input file(s); omit to convert everything in the models/ folder")
    parser.add_argument("--output", "-o", metavar="FILE",
        help="output path (single-file mode only)")
    parser.add_argument("--output-dir", "-d", metavar="DIR",
        help="write output files to this directory instead of alongside the source")
    parser.add_argument("--tolerance", "-t", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--reduce", "-r", metavar="PCT",
        help="reduce mesh by this %% of triangles before converting (0 = off)")
    parser.add_argument("--format", metavar="SCHEMA", default=DEFAULT_FORMAT,
        choices=["ap203", "ap214", "ap242"],
        help=f"STEP schema: ap203, ap214, ap242 (default: {DEFAULT_FORMAT})")
    parser.add_argument("--force", "-f", action="store_true",
        help="re-convert files even if the output is already up-to-date")
    parser.add_argument("--dry-run", "--dry", action="store_true",
        help="show what would be converted without actually converting")
    parser.add_argument("--watch", "-w", action="store_true",
        help="after batch conversion, watch the folder and convert new files automatically")
    parser.add_argument("--preview", action=argparse.BooleanOptionalAction, default=GENERATE_PREVIEW,
        help=f"generate a .png preview alongside each .stp (default: {GENERATE_PREVIEW})")
    args = parser.parse_args()
    GENERATE_PREVIEW = args.preview

    if args.reduce:
        reduce_fractions = _parse_reduction(args.reduce)
    elif isinstance(DEFAULT_REDUCE, str):
        reduce_fractions = _parse_reduction(DEFAULT_REDUCE)
    elif 0 < DEFAULT_REDUCE < 100:
        reduce_fractions = [((100.0 - DEFAULT_REDUCE) / 100.0)]
    else:
        reduce_fractions = None
    step_schema = _STEP_SCHEMAS.get(args.format, "AP203")

    print()
    _w = _BOX_CONTENT + 4
    print(f"  {C}{B}╔{'═' * _w}╗{X}")
    print(f"  {C}{B}║{'2STEP-Converter':^{_w}}║{X}")
    print(f"  {C}{B}╚{'═' * _w}╝{X}")
    print()

    if _cfg_warnings:
        for _w_msg in _cfg_warnings:
            print(f"  {Y}⚠  {_w_msg}{X}")
        print()

    out_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    if len(args.input) > 1:
        files = []
        for p in args.input:
            fp = Path(p).resolve()
            if not fp.exists():
                print(f"  {R}[ERROR]{X} File not found: {fp}")
            elif fp.suffix.lower() not in _SUPPORTED_EXTS:
                print(f"  {R}[ERROR]{X} Unsupported format: {fp.name}")
            else:
                files.append(fp)
        if not files:
            sys.exit(1)
        n = len(files)
        print(f"  {n} file{'s' if n > 1 else ''} to convert\n")
        ok_n, fail_n, skip_n = _run_batch(files, n, out_dir, args, reduce_fractions, step_schema)
        _print_summary(ok_n, fail_n, skip_n, dry_run=args.dry_run)
        print()
        input("  Press Enter to exit...")
        sys.exit(0 if fail_n == 0 else 1)

    if len(args.input) == 1:
        input_path = Path(args.input[0]).resolve()
        if not input_path.exists():
            print(f"  {R}[ERROR]{X} File not found: {input_path}\n")
            input("  Press Enter to exit...")
            sys.exit(1)

        _single_interactive = REDUCE_INTERACTIVE and not args.reduce
        _single_base_dir = out_dir or input_path.parent
        first_frac = reduce_fractions[0] if reduce_fractions else None

        if args.output:
            out = Path(args.output)
            output_path = input_path.parent / out if not out.is_absolute() else out
        else:
            output_path = _make_output_path(input_path, _single_base_dir, first_frac, STP_EXT)

        src_kb = input_path.stat().st_size // BYTES_PER_KB
        size_str = f"{src_kb:,} KB"

        if args.dry_run:
            for _dfrac in (reduce_fractions or [None]):
                _dout = output_path if args.output else _make_output_path(input_path, _single_base_dir, _dfrac, STP_EXT)
                if _is_up_to_date(input_path, _dout, args.force):
                    print(f"  {DIM}↷  {_trim(input_path.name)}  up-to-date{X}\n")
                else:
                    print(f"  {C}→  {_trim(input_path.name)}  {src_kb:,} KB → {_dout.name}{X}\n")
            input("  Press Enter to exit...")
            sys.exit(0)

        _preloaded_mesh = (_preload_mesh(input_path)
                           if not _single_interactive and reduce_fractions and len(reduce_fractions) > 1
                           else None)

        _box_top()
        _box_row(f"[1/1]  {_trim(input_path.name, _BOX_CONTENT - len(size_str) - 3)}", size_str, lc=B, rc=DIM)
        _box_sep()
        _chosen = {}
        _t0 = time.perf_counter()
        success, info = convert(input_path, output_path, args.tolerance, first_frac,
                                interactive=_single_interactive,
                                _chosen_out=_chosen if _single_interactive else None,
                                step_schema=step_schema,
                                _mesh_data=_preloaded_mesh)
        if _single_interactive and success and _chosen and not args.output:
            chosen_frac = _chosen.get('fraction')
            new_out = _make_output_path(input_path, _single_base_dir, chosen_frac, STP_EXT)
            if new_out != output_path:
                try:
                    output_path.rename(new_out)
                    output_path = new_out
                except Exception:
                    pass
        _elapsed = time.perf_counter() - _t0
        _box_sep()
        if success:
            out_str = f"{info['kb']:,} KB · {_fmt_time(_elapsed)}"
            _box_row(f"✓  {_trim(output_path.name, _BOX_CONTENT - len(out_str) - 3)}", out_str, lc=f"{G}{B}", rc=G)
        else:
            _box_err(info)

        _all_fracs = (_chosen.get('fractions') or []) if _single_interactive else (reduce_fractions or [])
        _extra_fracs = _all_fracs[1:] if success and not args.output else []
        _any_fail = not success
        _extra_mesh = _chosen.get('mesh_data') if _single_interactive else _preloaded_mesh
        for _xfrac in _extra_fracs:
            _xout = _make_output_path(input_path, _single_base_dir, _xfrac, STP_EXT)
            _box_sep()
            _tx0 = time.perf_counter()
            sx, ix = convert(input_path, _xout, args.tolerance, _xfrac,
                             step_schema=step_schema, _mesh_data=_extra_mesh)
            _elx = time.perf_counter() - _tx0
            _box_sep()
            if sx:
                _xs = f"{ix['kb']:,} KB · {_fmt_time(_elx)}"
                _box_row(f"✓  {_trim(_xout.name, _BOX_CONTENT - len(_xs) - 3)}", _xs, lc=f"{G}{B}", rc=G)
            else:
                _box_err(ix)
                _any_fail = True

        _box_bot()
        print()
        if success:
            print(f"  {G}{B}✓  Done{X}")
        print()
        input("  Press Enter to exit...")
        sys.exit(0 if not _any_fail else 1)

    folder = models_dir()
    folder.mkdir(exist_ok=True)

    files = sorted(f for f in folder.iterdir() if f.suffix.lower() in _SUPPORTED_EXTS)
    if not files:
        print(f"  No supported files found in {MODELS_DIR_NAME}\\\n")
        input("  Press Enter to exit...")
        sys.exit(0)

    n = len(files)
    print(f"  {n} file{'s' if n > 1 else ''} found in {C}{MODELS_DIR_NAME}\\{X}\n")

    ok_n, fail_n, skip_n = _run_batch(files, n, out_dir, args, reduce_fractions, step_schema)
    _print_summary(ok_n, fail_n, skip_n, dry_run=args.dry_run)
    print()

    if args.watch and not args.dry_run:
        known = {f.name for f in folder.iterdir() if f.suffix.lower() in _SUPPORTED_EXTS}
        print(f"  {DIM}Watching {C}{MODELS_DIR_NAME}\\{X}{DIM}  Ctrl+C to stop{X}\n")
        try:
            while True:
                time.sleep(2)
                current = {f.name for f in folder.iterdir() if f.suffix.lower() in _SUPPORTED_EXTS}
                new_names = current - known
                known = current
                for name in sorted(new_names):
                    src = folder / name
                    prev_size = -1
                    while True:
                        cur_size = src.stat().st_size
                        if cur_size == prev_size:
                            break
                        prev_size = cur_size
                        time.sleep(0.5)
                    src_kb = src.stat().st_size // BYTES_PER_KB
                    size_str = f"{src_kb:,} KB"
                    _wfracs = list(reduce_fractions) if reduce_fractions else [None]
                    _box_top()
                    _box_row(f"[new]  {_trim(src.name, _BOX_CONTENT - len(size_str) - 3)}", size_str, lc=B, rc=DIM)
                    _box_sep()
                    for _i_wfrac, _wfrac in enumerate(_wfracs):
                        dst = _make_output_path(src, out_dir or folder, _wfrac, STP_EXT)
                        if _i_wfrac > 0:
                            _box_sep()
                        _t0 = time.perf_counter()
                        success, info = convert(src, dst, args.tolerance, _wfrac,
                                                step_schema=step_schema)
                        elapsed = time.perf_counter() - _t0
                        _box_sep()
                        if success:
                            out_str = f"{info['kb']:,} KB · {_fmt_time(elapsed)}"
                            _box_row(f"✓  {_trim(dst.name, _BOX_CONTENT - len(out_str) - 3)}", out_str, lc=f"{G}{B}", rc=G)
                        else:
                            _box_err(info)
                    _box_bot()
                    print()
        except KeyboardInterrupt:
            print(f"\n  {DIM}Watch stopped.{X}\n")
        sys.exit(0 if fail_n == 0 else 1)
    else:
        input("  Press Enter to exit...")
        sys.exit(0 if fail_n == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("\n  Press Enter to exit...")
        sys.exit(1)
