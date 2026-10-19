"""
The existing destination files a copy job kept (overwrite policy, rclone_tuning
KEEP_EXISTING_FLAG): rclone logs them only at DEBUG level, so a job with
--ignore-existing also writes rclone's --combined report ('= name' for an existing file,
which --ignore-existing keeps) to a file of its own. On stdout that report would make
rclone redraw its --progress block after every file.

The file is created by rclone as the user at an unpredictable path in /tmp (new_path),
and read and removed by READER as the user after rclone exits (CopyJobQueue), on the
central node and on remote workers alike. Stdlib only.
"""
import json
import logging
import os
import secrets

from . import file_view
from .abstract_connection import sudo_as, user_process_env


KEPT_SAMPLE = 1000 # paths of kept files collected per job; the rest is only counted
READER_TIMEOUT = 60
COMBINED_FLAG = '--combined'

# argv: the report, the number of paths to return. Counts the '= ' lines, returns the
# first paths and removes the file (also after an error). Never follows a symlink.
READER = r'''
import json, os, sys
path, limit = sys.argv[1], int(sys.argv[2])
count, sample = 0, []
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        for line in f:
            if line.startswith(b'= '):
                count += 1
                if len(sample) < limit:
                    sample.append(line[2:].rstrip(b'\r\n').decode('utf-8', 'replace'))
except FileNotFoundError:
    pass
finally:
    try:
        os.unlink(path)
    except OSError:
        pass
sys.stdout.write(json.dumps({"count": count, "sample": sample}))
'''


def new_path(job_id):
    """Where rclone writes the report of `job_id`: unpredictable, so nobody can plant a link"""
    return '/tmp/motuz-kept-{}-{}'.format(int(job_id) if str(job_id).isdigit() else 'job', secrets.token_hex(16))


def combined_flag(path):
    return '{}={}'.format(COMBINED_FLAG, path)


def reader_command(user, path):
    """The command that runs READER as `user` on `path` (like space_check.reader_command)"""
    from .local_connection import ENV
    from .local_credentials import _python
    command = [*sudo_as(user), ENV]
    if os.environ.get('LD_LIBRARY_PATH'):
        command.append('LD_LIBRARY_PATH={}'.format(os.environ['LD_LIBRARY_PATH']))
    return command + [_python(), '-I', '-S', '-c', READER, path, str(KEPT_SAMPLE)]


def parse(stdout):
    """[count, sample] of READER's output, [0, []] if it is not what READER prints"""
    try:
        result = json.loads(stdout.decode('utf-8'))
        count, sample = int(result['count']), result['sample']
    except (ValueError, UnicodeDecodeError, KeyError, TypeError):
        return [0, []]
    if count < 0 or not isinstance(sample, list):
        return [0, []]
    return [count, [_without_link_suffix(path) for path in sample[:KEPT_SAMPLE] if isinstance(path, str)]]


# With --links (local to local, rclone_connection.local_to_local_flags) rclone names a
# symlink "<name>.rclonelink" in its reports
LINK_SUFFIX = '.rclonelink'


def _without_link_suffix(path):
    return path[:-len(LINK_SUFFIX)] if path.endswith(LINK_SUFFIX) and len(path) > len(LINK_SUFFIX) else path


def read(user, path, timeout=READER_TIMEOUT, run=file_view.run_limited):
    """[count, first KEPT_SAMPLE paths] of the report at `path`, read and removed as `user`"""
    try:
        returncode, stdout, _ = run(reader_command(user, path), timeout, env=user_process_env())
    except (file_view.ViewTimeoutError, OSError) as e:
        logging.warning('The kept files of a copy job could not be read: %s', e)
        return [0, []]
    if returncode != 0:
        logging.warning('The kept files of a copy job could not be read (exit status %s)', returncode)
        return [0, []]
    return parse(stdout)
