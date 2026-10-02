import os
import json
import math
import tempfile
import hashlib
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from version import VERSION
_EST_MIN = 5
_RECENT_LIMIT = 64


def _valid_bucket(bucket):
    if not isinstance(bucket, dict):
        return False
    matrix, vector = bucket.get('XtX'), bucket.get('Xty')
    if not isinstance(matrix, list) or len(matrix) != 3:
        return False
    if any(not isinstance(row, list) or len(row) != 3 for row in matrix):
        return False
    if not isinstance(vector, list) or len(vector) != 3:
        return False
    values = [*vector, *(value for row in matrix for value in row),
              bucket.get('scale', 1.0), bucket.get('sum_y2', 0.0)]
    try:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in values):
            return False
    except OverflowError:
        return False
    return (bucket.get('scale', 1.0) > 0 and bucket.get('sum_y2', 0.0) >= 0
            and all(matrix[index][index] >= 0 for index in range(3)))


def _valid_sample(sample):
    if not isinstance(sample, (list, tuple)) or len(sample) != 2:
        return False
    try:
        return all(not isinstance(value, bool) and isinstance(value, (int, float))
                   and math.isfinite(value) and value > 0 for value in sample)
    except OverflowError:
        return False


def _bucket_add(bucket, x, y):
    samples = bucket.get('samples', [])
    if not isinstance(samples, list):
        samples = []
    samples = [sample for sample in samples if _valid_sample(sample)][-(_RECENT_LIMIT - 1):]
    samples.append([x, y])
    return {'samples': samples}


def _bucket_predict(bucket, x):
    XtX = bucket.get('XtX', [[0.0] * 3 for _ in range(3)])
    Xty = bucket.get('Xty', [0.0] * 3)
    scale = bucket.get('scale', 1.0) or 1.0
    n = int(round(XtX[2][2]))
    if n < _EST_MIN:
        return (None, n, 0.0)
    mean_x, mean_y = XtX[1][2] / n, Xty[2] / n
    variance_x = XtX[1][1] - XtX[1][2] * mean_x
    covariance = Xty[1] - XtX[1][2] * mean_y
    a1 = max(0.0, covariance / variance_x) if variance_x > n * 1e-12 else 0.0
    a0 = mean_y - a1 * mean_x
    if a0 < 0:
        a0 = 0.0
        a1 = max(0.0, Xty[1] / XtX[1][1]) if XtX[1][1] > 0 else 0.0
    if not all(math.isfinite(value) for value in (a0, a1)):
        return (None, n, 0.0)
    xn = x / scale
    pred = a1 * xn + a0
    if not math.isfinite(pred):
        return (None, n, 0.0)
    pred = max(0.5, pred)
    return (pred, n, 0.0)


