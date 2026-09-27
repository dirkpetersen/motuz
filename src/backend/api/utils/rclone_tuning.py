"""
rclone performance settings (README, "Performance tuning").

- Installation defaults from the environment (MOTUZ_RCLONE_TRANSFERS, ...). Unset means
  rclone's own default, i.e. no flag at all.
- Per-job overrides from the New Copy Job dialog (`performance` of a copy or hashsum
  job): only the parameters in PARAMS with `per_job`, validated, capped by
  MOTUZ_RCLONE_MAX_* and by the memory budget.
- Presets ("Many small files", ...) are overrides fitted to the caps and the budget.
- The rclone flags they become: one argv item per flag (`--transfers=32`), values
  formatted here from parsed numbers, never user text, and never through a shell.

Memory estimate (a rough upper bound, bytes), for the destination's backend:

    transfers * ( max(1, multi_thread_streams) * buffer_size
                + max(upload_concurrency, multi_thread_streams) * chunk_size )

The second term only for S3 and Azure Blob destinations (their chunk size and upload
concurrency). Unset values count with rclone's defaults.

This module only depends on the standard library: config.py validates the environment
with it at import time, so the app and the worker refuse to start with a bad setting.
"""
import math
import os
import re
from collections import namedtuple


KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB
TiB = 1024 * GiB


class TuningError(ValueError):
    """An invalid performance setting of a job (HTTP 400)"""


class TuningConfigError(TuningError):
    """An invalid MOTUZ_RCLONE_* setting (the app and the worker refuse to start)"""


# name: the key in a job's `performance` and the MOTUZ_RCLONE_<NAME> variable
# kind: 'int' or 'size'; lo/hi: sanity bounds; cap: the MOTUZ_RCLONE_MAX_* that limits it
# backend: only passed when the destination is of this type
# per_job: users may override it (buffer_size is installation-wide only)
Param = namedtuple('Param', 'name kind flag lo hi cap backend per_job label')

PARAMS = (
    Param('transfers', 'int', '--transfers', 1, 1024, 'transfers', None, True, 'Parallel transfers'),
    Param('checkers', 'int', '--checkers', 1, 1024, 'checkers', None, True, 'Parallel checkers'),
    Param('multi_thread_streams', 'int', '--multi-thread-streams', 0, 256, 'multi_thread_streams', None, True,
          'Multi-thread streams'),
    Param('multi_thread_cutoff', 'size', '--multi-thread-cutoff', 1 * MiB, 1 * TiB, None, None, True,
          'Multi-thread cutoff'),
    Param('buffer_size', 'size', '--buffer-size', 0, 1 * GiB, None, None, False, 'Buffer size'),
    Param('s3_upload_concurrency', 'int', '--s3-upload-concurrency', 1, 256, 'upload_concurrency', 's3', True,
          'S3 upload concurrency'),
    Param('s3_chunk_size', 'size', '--s3-chunk-size', 5 * MiB, 5 * GiB, None, 's3', True, 'S3 chunk size'),
    Param('azureblob_upload_concurrency', 'int', '--azureblob-upload-concurrency', 1, 256, 'upload_concurrency',
          'azureblob', True, 'Azure upload concurrency'),
    Param('azureblob_chunk_size', 'size', '--azureblob-chunk-size', 1 * MiB, 4000 * MiB, None, 'azureblob', True,
          'Azure chunk size'),
)
PARAMS_BY_NAME = {p.name: p for p in PARAMS}
PER_JOB = tuple(p.name for p in PARAMS if p.per_job)

# rclone's defaults (rclone help flags, v1.75), only used for the memory estimate
RCLONE_DEFAULTS = {
    'transfers': 4,
    'checkers': 8,
    'multi_thread_streams': 4,
    'multi_thread_cutoff': 256 * MiB,
    'buffer_size': 16 * MiB,
    's3_upload_concurrency': 4,
    's3_chunk_size': 5 * MiB,
    'azureblob_upload_concurrency': 16,
    'azureblob_chunk_size': 4 * MiB,
}

# MOTUZ_RCLONE_MAX_<NAME>: (default, sanity bounds of the parameter it caps)
CAPS = {
    'transfers': 64,
    'checkers': 128,
    'multi_thread_streams': 32,
    'upload_concurrency': 64, # S3 and Azure Blob
}
_CAP_BOUNDS = {
    'transfers': (1, 1024),
    'checkers': (1, 1024),
    'multi_thread_streams': (0, 256),
    'upload_concurrency': (1, 256),
}
DEFAULT_MEMORY_BUDGET = 8 * GiB
_MEMORY_BUDGET_BOUNDS = (64 * MiB, 64 * TiB)

