import os
import sys
import time
import threading
import textwrap
from options import _fmt_time, _parse_reduction, _reduction_label
from estimator import _EST_MIN, Estimate
NAME_TRIM_WIDTH = 62
_BOX_CONTENT = 72
_BOX_LABEL = 13
_BOX_STATUS = 7
_BOX_TIME = 8
_BOX_DETAIL = _BOX_CONTENT - _BOX_LABEL - _BOX_STATUS - _BOX_TIME
_TIMER_PAD = _BOX_STATUS + _BOX_DETAIL + _BOX_TIME


def _trim(name: str, width: int=NAME_TRIM_WIDTH) -> str:
    return name if len(name) <= width else name[:width - 3] + '...'


def _result_details(info: dict) -> str:
    parts = []
    solids = info.get('solids', 0)
    open_shells = info.get('open_shells', 0)
    if solids:
        parts.append(f"{solids:,} solid{('s' if solids != 1 else '')}")
    elif open_shells:
        parts.append(f"{open_shells:,} open shell{('s' if open_shells != 1 else '')}")
    if info.get('faces') is not None:
        parts.append(f"{info['faces']:,} faces")
    if info.get('schema'):
        parts.append(info['schema'])
    return ' | '.join(parts)


def _err_line(tb: str) -> str:
    lines = [l for l in tb.splitlines() if l.strip()]
    return lines[-1] if lines else tb


