"""Shared helpers for the end-to-end suites. The suites expect the stack that
test/e2e/run.sh starts (Traefik on https://localhost, fake Microsoft on 127.0.0.1:5999).

Every suite records checks with check()/skip() and ends with finish(), which prints
"<passed>/<total> passed[, <n> skipped]" (parsed by run.sh) and sets the exit code."""
import os
import shutil
import ssl
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get('MOTUZ_E2E_WORK', os.path.join(HERE, '.work'))
FAKE_DIR = os.path.join(WORK, 'fake')
FAKE_LOG = os.path.join(FAKE_DIR, 'requests.jsonl')  # every request fake_ms.py received
FAKE_MODE = os.path.join(FAKE_DIR, 'mode.txt')  # "fail" makes the fake /token fail
BASE = os.environ.get('MOTUZ_E2E_BASE', 'https://localhost')
CTX = ssl._create_unverified_context()  # self-signed certificate made by run.sh


def _compose():
    if os.environ.get('MOTUZ_E2E_COMPOSE'):
        cmd = os.environ['MOTUZ_E2E_COMPOSE'].split()
    elif shutil.which('docker-compose'):
        cmd = ['docker-compose']
    else:
        cmd = ['docker', 'compose']
    return cmd + ['-f', os.path.join(HERE, 'compose.yml')]


COMPOSE = _compose()


def compose(*args, **kwargs):
    """Runs a compose command in test/e2e (where the generated .env is)"""
    return subprocess.run(COMPOSE + list(args), capture_output=True, text=True, cwd=HERE, **kwargs)


def sh(service, cmd, stdin=None):
    return compose('exec', '-T', service, 'sh', '-c', cmd, input=stdin)


def db_password():
    with open(os.path.join(WORK, 'secrets', 'MOTUZ_DATABASE_PASSWORD')) as f:
        return f.read()


def psql(sql):
    return compose('exec', '-T', 'database', 'psql', f'postgresql://motuz_user:{db_password()}@127.0.0.1:5432/motuz',
                   '-tAc', sql).stdout.strip()


def service_logs(*services):
    return compose('logs', *services).stdout


def set_fake_mode(mode):
    with open(FAKE_MODE, 'w') as f:
        f.write(mode)


_results = []
_prefix = ''


def set_prefix(prefix):
    global _prefix
    _prefix = prefix


def check(name, ok, detail=''):
    _results.append('PASS' if ok else 'FAIL')
    print('PASS' if ok else 'FAIL', _prefix + name, ('' if ok else detail))
    sys.stdout.flush()
    return bool(ok)


def skip(name, reason=''):
    _results.append('SKIP')
    print('SKIP', _prefix + name, reason)


def finish():
    passed, failed, skipped = (_results.count(s) for s in ('PASS', 'FAIL', 'SKIP'))
    print(f"\n{passed}/{passed + failed} passed" + (f", {skipped} skipped" if skipped else ''))
    sys.exit(1 if failed else 0)
