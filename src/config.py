import json
import math
from pathlib import Path
from dataclasses import dataclass, asdict, replace


def _valid_reduce_config(value) -> bool:
    values = value.split(',') if isinstance(value, str) else [value]
    if not values:
        return False
    for item in values:
        if isinstance(item, bool):
            return False
        if isinstance(item, str):
            item = item.strip().rstrip('%')
        try:
            pct = float(item)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(pct) or not 0 <= pct < 100:
            return False
    return True


def _finite_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positive_number(value) -> bool:
    return _finite_number(value) and value > 0


def _nonnegative_number(value) -> bool:
    return _finite_number(value) and value >= 0


def _valid_extension(value) -> bool:
    return (isinstance(value, str) and value.startswith('.') and len(value) > 1
            and all(character.isalnum() or character in '_-' for character in value[1:]))


@dataclass(frozen=True)
class Settings:
    sewing_tolerance: float = 0.01
    default_reduction_percent: int | float | str = 0
    auto_reduction_enabled: bool = True
    auto_reduction_target_triangles: int = 50000
    ask_for_reduction: bool = True
    skip_up_to_date_outputs: bool = True
    planar_merge_angle_radians: float = 0.01
    sewing_timeout_seconds: int = 1800
    cad_operation_timeout_seconds: int = 300
    sew_parts_separately: bool = True
    default_step_format: str = 'ap203'
    generate_png_preview: bool = True
    input_folder_name: str = 'models'
    check_mesh_quality: bool = True
    repair_mesh_before_conversion: bool = True
    repair_intersecting_mesh: bool = True
    max_repair_attached_triangle_area_ratio: float = 0.005
    vertex_merge_distance: float = 0.0
    fix_triangle_orientation: bool = True
    remove_non_manifold_triangles: bool = False
    reject_non_manifold_mesh: bool = False
    fill_small_mesh_holes: bool = False
    fill_small_planar_brep_gaps: bool = True
    max_brep_gap_edge_count: int = 8
    max_brep_gap_area_ratio: float = 0.005
    check_self_intersections: bool = True
    self_intersection_check_max_triangles: int = 50000
    reject_self_intersecting_mesh: bool = False
    use_scale_aware_sewing_tolerance: bool = True
    scale_aware_sewing_tolerance_ratio: float = 1e-06
    require_solid_output: bool = True
    validate_step_after_writing: bool = True
    preserve_boundaries_during_reduction: bool = True
    reduction_boundary_weight: float = 10.0
    max_reduction_size_change_percent: float = 0.5
    max_reduction_volume_change_percent: float = 2.0
    max_reduction_surface_deviation_percent: float = 0.1
    experimental_parametric_reconstruction: bool = False
    experimental_parametric_fit_error_ratio: float = 0.0005
    experimental_parametric_max_volume_change_percent: float = 0.1
    reconstruct_analytic_primitives: bool = True
    analytic_primitive_fit_error_ratio: float = 0.001
    analytic_primitive_min_triangles: int = 32
    analytic_primitive_max_volume_change_percent: float = 0.1
    reconstruct_analytic_through_holes: bool = True
    reconstruct_analytic_blind_holes: bool = True
    analytic_hole_fit_error_ratio: float = 0.002
    analytic_hole_min_sides: int = 12
    analytic_hole_max_radius_difference_ratio: float = 0.002
    analytic_hole_axis_tolerance_radians: float = 0.005
    analytic_hole_max_volume_change_percent: float = 0.1
    stl_file_extension: str = '.stl'
    three_mf_file_extension: str = '.3mf'
    obj_file_extension: str = '.obj'
    iges_file_extension: str = '.igs'
    amf_file_extension: str = '.amf'
    step_file_extension: str = '.stp'

    def __post_init__(self):
        for name in ('stl_file_extension', 'three_mf_file_extension', 'obj_file_extension',
                     'iges_file_extension', 'amf_file_extension'):
            value = getattr(self, name)
            if not _valid_extension(value):
                raise ValueError(f'invalid input extension for {name}: {value!r}')
            object.__setattr__(self, name, value.lower())

    @classmethod
    def from_mapping(cls, values):
        return cls(**{key.lower(): value for key, value in values.items()})

    def to_dict(self):
        return {key.upper(): value for key, value in asdict(self).items()}

    def with_overrides(self, **changes):
        return replace(self, **changes)

    @property
    def iges_extensions(self):
        return frozenset((self.iges_file_extension, '.iges'))

    @property
    def supported_extensions(self):
        return frozenset((
            self.stl_file_extension,
            self.three_mf_file_extension,
            self.obj_file_extension,
            self.amf_file_extension,
            *self.iges_extensions,
        ))


