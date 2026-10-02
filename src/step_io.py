import os
import tempfile
from pathlib import Path
from config import Settings
from kernel_io import quiet
from occ_geometry import _validate_occ_shape, geometry_change_error
from OCC.Core.STEPControl import STEPControl_Writer, STEPControl_Reader, STEPControl_AsIs, STEPControl_Controller
from OCC.Core.Interface import Interface_Static
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.TopoDS import TopoDS_Iterator
from OCC.Core.TopAbs import TopAbs_COMPOUND
BYTES_PER_KB = 1024


def _write_step_atomic(shape, output_path: Path, step_schema: str, *, settings: Settings=Settings(), source_snapshot=None) -> int:
    if source_snapshot is not None:
        source_snapshot.validate()
    STEPControl_Controller.Init()
    if not Interface_Static.SetCVal('write.step.schema', step_schema):
        raise ValueError(f'unsupported STEP schema: {step_schema}')
    Interface_Static.SetCVal('write.step.product.name', '')
    Interface_Static.SetCVal('write.step.assembly', '0')
    Interface_Static.SetCVal('write.step.unit', 'MM')
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
        prefix=f'.{output_path.name}.',
        suffix='.tmp.stp',
        dir=output_path.parent,
    )
    os.close(fd_tmp)
    tmp_output = Path(tmp_name)
    try:
        with quiet():
            for subshape in transfer_shapes:
                status = writer.Transfer(subshape, STEPControl_AsIs)
                if status != IFSelect_RetDone:
                    raise RuntimeError(f'STEP transfer failed with status {status}')
            status = writer.Write(tmp_output.as_posix())
        if status != IFSelect_RetDone:
            raise RuntimeError(f'STEP writer failed with status {status}')
        if not tmp_output.is_file() or tmp_output.stat().st_size == 0:
            raise RuntimeError('temporary STEP output is missing or empty')
        if settings.validate_step_after_writing:
            reader = STEPControl_Reader()
            with quiet():
                read_status = reader.ReadFile(tmp_output.as_posix())
                if read_status == IFSelect_RetDone:
                    reader.TransferRoots()
            if read_status != IFSelect_RetDone:
                raise RuntimeError(f'STEP readback failed with status {read_status}')
            read_shape = reader.OneShape()
            valid, validation_error = _validate_occ_shape(
                read_shape,
                require_solid=settings.require_solid_output,
            )
            if not valid:
                raise RuntimeError(f'STEP readback validation failed: {validation_error}')
            change_error = geometry_change_error(shape, read_shape, 1e-06)
            if change_error:
                raise RuntimeError(f'STEP readback geometry changed: {change_error}')
        if source_snapshot is not None:
            stat = tmp_output.stat()
            os.utime(tmp_output, ns=(stat.st_atime_ns, source_snapshot.started_ns))
            source_snapshot.validate()
        os.replace(tmp_output, output_path)
        return output_path.stat().st_size // BYTES_PER_KB
    finally:
        try:
            tmp_output.unlink(missing_ok=True)
        except OSError:
            pass