def timing_profile(settings, reduce_fraction=None, step_schema='AP203', preloaded=False, *, tolerance=None):
    excluded = {'ASK_FOR_REDUCTION', 'SKIP_UP_TO_DATE_OUTPUTS', 'INPUT_FOLDER_NAME',
                'DEFAULT_REDUCTION_PERCENT', 'DEFAULT_STEP_FORMAT', 'AUTO_REDUCTION_ENABLED',
                'AUTO_REDUCTION_TARGET_TRIANGLES'}
    options = {key: value for key, value in settings.to_dict().items()
               if key not in excluded and not key.endswith('_FILE_EXTENSION')}
    if not options.get('REPAIR_INTERSECTING_MESH'):
        options.pop('REPAIR_INTERSECTING_MESH', None)
        options.pop('MAX_REPAIR_ATTACHED_TRIANGLE_AREA_RATIO', None)
    if tolerance is not None:
        options['SEWING_TOLERANCE'] = tolerance
    options.update(version=VERSION, estimator=2, reduction=reduce_fraction,
                   schema=step_schema, preloaded=preloaded)
    return hashlib.sha256(json.dumps(options, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Estimate:
    seconds: float | None
    samples: int
    error_percent: float | None = None
    lower: float | None = None
    upper: float | None = None
    kind: str = 'learning'
    extrapolated: bool = False


def _fit_recent(samples, power):
    scale = max(size for size, _ in samples)
    if power == 0:
        return median(duration for _, duration in samples), 0.0, scale, power
    points = [((size / scale) ** power, duration) for size, duration in samples]
    slopes = [(right_y - left_y) / (right_x - left_x)
              for i, (left_x, left_y) in enumerate(points)
              for right_x, right_y in points[i + 1:] if abs(right_x - left_x) > 1e-10]
    slopes = [value for value in slopes if math.isfinite(value)]
    slope = max(0.0, median(slopes)) if slopes else 0.0
    intercept = median(duration - slope * size for size, duration in points)
    if intercept < 0:
        intercept = 0.0
        slope = median(duration / size for size, duration in points if size > 0)
    return intercept, slope, scale, power


def _predict_recent(model, value):
    intercept, slope, scale, power = model
    try:
        ratio = value / scale
        if power == 2 and ratio > 1:
            prediction = intercept + slope * (1 + 2 * (ratio - 1))
        else:
            prediction = intercept + slope * (ratio ** power if power else 1)
    except (OverflowError, ZeroDivisionError):
        return None
    return max(0.5, prediction) if math.isfinite(prediction) else None


def _select_power(errors):
    if len(errors[1]) < 3:
        return 1
    scores = {power: median(values[-24:]) for power, values in errors.items() if values}
    best = min(scores, key=lambda power: (scores[power], power))
    return best if scores[best] < scores[1] * 0.9 else 1


def _recent_estimate(samples, value):
    n = len(samples)
    if n < _EST_MIN:
        return Estimate(None, n)
    errors = {power: [] for power in (0, 1, 2)}
    validation = []
    for index in range(_EST_MIN, n):
        size, duration = samples[index]
        selected = _select_power(errors)
        for power in errors:
            prediction = _predict_recent(_fit_recent(samples[:index], power), size)
            if prediction is not None:
                error = abs(prediction - duration) / duration * 100
                if math.isfinite(error):
                    errors[power].append(error)
                    if power == selected:
                        validation.append(error)
    selected = _select_power(errors)
    prediction = _predict_recent(_fit_recent(samples, selected), value)
    if prediction is None:
        return Estimate(None, n)
    residuals = sorted(validation[-24:])
    error = median(residuals) if len(residuals) >= 5 else None
    margin = max(0.1, residuals[min(len(residuals) - 1, int(len(residuals) * 0.8))] / 100) if len(residuals) >= 5 else 0.25
    extrapolated = not min(size for size, _ in samples) <= value <= max(size for size, _ in samples)
    if extrapolated:
        margin = max(margin, 0.5)
    lower, upper = max(0.5, prediction * (1 - margin)), prediction * (1 + margin)
    if not math.isfinite(upper):
        lower = upper = None
    return Estimate(prediction, n, error, lower, upper, 'recent', extrapolated)


class Estimator:
    def __init__(self, history_path: Path):
        self.history_path = Path(history_path)

    def _est_load(self):
        if self.history_path.exists():
            try:
                data = json.loads(self.history_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    result = {}
                    for key, bucket in data.items():
                        if not isinstance(bucket, dict):
                            continue
                        if _valid_bucket(bucket):
                            result[key] = {field: bucket[field] for field in ('scale', 'XtX', 'Xty', 'sum_y2') if field in bucket}
                        if isinstance(bucket.get('samples'), list):
                            samples = [sample for sample in bucket['samples'] if _valid_sample(sample)][-_RECENT_LIMIT:]
                            if samples:
                                result.setdefault(key, {})['samples'] = samples
                    return result
            except Exception:
                pass
        return {}

    def _est_save(self, data):
        temp_path = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f'.{self.history_path.name}.',
                suffix='.tmp',
                dir=self.history_path.parent,
            )
            os.close(fd)
            temp_path = Path(temp_name)
            temp_path.write_text(json.dumps(data, indent=2, allow_nan=False), encoding='utf-8')
            os.replace(temp_path, self.history_path)
        except (OSError, ValueError):
            return
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _record(self, prefix, fmt, size, seconds, profile):
        if not _valid_sample([size, seconds]):
            return
        data = self._est_load()
        for key in (f'{prefix}:{fmt}:{profile or "v2"}', f'{prefix}:_all:{profile or "v2"}'):
            data[key] = _bucket_add(data.get(key, {}), float(size), float(seconds))
        self._est_save(data)

    def _rec_post_sew(self, fmt: str, n_faces: int, post_sew_seconds: float, profile=None):
        self._record('f', fmt, n_faces, post_sew_seconds, profile)

    def _rec_total(self, fmt: str, n_triangles: int, total_seconds: float, profile=None):
        self._record('t', fmt, n_triangles, total_seconds, profile)

    def estimate(self, prefix, value, fmt=None, profile=None):
        if not _valid_sample([value, 1.0]):
            return Estimate(None, 0)
        data = self._est_load()
        count = 0
        for key in (f'{prefix}:{fmt}:{profile or "v2"}', f'{prefix}:_all:{profile or "v2"}'):
            samples = data.get(key, {}).get('samples', [])
            if not isinstance(samples, list):
                continue
            samples = [sample for sample in samples if _valid_sample(sample)][-_RECENT_LIMIT:]
            count = max(count, len(samples))
            if len(samples) >= _EST_MIN:
                return _recent_estimate(samples, float(value))
        for key in (f'{prefix}:{fmt}', f'{prefix}:_all'):
            if key in data:
                prediction, n, _ = _bucket_predict(data[key], float(value))
                if prediction is not None:
                    return Estimate(prediction, n, kind='legacy',
                                    extrapolated=value > data[key].get('scale', 1.0))
        return Estimate(None, count)

