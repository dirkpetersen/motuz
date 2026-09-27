"""
Can this node run jobs? A worker must see the same storage as the app, at the same
paths: a job that starts on a node where a mount is missing would copy from an empty
directory or into the local disk below the mount point.

- MOTUZ_REQUIRED_PATHS / MOTUZ_REQUIRED_MOUNTS: colon-separated absolute paths that must
  be directories / directories that are mount points (e.g. /fh/fast:/fh/scratch).
  Checked before every job (tasks/celery_tasks.py: the job fails with the reason instead
  of running) and when the worker starts (ExecStartPre of the systemd install's
  motuz-celery.service: the unit fails and says why). Unset: nothing is checked (the
  docker install sets neither).
- The version of this node's code and rclone (`local_version`, `version_problems`), for
  comparing workers with the app. The systemd install has one worker, next to the app;
  remote workers come with the HTTPS worker API, which is meant to use these.

As a script (standard library only), with the venv's python:
    python node_check.py start           # exit 1 and the reasons on stderr if paths are missing
    python node_check.py version REPO    # this node's version as JSON
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading

RCLONE = '/usr/local/bin/rclone'
CHECK_TIMEOUT = 15 # seconds; a hung network mount must not hang the check


def _paths(value):
    return [p for p in (value or '').split(':') if p.strip()]


def _check_path(path, mount):
    if not path.startswith('/'):
        return '{} is not an absolute path'.format(path)
    try:
        if not os.path.isdir(path): # follows symlinks and triggers automounts
            return '{} is not a directory (missing or not mounted)'.format(path)
        if mount and not os.path.ismount(os.path.realpath(path)):
            return '{} is not a mount point'.format(path)
    except OSError as e:
        return '{}: {}'.format(path, e.strerror or e)
    return None


def required_paths_problems(environ=None, timeout=CHECK_TIMEOUT):
    """A list of problems (empty if all required paths and mounts are there)"""
    environ = os.environ if environ is None else environ
    checks = [(p, False) for p in _paths(environ.get('MOTUZ_REQUIRED_PATHS'))]
    checks += [(p, True) for p in _paths(environ.get('MOTUZ_REQUIRED_MOUNTS'))]
    problems = []
    for path, mount in checks:
        result = []
        thread = threading.Thread(target=lambda: result.append(_check_path(path, mount)), daemon=True)
        thread.start()
        thread.join(timeout)
        if not result:
            problems.append('{} does not respond (hung mount?)'.format(path))
        elif result[0]:
            problems.append(result[0])
    return problems


def code_version(repo):
    """sha256 over the backend source and requirements.txt (paths and contents), also
    for checkouts without git"""
    digest = hashlib.sha256()
    files = [os.path.join(repo, 'requirements.txt')]
    for root, dirs, names in os.walk(os.path.join(repo, 'src', 'backend')):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        files += [os.path.join(root, n) for n in names
                  if not n.endswith(('.pyc', '.sqlite3')) and not n.startswith('.')]
    for path in sorted(files):
        with open(path, 'rb') as f:
            digest.update(os.path.relpath(path, repo).encode() + b'\0' + hashlib.sha256(f.read()).digest())
    return digest.hexdigest()[:16]


def git_commit(repo):
    try:
        return subprocess.run(['git', '-C', repo, 'rev-parse', '--short', 'HEAD'], capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def rclone_version(rclone=RCLONE):
    try:
        first = subprocess.run([rclone, 'version'], capture_output=True, text=True, timeout=30).stdout.split('\n')[0]
    except (OSError, subprocess.SubprocessError):
        return None
    return first.split()[-1] if first.startswith('rclone ') else None


def local_version(repo):
    return {'code': code_version(repo), 'commit': git_commit(repo), 'rclone': rclone_version()}


def version_problems(local, expected):
    """Why a node with version `local` must not run jobs for an app with `expected`"""
    if not expected:
        return ['the expected version is unknown']
    problems = []
    if local.get('code') != expected.get('code'):
        problems.append('Motuz code {} (commit {}) differs from the expected {} (commit {})'.format(
            local.get('code'), local.get('commit'), expected.get('code'), expected.get('commit')))
    if local.get('rclone') != expected.get('rclone'):
        problems.append('rclone {} differs from the expected {}'.format(local.get('rclone'), expected.get('rclone')))
    return problems


def main(argv, environ=None):
    if len(argv) == 3 and argv[1] == 'version':
        print(json.dumps(local_version(argv[2])))
        return 0
    if len(argv) != 2 or argv[1] != 'start':
        sys.stderr.write('usage: node_check.py start | version REPO\n')
        return 2
    node = socket.gethostname()
    problems = required_paths_problems(environ)
    if problems:
        for problem in problems:
            print('motuz node check on {}: {}'.format(node, problem), file=sys.stderr)
        print('motuz node check on {}: refusing to start the worker'.format(node), file=sys.stderr)
        return 1
    print('motuz node check on {}: ok'.format(node))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
