"""
The free space limit (MOTUZ_MAX_FILL_PERCENT, README "Free space limit"): a copy job or
an upload to the local filesystem is refused before it starts when, once its data is
there, the destination's filesystem or quota would be more than that full (default 90%;
100 = no check). Shared by the central node (managers/space_manager.py, at create) and
remote workers (src/worker/motuz_worker.py, before rclone starts, when the destination
is on the worker), so it must not import the Flask application. Stdlib only.

What is checked, read as the user by READER (sudo ... /usr/bin/env python3 -I -S -c),
on the nearest existing ancestor of the destination:
- the filesystem: statvfs, total = f_blocks * f_frsize, used = total - f_bfree * f_frsize.
  The CephFS kernel client reports the quota of the mount's root (or of the quota realm
  above it) here already, XFS the project quota of a directory tree with prjquota;
- a CephFS directory quota: `ceph.quota.max_bytes` of the directory or of its nearest
  ancestor on the same mount that has one, with `ceph.dir.rbytes` of that directory as
  its usage (recursive statistics, which the MDS updates lazily);
- the user's and group's block quotas (quotactl, Q_GETQUOTA) of a filesystem on a local
  block device (ext4, XFS); NFS (rquota), Lustre, GPFS or BeeGFS quotas are not read.
When a quota applies, it is the limit (the fullest one after the job, if there are
several), otherwise the filesystem.

The bytes to add are `rclone size` of the source minus `rclone size` of what already
exists at the destination (so a Retry of a half-done copy is not refused), never below
0; for an upload the browser's sizes of the files it sends. If the space or a size
cannot be determined within the timeout, the job is not blocked: it runs with a note
("Free space not checked: ...") and a full disk still stops it (utils/disk_full.py).
"""
import json
import logging
import math
import os
import time
from collections import namedtuple

from .abstract_connection import RcloneException, sudo_as, user_process_env
from . import file_view


ENV_MAX_FILL = 'MOTUZ_MAX_FILL_PERCENT'
DEFAULT_MAX_FILL = 90
NO_CHECK = 100
READER_TIMEOUT = 30 # seconds, at most, of the job's timeout for reading the space

FILESYSTEM = 'filesystem'
CEPH_QUOTA = 'ceph-quota'
USER_QUOTA = 'user-quota'
GROUP_QUOTA = 'group-quota'
KINDS = (FILESYSTEM, CEPH_QUOTA, USER_QUOTA, GROUP_QUOTA)

# The capacity of one limit of a destination: `total` and `used` bytes; `path` is the
# directory it was read on (the quota's directory for CEPH_QUOTA)
Limit = namedtuple('Limit', 'kind total used path')


class SpaceConfigError(ValueError):
    pass


class SpaceUnknown(Exception):
    """The space or a size could not be determined: the job runs without the check"""


class NotEnoughSpace(Exception):
    """The job would fill its destination above the limit; the message is for the user"""


def parse_max_fill(value):
    """
    MOTUZ_MAX_FILL_PERCENT: a whole number from 1 to 100 (100: no check); unset or
    empty: DEFAULT_MAX_FILL. Raises SpaceConfigError.
    """
    value = (value or '').strip().rstrip('%').strip()
    if not value:
        return DEFAULT_MAX_FILL
    if not value.isdigit() or not 1 <= int(value) <= NO_CHECK:
        raise SpaceConfigError('{} must be a whole number from 1 to {} (100: no check), not {!r}'.format(
            ENV_MAX_FILL, NO_CHECK, value))
    return int(value)


def max_fill_percent(environ=None):
    environ = os.environ if environ is None else environ
    return parse_max_fill(environ.get(ENV_MAX_FILL))


def exceeds(used, to_add, total, max_percent):
    """True if `used` + `to_add` bytes are more than `max_percent` of `total` (exact)"""
    if max_percent >= NO_CHECK:
        return False
    if total <= 0:
        return to_add > 0
    return (used + to_add) * 100 > max_percent * total


def fill_percent(used, to_add, total):
    return (used + to_add) * 100 / total if total > 0 else math.inf


