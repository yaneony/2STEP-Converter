import argparse
import math


def _parse_reduction(value: str, *, strict: bool=False):
    if value is None:
        return None
    if not str(value).strip():
        if strict:
            raise ValueError('reduction percentage cannot be empty')
        return None
    results = []
    seen = set()
    for part in str(value).split(','):
        part = part.strip().rstrip('%')
        if not part:
            if strict:
                raise ValueError('empty reduction percentage')
            continue
        try:
            pct = float(part)
        except ValueError as exc:
            if strict:
                raise ValueError(f'invalid reduction percentage: {part!r}') from exc
            continue
        if not math.isfinite(pct) or not 0 <= pct < 100:
            if strict:
                raise ValueError(f'reduction percentage must be at least 0 and less than 100: {part!r}')
            continue
        key = round(pct, 9)
        if key in seen:
            continue
        seen.add(key)
        results.append(None if key == 0 else (100.0 - key) / 100.0)
    results.sort(key=lambda f: 0.0 if f is None else (1.0 - f) * 100.0)
    if strict and (not results):
        raise ValueError('no reduction percentages were provided')
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
        raise argparse.ArgumentTypeError(f'expected a number, got {value!r}') from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('value must be a finite number greater than zero')
    return number


def _reduction_pct(fraction) -> float:
    return 0.0 if fraction is None else (1.0 - fraction) * 100.0


def _reduction_label(fraction) -> str:
    pct = _reduction_pct(fraction)
    if math.isclose(pct, round(pct), rel_tol=0.0, abs_tol=5e-10):
        return str(int(round(pct)))
    return f'{pct:.9f}'.rstrip('0').rstrip('.')


def _fmt_time(seconds: float) -> str:
    if seconds < 1.0:
        return f'{int(seconds * 1000)}ms'
    s = int(round(seconds))
    if s < 60:
        return f'{s}s'
    return f'{s // 60}m {s % 60:02d}s'