class Console:
    def __init__(self, estimator=None, pause_mode=None):
        self.estimator = estimator
        self.estimate_profile = None
        self.pause_mode = pause_mode
        use_color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
        self.G = '\x1b[92m' if use_color else ''
        self.R = '\x1b[91m' if use_color else ''
        self.Y = '\x1b[93m' if use_color else ''
        self.C = '\x1b[96m' if use_color else ''
        self.DIM = '\x1b[2m' if use_color else ''
        self.B = '\x1b[1m' if use_color else ''
        self.X = '\x1b[0m' if use_color else ''
        self._step_open = False
        self._timer_label = ''
        self._timer_t0 = 0.0
        self._timer_stop = threading.Event()
        self._timer_th = None
        self._real_stdout_fd = None
        if use_color:
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleMode(
                    ctypes.windll.kernel32.GetStdHandle(-11), 7)
            except (AttributeError, OSError):
                pass

    def close(self):
        self._stop_timer()
        if self._real_stdout_fd is not None:
            os.close(self._real_stdout_fd)
            self._real_stdout_fd = None

    def _pause(self, prompt: str='  Press Enter to exit...') -> None:
        should_pause = self.pause_mode is True or (self.pause_mode is None and sys.stdin.isatty())
        if should_pause:
            try:
                input(prompt)
            except EOFError:
                pass

    def _box_top(self):
        print(f"  {self.DIM}+{'-' * (_BOX_CONTENT + 4)}+{self.X}")

    def _box_sep(self):
        print(f"  {self.DIM}+{'-' * (_BOX_CONTENT + 4)}+{self.X}")

    def _box_bot(self):
        print(f"  {self.DIM}+{'-' * (_BOX_CONTENT + 4)}+{self.X}")

    def _box_row(self, left: str, right: str='', lc: str='', rc: str='') -> None:
        max_left = _BOX_CONTENT - len(right) - (1 if right else 0)
        if len(left) > max_left:
            left = left[:max_left - 3] + '...'
        gap = _BOX_CONTENT - len(left) - len(right)
        print(f"  {self.DIM}|{self.X}  {lc}{left}{self.X}{' ' * gap}{rc}{right}{self.X}  {self.DIM}|{self.X}")

    def _box_file_row(self, status: str, name: str, details: str='', color: str='') -> None:
        prefix = f'OUTPUT  {status:<6}'
        width = _BOX_CONTENT - len(prefix) - len(details) - (1 if details else 0)
        self._box_row(f'{prefix}{_trim(name, width)}', details, lc=color, rc=color)

    def _box_input_row(self, progress: str, name: str, size: str) -> None:
        prefix = f'INPUT   {progress:<10}'
        width = _BOX_CONTENT - len(prefix) - len(size) - 1
        self._box_row(f'{prefix}{_trim(name, width)}', size, lc=self.B, rc=self.DIM)

    def _box_success_row(self, name: str, info: dict, elapsed: float) -> None:
        self._box_file_row('OK', name, f"{info['kb']:,} KB | {_fmt_time(elapsed)}", f'{self.G}{self.B}')
        details = _result_details(info)
        if details:
            self._box_row(f'DETAILS       {details}', lc=self.C)
        if info.get('repair'):
            self._box_row('REPAIRED      Inspect walls, cavities and body connections in CAD.', lc=self.Y)

    def _stop_timer(self) -> None:
        self._timer_stop.set()
        t = self._timer_th
        if t is not None:
            t.join(timeout=1.0)
            self._timer_th = None

    def _step_start(self, label: str) -> float:
        self._step_open = True
        self._timer_label = label.upper()
        t0 = time.perf_counter()
        self._timer_t0 = t0
        self._timer_stop.clear()
        label = self._timer_label
        print(f'  {self.DIM}|{self.X}  {self.DIM}{label:<{_BOX_LABEL}}', end='', flush=True)
        if not sys.stdout.isatty():
            self._timer_th = None
            return t0

        def _tick():
            while not self._timer_stop.wait(0.5):
                t_str = _fmt_time(time.perf_counter() - self._timer_t0)
                line = f'\r  {self.DIM}|{self.X}  {self.DIM}{label:<{_BOX_LABEL}}{self.DIM}{t_str:>{_TIMER_PAD}}{self.X}\x1b[K'
                try:
                    os.write(self._real_stdout_fd, line.encode())
                except Exception:
                    pass
        if self._real_stdout_fd is None:
            self._real_stdout_fd = os.dup(1)
        self._timer_th = threading.Thread(target=_tick, daemon=True)
        self._timer_th.start()
        return t0

    def _step_end(self, t0: float, detail: str='') -> None:
        self._stop_timer()
        self._step_open = False
        elapsed = time.perf_counter() - t0
        if len(detail) > _BOX_DETAIL:
            detail = detail[:_BOX_DETAIL - 3] + '...'
        time_str = _fmt_time(elapsed)
        print(f"\r  {self.DIM}|{self.X}  {self.DIM}{self._timer_label:<{_BOX_LABEL}}{self.X}{self.G}{'OK':<{_BOX_STATUS}}{self.X}{self.C}{detail:<{_BOX_DETAIL}}{self.X}{self.Y}{time_str:>{_BOX_TIME}}{self.X}  {self.DIM}|{self.X}")

    def _step_fail(self) -> None:
        if self._step_open:
            self._stop_timer()
            self._step_open = False
            print(f"\r  {self.DIM}|{self.X}  {self.DIM}{self._timer_label:<{_BOX_LABEL}}{self.X}{self.R}{'FAIL':<{_BOX_STATUS}}{'step failed':<{_BOX_DETAIL}}{'-':>{_BOX_TIME}}{self.X}  {self.DIM}|{self.X}")

    def _show_estimate(self, metric, size, fmt, elapsed, profile, label):
        result = self.estimator.estimate(metric, size, fmt, profile or self.estimate_profile) if self.estimator else Estimate(None, 0)
        self._box_sep()
        if result.seconds is not None:
            if elapsed >= (result.upper if result.upper is not None else result.seconds):
                self._box_row('ETA     taking longer than estimated',
                              'updated after sewing', lc=self.Y, rc=self.DIM)
                self._box_sep()
                return
            remaining = max(0.5, result.seconds - elapsed)
            detail = f'{label} | {result.samples} runs'
            if result.kind == 'legacy':
                detail = f'legacy estimate | {result.samples} runs'
            elif result.error_percent is not None:
                detail += f' | err ~{result.error_percent:.0f}%'
            else:
                detail += ' | limited data'
            self._box_row(
                f'ETA     ~{_fmt_time(remaining)} remaining',
                detail,
                lc=self.Y,
                rc=self.DIM,
            )
            if result.lower is not None and result.upper is not None:
                low, high = max(0.5, result.lower - elapsed), max(0.5, result.upper - elapsed)
                self._box_row(f'RANGE   ~{_fmt_time(low)} - {_fmt_time(high)} remaining',
                              'extrapolated' if result.extrapolated else 'approximate', lc=self.DIM, rc=self.DIM)
        else:
            self._box_row(
                (f'ETA     learning...  {result.samples} of {_EST_MIN} conversions'
                 if result.samples < _EST_MIN else 'ETA     unavailable for this model size'),
                f'{label} estimate',
                lc=self.DIM,
                rc=self.DIM,
            )
        self._box_sep()

    def _show_post_sew_estimate(self, n_faces: int, fmt: str=None, profile=None) -> None:
        self._show_estimate('f', n_faces, fmt, 0.0, profile, 'updated')

    def _show_early_estimate(self, n_triangles: int, fmt: str=None, elapsed: float=0.0, profile=None) -> None:
        self._show_estimate('t', n_triangles, fmt, elapsed, profile, 'early')

    def _startup_reduce_prompt(self, single_output=False):
        self._box_top()
        self._box_row('REDUCTION FOR ALL MODELS', lc=self.B)
        self._box_row('Enter a percentage below 100 for all models. 0 = off.')
        self._box_row('Press Enter to choose separately for each model.')
        self._box_bot()
        while True:
            try:
                raw = input('  Reduction [%, Enter = per model]: ').strip()
            except EOFError:
                raw = ''
            if not raw:
                print()
                return None
            try:
                fractions = _parse_reduction(raw, strict=True)
                if single_output and len(fractions) > 1:
                    raise ValueError('use one percentage with --output')
            except ValueError as exc:
                print(f'  {self.Y}Invalid reduction: {exc}{self.X}')
                continue
            print()
            return fractions

    def _reduce_prompt(self, default_fractions, n_tris=None, batch=False):
        if isinstance(default_fractions, float):
            default_fractions = [default_fractions]
        if default_fractions:
            pct_str = ','.join((_reduction_label(f) for f in default_fractions))
        else:
            pct_str = '0'
        hint = ''
        if (
            n_tris is not None
            and default_fractions
            and len(default_fractions) == 1
            and default_fractions[0] is not None
        ):
            hint = f'  {self.DIM}({n_tris:,} -> ~{max(1, int(n_tris * default_fractions[0])):,}){self.X}'
        batch_hint = f'  {self.DIM}[!N,N = all files]{self.X}' if batch else ''
        while True:
            try:
                raw = input(f"  {self.DIM}|{self.X}  {self.DIM}{'reduce':<{_BOX_LABEL}}{self.X}{self.Y}{pct_str}%{self.X}{hint}{batch_hint}  ").strip()
            except EOFError:
                raw = ''
            self._box_sep()
            lock_all = raw.startswith('!')
            if lock_all:
                raw = raw[1:].strip()
            try:
                fractions = _parse_reduction(raw, strict=True) if raw else default_fractions
            except ValueError as exc:
                self._box_row(f'invalid reduction: {exc}', lc=self.R)
                self._box_sep()
                continue
            return (fractions, lock_all)

    def _box_err(self, info: str) -> None:
        msg = _err_line(info)
        prefix = 'ERROR   FAIL  '
        width = _BOX_CONTENT - len(prefix)
        parts = textwrap.wrap(msg, width=width, break_long_words=True, break_on_hyphens=False) or ['']
        self._box_row(f'{prefix}{parts[0]}', lc=self.R)
        for line in parts[1:]:
            self._box_row(f"{' ' * len(prefix)}{line}", lc=self.R)

    def _print_summary(self, ok_n, fail_n, skip_n, dry_run=False):
        total = ok_n + fail_n + skip_n
        title = 'DRY RUN SUMMARY' if dry_run else 'CONVERSION SUMMARY'
        ok_label = 'WOULD CONVERT' if dry_run else 'CONVERTED'
        self._box_top()
        self._box_row(title, f"{total:,} item{('s' if total != 1 else '')}", lc=self.B, rc=self.DIM)
        self._box_sep()
        self._box_row(ok_label, f'{ok_n:,}', lc=self.G, rc=self.G)
        self._box_row('SKIPPED', f'{skip_n:,}', lc=self.DIM, rc=self.DIM)
        self._box_row(
            'FAILED',
            f'{fail_n:,}',
            lc=self.R if fail_n else self.DIM,
            rc=self.R if fail_n else self.DIM,
        )
        self._box_bot()


class NullProgress:
    def _step_start(self, label):
        return time.perf_counter()

    def _step_end(self, started, detail=''):
        pass

    def _step_fail(self):
        pass

    def _show_early_estimate(self, *args, **kwargs):
        pass

    def _show_post_sew_estimate(self, *args, **kwargs):
        pass