# Runs as the user: finds the nearest existing ancestor of the path (argv[1]), and
# prints {"path", "limits": [{"kind", "path", "total", "used"}]}: its filesystem
# (statvfs), the CephFS quota that applies (xattrs, on the same mount) and the user's
# and group's block quotas (quotactl, for a filesystem on a /dev device). Reads no file.
# Errors: exit status 3 and {"error": kind} on stdout.
READER = r'''
import errno, json, os, stat, sys
def fail(kind):
    sys.stdout.write(json.dumps({"error": kind})); sys.stdout.flush(); os._exit(3)
p = os.path.normpath(sys.argv[1])
while True:
    try:
        st = os.stat(p)
        break
    except (FileNotFoundError, NotADirectoryError):
        if p == "/": fail("missing")
        p = os.path.dirname(p)
    except PermissionError:
        fail("denied")
    except OSError:
        fail("error")
d = p if stat.S_ISDIR(st.st_mode) else os.path.dirname(p)
try:
    sv = os.statvfs(p)
    dst = os.stat(d)
except PermissionError:
    fail("denied")
except OSError:
    fail("error")
total = sv.f_blocks * sv.f_frsize
limits = [{"kind": "filesystem", "path": p, "total": total, "used": total - sv.f_bfree * sv.f_frsize}]
def number(q, name):
    try:
        return int(os.getxattr(q, name).split(b"\0")[0].strip() or 0)
    except ValueError:
        return 0
q = d
while True:
    try:
        max_bytes = number(q, "ceph.quota.max_bytes")
    except OSError as e:
        if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP): break # not CephFS
        max_bytes = 0 # ENODATA: no quota on this directory
    if max_bytes > 0:
        try:
            limits.append({"kind": "ceph-quota", "path": q, "total": max_bytes, "used": number(q, "ceph.dir.rbytes")})
        except OSError:
            pass
        break
    parent = os.path.dirname(q)
    try:
        if parent == q or os.stat(parent).st_dev != dst.st_dev: break # the mount's root
    except OSError:
        break
    q = parent
try:
    import ctypes
    want = "%d:%d" % (os.major(dst.st_dev), os.minor(dst.st_dev))
    special = None
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, sep, right = line.partition(" - ")
            fields, rest = left.split(), right.split()
            if sep and len(fields) > 4 and len(rest) > 1 and fields[2] == want:
                special = rest[1]
    if special and special.startswith("/dev/"):
        class Dqblk(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("bhard", "bsoft", "space", "ihard", "isoft", "inodes", "btime", "itime")] + [("valid", ctypes.c_uint32)]
        libc = ctypes.CDLL(None, use_errno=True)
        libc.quotactl.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        gid = dst.st_gid if dst.st_mode & stat.S_ISGID else os.getegid() # the group of new files
        for kind, qtype, qid in (("user-quota", 0, os.geteuid()), ("group-quota", 1, gid)):
            cmd = (0x800007 << 8) | qtype # QCMD(Q_GETQUOTA, type)
            cmd -= 1 << 32
            dq = Dqblk()
            if libc.quotactl(cmd, special.encode(), qid, ctypes.byref(dq)) != 0: continue # no quota
            limit = dq.bhard or dq.bsoft # 1 KiB blocks
            if dq.valid & 1 and limit: # QIF_BLIMITS
                limits.append({"kind": kind, "path": p, "total": limit * 1024, "used": dq.space})
except Exception:
    pass
sys.stdout.write(json.dumps({"path": p, "limits": limits}))
'''

_READER_ERRORS = {
    'missing': "no part of '{path}' exists",
    'denied': "{user} cannot access '{path}'",
    'error': "'{path}' could not be read",
}


def reader_command(user, path):
    """The command that runs READER as `user` on `path`"""
    from .local_connection import ENV
    from .local_credentials import _python
    command = [*sudo_as(user), ENV]
    # sudo drops LD_LIBRARY_PATH, which the image's python needs when /etc is the host's
    if os.environ.get('LD_LIBRARY_PATH'):
        command.append('LD_LIBRARY_PATH={}'.format(os.environ['LD_LIBRARY_PATH']))
    # The path is an argument of the script, never an option of env or python
    return command + [_python(), '-I', '-S', '-c', READER, path]


