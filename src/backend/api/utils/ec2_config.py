"""
Settings of the temporary EC2 workers (README, "Temporary EC2 workers";
managers/ec2_launcher.py). All off unless MOTUZ_EC2_WORKERS=true.

Standard library only: config.py validates the environment with it at import time, so
the app and the Celery worker refuse to start with a bad value (Ec2ConfigError names
the variable), like the rclone performance settings.
"""
import os
import re
from collections import namedtuple
from urllib.parse import urlsplit


class Ec2ConfigError(ValueError):
    """An invalid MOTUZ_EC2_* / MOTUZ_AWS_REGION / MOTUZ_WORKER_SOURCE* setting"""


# The rclone release the workers install, the one pinned in
# deployment/docker/app/Dockerfile (test_ec2_launcher checks that they are the same)
RCLONE_VERSION = '1.75.1'
RCLONE_SHA256 = {
    'amd64': '982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab',
    'arm64': '03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9',
}

DEFAULT_INSTANCE_TYPES = '1T:c7gn.large,10T:c7gn.2xlarge,*:c7gn.4xlarge'
DEFAULT_SOURCE = 'https://github.com/dirkpetersen/motuz'

# (limit, instance type): a job whose source has fewer than `limit` bytes gets that type;
# limit None is the last entry ("*": everything larger, and sizes that are unknown
# because the listing took longer than MOTUZ_JOB_SIZE_TIMEOUT)
InstanceTypeRule = namedtuple('InstanceTypeRule', 'limit instance_type')

Ec2Settings = namedtuple('Ec2Settings', [
    'enabled',            # MOTUZ_EC2_WORKERS
    'pool',               # MOTUZ_EC2_POOL: the pool whose jobs get an EC2 worker each
    'region',             # MOTUZ_AWS_REGION
    'launch_templates',   # {'arm64': id or name, 'amd64': id or name}
    'template_version',   # '$Default', '$Latest' or a number
    'subnet_id',          # optional
    'instance_types',     # tuple of InstanceTypeRule, ascending
    'max_workers',        # concurrent instances
    'max_runtime',        # seconds: the instance is terminated and its job failed after this
    'boot_timeout',       # seconds: bootstrap token lifetime; unclaimed job + instance fail after this
    'reap_interval',      # seconds between reaper runs (manage.py ec2 reap --loop)
    'halt_delay',         # seconds a worker waits before shutting down after a failure (debugging)
    'source_url',         # MOTUZ_WORKER_SOURCE: where workers fetch Motuz
    'source_ref',         # MOTUZ_WORKER_SOURCE_REF (default: MOTUZ_SOURCE_COMMIT of the image)
])

TRUE = ('true', '1', 'yes', 'on')
FALSE = ('false', '0', 'no', 'off', '')

TYPE_RE = re.compile(r'^[a-z][a-z0-9-]*\.[a-z0-9]+$')
TEMPLATE_RE = re.compile(r'^(lt-[0-9a-f]{8,32}|[A-Za-z0-9().\-/_]{3,128})$')
SUBNET_RE = re.compile(r'^subnet-[0-9a-f]{8,32}$')
REGION_RE = re.compile(r'^[a-z]{2}(-[a-z]+)+-\d$')
REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
POOL_RE = re.compile(r'^[a-z][a-z0-9-]{0,31}$')

_SIZE_UNITS = {'': 1, 'B': 1, 'K': 1000, 'KB': 1000, 'M': 1000 ** 2, 'MB': 1000 ** 2, 'G': 1000 ** 3,
               'GB': 1000 ** 3, 'T': 1000 ** 4, 'TB': 1000 ** 4, 'P': 1000 ** 5, 'PB': 1000 ** 5}
_DURATION_UNITS = {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}


def _get(environ, name, default=''):
    return (environ.get(name) or '').strip() or default


def parse_bool(name, raw):
    value = (raw or '').strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise Ec2ConfigError('{}={!r} must be true or false'.format(name, raw))


def parse_size(name, raw):
    """Decimal sizes like MOTUZ_LARGE_JOB_BYTES: 500G = 500 * 10^9, 1T = 10^12"""
    match = re.match(r'^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$', raw or '')
    unit = match.group(2).upper() if match else None
    if not match or unit not in _SIZE_UNITS:
        raise Ec2ConfigError('{}: {!r} is not a size like 500G or 10T'.format(name, raw))
    return int(float(match.group(1)) * _SIZE_UNITS[unit])