# Backends whose chunk size / upload concurrency are in the estimate and the flags
CHUNKED_BACKENDS = ('s3', 'azureblob')


# --- values ---------------------------------------------------------------------------

_INT_RE = re.compile(r'[0-9]{1,7}')
# rclone's size syntax (SizeSuffix) with a mandatory unit: 64M, 64Mi, 64MiB, 1.5G, 512K,
# 100B. Units are binary, like rclone's. A bare number would mean KiB to rclone, which
# is too easy to get wrong, so it is refused.
_SIZE_RE = re.compile(r'([0-9]{1,7})(?:\.([0-9]{1,3}))?(?:(B)|([KMGTP])(?:i?B?))', re.IGNORECASE)
_UNITS = {'B': 1, 'K': KiB, 'M': MiB, 'G': GiB, 'T': TiB, 'P': 1024 * TiB}


def parse_int(value):
    """An int from an int (not bool) or a string of digits; None if it is neither"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_RE.fullmatch(value):
        return int(value)
    return None


def parse_size(value):
    """Bytes from an rclone size like '64M', '64Mi', '1.5G'; None if it is not one"""
    if not isinstance(value, str):
        return None
    match = _SIZE_RE.fullmatch(value)
    if not match:
        return None
    whole, fraction, byte, unit = match.groups()
    multiplier = _UNITS[(byte or unit).upper()]
    size = int(whole) * multiplier
    if fraction:
        size += int(fraction) * multiplier // (10 ** len(fraction))
    return size


def format_size(size):
    """The rclone form of a size in bytes: 64Mi, 1Gi, 1536Ki, 100B"""
    for suffix, unit in (('Ti', TiB), ('Gi', GiB), ('Mi', MiB), ('Ki', KiB)):
        if size >= unit and size % unit == 0:
            return '{}{}'.format(size // unit, suffix)
    return '{}B'.format(size)


def format_value(param, value):
    return format_size(value) if param.kind == 'size' else str(value)


def _describe_size(size):
    if size >= GiB:
        return '{:.1f} GiB'.format(size / GiB).replace('.0 ', ' ')
    return '{} MiB'.format(int(math.ceil(size / MiB)))


def _parse(param, value, error, source):
    """The parsed value of one parameter within its sanity bounds, or raises `error`"""
    if param.kind == 'int':
        parsed = parse_int(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value # bytes (API); the environment always has strings
    else:
        parsed = parse_size(value)
    if parsed is None:
        if param.kind == 'int':
            raise error('{} must be a whole number, not {!r}'.format(source, value))
        raise error('{} must be a size like 64M or 1G, not {!r}'.format(source, value))
    if not param.lo <= parsed <= param.hi:
        bounds = (param.lo, param.hi) if param.kind == 'int' else (format_size(param.lo), format_size(param.hi))
        raise error('{} must be between {} and {}, not {!r}'.format(source, bounds[0], bounds[1], value))
    return parsed


# --- installation settings --------------------------------------------------------------

Settings = namedtuple('Settings', 'defaults caps memory_budget extra_flags')


def env_name(name):
    return 'MOTUZ_RCLONE_{}'.format(name.upper())


def cap_env_name(cap):
    return 'MOTUZ_RCLONE_MAX_{}'.format(cap.upper())


def _env(environ, name):
    return (environ.get(name) or '').strip() or None


def load_settings(environ=None):
    """
    The installation's settings from MOTUZ_RCLONE_* (empty = unset). Raises
    TuningConfigError, naming the variable, for an invalid value, a default above its
    cap, or defaults whose memory estimate is above the budget.
    """
    if environ is None:
        environ = os.environ

    caps = {}
    for cap, default in CAPS.items():
        name = cap_env_name(cap)
        raw = _env(environ, name)
        if raw is None:
            caps[cap] = default
            continue
        value = parse_int(raw)
        lo, hi = _CAP_BOUNDS[cap]
        if value is None or not lo <= value <= hi:
            raise TuningConfigError('{}={!r} must be a whole number between {} and {}'.format(name, raw, lo, hi))
        caps[cap] = value

    name = 'MOTUZ_RCLONE_MEMORY_BUDGET'
    raw = _env(environ, name)
    memory_budget = DEFAULT_MEMORY_BUDGET
    if raw is not None:
        memory_budget = parse_size(raw)
        lo, hi = _MEMORY_BUDGET_BOUNDS
        if memory_budget is None or not lo <= memory_budget <= hi:
            raise TuningConfigError('{}={!r} must be a size like 8G between {} and {}'.format(
                name, raw, format_size(lo), format_size(hi)))

    defaults = {}
    for param in PARAMS:
        name = env_name(param.name)
        raw = _env(environ, name)
        if raw is None:
            continue
        value = _parse(param, raw, TuningConfigError, name)
        if param.cap and value > caps[param.cap]:
            raise TuningConfigError('{}={} is above {}={}'.format(name, value, cap_env_name(param.cap), caps[param.cap]))
        defaults[param.name] = value

    extra_flags = parse_extra_flags(_env(environ, 'MOTUZ_RCLONE_EXTRA_FLAGS') or '')

    settings = Settings(defaults, caps, memory_budget, extra_flags)
    worst = max(CHUNKED_BACKENDS + (None,), key=lambda backend: estimate_memory(defaults, backend))
    needed = estimate_memory(defaults, worst)
    if needed > memory_budget:
        raise TuningConfigError(
            'The MOTUZ_RCLONE_* defaults need about {} of memory per job ({} destinations), more than '
            'MOTUZ_RCLONE_MEMORY_BUDGET={} (README, "Performance tuning")'.format(
                _describe_size(needed), worst or 'local', format_size(memory_budget)))
    return settings


_settings = None


def settings():
    """The installation's settings, read from the environment once"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


