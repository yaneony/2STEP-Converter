import time
import traceback
from pathlib import Path
from config import Settings
from console import NullProgress
from kernel_io import quiet
from options import _reduction_label


def convert(
    input_path: Path,
    output_path: Path,
    tolerance: float | None = None,
    reduce_fraction=None,
    step_schema: str = 'AP203',
    _suppress_read_step: bool = False,
    _mesh_data=None,
    _mesh_report=None,
    _source_snapshot=None,
    *,
    settings: Settings = Settings(),
    progress=None,
    estimator=None,
):
    _t_convert = time.perf_counter()
    from mesh_readers import SourceSnapshot, _read_iges_shape, _load_mesh_arrays
    from mesh_geometry import (_repair_mesh_arrays, _effective_tolerance,
        _conversion_quality_report, _needs_volume_repair, _mesh_quality_report, _reduce_mesh_arrays)
    from reconstruction import _reconstruct_analytic_shape
    from occ_geometry import (_mesh_to_shape, _count_topo, _count_surface_type,
        _count_free_shells, _validate_occ_shape, _has_candidate_hole_wires,
        geometry_change_error, mesh_conversion_error, parts as _shape_parts)
    from brep_operations import (_parallel_fix, _parallel_refine,
        _parallel_reconstruct_holes, _parallel_fill_brep_holes,
        _parallel_solidify, _parallel_repair_mesh, _subprocess_sew)
    from step_io import _write_step_atomic
    from estimator import timing_profile
    from preview import _render_preview_atomic
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.BRepCheck import BRepCheck_Analyzer
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_SOLID, TopAbs_SHELL
    from OCC.Core.TopoDS import topods
    from OCC.Core.GeomAbs import GeomAbs_Cylinder

    if tolerance is None:
        tolerance = settings.sewing_tolerance
    if progress is None:
        progress = NullProgress()
    n_verts = n_tris = None
    input_triangles = None
    verts_np = tris_np = None
    primitive_name = None
    reconstructed_holes = 0
    shape = None
    quality_report = None
    repair_info = None
    repaired_shape = None
    reduction_skipped = False
    try:
        profile = timing_profile(
            settings, reduce_fraction, step_schema, _mesh_data is not None, tolerance=tolerance,
        ) if estimator is not None else None
        if input_path.resolve() == output_path.resolve():
            return (False, 'output must not overwrite the input file')
        source_snapshot = _source_snapshot or SourceSnapshot.capture(input_path)
        source_snapshot.validate()
        original_ext = input_path.suffix.lower()
        if not _suppress_read_step:
            t = progress._step_start('reading')
        if original_ext in settings.iges_extensions:
            shape, n_verts, n_tris = _read_iges_shape(input_path)
            if shape.IsNull():
                if not _suppress_read_step:
                    progress._step_fail()
                return (False, 'input produced an empty shape')
            if not _suppress_read_step:
                read_parts = []
                if n_verts is not None:
                    read_parts.append(f'{n_verts:,} vertices')
                if n_tris is not None:
                    read_parts.append(f'{n_tris:,} triangles')
                if not read_parts:
                    read_parts.append(f'{_count_topo(shape, TopAbs_FACE):,} faces')
                progress._step_end(t, ' | '.join(read_parts))
        else:
            if _mesh_data is not None:
                verts_np, tris_np = _mesh_data
                n_verts = len(verts_np)
                n_tris = len(tris_np)
            else:
                verts_np, tris_np = _load_mesh_arrays(input_path, settings=settings)
                verts_np, tris_np = _repair_mesh_arrays(verts_np, tris_np, settings=settings)
                n_verts = len(verts_np)
                n_tris = len(tris_np)
            if not _suppress_read_step:
                progress._step_end(t, f'{n_verts:,} vertices | {n_tris:,} triangles')
        input_triangles = n_tris
        source_snapshot.validate()
        effective_tolerance = _effective_tolerance(verts_np, tolerance, settings=settings)
        if (settings.check_mesh_quality or settings.repair_intersecting_mesh) and verts_np is not None:
            t = progress._step_start('quality')
            quality_report = _mesh_report if _mesh_data is not None and _mesh_report is not None else (
                _conversion_quality_report(verts_np, tris_np, settings=settings)
            )
            detail_parts = [
                'watertight' if quality_report['watertight'] else f"{quality_report['boundary_edges']:,} boundary edges",
                f"{quality_report['components']:,} part{('s' if quality_report['components'] != 1 else '')}",
            ]
            if quality_report['non_manifold_edges']:
                detail_parts.append(f"{quality_report['non_manifold_edges']:,} non-manifold")
            if quality_report['internal_self_intersections']:
                detail_parts.append(f"{quality_report['internal_self_intersections']:,} intersections")
            if quality_report['cross_component_intersections']:
                detail_parts.append(f"{quality_report['cross_component_intersections']:,} part overlaps")
            if settings.check_self_intersections and quality_report['self_intersections'] is None:
                detail_parts.append('intersection scan skipped')
            needs_repair = settings.repair_intersecting_mesh and _needs_volume_repair(quality_report)
            if needs_repair and quality_report['boundary_edges']:
                progress._step_fail()
                return (False, 'volume repair requires a closed triangle surface; open boundaries remain')
            if needs_repair and quality_report['self_intersections'] is None:
                progress._step_fail()
                return (False, 'model repair requires a full intersection scan; increase SELF_INTERSECTION_CHECK_MAX_TRIANGLES')
            if (
                (settings.reject_self_intersecting_mesh or settings.require_solid_output)
                and quality_report['internal_self_intersections']
                and not needs_repair
            ):
                progress._step_fail()
                return (
                    False,
                    f"mesh contains {quality_report['internal_self_intersections']:,} internal self-intersections",
                )
            if (
                (settings.reject_non_manifold_mesh or settings.require_solid_output)
                and quality_report['non_manifold_edges']
                and not needs_repair
            ):
                progress._step_fail()
                return (False, f"mesh contains {quality_report['non_manifold_edges']:,} non-manifold edges")
            if (
                settings.require_solid_output
                and (quality_report['orientable'] is False or quality_report['vertex_manifold'] is False)
                and not needs_repair
            ):
                progress._step_fail()
                return (False, 'mesh is non-orientable or contains non-manifold vertices')
            progress._step_end(t, ' | '.join(detail_parts))
            if needs_repair:
                if reduce_fraction is not None:
                    t = progress._step_start('reducing')
                    progress._step_end(t, 'skipped | damaged mesh requires repair')
                    reduction_skipped = True
                    reduce_fraction = None
                    profile = timing_profile(
                        settings, None, step_schema, _mesh_data is not None, tolerance=tolerance,
                    ) if estimator is not None else None
                t = progress._step_start('repairing')
                from model_repair import repair_metrics
                with quiet():
                    shape, repair_error = _parallel_repair_mesh(
                        None, verts_np, tris_np, settings=settings,
                    )
                if repair_error:
                    progress._step_fail()
                    return (False, f'model repair failed: {repair_error}')
                repair_info = repair_metrics(verts_np, tris_np, shape,
                    max_attached_area_ratio=settings.max_repair_attached_triangle_area_ratio)
                repaired_shape = shape
                correction_detail = (f" | {repair_info['removed_attached_triangles']:,} attached triangles removed"
                                     if repair_info['removed_attached_triangles'] else '')
                progress._step_end(t, f"{repair_info['solids']:,} solids{correction_detail} | inspect repaired areas")
        if not _suppress_read_step and n_tris is not None and (original_ext not in settings.iges_extensions):
            progress._show_early_estimate(
                input_triangles, fmt=original_ext,
                elapsed=time.perf_counter() - _t_convert, profile=profile,
            )
        if reduce_fraction is not None and original_ext not in settings.iges_extensions:
            t = progress._step_start('reducing')
            try:
                s_verts, s_tris, n_before, n_after = _reduce_mesh_arrays(
                    verts_np,
                    tris_np,
                    reduce_fraction,
                    settings=settings,
                )
                red_pct = int(round((1.0 - n_after / n_before) * 100))
                progress._step_end(t, f'{n_before:,} -> {n_after:,} triangles | {red_pct}% removed')
                verts_np, tris_np = (s_verts, s_tris)
                n_tris = n_after
                quality_report = None
            except Exception as e:
                progress._step_fail()
                return (False, f'mesh reduction failed: {e}')
        if shape is None and verts_np is not None:
            t = progress._step_start('fitting')
            shape, primitive_name = _reconstruct_analytic_shape(
                verts_np,
                tris_np,
                report=quality_report,
                settings=settings,
            )
            if shape is None:
                shape = _mesh_to_shape(verts_np, tris_np, settings=settings)
                progress._step_end(t, 'triangle B-Rep | planar merge enabled')
            elif primitive_name == 'linear extrusion':
                progress._step_end(t, 'experimental linear extrusion | exact profile prism')
            else:
                progress._step_end(t, f'analytic {primitive_name} | exact CAD surfaces')
        if shape is None or shape.IsNull():
            return (False, 'input produced an empty shape')
        if primitive_name is not None:
            refined = shape
            n_faces_after = _count_topo(refined, TopAbs_FACE)
            n_solids = _count_topo(refined, TopAbs_SOLID)
            free_shells = 0
            n_faces_sewn = n_faces_after
            t_post_sew = None
        else:
            t = progress._step_start('sewing')
            if verts_np is not None and BRepCheck_Analyzer(shape).IsValid():
                sewn, sew_err = (shape, None)
            else:
                with quiet():
                    sewn, sew_err = _subprocess_sew(shape, effective_tolerance, settings=settings)
            if sewn.IsNull():
                progress._step_fail()
                detail = f': {sew_err}' if sew_err else ''
                return (False, f'sewing failed{detail}')
            n_shells = _count_topo(sewn, TopAbs_SHELL)
            n_faces_sewn = _count_topo(sewn, TopAbs_FACE)
            progress._step_end(
                t,
                f"{n_shells:,} shell{('s' if n_shells != 1 else '')} | {n_faces_sewn:,} faces",
            )
            t_post_sew = time.perf_counter()
            progress._show_post_sew_estimate(n_faces_sewn, fmt=original_ext, profile=profile)
            t = progress._step_start('fixing')
            if BRepCheck_Analyzer(sewn).IsValid():
                fixed, fix_err = sewn, None
            else:
                with quiet():
                    fixed, fix_err = _parallel_fix(sewn, settings=settings)
            if fix_err:
                progress._step_fail()
                return (False, f'shape fixing failed: {fix_err}')
            n_faces_out = _count_topo(fixed, TopAbs_FACE)
            progress._step_end(t, f'{n_faces_sewn:,} -> {n_faces_out:,} faces')
            closed_shape = fixed
            if (
                settings.fill_small_planar_brep_gaps
                and any(((
                    part.ShapeType() == TopAbs_SHELL
                    and not BRep_Tool.IsClosed(topods.Shell(part))
                ) for part in _shape_parts(fixed)))
            ):
                t = progress._step_start('closing')
                with quiet():
                    candidate, close_err = _parallel_fill_brep_holes(
                        fixed,
                        effective_tolerance,
                        settings=settings,
                    )
                candidate_valid, _ = _validate_occ_shape(candidate, require_solid=False)
                if close_err or not candidate_valid:
                    detail = close_err or 'invalid result'
                    progress._step_end(t, f'kept original | {detail}')
                else:
                    closed_shape = candidate
                    n_faces_closed = _count_topo(closed_shape, TopAbs_FACE)
                    progress._step_end(t, f'{n_faces_out:,} -> {n_faces_closed:,} faces')
            t = progress._step_start('solidifying')
            if repaired_shape is not None:
                final_shape, solid_err = closed_shape, None
            else:
                with quiet():
                    final_shape, solid_err = _parallel_solidify(closed_shape, settings=settings)
            n_solids = _count_topo(final_shape, TopAbs_SOLID)
            free_shells = _count_free_shells(final_shape)
            valid, validation_error = _validate_occ_shape(
                final_shape,
                require_solid=settings.require_solid_output,
            )
            if not valid:
                progress._step_fail()
                detail = f' | {solid_err}' if solid_err else ''
                return (False, f'solid validation failed: {validation_error}{detail}')
            if solid_err:
                progress._step_end(t, f'{n_solids:,} solids | fallback: {solid_err}')
            elif n_solids:
                detail = f"{n_solids:,} solid{('s' if n_solids != 1 else '')}"
                if free_shells:
                    detail += f" | {free_shells:,} free shell{('s' if free_shells != 1 else '')}"
                progress._step_end(t, detail)
            else:
                progress._step_end(
                    t,
                    f"0 solids | {free_shells:,} open shell{('s' if free_shells != 1 else '')}",
                )
            n_faces_solid = _count_topo(final_shape, TopAbs_FACE)
            t = progress._step_start('refining')
            with quiet():
                refined, refine_err = _parallel_refine(final_shape, effective_tolerance, settings=settings)
            if refine_err:
                refined = final_shape
                n_faces_after = n_faces_solid
                progress._step_end(t, f'kept original | {refine_err}')
            else:
                refined_valid, _ = _validate_occ_shape(refined, require_solid=settings.require_solid_output)
                change_error = geometry_change_error(final_shape, refined, effective_tolerance)
                if not refined_valid or change_error:
                    refined = final_shape
                    n_faces_after = n_faces_solid
                    progress._step_end(t, f"kept original | {change_error or 'invalid result'}")
                else:
                    n_faces_after = _count_topo(refined, TopAbs_FACE)
                    progress._step_end(t, f'{n_faces_solid:,} -> {n_faces_after:,} faces')
            n_solids = _count_topo(refined, TopAbs_SOLID)
            free_shells = _count_free_shells(refined)
        if (
            (settings.reconstruct_analytic_through_holes or settings.reconstruct_analytic_blind_holes)
            and repaired_shape is None
            and original_ext not in settings.iges_extensions
            and _has_candidate_hole_wires(refined, settings=settings)
        ):
            t = progress._step_start('hole fitting')
            faces_before_holes = _count_topo(refined, TopAbs_FACE)
            cylinders_before = _count_surface_type(refined, GeomAbs_Cylinder)
            with quiet():
                hole_shape, hole_err = _parallel_reconstruct_holes(
                    refined,
                    effective_tolerance,
                    settings=settings,
                )
            hole_valid, _ = _validate_occ_shape(hole_shape, require_solid=settings.require_solid_output)
            cylinders_after = _count_surface_type(hole_shape, GeomAbs_Cylinder)
            reconstructed_holes = max(0, cylinders_after - cylinders_before)
            if hole_err or not hole_valid or (not reconstructed_holes):
                reconstructed_holes = 0
                detail = hole_err or 'no safe matches'
                progress._step_end(t, f'kept faceted | {detail}')
            else:
                refined = hole_shape
                n_faces_after = _count_topo(refined, TopAbs_FACE)
                n_solids = _count_topo(refined, TopAbs_SOLID)
                free_shells = _count_free_shells(refined)
                label = 'hole' if reconstructed_holes == 1 else 'holes'
                progress._step_end(
                    t,
                    f'{reconstructed_holes:,} {label} | {faces_before_holes:,} -> {n_faces_after:,} faces',
                )
        if repaired_shape is not None:
            valid, validation_error = _validate_occ_shape(refined, require_solid=True)
            if not valid:
                return (False, f'repaired solid validation failed: {validation_error}')
            change_error = geometry_change_error(repaired_shape, refined, effective_tolerance)
            if change_error:
                return (False, f'repaired geometry changed during conversion: {change_error}')
            repair_info = repair_metrics(verts_np, tris_np, refined,
                max_attached_area_ratio=settings.max_repair_attached_triangle_area_ratio)
            t = progress._step_start('repair stats')
            progress._step_end(t, f"volume {repair_info['volume']:.8g} | bounds change {repair_info['bounds_change']:.3g}")
            t = progress._step_start('reference')
            reference_change = repair_info['volume_change_percent']
            detail = f'{reference_change:+.4g}%' if reference_change is not None else 'unavailable'
            progress._step_end(t, f'{detail} vs invalid source signed volume')
        if verts_np is not None and settings.require_solid_output and repaired_shape is None:
            if quality_report is None:
                quality_report = _mesh_quality_report(verts_np, tris_np, False)
            if quality_report['watertight']:
                fit_ratio, volume_limit = (0.0, 0.001)
                if primitive_name == 'linear extrusion':
                    fit_ratio = settings.experimental_parametric_fit_error_ratio
                    volume_limit = settings.experimental_parametric_max_volume_change_percent
                elif primitive_name:
                    fit_ratio = settings.analytic_primitive_fit_error_ratio
                    volume_limit = settings.analytic_primitive_max_volume_change_percent
                elif reconstructed_holes:
                    volume_limit = settings.analytic_hole_max_volume_change_percent
                change_error = mesh_conversion_error(
                    verts_np,
                    tris_np,
                    refined,
                    effective_tolerance,
                    fit_error_ratio=fit_ratio,
                    max_volume_percent=volume_limit,
                )
                if change_error and primitive_name is None:
                    fallback_error = mesh_conversion_error(
                        verts_np, tris_np, final_shape, effective_tolerance,
                    )
                    if fallback_error is None:
                        refined = final_shape
                        n_faces_after = _count_topo(refined, TopAbs_FACE)
                        n_solids = _count_topo(refined, TopAbs_SOLID)
                        free_shells = _count_free_shells(refined)
                        reconstructed_holes = 0
                        t = progress._step_start('validation')
                        progress._step_end(t, f'kept triangles | {change_error}')
                        change_error = None
                if change_error:
                    return (False, change_error)
        t = progress._step_start('writing')
        try:
            write_settings = settings.with_overrides(
                require_solid_output=True, validate_step_after_writing=True,
            ) if repaired_shape is not None else settings
            out_kb = _write_step_atomic(refined, output_path, step_schema, settings=write_settings,
                                        source_snapshot=source_snapshot)
        except Exception as exc:
            progress._step_fail()
            return (False, str(exc))
        progress._step_end(t, f'{step_schema} | {out_kb:,} KB')
        if settings.generate_png_preview:
            t = progress._step_start('preview')
            png_path = output_path.with_suffix('.png')
            _red_pct = _reduction_label(reduce_fraction)
            err = _render_preview_atomic(
                output_path,
                png_path,
                duration=time.perf_counter() - _t_convert,
                display_name=input_path.stem,
                reduction_pct=_red_pct,
            )
            progress._step_end(t, png_path.name if err is None else f'not created | {err}')
        if estimator is not None and t_post_sew is not None:
            estimator._rec_post_sew(original_ext, n_faces_sewn, time.perf_counter() - t_post_sew, profile=profile)
        if estimator is not None and input_triangles is not None and original_ext not in settings.iges_extensions:
            estimator._rec_total(original_ext, input_triangles, time.perf_counter() - _t_convert, profile=profile)
        return (
            True,
            {
                'kb': out_kb,
                'faces': n_faces_after,
                'solids': n_solids,
                'open_shells': free_shells,
                'schema': step_schema,
                'tolerance': effective_tolerance,
                'repair': repair_info,
                'reduction_fraction': reduce_fraction,
                'reduction_skipped': reduction_skipped,
            },
        )
    except Exception:
        progress._step_fail()
        return (False, traceback.format_exc().strip())


if __name__ == "__main__":
    from cli import main
    main()
