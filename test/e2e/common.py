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


# ---------------------------------------------------------------- viewer (pager) fixtures and checks
# A ~5 MiB log of numbered lines ("line 000001 ...") of varying length, some with
# multibyte characters. The same code writes it in a container (as a user, run with
# `python3 - <path>`) and builds the expected bytes here.
NUMBERED_LOG_LINES = 105000
NUMBERED_LOG_CODE = f'''
import sys
def numbered_log():
    return b"".join(("line %06d %s of a numbered log\\n" % (i, "\\u00fc" * (i % 5) + "x" * (i % 29))).encode()
                    for i in range(1, {NUMBERED_LOG_LINES} + 1))
if __name__ == "__main__" and len(sys.argv) > 1:
    with open(sys.argv[1], "wb") as f:
        f.write(numbered_log())
'''
CHUNK_BYTES = 256 * 1024


def numbered_log():
    namespace = {'__name__': 'numbered_log'}
    exec(NUMBERED_LOG_CODE, namespace)
    return namespace['numbered_log']()


def check_chunked_reads(req, token, path, connection_id, data, label):
    """
    Reads `path` through /api/system/files/view/chunk/ forward from the start, the
    tail, and backward from the tail to the start; checks that the chunks are
    contiguous, hold whole lines and together are the file (`data`).
    """
    def chunk(**params):
        status, body, raw = req('POST', '/api/system/files/view/chunk/', token,
                                dict(path=path, connection_id=connection_id, **params))
        return (body if status == 200 else None), (status, str(raw)[:300])

    def whole(chunks, data):
        contiguous = all(a['end'] == b['offset'] for a, b in zip(chunks, chunks[1:]))
        lines = all(c['content'].startswith('line ') and c['content'].endswith('\n') for c in chunks)
        small = all(0 < c['end'] - c['offset'] <= CHUNK_BYTES for c in chunks)
        exact = all(c['content'].encode() == data[c['offset']:c['end']] for c in chunks)
        return (bool(chunks) and chunks[0]['offset'] == 0 and chunks[0]['bof'] and chunks[-1]['end'] == len(data)
                and chunks[-1]['eof'] and contiguous and lines and small and exact
                and ''.join(c['content'] for c in chunks).encode() == data)

    def summary(chunks, error):
        return error or [(c['offset'], c['end'], c['bof'], c['eof']) for c in chunks[:3]] + ['...', len(chunks)]

    forward, error = [], None
    while len(forward) < 200:
        c, error = chunk(offset=forward[-1]['end'] if forward else 0)
        if c is None:
            break
        forward.append(c)
        error = None
        if c['eof']:
            break
    first = forward[0] if forward else {}
    check(f'{label}: first chunk: whole lines from line 000001, at most 256 KiB, bof, not eof, file size',
          first.get('offset') == 0 and first.get('bof') is True and first.get('eof') is False and first.get('size') == len(data)
          and first['content'].startswith('line 000001 ') and first['content'].endswith('\n')
          and CHUNK_BYTES - 200 < first['end'] <= CHUNK_BYTES and first.get('encoding') == 'utf-8', summary(forward, error))
    check(f'{label}: read forward chunk by chunk: contiguous offsets, no line split, together the file',
          error is None and len(forward) >= len(data) // CHUNK_BYTES and whole(forward, data), summary(forward, error))

    tail, error = chunk(from_end=True)
    last_line = f'line {NUMBERED_LOG_LINES:06d} '
    check(f'{label}: tail read: the last lines, bof false, eof true',
          tail is not None and tail['eof'] is True and tail['bof'] is False and tail['end'] == len(data)
          and tail['content'].startswith('line ') and tail['content'].rstrip('\n').split('\n')[-1].startswith(last_line)
          and len(data) - CHUNK_BYTES <= tail['offset'] < len(data) - CHUNK_BYTES + 200
          and tail['content'].encode() == data[tail['offset']:],
          error if tail is None else {k: tail[k] for k in tail if k != 'content'})

    backward = [tail] if tail else []
    while backward and not backward[0]['bof'] and len(backward) < 200:
        c, error = chunk(before=backward[0]['offset'])
        if c is None:
            break
        backward.insert(0, c)
    check(f'{label}: read backward from the tail to the beginning: contiguous, no line split, together the file',
          whole(backward, data), summary(backward, error))
