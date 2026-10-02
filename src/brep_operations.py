import json
from pathlib import Path
import subprocess
import sys
import tempfile
from OCC.Core.BRep import BRep_Builder
from OCC.Core.BRepTools import breptools
from OCC.Core.TopoDS import TopoDS_Shape
from config import Settings


def run_operation(shape, operation, settings=None, timeout=300, fallback=None, mesh_data=None):
    try:
        with tempfile.TemporaryDirectory(prefix="2step-") as directory:
            input_path = (Path(directory) / "input.brep").as_posix()
            output_path = (Path(directory) / "output.brep").as_posix()
            worker_settings = dict(settings or {})
            if mesh_data is not None:
                import numpy as np
                mesh_path = Path(directory) / 'mesh.npz'
                np.savez(mesh_path, vertices=mesh_data[0], faces=mesh_data[1])
                worker_settings['mesh_path'] = mesh_path.as_posix()
            if shape is None and (operation != 'repair_mesh' or mesh_data is None):
                raise ValueError('only mesh repair can run without an input B-Rep')
            if shape is not None and not breptools.Write(shape, input_path):
                raise RuntimeError("could not serialize the input B-Rep")
            worker = Path(__file__).with_name("brep_worker.py")
            process = subprocess.run(
                [sys.executable, str(worker), operation, input_path, output_path,
                 json.dumps(worker_settings, allow_nan=False)],
                capture_output=True, timeout=timeout,
            )
            if process.returncode != 0:
                lines = process.stderr.decode("utf-8", errors="replace").strip().splitlines()
                raise RuntimeError(lines[-1][:200] if lines else
                                   f"subprocess exited with code {process.returncode}")
            if not Path(output_path).is_file() or not Path(output_path).stat().st_size:
                raise RuntimeError("subprocess produced an empty B-Rep")
            result = TopoDS_Shape()
            if not breptools.Read(result, output_path, BRep_Builder()) or result.IsNull():
                raise RuntimeError("subprocess produced an unreadable B-Rep")
            return result, None
    except subprocess.TimeoutExpired:
        error = f"subprocess timed out after {timeout}s"
    except Exception as exc:
        error = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
    return (shape if fallback is None else fallback), error


def _parallel_fix(shape, *, settings: Settings=Settings()):
    return run_operation(shape, 'fix', timeout=settings.cad_operation_timeout_seconds)


def _parallel_refine(shape, tolerance, *, settings: Settings=Settings()):
    return run_operation(shape, 'refine', dict(settings.to_dict(), tolerance=tolerance),
                         timeout=settings.cad_operation_timeout_seconds)


def _parallel_reconstruct_holes(shape, tolerance, *, settings: Settings=Settings()):
    return run_operation(
        shape,
        'reconstruct_holes',
        dict(settings.to_dict(), tolerance=tolerance),
        timeout=settings.sewing_timeout_seconds,
    )


def _parallel_fill_brep_holes(shape, tolerance, *, settings: Settings=Settings()):
    return run_operation(shape, 'fill_gaps', dict(settings.to_dict(), tolerance=tolerance),
                         timeout=settings.cad_operation_timeout_seconds)


def _parallel_solidify(shape, *, settings: Settings=Settings()):
    return run_operation(shape, 'solidify', timeout=settings.cad_operation_timeout_seconds)


def _parallel_repair_mesh(shape, vertices, faces, *, settings: Settings=Settings()):
    return run_operation(
        shape, 'repair_mesh',
        settings.to_dict(),
        timeout=settings.sewing_timeout_seconds,
        mesh_data=(vertices, faces),
    )


def _subprocess_sew(shape, tolerance, *, settings: Settings=Settings()):
    return run_operation(
        shape,
        'sew',
        dict(settings.to_dict(), tolerance=tolerance),
        timeout=settings.sewing_timeout_seconds,
        fallback=TopoDS_Shape(),
    )