def parse_duration(name, raw, lo, hi):
    match = re.match(r'^\s*(\d+)\s*([smhd]?)\s*$', (raw or '').lower())
    if not match:
        raise Ec2ConfigError('{}={!r} must be a duration like 90m, 24h or 2d'.format(name, raw))
    seconds = int(match.group(1)) * _DURATION_UNITS[match.group(2)]
    if not lo <= seconds <= hi:
        raise Ec2ConfigError('{}={!r} must be between {} and {}'.format(
            name, raw, format_duration(lo), format_duration(hi)))
    return seconds


def format_duration(seconds):
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= size and seconds % size == 0 and (unit != 'd' or seconds >= 2 * 86400):
            return '{}{}'.format(seconds // size, unit)
    return '{}s'.format(seconds)


def architecture(instance_type):
    """'arm64' for Graviton types (a 'g' after the generation: c7gn, c8gn, t4g, m7gd), else 'amd64'"""
    family = instance_type.split('.', 1)[0]
    match = re.match(r'^[a-z]+\d+([a-z-]*)$', family)
    return 'arm64' if match and 'g' in match.group(1) else 'amd64'


def parse_instance_types(raw, name='MOTUZ_EC2_INSTANCE_TYPES'):
    """'1T:c7gn.large,10T:c7gn.2xlarge,*:c7gn.4xlarge' -> ascending InstanceTypeRules"""
    rules = []
    entries = [e.strip() for e in (raw or '').split(',') if e.strip()]
    if not entries:
        raise Ec2ConfigError('{} is empty'.format(name))
    for index, entry in enumerate(entries):
        limit_text, sep, instance_type = entry.partition(':')
        instance_type = instance_type.strip().lower()
        if not sep or not TYPE_RE.match(instance_type):
            raise Ec2ConfigError('{}: {!r} is not <size>:<instance type>, e.g. 1T:c7gn.large'.format(name, entry))
        last = index == len(entries) - 1
        if limit_text.strip() == '*':
            if not last:
                raise Ec2ConfigError('{}: only the last entry may be *:<type>'.format(name))
            limit = None
        else:
            limit = parse_size(name, limit_text)
            if rules and limit <= rules[-1].limit:
                raise Ec2ConfigError('{}: the sizes must increase ({!r})'.format(name, entry))
        rules.append(InstanceTypeRule(limit, instance_type))
    if rules[-1].limit is not None:
        # Larger jobs get the last type too
        rules[-1] = InstanceTypeRule(None, rules[-1].instance_type)
    return tuple(rules)


def choose_instance_type(rules, source_bytes):
    """The type for a job whose source has `source_bytes` (None: unknown, i.e. huge)"""
    if source_bytes is not None:
        for rule in rules:
            if rule.limit is None or source_bytes < rule.limit:
                return rule.instance_type
    return rules[-1].instance_type


def fallback_instance_type(rules, instance_type):
    """
    The type to try once when `instance_type` has no capacity: the next entry of the
    table with another type, or for the last entry the one before it. None if the
    table has only this type.
    """
    types = []
    for rule in rules:
        if rule.instance_type not in types:
            types.append(rule.instance_type)
    if instance_type not in types:
        return types[0] if types else None
    index = types.index(instance_type)
    if index + 1 < len(types):
        return types[index + 1]
    return types[index - 1] if index > 0 else None


def is_git_sha(ref):
    return bool(ref and SHA_RE.match(ref))


def load_settings(environ=None, local_job_pool='central', large_job_pool='central', public_url=None):
    """Ec2Settings from the environment; Ec2ConfigError for an invalid value"""
    environ = os.environ if environ is None else environ
    enabled = parse_bool('MOTUZ_EC2_WORKERS', environ.get('MOTUZ_EC2_WORKERS'))
    pool = _get(environ, 'MOTUZ_EC2_POOL', 'aws')
    if not POOL_RE.match(pool) or pool == 'central':
        raise Ec2ConfigError('MOTUZ_EC2_POOL={!r} must be a pool name like aws (not central)'.format(pool))

    region = _get(environ, 'MOTUZ_AWS_REGION') or None
    if region is not None and not REGION_RE.match(region):
        raise Ec2ConfigError('MOTUZ_AWS_REGION={!r} is not a region like us-west-2'.format(region))

    templates = {}
    for arch, variable, default in (('arm64', 'MOTUZ_EC2_LAUNCH_TEMPLATE_ARM64', 'motuz-worker-arm64'),
                                    ('amd64', 'MOTUZ_EC2_LAUNCH_TEMPLATE_AMD64', 'motuz-worker')):
        value = _get(environ, variable, default)
        if not TEMPLATE_RE.match(value):
            raise Ec2ConfigError('{}={!r} is not a launch template id (lt-...) or name'.format(variable, value))
        templates[arch] = value
    version = _get(environ, 'MOTUZ_EC2_LAUNCH_TEMPLATE_VERSION', '$Default')
    if version not in ('$Default', '$Latest') and not version.isdigit():
        raise Ec2ConfigError('MOTUZ_EC2_LAUNCH_TEMPLATE_VERSION={!r} must be $Default, $Latest or a number'.format(version))

    subnet = _get(environ, 'MOTUZ_EC2_SUBNET_ID') or None
    if subnet is not None and not SUBNET_RE.match(subnet):
        raise Ec2ConfigError('MOTUZ_EC2_SUBNET_ID={!r} is not a subnet id (subnet-...)'.format(subnet))

    rules = parse_instance_types(_get(environ, 'MOTUZ_EC2_INSTANCE_TYPES', DEFAULT_INSTANCE_TYPES))

    raw_max = _get(environ, 'MOTUZ_EC2_MAX_WORKERS', '2')
    if not raw_max.isdigit() or not 1 <= int(raw_max) <= 100:
        raise Ec2ConfigError('MOTUZ_EC2_MAX_WORKERS={!r} must be a whole number between 1 and 100'.format(raw_max))

    max_runtime = parse_duration('MOTUZ_EC2_MAX_RUNTIME', _get(environ, 'MOTUZ_EC2_MAX_RUNTIME', '24h'), 600, 14 * 86400)
    boot_timeout = parse_duration('MOTUZ_EC2_BOOT_TIMEOUT', _get(environ, 'MOTUZ_EC2_BOOT_TIMEOUT', '20m'), 180, 3 * 3600)
    reap_interval = parse_duration('MOTUZ_EC2_REAP_INTERVAL', _get(environ, 'MOTUZ_EC2_REAP_INTERVAL', '60s'), 10, 3600)
    halt_delay = parse_duration('MOTUZ_EC2_HALT_DELAY', _get(environ, 'MOTUZ_EC2_HALT_DELAY', '0'), 0, 3 * 3600)
    if boot_timeout >= max_runtime:
        raise Ec2ConfigError('MOTUZ_EC2_BOOT_TIMEOUT must be shorter than MOTUZ_EC2_MAX_RUNTIME')

    source_url = _get(environ, 'MOTUZ_WORKER_SOURCE', DEFAULT_SOURCE).rstrip('/')
    parts = urlsplit(source_url)
    if (parts.scheme != 'https' or not parts.netloc or parts.query or parts.fragment
            or not re.match(r'^[A-Za-z0-9._~/%-]*$', parts.path) or not re.match(r'^[A-Za-z0-9.-]+(:\d+)?$', parts.netloc)):
        raise Ec2ConfigError('MOTUZ_WORKER_SOURCE={!r} must be an https:// git repository URL'.format(source_url))
    source_ref = _get(environ, 'MOTUZ_WORKER_SOURCE_REF') or _get(environ, 'MOTUZ_SOURCE_COMMIT') or None
    if source_ref is not None and (not REF_RE.match(source_ref) or '..' in source_ref):
        raise Ec2ConfigError('MOTUZ_WORKER_SOURCE_REF={!r} is not a commit, tag or branch'.format(source_ref))

    if enabled:
        if region is None:
            raise Ec2ConfigError('MOTUZ_EC2_WORKERS=true needs MOTUZ_AWS_REGION')
        if large_job_pool != pool:
            raise Ec2ConfigError('MOTUZ_EC2_WORKERS=true needs MOTUZ_LARGE_JOB_POOL={} (the EC2 pool), not {!r}'.format(
                pool, large_job_pool))
        if local_job_pool == pool:
            raise Ec2ConfigError('MOTUZ_LOCAL_JOB_POOL must not be the EC2 pool {}: jobs with a local path never '
                                 'run on EC2 workers'.format(pool))
        if not public_url or not public_url.startswith('https://'):
            raise Ec2ConfigError('MOTUZ_EC2_WORKERS=true needs MOTUZ_PUBLIC_URL, the https:// address workers reach')
        if source_ref is None:
            raise Ec2ConfigError('MOTUZ_EC2_WORKERS=true needs MOTUZ_WORKER_SOURCE_REF (the commit or tag this '
                                 'node runs; images built by bin/prod/build.sh know their commit)')

    return Ec2Settings(
        enabled=enabled,
        pool=pool,
        region=region,
        launch_templates=templates,
        template_version=version,
        subnet_id=subnet,
        instance_types=rules,
        max_workers=int(raw_max),
        max_runtime=max_runtime,
        boot_timeout=boot_timeout,
        reap_interval=reap_interval,
        halt_delay=halt_delay,
        source_url=source_url,
        source_ref=source_ref,
    )
