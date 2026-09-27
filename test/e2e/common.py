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


# Image and Markdown viewer fixtures (viewer_fixtures.py): run in a container as a user
# with `python3 - <folder>`, and here for the expected bytes
with open(os.path.join(HERE, 'viewer_fixtures.py')) as _f:
    VIEWER_FIXTURES_CODE = _f.read()
VIEW_IMAGE_MAX = 2 * 1024 * 1024 # MOTUZ_VIEW_IMAGE_MAX_BYTES in compose.yml

# Document viewer fixtures (document_fixtures.py), used the same way
with open(os.path.join(HERE, 'document_fixtures.py')) as _f:
    DOCUMENT_FIXTURES_CODE = _f.read()
VIEW_DOCUMENT_MAX = 4 * 1024 * 1024 # MOTUZ_VIEW_DOCUMENT_MAX_BYTES in compose.yml
DOCUMENT_RANGE_MAX = 4 * 1024 * 1024 # document_view.RANGE_MAX_BYTES


def document_fixtures():
    namespace = {'__name__': 'document_fixtures'}
    exec(DOCUMENT_FIXTURES_CODE, namespace)
    return namespace['fixtures']()


def viewer_fixtures():
    namespace = {'__name__': 'viewer_fixtures'}
    exec(VIEWER_FIXTURES_CODE, namespace)
    return namespace['fixtures']()


def api_request(method, path, token=None, body=None):
    """(status, JSON or the raw bytes, headers) of an API call through Traefik"""
    import json
    import urllib.error
    import urllib.request
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=180) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and 'json' in resp.headers.get('Content-Type', '') else raw), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except ValueError:
            return e.code, raw, e.headers


def check_image_view(token, other_token, folder, connection_id, label, other_status):
    """
    POST /api/system/files/view/image/ on the viewer fixtures in `folder`: the images
    with their detected type and the security headers, everything else refused.
    `other_token` (another user) gets `other_status`.
    """
    files = viewer_fixtures()

    def image(name, tok=token, path=None):
        return api_request('POST', '/api/system/files/view/image/', tok,
                   {'connection_id': connection_id, 'path': path or f'{folder}/{name}'})

    def message(body):
        return body.get('message', '') if isinstance(body, dict) else str(body)

    for name, mime in (('pic.png', 'image/png'), ('small.png', 'image/png'), ('pic.jpg', 'image/jpeg'),
                       ('PIC2.JPEG', 'image/jpeg'), ('pic.gif', 'image/gif'), ('pic.webp', 'image/webp')):
        status, body, headers = image(name)
        check(f'{label}: {name} served as {mime}', status == 200 and body == files[name]
              and headers.get('Content-Type') == mime and headers.get('Content-Length') == str(len(files[name])),
              (status, headers.get('Content-Type'), len(body) if isinstance(body, bytes) else body))
    status, body, headers = image('pic.png')
    check(f'{label}: security headers (nosniff, sandbox CSP, no-store, inline with the file name)',
          headers.get('X-Content-Type-Options') == 'nosniff'
          and headers.get('Content-Security-Policy') == "default-src 'none'; sandbox"
          and headers.get('Cache-Control') == 'no-store'
          and headers.get('Content-Disposition') == 'inline; filename="pic.png"; filename*=UTF-8\'\'pic.png', dict(headers))
    status, body, _ = image('fake.png')
    check(f'{label}: a .png that is text is refused (415)', status == 415 and 'not a PNG, JPEG, GIF or WebP' in message(body), (status, body))
    for name in ('logo.svg', 'svg-named.png'):
        status, body, _ = image(name)
        check(f'{label}: SVG refused ({name}, 415)', status == 415 and 'SVG' in message(body), (status, body))
    status, body, _ = image('README.md')
    check(f'{label}: Markdown is not an image (415)', status == 415, (status, body))
    status, body, _ = image('big.png')
    check(f'{label}: above MOTUZ_VIEW_IMAGE_MAX_BYTES refused (413)', status == 413
          and 'images up to 2.0 MiB' in message(body) and '3.0 MiB' in message(body), (status, body))
    status, body, _ = image(None, path=folder)
    check(f'{label}: a folder is refused (400)', status == 400 and 'folder' in message(body), (status, body))
    status, body, _ = image('missing.png')
    check(f'{label}: a missing file is refused', status in (400, 404) and 'does not exist' in message(body), (status, body))
    status, body, _ = image('pic.png', tok=other_token)
    check(f'{label}: another user gets {other_status}', status == other_status and not isinstance(body, bytes), (status, body))
    status, body, _ = image('pic.png', tok=None)
    check(f'{label}: needs a token (401)', status == 401, status)