# --- extra flags (admin only) ------------------------------------------------------------

# MOTUZ_RCLONE_EXTRA_FLAGS accepts only these flags, with a validated value. There is no
# free-form option: rclone flags can start a remote-control server (--rc), run programs
# (--password-command, --metadata-mapper), write files anywhere as the user (--log-file,
# --dump-* with credentials in the output) or change what is copied and checked
# (--ignore-checksum, --size-only). Performance flags that have their own setting
# (--transfers, ...) are not here either.
EXTRA_FLAGS = {
    '--fast-list': ('bool', None),
    '--use-mmap': ('bool', None),
    '--no-traverse': ('bool', None),
    '--disable-http2': ('bool', None),
    '--s3-disable-http2': ('bool', None),
    '--max-buffer-memory': ('size', (1 * MiB, 64 * TiB)),
    '--multi-thread-chunk-size': ('size', (1 * MiB, 5 * GiB)),
    '--multi-thread-write-buffer-size': ('size', (0, 1 * GiB)),
    '--s3-upload-cutoff': ('size', (0, 5 * GiB)),
    '--s3-copy-cutoff': ('size', (5 * MiB, 5 * GiB)),
    '--low-level-retries': ('int', (1, 100)),
    '--retries': ('int', (1, 100)),
}
_EXTRA_TOKEN_RE = re.compile(r'(--[a-z0-9][a-z0-9-]*)(?:=(.*))?')


def parse_extra_flags(text):
    """
    MOTUZ_RCLONE_EXTRA_FLAGS, e.g. '--fast-list --max-buffer-memory=16G', as a tuple of
    argv items (formatted here). Raises TuningConfigError for anything else.
    """
    flags = []
    seen = set()
    for token in text.split():
        match = _EXTRA_TOKEN_RE.fullmatch(token)
        if not match:
            raise TuningConfigError('MOTUZ_RCLONE_EXTRA_FLAGS: {!r} is not a --flag or --flag=value'.format(token))
        flag, raw = match.groups()
        if flag not in EXTRA_FLAGS:
            raise TuningConfigError('MOTUZ_RCLONE_EXTRA_FLAGS: {} is not allowed (allowed: {})'.format(
                flag, ', '.join(sorted(EXTRA_FLAGS))))
        if flag in seen:
            raise TuningConfigError('MOTUZ_RCLONE_EXTRA_FLAGS: {} is given twice'.format(flag))
        seen.add(flag)
        kind, bounds = EXTRA_FLAGS[flag]
        if kind == 'bool':
            if raw is None or raw.lower() == 'true':
                flags.append(flag)
            elif raw.lower() == 'false':
                flags.append(flag + '=false')
            else:
                raise TuningConfigError('MOTUZ_RCLONE_EXTRA_FLAGS: {} takes true or false, not {!r}'.format(flag, raw))
            continue
        value = parse_int(raw) if kind == 'int' else parse_size(raw)
        if raw is None or value is None or not bounds[0] <= value <= bounds[1]:
            raise TuningConfigError('MOTUZ_RCLONE_EXTRA_FLAGS: {} needs a {} between {} and {}, not {!r}'.format(
                flag, 'whole number' if kind == 'int' else 'size like 64M',
                *(bounds if kind == 'int' else map(format_size, bounds)), raw))
        flags.append('{}={}'.format(flag, value if kind == 'int' else format_size(value)))
    return tuple(flags)