_CONFIG_DEFAULTS = Settings().to_dict()


def load_config(path: Path, *, create: bool=True) -> tuple[Settings, list[str]]:
    path = Path(path)
    warnings = []
    if not path.exists():
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_CONFIG_DEFAULTS, indent=4), encoding='utf-8')
        raw = {}
    else:
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('top-level value must be an object')
        except Exception as exc:
            warnings.append(f'config: could not read {path.name}: {exc}; using defaults')
            raw = {}
    cfg = dict(_CONFIG_DEFAULTS)
    validators = {
        'SEWING_TOLERANCE': _positive_number,
        'PLANAR_MERGE_ANGLE_RADIANS': _positive_number,
        'SEWING_TIMEOUT_SECONDS': _positive_number,
        'CAD_OPERATION_TIMEOUT_SECONDS': _positive_number,
        'SEW_PARTS_SEPARATELY': lambda v: isinstance(v, bool),
        'DEFAULT_REDUCTION_PERCENT': _valid_reduce_config,
        'AUTO_REDUCTION_ENABLED': lambda v: isinstance(v, bool),
        'AUTO_REDUCTION_TARGET_TRIANGLES': lambda v: (
            isinstance(v, int)
            and not isinstance(v, bool)
            and v >= 4
        ),
        'DEFAULT_STEP_FORMAT': lambda v: v in ('ap203', 'ap214', 'ap242'),
        'ASK_FOR_REDUCTION': lambda v: isinstance(v, bool),
        'SKIP_UP_TO_DATE_OUTPUTS': lambda v: isinstance(v, bool),
        'GENERATE_PNG_PREVIEW': lambda v: isinstance(v, bool),
        'CHECK_MESH_QUALITY': lambda v: isinstance(v, bool),
        'REPAIR_MESH_BEFORE_CONVERSION': lambda v: isinstance(v, bool),
        'REPAIR_INTERSECTING_MESH': lambda v: isinstance(v, bool),
        'MAX_REPAIR_ATTACHED_TRIANGLE_AREA_RATIO': lambda v: _nonnegative_number(v) and v <= 1,
        'VERTEX_MERGE_DISTANCE': lambda v: _nonnegative_number(v),
        'FIX_TRIANGLE_ORIENTATION': lambda v: isinstance(v, bool),
        'REMOVE_NON_MANIFOLD_TRIANGLES': lambda v: isinstance(v, bool),
        'REJECT_NON_MANIFOLD_MESH': lambda v: isinstance(v, bool),
        'FILL_SMALL_MESH_HOLES': lambda v: isinstance(v, bool),
        'FILL_SMALL_PLANAR_BREP_GAPS': lambda v: isinstance(v, bool),
        'MAX_BREP_GAP_EDGE_COUNT': lambda v: isinstance(v, int) and (not isinstance(v, bool)) and (v >= 3),
        'MAX_BREP_GAP_AREA_RATIO': lambda v: _nonnegative_number(v),
        'CHECK_SELF_INTERSECTIONS': lambda v: isinstance(v, bool),
        'SELF_INTERSECTION_CHECK_MAX_TRIANGLES': lambda v: (
            isinstance(v, int)
            and not isinstance(v, bool)
            and v >= 0
        ),
        'REJECT_SELF_INTERSECTING_MESH': lambda v: isinstance(v, bool),
        'USE_SCALE_AWARE_SEWING_TOLERANCE': lambda v: isinstance(v, bool),
        'SCALE_AWARE_SEWING_TOLERANCE_RATIO': lambda v: _positive_number(v),
        'REQUIRE_SOLID_OUTPUT': lambda v: isinstance(v, bool),
        'VALIDATE_STEP_AFTER_WRITING': lambda v: isinstance(v, bool),
        'PRESERVE_BOUNDARIES_DURING_REDUCTION': lambda v: isinstance(v, bool),
        'REDUCTION_BOUNDARY_WEIGHT': lambda v: _positive_number(v),
        'MAX_REDUCTION_SIZE_CHANGE_PERCENT': lambda v: _nonnegative_number(v),
        'MAX_REDUCTION_VOLUME_CHANGE_PERCENT': lambda v: _nonnegative_number(v),
        'MAX_REDUCTION_SURFACE_DEVIATION_PERCENT': _nonnegative_number,
        'EXPERIMENTAL_PARAMETRIC_RECONSTRUCTION': lambda v: isinstance(v, bool),
        'EXPERIMENTAL_PARAMETRIC_FIT_ERROR_RATIO': lambda v: _positive_number(v),
        'EXPERIMENTAL_PARAMETRIC_MAX_VOLUME_CHANGE_PERCENT': lambda v: _nonnegative_number(v),
        'RECONSTRUCT_ANALYTIC_PRIMITIVES': lambda v: isinstance(v, bool),
        'ANALYTIC_PRIMITIVE_FIT_ERROR_RATIO': lambda v: _positive_number(v),
        'ANALYTIC_PRIMITIVE_MIN_TRIANGLES': lambda v: (
            isinstance(v, int)
            and not isinstance(v, bool)
            and v >= 4
        ),
        'ANALYTIC_PRIMITIVE_MAX_VOLUME_CHANGE_PERCENT': _nonnegative_number,
        'RECONSTRUCT_ANALYTIC_THROUGH_HOLES': lambda v: isinstance(v, bool),
        'RECONSTRUCT_ANALYTIC_BLIND_HOLES': lambda v: isinstance(v, bool),
        'ANALYTIC_HOLE_FIT_ERROR_RATIO': lambda v: _positive_number(v),
        'ANALYTIC_HOLE_MIN_SIDES': lambda v: isinstance(v, int) and (not isinstance(v, bool)) and (v >= 8),
        'ANALYTIC_HOLE_MAX_RADIUS_DIFFERENCE_RATIO': lambda v: _positive_number(v),
        'ANALYTIC_HOLE_AXIS_TOLERANCE_RADIANS': lambda v: _positive_number(v),
        'ANALYTIC_HOLE_MAX_VOLUME_CHANGE_PERCENT': lambda v: _nonnegative_number(v),
        'INPUT_FOLDER_NAME': lambda v: (
            isinstance(v, str)
            and bool(v.strip())
            and not Path(v).anchor
            and '..' not in Path(v).parts
        ),
        'STL_FILE_EXTENSION': _valid_extension,
        'THREE_MF_FILE_EXTENSION': _valid_extension,
        'OBJ_FILE_EXTENSION': _valid_extension,
        'IGES_FILE_EXTENSION': _valid_extension,
        'AMF_FILE_EXTENSION': _valid_extension,
        'STEP_FILE_EXTENSION': _valid_extension,
    }
    for key in raw:
        if key not in _CONFIG_DEFAULTS:
            warnings.append(f'config: unknown setting {key}; setting ignored')
    for key, default in _CONFIG_DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]
        if validators[key](value):
            cfg[key] = value.lower() if key.endswith('_FILE_EXTENSION') else value
        else:
            warnings.append(f'config: invalid {key}={value!r}; using default {default!r}')
    extension_keys = [key for key in cfg if key.endswith('_FILE_EXTENSION')]
    owners = {'.iges': 'IGES_FILE_EXTENSION'}
    conflicts = set()
    for key in extension_keys:
        previous = owners.setdefault(cfg[key], key)
        if previous != key:
            conflicts.update((previous, key))
    if conflicts:
        warnings.append(f"config: conflicting file extensions for {', '.join(sorted(conflicts))}; using default file extensions")
        for key in extension_keys:
            cfg[key] = _CONFIG_DEFAULTS[key]
    return (Settings.from_mapping(cfg), warnings)