def check_document_view(token, other_token, folder, connection_id, label, other_status):
    """
    POST /api/system/files/view/document/ on the document fixtures in `folder`: whole
    files with their container type and the security headers, PDF ranges (also of a PDF
    above the whole-file cap), everything else refused. `other_token` (another user)
    gets `other_status`.
    """
    files = document_fixtures()

    def document(name, tok=token, path=None, **params):
        return api_request('POST', '/api/system/files/view/document/', tok,
                           dict(connection_id=connection_id, path=path or f'{folder}/{name}', **params))

    def message(body):
        return body.get('message', '') if isinstance(body, dict) else str(body)[:300]

    for name, container, mime in (('letter.docx', 'zip', 'application/zip'), ('numbers.xlsx', 'zip', 'application/zip'),
                                  ('slides.pptx', 'zip', 'application/zip'), ('legacy.doc', 'cfb', 'application/x-cfb'),
                                  ('report.pdf', 'pdf', 'application/pdf')):
        status, body, headers = document(name)
        check(f'{label}: {name} read whole as {container}', status == 200 and body == files[name]
              and headers.get('Content-Type') == mime and headers.get('X-Motuz-Document-Type') == container
              and headers.get('X-Motuz-File-Size') == str(len(files[name])) and headers.get('X-Motuz-Range-Start') == '0',
              (status, dict(headers), len(body) if isinstance(body, bytes) else body))
    status, body, headers = document('letter.docx')
    check(f'{label}: security headers (nosniff, sandbox CSP, no-store, attachment)',
          headers.get('X-Content-Type-Options') == 'nosniff'
          and headers.get('Content-Security-Policy') == "default-src 'none'; sandbox"
          and headers.get('Cache-Control') == 'no-store'
          and headers.get('Content-Disposition', '').startswith('attachment; filename="letter.docx"'), dict(headers))

    pdf = files['report.pdf']
    status, body, headers = document('report.pdf', offset=0, length=100)
    check(f'{label}: PDF range from the start', status == 200 and body == pdf[:100]
          and headers.get('X-Motuz-File-Size') == str(len(pdf)) and headers.get('X-Motuz-Range-Start') == '0', (status, headers))
    status, body, headers = document('report.pdf', offset=1500, length=64)
    check(f'{label}: PDF range further in (type checked on the first bytes)', status == 200 and body == pdf[1500:1564]
          and headers.get('X-Motuz-Range-Start') == '1500', (status, body if not isinstance(body, bytes) else len(body)))
    big = files['big.pdf']
    status, body, _ = document('big.pdf')
    check(f'{label}: a PDF above MOTUZ_VIEW_DOCUMENT_MAX_BYTES is not read whole (413)', status == 413
          and 'documents up to 4.0 MiB' in message(body), (status, body))
    status, body, headers = document('big.pdf', offset=len(big) - 5000, length=DOCUMENT_RANGE_MAX)
    check(f'{label}: ... but in ranges; the last range ends at the end of the file', status == 200 and body == big[-5000:]
          and headers.get('X-Motuz-File-Size') == str(len(big)), (status, len(body) if isinstance(body, bytes) else body))
    status, body, _ = document('report.pdf', offset=len(pdf), length=10)
    check(f'{label}: a range beyond the end is refused (400)', status == 400 and 'beyond the end' in message(body), (status, body))
    for params in ({'offset': 0}, {'length': 10}, {'offset': 0, 'length': DOCUMENT_RANGE_MAX + 1}, {'offset': -1, 'length': 1},
                   {'offset': 0, 'length': 0}):
        status, body, _ = document('report.pdf', **params)
        check(f'{label}: invalid range {params} refused (400)', status == 400, (status, body))
    status, body, _ = document('letter.docx', offset=10, length=10)
    check(f'{label}: only PDFs are read in ranges (415)', status == 415 and 'only PDFs' in message(body), (status, body))
    status, body, _ = document('notreally.docx')
    check(f'{label}: a .docx that is text is refused (415)', status == 415 and 'not a PDF, Office' in message(body), (status, body))
    status, body, _ = document('huge.xlsx')
    check(f'{label}: above MOTUZ_VIEW_DOCUMENT_MAX_BYTES refused (413)', status == 413
          and 'documents up to 4.0 MiB' in message(body) and '5.0 MiB' in message(body), (status, body))
    status, body, _ = document(None, path=folder)
    check(f'{label}: a folder is refused (400)', status == 400 and 'folder' in message(body), (status, body))
    status, body, _ = document('missing.pdf', offset=0, length=10)
    check(f'{label}: a missing file is refused', status in (400, 404) and 'does not exist' in message(body), (status, body))
    status, body, _ = document('report.pdf', tok=other_token, offset=0, length=10)
    check(f'{label}: another user gets {other_status}', status == other_status and not isinstance(body, bytes), (status, body))
    status, body, _ = document('report.pdf', tok=None)
    check(f'{label}: needs a token (401)', status == 401, status)


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