# --- memory -------------------------------------------------------------------------------

def estimate_memory(values, dst_type):
    """
    Rough upper bound of the buffer memory of one copy job in bytes (module docstring).
    `values` are effective settings; unset ones count with rclone's defaults.
    """
    v = dict(RCLONE_DEFAULTS)
    v.update({k: val for k, val in values.items() if val is not None})
    streams = max(1, v['multi_thread_streams'])
    per_transfer = streams * v['buffer_size']
    if dst_type in CHUNKED_BACKENDS:
        concurrency = v['{}_upload_concurrency'.format(dst_type)]
        per_transfer += max(concurrency, v['multi_thread_streams']) * v['{}_chunk_size'.format(dst_type)]
    return v['transfers'] * per_transfer


def _applies(param, dst_type):
    return param.backend is None or param.backend == dst_type


# --- per-job overrides ----------------------------------------------------------------------

def validate_overrides(raw, dst_type, tuning=None, *, only=None):
    """
    The validated per-job overrides (a new dict of parsed values, what a job stores),
    from the `performance` object of an API request or of a stored job. None or {} means
    the installation defaults. Settings for another destination type than `dst_type`
    (e.g. S3 chunk size for a local destination) are dropped. `only` limits the allowed
    names (hashsum jobs: checkers). Raises TuningError with a message for the user.
    """
    tuning = tuning or settings()
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TuningError('performance must be an object')

    allowed = PER_JOB if only is None else only
    result = {}
    for name, value in raw.items():
        if name not in allowed:
            raise TuningError('Unknown performance setting {!r}'.format(name)[:200])
        if value is None or value == '':
            continue
        param = PARAMS_BY_NAME[name]
        value = _parse(param, value, TuningError, param.label)
        if param.cap and value > tuning.caps[param.cap]:
            raise TuningError('{} must be at most {} on this server'.format(param.label, tuning.caps[param.cap]))
        if _applies(param, dst_type):
            result[name] = value

    if only is None:
        needed = estimate_memory(effective(result, dst_type, tuning), dst_type)
        if needed > tuning.memory_budget:
            raise TuningError(
                'These settings need about {} of memory, more than the {} allowed per job on this server. '
                'Use fewer transfers, streams or a smaller chunk size.'.format(
                    _describe_size(needed), _describe_size(tuning.memory_budget)))
    return result


def effective(overrides, dst_type, tuning=None):
    """Installation defaults plus overrides, for parameters that apply to `dst_type`"""
    tuning = tuning or settings()
    values = {}
    for param in PARAMS:
        if not _applies(param, dst_type):
            continue
        value = overrides.get(param.name, tuning.defaults.get(param.name))
        if value is not None:
            values[param.name] = value
    return values


def copy_flags(overrides, dst_type, tuning=None):
    """
    rclone flags of a copy job: the effective settings (none: rclone's defaults) plus the
    extra flags. Re-validates stored overrides, since the caps may have changed.
    """
    tuning = tuning or settings()
    overrides = validate_overrides(overrides, dst_type, tuning)
    values = effective(overrides, dst_type, tuning)
    flags = ['{}={}'.format(PARAMS_BY_NAME[name].flag, format_value(PARAMS_BY_NAME[name], value))
             for name, value in values.items()]
    return flags + list(tuning.extra_flags)


def hashsum_flags(overrides, tuning=None):
    """rclone flags of an md5sum (integrity check): --checkers plus the extra flags"""
    tuning = tuning or settings()
    overrides = validate_overrides(overrides, None, tuning, only=('checkers',))
    checkers = overrides.get('checkers', tuning.defaults.get('checkers'))
    flags = [] if checkers is None else ['--checkers={}'.format(checkers)]
    return flags + list(tuning.extra_flags)


# --- presets ---------------------------------------------------------------------------------

# Target values; presets() clamps them to the caps and fits them to the memory budget
PRESETS = (
    ('default', 'Default', 'The settings of this server', {}),
    ('small_files', 'Many small files', 'Many files in parallel; for folders of files below ~100 MB', {
        'transfers': 32,
        'checkers': 64,
    }),
    ('large_files', 'Few large files', 'Each file split into parallel streams and large chunks', {
        'transfers': 4,
        'multi_thread_streams': 16,
        'multi_thread_cutoff': 64 * MiB,
        's3_upload_concurrency': 16,
        's3_chunk_size': 64 * MiB,
        'azureblob_upload_concurrency': 16,
        'azureblob_chunk_size': 64 * MiB,
    }),
    ('maximum', 'Maximum', 'As much parallelism as this server allows, for 100 Gb/s links', {
        'transfers': 64,
        'checkers': 128,
        'multi_thread_streams': 16,
        'multi_thread_cutoff': 64 * MiB,
        's3_upload_concurrency': 16,
        's3_chunk_size': 64 * MiB,
        'azureblob_upload_concurrency': 16,
        'azureblob_chunk_size': 64 * MiB,
    }),
)