def parse_reader(returncode, stdout, user, path):
    """The [Limit] of READER's output; raises SpaceUnknown"""
    try:
        result = json.loads(stdout.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        result = None
    if not isinstance(result, dict):
        raise SpaceUnknown("the space of '{}' could not be read".format(path))
    if returncode == 3 or 'error' in result:
        message = _READER_ERRORS.get(result.get('error'), _READER_ERRORS['error'])
        raise SpaceUnknown(message.format(user=user, path=path))
    limits = []
    for entry in result.get('limits') or ():
        try:
            limit = Limit(entry['kind'], int(entry['total']), int(entry['used']), str(entry['path']))
        except (KeyError, TypeError, ValueError):
            continue
        if limit.kind in KINDS and limit.total > 0 and limit.used >= 0:
            limits.append(limit)
    if returncode != 0 or not limits:
        raise SpaceUnknown("the space of '{}' could not be read".format(path))
    return limits


def read_limits(user, path, timeout, run=file_view.run_limited):
    """The [Limit] of the local destination `path`, read as `user`; raises SpaceUnknown"""
    if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
        raise SpaceUnknown("'{}' is not an absolute path".format(path))
    try:
        returncode, stdout, stderr = run(reader_command(user, path), timeout, env=user_process_env())
    except file_view.ViewTimeoutError:
        raise SpaceUnknown('reading the space of {} took longer than {} seconds'.format(path, int(timeout)))
    except OSError as e:
        raise SpaceUnknown(str(e))
    if returncode not in (0, 3):
        logging.warning("Reading the space of '%s' as %s failed (%s): %s", path, user, returncode,
                        stderr.decode('utf-8', 'replace')[-500:])
    return parse_reader(returncode, stdout, user, path)


def human(n):
    """1.2 TiB"""
    n = float(max(0, n))
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if n < 1024 or unit == 'PiB':
            return '{} B'.format(int(n)) if unit == 'B' else '{:.1f} {}'.format(n, unit)
        n /= 1024


def _percent(value, max_percent):
    """96%; 90.4% where the whole number would look like the limit itself"""
    if math.isinf(value):
        return 'over 100%'
    text = '{:.0f}'.format(value)
    if value > max_percent and int(text) <= max_percent:
        text = '{:.1f}'.format(value)
    return text + '%'


def _what(limit):
    if limit.kind == CEPH_QUOTA:
        return 'its quota (CephFS quota of {})'.format(limit.path)
    if limit.kind == USER_QUOTA:
        return 'your disk quota'
    if limit.kind == GROUP_QUOTA:
        return "its group's disk quota"
    return 'its filesystem'


def evaluate(verb, dst_path, limits, to_add, max_percent):
    """
    The note of a job that adds `to_add` bytes below `dst_path` ('Copying' or
    'Uploading'), or NotEnoughSpace if one of the `limits` would be more than
    `max_percent` full: the quotas if there are any (the fullest after the job), else
    the filesystem.
    """
    # A quota, when one applies, is the limit instead of the filesystem's size
    quotas = [limit for limit in limits if limit.kind != FILESYSTEM]
    worst = None
    for limit in quotas or limits:
        if worst is None or fill_percent(limit.used, to_add, limit.total) > fill_percent(worst.used, to_add, worst.total):
            worst = limit
    if worst is None:
        return not_checked('the space of {} is not known'.format(dst_path))
    fill = fill_percent(worst.used, to_add, worst.total)
    if exceeds(worst.used, to_add, worst.total, max_percent):
        advice = ('Free space or choose another destination.' if worst.kind == FILESYSTEM
                  else 'Free space, ask for a larger quota or choose another destination.')
        raise NotEnoughSpace('{} {} to {} would fill {} to {} (limit {}%): {} of {} free. {}'.format(
            verb, human(to_add), dst_path, _what(worst), _percent(fill, max_percent), max_percent,
            human(worst.total - worst.used), human(worst.total), advice))
    return 'Free space checked: {} {} leaves {} {:.0f}% full (limit {}%, {} of {} free before).'.format(
        verb.lower(), human(to_add), _what(worst), fill, max_percent, human(worst.total - worst.used), human(worst.total))


def not_checked(reason):
    return 'Free space not checked: {}. A full disk still stops the job.'.format(str(reason).rstrip('.')[:500])


def checked_percent(value):
    """A max_fill_percent from a job ticket (1 to 100); None/absent: no check"""
    if value is None:
        return NO_CHECK
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= NO_CHECK:
        raise RcloneException('Invalid max_fill_percent in the job ticket')
    return value


class _Deadline:
    def __init__(self, timeout):
        self.timeout = timeout
        self.end = time.monotonic() + timeout

    def remaining(self, cap=None):
        left = self.end - time.monotonic()
        if left <= 0:
            raise SpaceUnknown('measuring took longer than {} seconds'.format(int(self.timeout)))
        return min(left, cap) if cap else left


def check_copy(connection, user, *, src_credentials, src_path, src_local, dst_path, copy_links,
               max_percent, timeout, extra_env=None, read=None):
    """
    The free space check of a copy job to the local path `dst_path` (copyto semantics):
    a note for the job (None: no limit), or NotEnoughSpace. `connection` is an
    RcloneConnection (its `measure`), `src_credentials` the source's RCLONE_CONFIG_SRC_*
    ({} for a local source); everything runs as `user` within `timeout` seconds.
    """
    if max_percent >= NO_CHECK:
        return None
    deadline = _Deadline(timeout)
    try:
        limits = (read or read_limits)(user, dst_path, deadline.remaining(READER_TIMEOUT))
        src_bytes = connection.measure(src_credentials, src_path, local=src_local, user=user,
                                       timeout=deadline.remaining(), copy_links=copy_links, extra_env=extra_env)[0]
        dst_bytes = connection.measure({}, dst_path, local=True, user=user, timeout=deadline.remaining(),
                                       missing_ok=True)[0]
    except file_view.ViewTimeoutError:
        return not_checked('measuring took longer than {} seconds'.format(int(timeout)))
    except (SpaceUnknown, RcloneException, OSError) as e:
        return not_checked(e)
    return evaluate('Copying', dst_path, limits, max(0, src_bytes - dst_bytes), max_percent)


def check_upload(user, folder, to_add, max_percent, timeout, read=None):
    """The free space check of an upload of `to_add` bytes to the local `folder`"""
    if max_percent >= NO_CHECK:
        return None
    try:
        limits = (read or read_limits)(user, folder, min(timeout, READER_TIMEOUT))
    except SpaceUnknown as e:
        return not_checked(e)
    return evaluate('Uploading', folder, limits, max(0, to_add), max_percent)
