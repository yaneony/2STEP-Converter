import argparse
import os
import sys
import time
import traceback
from pathlib import Path
from config import load_config
from version import VERSION
from update_check import check_update_status
from console import Console, _trim, _BOX_CONTENT
from estimator import Estimator, timing_profile
from mesh_readers import SourceSnapshot, _load_mesh_arrays, _quick_tri_count
from options import _arg_reduction, _arg_positive_float, _parse_reduction, _reduction_label
BYTES_PER_KB = 1024
_STEP_SCHEMAS = {"ap203": "AP203", "ap214": "AP214IS", "ap242": "AP242DIS"}


def _file_signature(path: Path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _wait_for_stable_file(path: Path, checks: int=3, interval: float=0.5, timeout: float=30.0):
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
    raise TimeoutError(f'file did not become stable within {timeout:g}s')


class CliApp:
    def __init__(self, settings=None, project_dir=None, config_path=None):
        self.project_dir = Path(project_dir) if project_dir is not None else Path(__file__).resolve().parent.parent
        self.config_path = Path(config_path) if config_path is not None else self.project_dir / 'data' / 'config.json'
        if settings is None:
            self.settings, self.warnings = load_config(self.config_path, create=False)
        else:
            self.settings, self.warnings = (settings, [])
        self.estimator = Estimator(self.project_dir / 'data' / 'estimator.json')
        self.console = Console(self.estimator)
        self._prepared_source = None
        self._prepared_mesh = None
        self._prepared_report = None
        self._prepared_snapshot = None
        self._claimed_outputs = {}

    def convert(self, *args, **kwargs):
        from converter import convert
        source = args[0] if args else kwargs.get('input_path')
        if source is not None and Path(source).resolve() == self._prepared_source:
            mesh = kwargs.get('_mesh_data')
            if mesh is None or mesh is self._prepared_mesh:
                kwargs['_mesh_data'] = self._prepared_mesh
                kwargs['_mesh_report'] = self._prepared_report
                kwargs['_source_snapshot'] = self._prepared_snapshot
        return convert(
            *args,
            **kwargs,
            settings=self.settings,
            progress=self.console,
            estimator=self.estimator,
        )

    def models_dir(self) -> Path:
        return self.project_dir / self.settings.input_folder_name

    def _requires_repair(self, path):
        if (not self.settings.repair_intersecting_mesh or self._prepared_report is None
                or Path(path).resolve() != self._prepared_source):
            return False
        from mesh_geometry import _needs_volume_repair
        return _needs_volume_repair(self._prepared_report)

    def _prepare_source(self, source):
        self._prepared_source = Path(source).resolve()
        self._prepared_mesh = None
        self._prepared_report = None
        self._prepared_snapshot = None
        if not self.settings.repair_intersecting_mesh or source.suffix.lower() in self.settings.iges_extensions:
            return
        self._prepared_mesh = self._preload_mesh(source)
        if self._prepared_mesh is None:
            return
        from mesh_geometry import _conversion_quality_report
        try:
            self._prepared_report = _conversion_quality_report(*self._prepared_mesh, settings=self.settings)
        except Exception:
            return
        if self._requires_repair(source):
            print(f'  {self.console.Y}!  {_trim(source.name)}: damage detected; automatic repair, reduction skipped{self.console.X}')

    def _source_triangle_count(self, source):
        if Path(source).resolve() == self._prepared_source and self._prepared_mesh is not None:
            return len(self._prepared_mesh[1])
        return _quick_tri_count(source, settings=self.settings)

    def _automatic_reduction_fractions(self, path: Path, fractions, triangle_count=None):
        if self._requires_repair(path):
            return [None]
        if (
            fractions
            or not self.settings.auto_reduction_enabled
            or path.suffix.lower() in self.settings.iges_extensions
        ):
            return fractions
        count = triangle_count if triangle_count is not None else self._source_triangle_count(path)
        if count is None or count <= self.settings.auto_reduction_target_triangles:
            return fractions
        keep_fraction = self.settings.auto_reduction_target_triangles / count
        return [max(0.01, min(0.99, keep_fraction))]

    def _make_output_path(self, src: Path, base_dir: Path, fraction) -> Path:
        repair_label = ' [repaired]' if self._requires_repair(src) else ''
        return base_dir / f'{src.stem}{repair_label} [{_reduction_label(fraction)}]{self.settings.step_file_extension}'

    def _is_up_to_date(self, src: Path, dst: Path, force: bool) -> bool:
        if not self.settings.skip_up_to_date_outputs or force:
            return False
        try:
            source_stat = src.stat()
            output_stat = dst.stat()
        except OSError:
            return False
        if not dst.is_file() or output_stat.st_size == 0:
            return False
        dependency_paths = (
            *Path(__file__).resolve().parent.glob('*.py'),
            Path(__file__).with_name('environment.yml'),
            self.config_path,
        )
        newest_input_mtime = source_stat.st_mtime_ns
        for dependency in dependency_paths:
            try:
                newest_input_mtime = max(newest_input_mtime, dependency.stat().st_mtime_ns)
            except OSError:
                return False
        if output_stat.st_mtime_ns < newest_input_mtime:
            return False
        if self.settings.generate_png_preview:
            preview = dst.with_suffix('.png')
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

    def _scan_supported_files(self, folder: Path):
        result = {}
        try:
            entries = list(folder.iterdir())
        except OSError:
            return result
        for entry in entries:
            try:
                if entry.is_file() and entry.suffix.lower() in self.settings.supported_extensions:
                    result[entry.name] = _file_signature(entry)
            except OSError:
                continue
        return result

    def _preload_mesh(self, src: Path):
        if src.resolve() == self._prepared_source and self._prepared_mesh is not None:
            return self._prepared_mesh
        from mesh_geometry import _repair_mesh_arrays
        if src.suffix.lower() in self.settings.iges_extensions:
            return None
        try:
            snapshot = SourceSnapshot.capture(src)
            verts, tris = _load_mesh_arrays(src, settings=self.settings)
            verts, tris = _repair_mesh_arrays(verts, tris, settings=self.settings)
            snapshot.validate()
            self._prepared_source = src.resolve()
            self._prepared_mesh = (verts, tris)
            self._prepared_report = None
            self._prepared_snapshot = snapshot
            return self._prepared_mesh
        except Exception:
            return None

    def _run_batch(self, files, n, out_dir, args, reduce_fractions, step_schema):
        ok_n = fail_n = skip_n = 0
        failed_sources = set()
        _lock_fracs = None
        _locked = False
        claimed_outputs = self._claimed_outputs = {}

        def claim_output(source, output):
            key = os.path.normcase(str(output.resolve()))
            previous = claimed_outputs.get(key)
            if previous is None:
                claimed_outputs[key] = source
            return (key, previous)

        def release_output(key, source):
            if claimed_outputs.get(key) == source:
                claimed_outputs.pop(key, None)
        for i, src_file in enumerate(files):
            self._prepare_source(src_file)
            base_dir = out_dir or src_file.parent
            is_iges = src_file.suffix.lower() in self.settings.iges_extensions
            _is_interactive = (
                self.settings.ask_for_reduction
                and args.reduce is None
                and sys.stdin.isatty()
                and not _locked
                and not is_iges
                and not self._requires_repair(src_file)
            )
            eff_fractions = None if is_iges else _lock_fracs if _locked else reduce_fractions
            eff_fractions = self._automatic_reduction_fractions(src_file, eff_fractions)
            src_kb = src_file.stat().st_size // BYTES_PER_KB
            size_str = f'{src_kb:,} KB'
            progress = f'[{i + 1}/{n}]'
            if is_iges and reduce_fractions:
                print(f'  {self.console.Y}!  {_trim(src_file.name)}: reduction ignored for IGES input{self.console.X}')
            if _is_interactive and (not args.dry_run):
                self.console._box_top()
                self.console._box_input_row(progress, src_file.name, size_str)
                self.console._box_sep()
                _t_read = self.console._step_start('reading')
                n_tris_preview = self._source_triangle_count(src_file)
                _read_detail = f'{n_tris_preview:,} triangles' if n_tris_preview is not None else ''
                self.console._step_end(_t_read, _read_detail)
                if n_tris_preview is not None:
                    estimate_fraction = eff_fractions[0] if eff_fractions else None
                    self.console._show_early_estimate(
                        n_tris_preview, fmt=src_file.suffix.lower(),
                        profile=timing_profile(self.settings, estimate_fraction, step_schema,
                                               self._prepared_mesh is not None, tolerance=args.tolerance),
                    )
                else:
                    self.console._box_sep()
                chosen_fracs, lock_all = self.console._reduce_prompt(
                    eff_fractions,
                    n_tris=n_tris_preview,
                    batch=True,
                )
                if lock_all:
                    _lock_fracs = chosen_fracs or [None]
                    _locked = True
                    args.reduce = _lock_fracs
                _inter_fracs = chosen_fracs or [None]
                _inter_mesh = self._preload_mesh(src_file) if len(_inter_fracs) > 1 else None
                for _i_cfrac, _cfrac in enumerate(_inter_fracs):
                    out_file = self._make_output_path(src_file, base_dir, _cfrac)
                    _claim_key, _claimed_by = claim_output(src_file, out_file)
                    _up_to_date = self._is_up_to_date(src_file, out_file, args.force)
                    if _i_cfrac > 0:
                        self.console._box_sep()
                    if _claimed_by is not None:
                        self.console._box_file_row(
                            'SKIP',
                            out_file.name,
                            f'same output as {_claimed_by.name}',
                            self.console.DIM,
                        )
                        skip_n += 1
                        continue
                    if _up_to_date:
                        _skip_kb = out_file.stat().st_size // BYTES_PER_KB
                        self.console._box_file_row(
                            'SKIP',
                            out_file.name,
                            f'up to date | {_skip_kb:,} KB',
                            self.console.DIM,
                        )
                        skip_n += 1
                        continue
                    _t0 = time.perf_counter()
                    success, info = self.convert(
                        src_file,
                        out_file,
                        args.tolerance,
                        _cfrac,
                        step_schema=step_schema,
                        _suppress_read_step=True,
                        _mesh_data=_inter_mesh,
                    )
                    _elapsed = time.perf_counter() - _t0
                    self.console._box_sep()
                    if success:
                        self.console._box_success_row(out_file.name, info, _elapsed)
                        ok_n += 1
                    else:
                        release_output(_claim_key, src_file)
                        failed_sources.add(src_file)
                        self.console._box_err(info)
                        fail_n += 1
                self.console._box_bot()
                print()
                continue
            fracs = list(eff_fractions) if eff_fractions else [None]
            if args.dry_run:
                for _efrac in fracs:
                    out_file = self._make_output_path(src_file, base_dir, _efrac)
                    _claim_key, _claimed_by = claim_output(src_file, out_file)
                    if _claimed_by is not None:
                        print(f'  {self.console.DIM}-  {_trim(src_file.name)}  same output as {_claimed_by.name}{self.console.X}')
                        skip_n += 1
                        continue
                    _up_to_date = self._is_up_to_date(src_file, out_file, args.force)
                    if _up_to_date:
                        print(f'  {self.console.DIM}-  {_trim(src_file.name)}  up to date{self.console.X}')
                        skip_n += 1
                    else:
                        print(f'  {self.console.C}->  {_trim(src_file.name)}  {src_kb:,} KB -> {out_file.name}{self.console.X}')
                        ok_n += 1
                continue
            _preloaded_mesh = self._preload_mesh(src_file) if len(fracs) > 1 else None
            self.console._box_top()
            self.console._box_input_row(progress, src_file.name, size_str)
            self.console._box_sep()
            for _i_efrac, _efrac in enumerate(fracs):
                out_file = self._make_output_path(src_file, base_dir, _efrac)
                _claim_key, _claimed_by = claim_output(src_file, out_file)
                _up_to_date = self._is_up_to_date(src_file, out_file, args.force)
                if _i_efrac > 0:
                    self.console._box_sep()
                if _claimed_by is not None:
                    self.console._box_file_row(
                        'SKIP',
                        out_file.name,
                        f'same output as {_claimed_by.name}',
                        self.console.DIM,
                    )
                    skip_n += 1
                    continue
                if _up_to_date:
                    _skip_kb = out_file.stat().st_size // BYTES_PER_KB
                    self.console._box_file_row(
                        'SKIP',
                        out_file.name,
                        f'up to date | {_skip_kb:,} KB',
                        self.console.DIM,
                    )
                    skip_n += 1
                    continue
                _t0 = time.perf_counter()
                success, info = self.convert(
                    src_file,
                    out_file,
                    args.tolerance,
                    _efrac,
                    step_schema=step_schema,
                    _mesh_data=_preloaded_mesh,
                )
                _elapsed = time.perf_counter() - _t0
                self.console._box_sep()
                if success:
                    self.console._box_success_row(out_file.name, info, _elapsed)
                    ok_n += 1
                else:
                    release_output(_claim_key, src_file)
                    failed_sources.add(src_file)
                    self.console._box_err(info)
                    fail_n += 1
            self.console._box_bot()
            print()
        return (ok_n, fail_n, skip_n, failed_sources)

    def run(self, argv=None):
        parser = argparse.ArgumentParser(description='Convert to STEP.')
        parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')
        parser.add_argument(
            '--repair', action=argparse.BooleanOptionalAction, default=None,
            help=f'try experimental OpenCASCADE repair of closed damaged meshes; inspect the result (default: {self.settings.repair_intersecting_mesh})',
        )
        parser.add_argument(
            '--update-check',
            action=argparse.BooleanOptionalAction,
            default=True,
            help='check GitHub for a newer release at startup (default: enabled)',
        )
        parser.add_argument(
            'input',
            nargs='*',
            help='input file(s); omit to convert everything in the models/ folder',
        )
        parser.add_argument('--output', '-o', metavar='FILE', help='output path (single-file mode only)')
        parser.add_argument(
            '--output-dir',
            '-d',
            metavar='DIR',
            help='write output files to this directory instead of alongside the source',
        )
        parser.add_argument(
            '--tolerance',
            '-t',
            type=_arg_positive_float,
            default=None,
            help=f'sewing tolerance (default: {self.settings.sewing_tolerance})',
        )
        parser.add_argument(
            '--reduce',
            '-r',
            metavar='PCT',
            type=_arg_reduction,
            help='reduce mesh by this %% of triangles before converting (0 = off)',
        )
        parser.add_argument(
            '--format',
            metavar='SCHEMA',
            default=None,
            choices=['ap203', 'ap214', 'ap242'],
            help=f'STEP schema: ap203, ap214, ap242 (default: {self.settings.default_step_format})',
        )
        parser.add_argument(
            '--force',
            '-f',
            action='store_true',
            help='re-convert files even if the output is already up-to-date',
        )
        parser.add_argument(
            '--dry-run',
            '--dry',
            action='store_true',
            help='show what would be converted without actually converting',
        )
        parser.add_argument(
            '--watch',
            '-w',
            action='store_true',
            help='after batch conversion, watch the folder and convert new or changed files',
        )
        parser.add_argument(
            '--preview',
            action=argparse.BooleanOptionalAction,
            default=self.settings.generate_png_preview,
            help=f'generate a .png preview alongside each .stp (default: {self.settings.generate_png_preview})',
        )
        parser.add_argument(
            '--experimental-parametric',
            action=argparse.BooleanOptionalAction,
            default=None,
            help=f'try experimental exact linear-extrusion reconstruction (default: {self.settings.experimental_parametric_reconstruction})',
        )
        parser.add_argument(
            '--pause',
            action=argparse.BooleanOptionalAction,
            default=None,
            help='pause before exiting (default: only in an interactive terminal)',
        )
        args = parser.parse_args(argv)
        explicit_geometry_options = (
            args.tolerance is not None
            or args.format is not None
            or args.experimental_parametric is not None
            or args.repair is not None
        )
        if args.tolerance is None:
            args.tolerance = self.settings.sewing_tolerance
        selected_format = args.format or self.settings.default_step_format
        if explicit_geometry_options:
            args.force = True
        self.settings = self.settings.with_overrides(generate_png_preview=args.preview)
        if args.repair is not None:
            self.settings = self.settings.with_overrides(repair_intersecting_mesh=args.repair)
        if args.experimental_parametric is not None:
            self.settings = self.settings.with_overrides(experimental_parametric_reconstruction=args.experimental_parametric)
        self.console.pause_mode = args.pause
        if args.output and len(args.input) != 1:
            parser.error('--output requires exactly one input file')
        if args.output and args.output_dir:
            parser.error('--output and --output-dir cannot be used together')
        if args.watch and args.input:
            parser.error('--watch operates on the models folder and cannot be combined with input files')
        if args.watch and args.dry_run:
            parser.error('--watch cannot be combined with --dry-run')
        if args.output and args.reduce is not None and (len(args.reduce) > 1):
            parser.error('--output cannot be combined with multiple reduction percentages')
        if args.reduce is not None:
            reduce_fractions = args.reduce
        elif isinstance(self.settings.default_reduction_percent, str):
            reduce_fractions = _parse_reduction(self.settings.default_reduction_percent, strict=True)
        elif 0 < self.settings.default_reduction_percent < 100:
            reduce_fractions = [(100.0 - self.settings.default_reduction_percent) / 100.0]
        else:
            reduce_fractions = None
        if args.output and reduce_fractions and (len(reduce_fractions) > 1):
            parser.error('--output cannot be combined with multiple default reductions')
        step_schema = _STEP_SCHEMAS[selected_format]
        self.console.estimate_profile = timing_profile(self.settings, step_schema=step_schema, tolerance=args.tolerance)
        if not self.config_path.exists():
            load_config(self.config_path, create=True)
        print()
        _w = _BOX_CONTENT + 4
        print(f"  {self.console.C}{self.console.B}+{'=' * _w}+{self.console.X}")
        print(f"  {self.console.C}{self.console.B}|{f'2STEP-Converter {VERSION}':^{_w}}|{self.console.X}")
        print(f"  {self.console.C}{self.console.B}+{'=' * _w}+{self.console.X}")
        print()
        if args.update_check:
            print(f'  {self.console.DIM}Checking for updates...{self.console.X}', flush=True)
            update = check_update_status(VERSION)
            if update.status == 'update':
                print(f'  {self.console.Y}!  New version available: {VERSION} -> {update.release.version}{self.console.X}')
                print(f'  Download: {update.release.url}')
            elif update.status == 'current':
                print(f'  {self.console.G}Version {VERSION} is up to date.{self.console.X}')
            else:
                print(f'  {self.console.Y}Could not check for updates. Continuing with version {VERSION}.{self.console.X}')
            print()
        if (
            self.settings.ask_for_reduction and args.reduce is None
            and not args.dry_run and sys.stdin.isatty()
            and (not args.input or any(
                Path(source).suffix.lower() not in self.settings.iges_extensions
                for source in args.input
            ))
        ):
            common_reduction = self.console._startup_reduce_prompt(single_output=bool(args.output))
            if common_reduction is not None:
                reduce_fractions = common_reduction
                args.reduce = common_reduction
        if self.warnings:
            for _w_msg in self.warnings:
                print(f'  {self.console.Y}!  {_w_msg}{self.console.X}')
            print()
        out_dir = Path(args.output_dir).resolve() if args.output_dir else None
        if len(args.input) > 1:
            files = []
            invalid = []
            for p in args.input:
                fp = Path(p).resolve()
                if not fp.is_file():
                    invalid.append(f'file not found: {fp}')
                elif fp.suffix.lower() not in self.settings.supported_extensions:
                    invalid.append(f'unsupported format: {fp.name}')
                else:
                    files.append(fp)
            if invalid:
                parser.error('; '.join(invalid))
            n = len(files)
            print(f"  {n} file{('s' if n > 1 else '')} to convert\n")
            ok_n, fail_n, skip_n, _ = self._run_batch(files, n, out_dir, args, reduce_fractions, step_schema)
            self.console._print_summary(ok_n, fail_n, skip_n, dry_run=args.dry_run)
            print()
            self.console._pause()
            sys.exit(0 if fail_n == 0 else 1)
        if len(args.input) == 1:
            input_path = Path(args.input[0]).resolve()
            if not input_path.is_file():
                print(f'  {self.console.R}[ERROR]{self.console.X} File not found: {input_path}\n')
                self.console._pause()
                sys.exit(1)
            if input_path.suffix.lower() not in self.settings.supported_extensions:
                parser.error(f'unsupported format: {input_path.name}')
            if args.output and Path(args.output).resolve() == input_path:
                parser.error('--output must not overwrite the input file')
            self._prepare_source(input_path)
            _single_interactive = (
                self.settings.ask_for_reduction
                and args.reduce is None
                and sys.stdin.isatty()
                and input_path.suffix.lower() not in self.settings.iges_extensions
                and not self._requires_repair(input_path)
            )
            _single_base_dir = out_dir or input_path.parent
            src_kb = input_path.stat().st_size // BYTES_PER_KB
            size_str = f'{src_kb:,} KB'
            single_fractions = self._automatic_reduction_fractions(input_path, reduce_fractions)
            if input_path.suffix.lower() in self.settings.iges_extensions:
                if reduce_fractions:
                    print(f'  {self.console.Y}!  reduction ignored for IGES input{self.console.X}\n')
                fractions = [None]
            else:
                fractions = list(single_fractions) if single_fractions else [None]
            if args.dry_run:
                for _dfrac in fractions:
                    _dout = Path(args.output).resolve() if args.output else self._make_output_path(
                        input_path,
                        _single_base_dir,
                        _dfrac,
                    )
                    if self._is_up_to_date(input_path, _dout, args.force):
                        print(f'  {self.console.DIM}-  {_trim(input_path.name)}  up to date{self.console.X}\n')
                    else:
                        print(f'  {self.console.C}->  {_trim(input_path.name)}  {src_kb:,} KB -> {_dout.name}{self.console.X}\n')
                self.console._pause()
                sys.exit(0)
            self.console._box_top()
            self.console._box_input_row('[1/1]', input_path.name, size_str)
            self.console._box_sep()
            if _single_interactive:
                _t_read = self.console._step_start('reading')
                n_tris_preview = self._source_triangle_count(input_path)
                self.console._step_end(
                    _t_read,
                    f'{n_tris_preview:,} triangles' if n_tris_preview is not None else '',
                )
                if n_tris_preview is not None:
                    estimate_fraction = single_fractions[0] if single_fractions else None
                    self.console._show_early_estimate(
                        n_tris_preview, fmt=input_path.suffix.lower(),
                        profile=timing_profile(self.settings, estimate_fraction, step_schema,
                                               self._prepared_mesh is not None, tolerance=args.tolerance),
                    )
                else:
                    self.console._box_sep()
                chosen, _ = self.console._reduce_prompt(single_fractions, n_tris=n_tris_preview)
                fractions = list(chosen) if chosen else [None]
            if args.output and len(fractions) > 1:
                self.console._box_err('--output cannot be used with multiple interactive reductions')
                self.console._box_bot()
                print()
                self.console._pause()
                sys.exit(2)
            outputs = [Path(args.output).resolve() if args.output else self._make_output_path(
                input_path,
                _single_base_dir,
                fraction,
            ) for fraction in fractions]
            pending = [(fraction, output) for fraction, output in zip(
                fractions,
                outputs,
            ) if not self._is_up_to_date(
                input_path,
                output,
                args.force,
            )]
            preloaded_mesh = self._preload_mesh(input_path) if (
                len(pending) > 1
                and input_path.suffix.lower() not in self.settings.iges_extensions
            ) else None
            any_fail = False
            success_count = skip_count = fail_count = 0
            for index, (fraction, output_path) in enumerate(zip(fractions, outputs)):
                if index > 0:
                    self.console._box_sep()
                if self._is_up_to_date(input_path, output_path, args.force):
                    out_kb = output_path.stat().st_size // BYTES_PER_KB
                    self.console._box_file_row(
                        'SKIP',
                        output_path.name,
                        f'up to date | {out_kb:,} KB',
                        self.console.DIM,
                    )
                    skip_count += 1
                    continue
                started = time.perf_counter()
                success, info = self.convert(
                    input_path,
                    output_path,
                    args.tolerance,
                    fraction,
                    step_schema=step_schema,
                    _mesh_data=preloaded_mesh,
                )
                elapsed = time.perf_counter() - started
                self.console._box_sep()
                if success:
                    self.console._box_success_row(output_path.name, info, elapsed)
                    success_count += 1
                else:
                    self.console._box_err(info)
                    any_fail = True
                    fail_count += 1
            self.console._box_bot()
            print()
            self.console._print_summary(success_count, fail_count, skip_count)
            print()
            self.console._pause()
            sys.exit(1 if any_fail else 0)
        folder = self.models_dir()
        folder.mkdir(parents=True, exist_ok=True)
        files = sorted((f for f in folder.iterdir() if (
            f.is_file()
            and f.suffix.lower() in self.settings.supported_extensions
        )))
        if not files:
            print(f'  No supported files found in {self.settings.input_folder_name}\\\n')
            ok_n = fail_n = skip_n = 0
            initial_failed_sources = set()
            if not args.watch:
                self.console._pause()
                sys.exit(0)
        else:
            n = len(files)
            print(f"  {n} file{('s' if n > 1 else '')} found in {self.console.C}{self.settings.input_folder_name}\\{self.console.X}\n")
            ok_n, fail_n, skip_n, initial_failed_sources = self._run_batch(
                files,
                n,
                out_dir,
                args,
                reduce_fractions,
                step_schema,
            )
            self.console._print_summary(ok_n, fail_n, skip_n, dry_run=args.dry_run)
            print()
        if args.watch and (not args.dry_run):
            if args.reduce is not None:
                reduce_fractions = args.reduce
            known = self._scan_supported_files(folder)
            unresolved_failures = {path.name for path in initial_failed_sources}
            for failed_name in unresolved_failures:
                known.pop(failed_name, None)
            print(f'  {self.console.DIM}Watching {self.console.C}{self.settings.input_folder_name}\\{self.console.X}{self.console.DIM}  Ctrl+C to stop{self.console.X}\n')
            try:
                while True:
                    time.sleep(2)
                    current = self._scan_supported_files(folder)
                    self._claimed_outputs = {
                        key: source for key, source in self._claimed_outputs.items()
                        if source.name in current
                    }
                    changed_names = [name for (
                        name,
                        signature,
                    ) in current.items() if known.get(name) != signature]
                    missing_names = (set(known) | unresolved_failures) - set(current)
                    for missing_name in missing_names:
                        known.pop(missing_name, None)
                        unresolved_failures.discard(missing_name)
                    for name in sorted(changed_names):
                        src = folder / name
                        try:
                            stable_signature = _wait_for_stable_file(src)
                        except (OSError, TimeoutError) as exc:
                            print(f'  {self.console.R}X  {name}: {exc}{self.console.X}')
                            unresolved_failures.add(name)
                            continue
                        src_kb = stable_signature[0] // BYTES_PER_KB
                        size_str = f'{src_kb:,} KB'
                        self._prepare_source(src)
                        watch_fractions = self._automatic_reduction_fractions(src, reduce_fractions)
                        all_ok = True
                        self.console._box_top()
                        self.console._box_input_row('[changed]', src.name, size_str)
                        self.console._box_sep()
                        if (
                            self.settings.ask_for_reduction and args.reduce is None
                            and sys.stdin.isatty() and not self._requires_repair(src)
                            and src.suffix.lower() not in self.settings.iges_extensions
                        ):
                            watch_fractions, lock_all = self.console._reduce_prompt(
                                watch_fractions, n_tris=self._source_triangle_count(src), batch=True,
                            )
                            if lock_all:
                                reduce_fractions = watch_fractions or [None]
                                args.reduce = reduce_fractions
                        _wfracs = [None] if src.suffix.lower() in self.settings.iges_extensions else list(watch_fractions) if watch_fractions else [None]
                        _watch_mesh = self._preload_mesh(src) if len(_wfracs) > 1 else None
                        for _i_wfrac, _wfrac in enumerate(_wfracs):
                            dst = self._make_output_path(src, out_dir or folder, _wfrac)
                            output_key = os.path.normcase(str(dst.resolve()))
                            claimed_by = self._claimed_outputs.get(output_key)
                            if _i_wfrac > 0:
                                self.console._box_sep()
                            if claimed_by is not None and claimed_by != src:
                                self.console._box_file_row(
                                    'SKIP', dst.name, f'same output as {claimed_by.name}', self.console.DIM,
                                )
                                continue
                            if self._is_up_to_date(src, dst, args.force):
                                self._claimed_outputs[output_key] = src
                                out_kb = dst.stat().st_size // BYTES_PER_KB
                                self.console._box_file_row(
                                    'SKIP',
                                    dst.name,
                                    f'up to date | {out_kb:,} KB',
                                    self.console.DIM,
                                )
                                continue
                            _t0 = time.perf_counter()
                            success, info = self.convert(
                                src,
                                dst,
                                args.tolerance,
                                _wfrac,
                                step_schema=step_schema,
                                _mesh_data=_watch_mesh,
                            )
                            elapsed = time.perf_counter() - _t0
                            self.console._box_sep()
                            if success:
                                self._claimed_outputs[output_key] = src
                                self.console._box_success_row(dst.name, info, elapsed)
                            else:
                                self.console._box_err(info)
                                all_ok = False
                                unresolved_failures.add(name)
                        self.console._box_bot()
                        print()
                        if all_ok:
                            known[name] = stable_signature
                            unresolved_failures.discard(name)
            except KeyboardInterrupt:
                print(f'\n  {self.console.DIM}Watch stopped.{self.console.X}\n')
            sys.exit(0 if not unresolved_failures else 1)
        else:
            self.console._pause()
            sys.exit(0 if fail_n == 0 else 1)


def main(argv=None):
    app = CliApp()
    try:
        return app.run(argv)
    except Exception:
        traceback.print_exc()
        app.console._pause("\n  Press Enter to exit...")
        raise SystemExit(1)
    finally:
        app.console.close()


if __name__ == "__main__":
    main()
