"""
rclone performance settings (README, "Performance tuning").

- Installation defaults from the environment (MOTUZ_RCLONE_TRANSFERS, ...). Unset means
  Motuz's own default (MOTUZ_DEFAULTS: 64 transfers, 128 checkers, S3 parts of 16 MiB
  sent 8 at a time, measured 2026-10-18; rclone's 4/8 leave a data mover mostly waiting
  on round trips), else rclone's default, i.e. no flag.
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

# Motuz's defaults when MOTUZ_RCLONE_<NAME> is unset (lowered to a smaller cap). Measured
# 2026-10-18 from local files to AWS S3 (8 vCPUs, CRC32C parts, buffer 0), GB/s for 100 MB
# / 1 MB files: rclone's 4 transfers and 5 MiB parts 2.2 / 0.18 with 16 transfers; 32
# transfers with 16 MiB parts x 16 4.0 / 0.37; these values 3.8 / 0.67; 128 transfers
# 3.3 / 1.2. 64 MiB parts were slower (1.8). Estimate: 64 x (8 x 16 MiB + 4 x 16M buffer
# from a cloud source) = 12 GiB, within the default budget of a machine with >= 48 GiB.
MOTUZ_DEFAULTS = {
    'transfers': 64,
    'checkers': 128,
    's3_upload_concurrency': 8,
    's3_chunk_size': 16 * MiB,
}

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
    'transfers': 128,
    'checkers': 256,
    'multi_thread_streams': 32,
    'upload_concurrency': 64, # S3 and Azure Blob
}
_CAP_BOUNDS = {
    'transfers': (1, 1024),
    'checkers': (1, 1024),
    'multi_thread_streams': (0, 256),
    'upload_concurrency': (1, 256),
}
# MOTUZ_RCLONE_MEMORY_BUDGET unset: a quarter of the memory the server may use (its RAM or
# its cgroup limit), between these bounds (default_memory_budget)
DEFAULT_MEMORY_BUDGET_RANGE = (4 * GiB, 64 * GiB)
_MEMORY_BUDGET_BOUNDS = (64 * MiB, 64 * TiB)

# Backends whose chunk size / upload concurrency are in the estimate and the flags
CHUNKED_BACKENDS = ('s3', 'azureblob')

# MOTUZ_RCLONE_MD5_SIMD_SERVERS: rclone computes MD5s (local files, S3 upload parts) with
# md5-simd, which hashes up to 16 streams at once on one core (--md5-simd of the rclone
# build at github.com/dirkpetersen/rclone, branch s3-md5-simd). Each server uses up to
# one core; 0 = Go's crypto/md5. 2 measured best on 8 vCPUs with 16-32 transfers.
# Passed as RCLONE_MD5_SIMD* variables (md5_env), which an rclone without the option
# ignores, so the setting is harmless with a stock rclone.
DEFAULT_MD5_SIMD_SERVERS = 2
_MD5_SIMD_SERVERS_BOUNDS = (0, 16)

# --buffer-size of copy jobs from a local source when MOTUZ_RCLONE_BUFFER_SIZE is unset:
# rclone's read-ahead only copies data the kernel's page cache already holds (about 10%
# more throughput to S3 at 4 GB/s, README "S3 connections"). Cloud sources keep rclone's
# 16M, which hides their latency.
LOCAL_SOURCE_BUFFER_SIZE = 0


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

Settings = namedtuple('Settings', 'defaults caps memory_budget extra_flags md5_simd_servers',
                      defaults=(DEFAULT_MD5_SIMD_SERVERS,))


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
    memory_budget = default_memory_budget()
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

    _add_motuz_defaults(defaults, caps, memory_budget)

    extra_flags = parse_extra_flags(_env(environ, 'MOTUZ_RCLONE_EXTRA_FLAGS') or '')

    name = 'MOTUZ_RCLONE_MD5_SIMD_SERVERS'
    raw = _env(environ, name)
    md5_simd_servers = DEFAULT_MD5_SIMD_SERVERS
    if raw is not None:
        md5_simd_servers = parse_int(raw)
        lo, hi = _MD5_SIMD_SERVERS_BOUNDS
        if md5_simd_servers is None or not lo <= md5_simd_servers <= hi:
            raise TuningConfigError('{}={!r} must be a whole number between {} and {} (0: no md5-simd)'.format(
                name, raw, lo, hi))

    settings = Settings(defaults, caps, memory_budget, extra_flags, md5_simd_servers)
    worst = max(CHUNKED_BACKENDS + (None,), key=lambda backend: estimate_memory(defaults, backend))
    needed = estimate_memory(defaults, worst)
    if needed > memory_budget:
        raise TuningConfigError(
            'The MOTUZ_RCLONE_* defaults need about {} of memory per job ({} destinations), more than '
            'MOTUZ_RCLONE_MEMORY_BUDGET={} (README, "Performance tuning")'.format(
                _describe_size(needed), worst or 'local', format_size(memory_budget)))
    return settings


def default_memory_budget(meminfo='/proc/meminfo', cgroup='/sys/fs/cgroup/memory.max'):
    """
    A quarter of the memory this server may use: MemTotal, or a lower cgroup v2 limit
    (a container), within DEFAULT_MEMORY_BUDGET_RANGE; the lower bound if unknown
    """
    lo, hi = DEFAULT_MEMORY_BUDGET_RANGE
    total = None
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    total = int(line.split()[1]) * KiB
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(cgroup) as f:
            limit = f.read().strip()
        if limit.isdigit():
            total = min(total, int(limit)) if total else int(limit)
    except OSError:
        pass
    if not total:
        return lo
    return max(lo, min(hi, total // 4 // GiB * GiB))


def _add_motuz_defaults(defaults, caps, memory_budget):
    """
    MOTUZ_DEFAULTS for the parameters the environment leaves unset, lowered to their
    caps and, on a smaller server, halved (transfers down to 16, S3 upload concurrency
    down to 4) until they fit the memory budget with the other settings; dropped if even
    that does not fit: they must never stop an installation that worked with rclone's
    defaults (e.g. a large buffer size).
    """
    added = dict(defaults)
    unset = [name for name in MOTUZ_DEFAULTS if name not in defaults]
    for name in unset:
        cap = PARAMS_BY_NAME[name].cap
        added[name] = min(MOTUZ_DEFAULTS[name], caps[cap]) if cap else MOTUZ_DEFAULTS[name]

    def fits():
        return all(estimate_memory(added, backend) <= memory_budget for backend in CHUNKED_BACKENDS + (None,))

    for name, floor in (('s3_upload_concurrency', 4), ('transfers', 16), ('s3_upload_concurrency', 4), ('transfers', 16)):
        while not fits() and name in unset and added[name] // 2 >= floor:
            added[name] //= 2
            if name == 'transfers' and 'checkers' in unset:
                added['checkers'] = max(added['transfers'] * 2, 32)
    if fits():
        defaults.update(added)


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


def copy_flags(overrides, dst_type, tuning=None, *, src_local=False, overwrite=True):
    """
    rclone flags of a copy job: the effective settings (none: rclone's defaults) plus the
    extra flags. Re-validates stored overrides, since the caps may have changed. A local
    source (`src_local`) gets LOCAL_SOURCE_BUFFER_SIZE unless the installation sets one.
    `overwrite` False (the overwrite policy, managers/overwrite_policy.py) adds
    KEEP_EXISTING_FLAG.
    """
    tuning = tuning or settings()
    overrides = validate_overrides(overrides, dst_type, tuning)
    values = effective(overrides, dst_type, tuning)
    if src_local:
        values.setdefault('buffer_size', LOCAL_SOURCE_BUFFER_SIZE)
    flags = ['{}={}'.format(PARAMS_BY_NAME[name].flag, format_value(PARAMS_BY_NAME[name], value))
             for name, value in values.items()]
    if overwrite is False:
        flags.extend(KEEP_EXISTING_FLAGS)
    return flags + list(tuning.extra_flags)


# Existing destination files are never modified, new ones are copied: a copy job whose
# destination was not written by Motuz before (managers/overwrite_policy.py). rclone
# logs the files it keeps only at DEBUG level, so it also reports every file on stdout
# (--combined: '= name' for an existing file, which --ignore-existing keeps; '+ name'
# new); CopyJobQueue collects the kept ones (copy_kept)
KEEP_EXISTING_FLAG = '--ignore-existing'
KEEP_EXISTING_FLAGS = (KEEP_EXISTING_FLAG, '--combined=-')


def hashsum_flags(overrides, tuning=None):
    """rclone flags of an md5sum (integrity check): --checkers plus the extra flags"""
    tuning = tuning or settings()
    overrides = validate_overrides(overrides, None, tuning, only=('checkers',))
    checkers = overrides.get('checkers', tuning.defaults.get('checkers'))
    flags = [] if checkers is None else ['--checkers={}'.format(checkers)]
    return flags + list(tuning.extra_flags)


def md5_env(tuning=None):
    """
    rclone's process variables for md5-simd (MOTUZ_RCLONE_MD5_SIMD_SERVERS), for copy and
    md5sum jobs: rclone reads every option from RCLONE_<OPTION> too, and one that does not
    know --md5-simd ignores them
    """
    servers = (tuning or settings()).md5_simd_servers
    return {'RCLONE_MD5_SIMD': 'true', 'RCLONE_MD5_SIMD_SERVERS': '{:d}'.format(servers)} if servers else {}


# --- presets ---------------------------------------------------------------------------------

# Target values; presets() clamps them to the caps and fits them to the memory budget
PRESETS = (
    ('default', 'Default', 'The settings of this server', {}),
    # Measured 2026-10-18, local files to AWS S3, 8 vCPUs (MOTUZ_DEFAULTS has the numbers):
    # small files scale with transfers (128: 1.2 GB/s for 1 MB files), large files peak
    # at 32 transfers with 16 MiB parts sent 16 at a time (4.0 GB/s); 64 MiB parts and few
    # transfers were slow (4 transfers: 0.5 GB/s). fit_preset lowers them to the budget.
    ('small_files', 'Many small files', 'Many files in parallel; for folders of files below ~30 MB', {
        'transfers': 128,
        'checkers': 256,
        's3_upload_concurrency': 4,
        's3_chunk_size': 16 * MiB,
    }),
    ('large_files', 'Large files', 'Fewer files, each sent in many parallel parts; for files of 100 MB and more', {
        'transfers': 32,
        'checkers': 64,
        'multi_thread_streams': 16,
        'multi_thread_cutoff': 64 * MiB,
        's3_upload_concurrency': 16,
        's3_chunk_size': 16 * MiB,
        'azureblob_upload_concurrency': 16,
        'azureblob_chunk_size': 16 * MiB,
    }),
    ('maximum', 'Maximum', 'As much parallelism as this server allows, for mixed folders on 100 Gb/s links', {
        'transfers': 128,
        'checkers': 256,
        'multi_thread_streams': 16,
        'multi_thread_cutoff': 64 * MiB,
        's3_upload_concurrency': 16,
        's3_chunk_size': 16 * MiB,
        'azureblob_upload_concurrency': 16,
        'azureblob_chunk_size': 16 * MiB,
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
