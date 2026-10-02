import os
import tempfile
from pathlib import Path
from kernel_io import quiet
from options import _fmt_time
from occ_geometry import _count_topo_unique
from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.TopExp import topexp
from OCC.Core.TopTools import TopTools_IndexedMapOfShape
from OCC.Core.TopoDS import topods
from OCC.Core.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE


def _preview_topology_text(shape) -> str:
    n_solids = _count_topo_unique(shape, TopAbs_SOLID)
    n_faces = _count_topo_unique(shape, TopAbs_FACE)
    n_edges = _count_topo_unique(shape, TopAbs_EDGE)
    solid_label = 'solid' if n_solids == 1 else 'solids'
    return f'{n_solids:,} {solid_label} | {n_faces:,} faces | {n_edges:,} edges'


def _render_preview(
    step_path: Path,
    png_path: Path,
    duration: float=None,
    display_name: str=None,
    reduction_pct: int=None,
    shape=None,
):
    try:
        import numpy as np
        import math as _math
        if shape is None:
            with quiet():
                reader = STEPControl_Reader()
                status = reader.ReadFile(step_path.as_posix())
                if status != IFSelect_RetDone:
                    return f'STEP read failed (status {status})'
                reader.TransferRoots()
                shape = reader.OneShape()
            if shape.IsNull():
                return 'empty shape from STEP'
        _OUT_SIZE = 1200
        _er, _ar = (_math.radians(20), _math.radians(315))
        _ffx = -_math.cos(_er) * _math.cos(_ar)
        _ffy = -_math.cos(_er) * _math.sin(_ar)
        _ffz = -_math.sin(_er)
        _rm = _math.hypot(_ffy, _ffx)
        _rx, _ry = (_ffy / _rm, -_ffx / _rm)
        _ux = _ry * _ffz
        _uy = -_rx * _ffz
        _uz = _rx * _ffy - _ry * _ffx
        _um = _math.sqrt(_ux * _ux + _uy * _uy + _uz * _uz)
        _ux, _uy, _uz = (_ux / _um, _uy / _um, _uz / _um)
        mpl_arr = None
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            from mpl_toolkits.mplot3d.art3d import Line3DCollection
            from OCC.Core.BRepAdaptor import BRepAdaptor_Curve
            from OCC.Core.GCPnts import GCPnts_TangentialDeflection
            segments, raw_pts = ([], [])
            edges = TopTools_IndexedMapOfShape()
            topexp.MapShapes(shape, TopAbs_EDGE, edges)
            for edge_index in range(1, edges.Size() + 1):
                try:
                    edge = topods.Edge(edges.FindKey(edge_index))
                    curve = BRepAdaptor_Curve(edge)
                    disc = GCPnts_TangentialDeflection(curve, 0.3, 0.05)
                    pts = []
                    for i in range(1, disc.NbPoints() + 1):
                        p = disc.Value(i)
                        pts.append((p.X(), p.Y(), p.Z()))
                        raw_pts.append(pts[-1])
                    for j in range(len(pts) - 1):
                        segments.append([pts[j], pts[j + 1]])
                except Exception:
                    pass
            if segments:
                bg_hex = '#16213e'
                bg_rgb = np.array([22, 33, 62], dtype=np.uint8)
                arr_pts = np.array(raw_pts, dtype=np.float64)
                mins, maxs = (arr_pts.min(axis=0), arr_pts.max(axis=0))
                mid = ((mins + maxs) / 2).tolist()
                half = float((maxs - mins).max()) / 2 * 1.05 or 1.0
                fig = plt.figure(figsize=(10, 10), dpi=200, facecolor=bg_hex)
                try:
                    ax = fig.add_subplot(111, projection='3d', facecolor=bg_hex)
                    ax.add_collection3d(Line3DCollection(segments, linewidths=0.4, colors='#ffffff', alpha=0.9))
                    ax.set_xlim(mid[0] - half, mid[0] + half)
                    ax.set_ylim(mid[1] - half, mid[1] + half)
                    ax.set_zlim(mid[2] - half, mid[2] + half)
                    ax.set_axis_off()
                    ax.view_init(elev=20, azim=315)
                    fig.tight_layout(pad=0)
                    fig.canvas.draw()
                    mpl_arr = np.frombuffer(
                        fig.canvas.buffer_rgba(),
                        dtype=np.uint8,
                    ).reshape(fig.canvas.get_width_height()[::-1] + (4,))
                finally:
                    plt.close(fig)
                non_bg = ~np.all(mpl_arr[:, :, :3] == bg_rgb, axis=2)
                rows = np.where(np.any(non_bg, axis=1))[0]
                cols = np.where(np.any(non_bg, axis=0))[0]
                if len(rows) and len(cols):
                    pad = 30
                    mpl_arr = mpl_arr[max(0, rows[0] - pad):min(
                        mpl_arr.shape[0],
                        rows[-1] + pad + 1,
                    ), max(0, cols[0] - pad):min(mpl_arr.shape[1], cols[-1] + pad + 1)]
                bg_mask = np.all(mpl_arr[:, :, :3] == bg_rgb, axis=2)
                mpl_arr[bg_mask, 3] = 0
        except Exception:
            mpl_arr = None
        if mpl_arr is None:
            return 'preview render failed'
        try:
            from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
            _resample = getattr(getattr(_PIL_Image, 'Resampling', _PIL_Image), 'LANCZOS')
            canvas = _PIL_Image.new('RGBA', (_OUT_SIZE, _OUT_SIZE), (22, 33, 62, 255))
            _draw_bg = _PIL_Draw.Draw(canvas)
            _gc = (51, 61, 85, 255)
            _gs = 60
            _half = _OUT_SIZE // 2
            _diag = int(_math.sqrt(2) * _OUT_SIZE) + _gs
            _nl = _diag // _gs + 2
            _gdx = np.array([_rx, -_ux])
            _gdx /= np.hypot(*_gdx)
            _gdy = np.array([_ry, -_uy])
            _gdy /= np.hypot(*_gdy)
            for _gd, _gp in [(_gdx, np.array([-_gdx[1], _gdx[0]])), (_gdy, np.array([-_gdy[1], _gdy[0]]))]:
                for _ni in range(-_nl, _nl + 1):
                    _lox = _half + _ni * _gs * _gp[0]
                    _loy = _half + _ni * _gs * _gp[1]
                    _draw_bg.line(
                        [
                            (int(_lox - _gd[0] * _diag), int(_loy - _gd[1] * _diag)),
                            (int(_lox + _gd[0] * _diag), int(_loy + _gd[1] * _diag)),
                        ],
                        fill=_gc,
                        width=1,
                    )
            img = _PIL_Image.fromarray(mpl_arr)
            img.thumbnail((_OUT_SIZE, _OUT_SIZE), _resample)
            canvas.paste(img, ((_OUT_SIZE - img.width) // 2, (_OUT_SIZE - img.height) // 2), img)
            out_kb = step_path.stat().st_size // 1024
            lines = [
                (display_name or step_path.stem, (255, 255, 255, 220)),
                (f'reduction {reduction_pct or 0}%', (180, 190, 220, 155)),
                (_preview_topology_text(shape), (180, 190, 220, 170)),
                (
                    f'{out_kb:,} KB' + (f'  |  {_fmt_time(duration)}' if duration else ''),
                    (180, 190, 220, 130),
                ),
            ]
            lines = [(t, c) for t, c in lines if t]
            _font_sz = 18
            try:
                import matplotlib as _mpl
                _fp = str(Path(_mpl.__file__).parent / 'mpl-data' / 'fonts' / 'ttf' / 'DejaVuSans.ttf')
                _font = _PIL_Font.truetype(_fp, _font_sz)
                _font_ax = _PIL_Font.truetype(_fp, 14)
            except Exception:
                _font = _font_ax = _PIL_Font.load_default()
            draw = _PIL_Draw.Draw(canvas)
            margin, line_h = (20, _font_sz + 5)
            y0 = _OUT_SIZE - margin - len(lines) * line_h
            for i, (text, color) in enumerate(lines):
                draw.text(
                    (margin, y0 + i * line_h),
                    text if i == 0 else text[:1].upper() + text[1:],
                    fill=color,
                    font=_font,
                )
            _axis_dirs = {}
            for _n, _wv in [('X', (1.0, 0.0, 0.0)), ('Y', (0.0, 1.0, 0.0)), ('Z', (0.0, 0.0, 1.0))]:
                _sx = _wv[0] * _rx + _wv[1] * _ry
                _sy = _wv[0] * _ux + _wv[1] * _uy + _wv[2] * _uz
                _d = np.array([_sx, -_sy])
                _mag = float(np.hypot(*_d))
                _axis_dirs[_n] = _d / _mag if _mag > 0 else _d
            _arrow, _ix, _iy = (60, _OUT_SIZE - 90, _OUT_SIZE - 90)
            _ax_cols = {'X': (255, 85, 85, 230), 'Y': (85, 204, 85, 230), 'Z': (85, 136, 255, 230)}
            for _n, _col in _ax_cols.items():
                _d = _axis_dirs[_n]
                _ex, _ey = (int(_ix + _d[0] * _arrow), int(_iy + _d[1] * _arrow))
                draw.line([(_ix, _iy), (_ex, _ey)], fill=_col, width=2)
                draw.text((_ex + int(_d[0] * 10), _ey + int(_d[1] * 10) - 7), _n, fill=_col, font=_font_ax)
            canvas.save(str(png_path))
        except ImportError:
            import matplotlib.image as mpimg
            mpimg.imsave(str(png_path), mpl_arr)
        return None
    except Exception as e:
        return (str(e).strip().splitlines() or [type(e).__name__])[0][:50]


def _render_preview_atomic(step_path: Path, png_path: Path, **kwargs):
    temp_png = None
    try:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        fd_png, temp_png_name = tempfile.mkstemp(
            prefix=f'.{png_path.name}.',
            suffix='.tmp.png',
            dir=png_path.parent,
        )
        os.close(fd_png)
        temp_png = Path(temp_png_name)
        error = _render_preview(step_path, temp_png, **kwargs)
        if error is None and temp_png.stat().st_size > 0:
            os.replace(temp_png, png_path)
            return None
        png_path.unlink(missing_ok=True)
        return error or 'preview output is missing or empty'
    except Exception as exc:
        try:
            png_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f'preview commit failed: {exc}'
    finally:
        if temp_png is not None:
            try:
                temp_png.unlink(missing_ok=True)
            except OSError:
                pass