def fit_preset(target, dst_type, tuning=None):
    """
    A preset's values for `dst_type`, clamped to the caps and reduced until the memory
    estimate fits the budget: first transfers (down to 8), then halving the chunk size
    (down to 16 MiB) and the streams / upload concurrency (down to 4), finally transfers
    down to 1. Returns (values, reduced) or (None, True) if even that does not fit.
    """
    tuning = tuning or settings()
    values = {}
    reduced = False
    for name, value in target.items():
        param = PARAMS_BY_NAME[name]
        if not _applies(param, dst_type):
            continue
        if param.cap and value > tuning.caps[param.cap]:
            value = tuning.caps[param.cap]
            reduced = True
        values[name] = value
    if not values:
        return values, reduced

    def needed():
        return estimate_memory(effective(values, dst_type, tuning), dst_type)

    def per_transfer():
        return estimate_memory({**effective(values, dst_type, tuning), 'transfers': 1}, dst_type)

    chunk = '{}_chunk_size'.format(dst_type) if dst_type in CHUNKED_BACKENDS else None
    concurrency = '{}_upload_concurrency'.format(dst_type) if dst_type in CHUNKED_BACKENDS else None
    transfers = effective(values, dst_type, tuning).get('transfers', RCLONE_DEFAULTS['transfers'])
    while needed() > tuning.memory_budget:
        reduced = True
        fitting = tuning.memory_budget // per_transfer()
        if fitting >= min(8, transfers):
            values['transfers'] = int(fitting)
            break
        if chunk and values.get(chunk, 0) > 16 * MiB:
            values[chunk] = max(16 * MiB, PARAMS_BY_NAME[chunk].lo, values[chunk] // 2)
            continue
        halved = False
        for name in ('multi_thread_streams', concurrency):
            if name and values.get(name, 0) > 4:
                values[name] = max(4, values[name] // 2)
                halved = True
        if halved:
            continue
        if fitting < 1:
            return None, True
        values['transfers'] = int(fitting)
        break
    return values, reduced


def presets(dst_type, tuning=None):
    """The presets for a destination type, as shown by the New Copy Job dialog"""
    tuning = tuning or settings()
    result = []
    for preset_id, label, description, target in PRESETS:
        values, reduced = fit_preset(target, dst_type, tuning)
        result.append({
            'id': preset_id,
            'label': label,
            'description': description,
            'values': None if values is None else to_api(values),
            'available': values is not None,
            'reduced': reduced,
            'memory': None if values is None else estimate_memory(effective(values, dst_type, tuning), dst_type),
        })
    return result


def to_api(values):
    """Parsed values as the API shows them: ints, sizes as rclone strings (64Mi)"""
    return {name: format_value(PARAMS_BY_NAME[name], value) if PARAMS_BY_NAME[name].kind == 'size' else value
            for name, value in values.items()}


def describe(dst_type, tuning=None):
    """GET /api/copy-jobs/performance/: what the dialog needs for a destination type"""
    tuning = tuning or settings()
    fields = []
    for name in PER_JOB:
        param = PARAMS_BY_NAME[name]
        if not _applies(param, dst_type):
            continue
        hi = tuning.caps[param.cap] if param.cap else param.hi
        default = tuning.defaults.get(name, RCLONE_DEFAULTS[name])
        fields.append({
            'name': name,
            'label': param.label,
            'kind': param.kind,
            'min': param.lo if param.kind == 'int' else format_size(param.lo),
            'max': hi if param.kind == 'int' else format_size(hi),
            'default': default if param.kind == 'int' else format_size(default),
        })
    return {
        'dst_type': dst_type,
        'fields': fields,
        'memory_budget': tuning.memory_budget,
        # for the dialog's own estimate (installation-wide only)
        'buffer_size': tuning.defaults.get('buffer_size', RCLONE_DEFAULTS['buffer_size']),
        'memory_default': estimate_memory(effective({}, dst_type, tuning), dst_type),
        'presets': presets(dst_type, tuning),
    }
