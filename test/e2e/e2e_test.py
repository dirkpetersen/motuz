"""End-to-end checks against the e2e docker stack through Traefik (https://localhost):
login, local filesystem as the user, connection ownership and secrets, copy and
integrity-check jobs, stopping jobs, refresh and logout. Needs a fresh database."""
import datetime
import json
import re
import time
import urllib.error
import urllib.request

from common import (BASE, CTX, NUMBERED_LOG_CODE, VIEWER_FIXTURES_CODE, check, check_chunked_reads, check_image_view, finish,
                    numbered_log, psql, service_logs, sh)


def req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and 'json' in resp.headers.get('Content-Type', '') else raw), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except Exception:
            return e.code, raw, e.headers


def wait_job(kind, job_id, token, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, job, _ = req('GET', f'/api/{kind}/{job_id}', token)
        if status == 200 and job['progress_state'] != 'PROGRESS':
            return job
        time.sleep(1)
    return job


# --- basics
status, info, _ = req('GET', '/api/system/info/')
check('info healthy', status == 200 and info['status'] == 'healthy', info)
status, body, headers = req('GET', '/')
check('frontend index served', status == 200 and b'<script' in body, status)
check('security headers on index', headers.get('X-Frame-Options') == 'DENY' and 'max-age' in (headers.get('Strict-Transport-Security') or ''), dict(headers))
status, body, _ = req('GET', '/api/')
check('swagger ui', status == 200, status)
status, spec, _ = req('GET', '/api/swagger.json')
check('swagger spec', status == 200 and '/copy-jobs/' in spec['paths'], status)

# --- auth
status, body, _ = req('POST', '/api/auth/login/', body={'username': 'alice', 'password': 'wrong'})
check('bad password rejected', status == 401, (status, body))
status, a, _ = req('POST', '/api/auth/login/', body={'username': 'alice', 'password': 'AlicePass1'})
check('alice login', status == 200 and 'access' in a, (status, a))
status, b, _ = req('POST', '/api/auth/login/', body={'username': 'bob', 'password': 'BobPass1'})
check('bob login', status == 200, (status, b))
A, B = a['access'], b['access']
payload = json.loads(__import__('base64').urlsafe_b64decode(A.split('.')[1] + '=='))
check('token keeps identity claim', payload.get('identity') == 'alice', payload)
status, body, _ = req('GET', '/api/connections/')
check('no token -> 401', status == 401, status)
status, uid, _ = req('GET', '/api/system/uid/', A)
check('uid is the logged in user', status == 200 and uid['uid'] == 1501, uid)

# --- local filesystem as the user
status, home, _ = req('POST', '/api/system/files/home/', A, {})
check('ls home', status == 200 and home['path'] == '/home/alice', (status, home))
status, body, _ = req('POST', '/api/system/files/mkdir/', A, {'path': '/home/alice/src/sub', 'connection_id': 0})
check('mkdir', status == 200, (status, body))
sh('app', "sudo -u alice sh -c 'for i in 1 2 3; do echo hello$i > /home/alice/src/f$i.txt; done; echo deep > /home/alice/src/sub/d.txt'")
status, ls, _ = req('POST', '/api/system/files/', A, {'path': '/home/alice/src', 'connection_id': 0})
names = sorted(f['name'] for f in ls.get('files', [])) if status == 200 else ls
check('ls dir', names == ['f1.txt', 'f2.txt', 'f3.txt', 'sub'], names)
# Modification times: ISO 8601 UTC ('Z'), from epoch seconds, whatever the server's TZ
ISO_UTC = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
by_name = {f['name']: f for f in ls.get('files', [])} if status == 200 else {}
check('ls: every entry has a UTC modification time', by_name and all(ISO_UTC.match(f.get('modified') or '') for f in by_name.values()), by_name)
f1 = by_name.get('f1.txt', {})
check('ls: numeric size and a recent modification time', f1.get('size') == 7 and f1.get('type') == 'file'
      and abs(datetime.datetime.strptime(f1['modified'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp() - time.time()) < 600, f1)
sh('app', "sudo -u alice touch -d @1700000000 /home/alice/src/f3.txt; sudo -u alice ln -s /nonexistent /home/alice/src/broken")
status, ls, _ = req('POST', '/api/system/files/', A, {'path': '/home/alice/src', 'connection_id': 0})
by_name = {f['name']: f for f in ls.get('files', [])} if status == 200 else {}
check('ls: exact modification time in UTC', by_name.get('f3.txt', {}).get('modified') == '2023-11-14T22:13:20Z', by_name.get('f3.txt'))
check('ls: broken symlink listed without size and time', by_name.get('broken') == {'name': 'broken', 'type': 'symlink', 'size': None, 'modified': None}, by_name.get('broken'))
sh('app', "rm -f /home/alice/src/broken")
status, body, _ = req('POST', '/api/system/files/', A, {'path': '/home/bob', 'connection_id': 0})
check('alice cannot list bob home', status == 403, (status, body))
status, body, _ = req('POST', '/api/system/files/', A, {'path': '-la', 'connection_id': 0})
check('path "-la" not treated as option', status == 403, (status, body))
status, body, _ = req('POST', '/api/system/files/', A, {'path': '/home/alice/src/f1.txt/x', 'connection_id': 0})
check('bad path is an error, not 500', status in (400, 403), (status, body))

# --- read-only viewer: read as the user, text only, first 1 MiB
VIEW_FIXTURES = r"""
import os
os.chdir('/home/alice')
open('view.txt', 'w').write('hello viewer\nline 2 ü <b>not html</b>\n')
open('view.bin', 'wb').write(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + bytes(range(256)))
open('big.txt', 'w').write(('0123456789abcdef' * 4 + '\n') * 40000)
os.symlink('/etc/shadow', 'shadow-link')
"""
sh('app', 'sudo -u alice python3 -', stdin=VIEW_FIXTURES)
sh('app', "sudo -u bob sh -c 'echo only bob > /tmp/bob-only.txt; chmod 600 /tmp/bob-only.txt; echo bob > /home/bob/bob.txt'")


def msg(body):
    return body.get('message', '') if isinstance(body, dict) else str(body)


def view(token, path, connection_id=0):
    return req('POST', '/api/system/files/view/', token, {'path': path, 'connection_id': connection_id})


status, v, _ = view(A, '/home/alice/view.txt')
check('view text file', status == 200 and v == {'path': '/home/alice/view.txt', 'content': 'hello viewer\nline 2 ü <b>not html</b>\n',
                                                 'truncated': False, 'size': 39, 'encoding': 'utf-8'}, (status, v))
status, v, _ = view(A, '/home/alice/view.bin')
check('view binary file: 415 not a text file', status == 415 and 'not a text file' in msg(v), (status, v))
status, v, _ = view(A, '/home/alice/big.txt')
check('view large file: first 1 MiB, truncated', status == 200 and v['truncated'] is True and len(v['content']) == 1024 * 1024
      and v['size'] == 65 * 40000 and v['content'].startswith('0123456789abcdef'), (status, {k: v[k] for k in v if k != 'content'} if status == 200 else v))
status, v, _ = view(A, '/tmp/bob-only.txt')
check('alice cannot view a file only bob can read', status == 403 and 'only bob' not in json.dumps(v), (status, v))
status, v, _ = view(A, '/home/bob/bob.txt')
check('alice cannot view a file in bob\'s home', status == 403, (status, v))
status, v, _ = view(B, '/tmp/bob-only.txt')
check('bob can view his own file', status == 200 and v['content'] == 'only bob\n', (status, v))
status, v, _ = view(A, '/home/alice/shadow-link')
check('symlink to /etc/shadow fails (read as the user)', status == 403 and 'root:' not in json.dumps(v), (status, v))
status, v, _ = view(A, '/etc/shadow')
check('/etc/shadow fails', status == 403 and 'root:' not in json.dumps(v), (status, v))
status, v, _ = view(A, '/home/alice/src')
check('view a folder: 400', status == 400 and 'folder' in msg(v), (status, v))
status, v, _ = view(A, '/home/alice/missing.txt')
check('view a missing file: 404', status == 404, (status, v))
for bad in ('view.txt', '-la', '--help', ''):
    status, v, _ = view(A, bad)
    check(f'view relative path {bad!r} refused', status == 400 and 'absolute' in msg(v), (status, v))
status, v, _ = view(A, '/dev/zero')
check('view a device: refused, not read', status == 400 and 'regular file' in msg(v), (status, v))
status, v, _ = view(None, '/home/alice/view.txt')
check('view needs a token', status == 401, status)
check('file contents never logged', 'hello viewer' not in service_logs('app'), 'contents in the app log')

# --- the viewer's pager: chunks of whole lines, forward, from the end and backward
sh('app', 'sudo -u alice python3 - /home/alice/numbered.log', stdin=NUMBERED_LOG_CODE)
NUMBERED = numbered_log()
check_chunked_reads(req, A, '/home/alice/numbered.log', 0, NUMBERED, 'local pager')


def chunk(token, path, connection_id=0, **params):
    return req('POST', '/api/system/files/view/chunk/', token, dict(path=path, connection_id=connection_id, **params))


status, v, _ = chunk(A, '/home/alice/view.txt', from_end=True)
check('pager: small file from the end is the whole file', status == 200 and v == {
    'path': '/home/alice/view.txt', 'content': 'hello viewer\nline 2 ü <b>not html</b>\n', 'offset': 0, 'end': 39,
    'size': 39, 'bof': True, 'eof': True, 'encoding': 'utf-8'}, (status, v))
status, v, _ = chunk(A, '/home/alice/numbered.log', offset=1000, length=1000)
check('pager: a shorter length, from an offset', status == 200 and v['offset'] == 1000 and v['end'] <= 2000
      and v['content'].endswith('\n') and v['content'].encode() == NUMBERED[1000:v['end']], (status, v))
status, v, _ = chunk(A, '/home/alice/view.bin', from_end=True)
check('pager: binary file refused also from the end (415)', status == 415 and 'not a text file' in msg(v), (status, v))
status, v, _ = chunk(A, '/home/alice/big.txt', before=65 * 40000)
check('pager: backward read of the previous chunk', status == 200 and v['end'] == 65 * 40000 and v['eof'] and not v['bof'], (status, v))
status, v, _ = chunk(B, '/home/alice/numbered.log', from_end=True)
check('pager: bob cannot read alice\'s file (403)', status == 403 and 'line 1' not in json.dumps(v), (status, v))
status, v, _ = chunk(A, '/tmp/bob-only.txt')
check('pager: alice cannot read bob\'s file (403)', status == 403 and 'only bob' not in json.dumps(v), (status, v))
status, v, _ = chunk(A, '/home/alice/shadow-link', from_end=True)
check('pager: symlink to /etc/shadow fails (read as the user)', status == 403 and 'root:' not in json.dumps(v), (status, v))
for name, params in (('negative offset', {'offset': -1}), ('offset as string', {'offset': '10'}),
                     ('offset as bool', {'offset': True}), ('fractional offset', {'offset': 1.5}),
                     ('length 0', {'length': 0}), ('length over 256 KiB', {'length': 256 * 1024 + 1}),
                     ('negative before', {'before': -5}), ('offset and from_end', {'offset': 5, 'from_end': True}),
                     ('offset and before', {'offset': 5, 'before': 50}), ('from_end not a bool', {'from_end': 'yes'})):
    status, v, _ = chunk(A, '/home/alice/view.txt', **params)
    check(f'pager: {name} refused (400)', status == 400, (status, v))
status, v, _ = chunk(A, '/home/alice/view.txt', offset=40)
check('pager: offset beyond the end refused (400)', status == 400 and 'beyond the end' in msg(v), (status, v))
status, v, _ = chunk(A, '/home/alice/view.txt', offset=39)
check('pager: offset at the end: empty, eof', status == 200 and v['content'] == '' and v['eof'] and v['offset'] == 39, (status, v))
for bad in ('numbered.log', '-la'):
    status, v, _ = chunk(A, bad, from_end=True)
    check(f'pager: relative path {bad!r} refused', status == 400 and 'absolute' in msg(v), (status, v))
status, v, _ = chunk(A, '/dev/zero', from_end=True)
check('pager: a device is refused, not read', status == 400 and 'regular file' in msg(v), (status, v))
status, v, _ = chunk(None, '/home/alice/numbered.log')
check('pager: needs a token', status == 401, status)

# --- follow mode (tail -f): polls from the end of what the viewer has, with follow=true
FOLLOW = '/home/alice/follow.log'


def write_follow(content, append=True):
    sh('app', f"sudo -u alice sh -c 'cat {'>>' if append else '>'} {FOLLOW}'", stdin=content)


def follow(offset, **params):
    return chunk(A, FOLLOW, offset=offset, follow=True, **params)


def fields(v):
    return {k: v.get(k) for k in ('content', 'offset', 'end', 'size', 'eof')} if isinstance(v, dict) else v


write_follow('one\ntwo\n', append=False)
status, v, _ = follow(8)
check('follow: a read at offset=size is empty, eof, same offset', status == 200 and fields(v) == {
    'content': '', 'offset': 8, 'end': 8, 'size': 8, 'eof': True}, (status, v))
status, v, _ = chunk(A, FOLLOW, offset=8)
check('follow: also without follow, offset=size is empty with eof', status == 200 and v['content'] == '' and v['eof'], (status, v))
status, v, _ = chunk(A, '/home/alice/view.bin', offset=272)
check('follow: at the end nothing is read (no text check: a binary file\'s end is empty, not 415)',
      status == 200 and v['content'] == '' and v['eof'] and v['size'] == 272, (status, v))
write_follow('three\nfour\n')
status, v, _ = follow(8)
check('follow: after appending, the next read returns exactly the new lines', status == 200 and fields(v) == {
    'content': 'three\nfour\n', 'offset': 8, 'end': 19, 'size': 19, 'eof': True}, (status, v))
write_follow('partial')
status, v, _ = follow(19)
check('follow: an incomplete line (no newline yet) is withheld', status == 200 and fields(v) == {
    'content': '', 'offset': 19, 'end': 19, 'size': 26, 'eof': True}, (status, v))
write_follow(' done\nfive\n')
status, v, _ = follow(19)
check('follow: the completed line arrives whole', status == 200 and fields(v) == {
    'content': 'partial done\nfive\n', 'offset': 19, 'end': 37, 'size': 37, 'eof': True}, (status, v))
write_follow('new\n', append=False)
status, v, _ = follow(37)
check('follow: truncated file: no content, the new size (the viewer reloads the tail)', status == 200 and fields(v) == {
    'content': '', 'offset': 4, 'end': 4, 'size': 4, 'eof': True}, (status, v))
status, v, _ = chunk(A, FOLLOW, offset=37)
check('follow: without follow an offset past the end is still 400', status == 400 and 'beyond the end' in msg(v), (status, v))
for name, params in (('with from_end', {'from_end': True}), ('with before', {'before': 4}), ('not a bool', {'offset': 0, 'follow': 'yes'})):
    status, v, _ = chunk(A, FOLLOW, follow=params.pop('follow', True), **params)
    check(f'follow: {name} refused (400)', status == 400, (status, v))
status, v, _ = chunk(B, FOLLOW, offset=0, follow=True)
check('follow: bob cannot follow alice\'s file (403)', status == 403 and 'new' not in json.dumps(v), (status, v))
sh('app', f'rm -f {FOLLOW}')
status, v, _ = follow(4)
check('follow: a deleted file is 404', status == 404, (status, v))
logs = service_logs('app')
check('pager: file contents never logged', 'of a numbered log' not in logs and 'line 000001' not in logs, 'contents in the app log')

# --- image viewer: PNG/JPEG/GIF/WebP by their first bytes, read as the user, size cap
# (compose.yml: MOTUZ_VIEW_IMAGE_MAX_BYTES=2M); fixtures from viewer_fixtures.py
sh('app', 'sudo -u alice python3 - /home/alice/viewer', stdin=VIEWER_FIXTURES_CODE)
check_image_view(A, B, '/home/alice/viewer', 0, 'image viewer', 403)


def view_image(token, path, connection_id=0):
    return req('POST', '/api/system/files/view/image/', token, {'path': path, 'connection_id': connection_id})


for bad in ('viewer/pic.png', '-la', '--help'):
    status, v, _ = view_image(A, bad)
    check(f'image viewer: relative path {bad!r} refused', status == 400 and 'absolute' in msg(v), (status, v))
status, v, _ = view_image(A, '/dev/zero')
check('image viewer: a device is refused, not read', status == 400 and 'regular file' in msg(v), (status, v))
status, v, _ = view_image(A, '/home/alice/shadow-link')
check('image viewer: a link to /etc/shadow is read as alice (403)', status == 403, (status, v))
status, v, _ = view_image(A, '/home/alice/viewer/pic.png', 'x')
check('image viewer: connection_id must be an integer', status == 400, (status, v))
status, v, _ = chunk(A, '/home/alice/viewer/README.md')
check('Markdown viewer: the text comes from the pager\'s endpoint', status == 200 and v['content'].startswith('# Viewer test heading\n')
      and v['eof'], (status, v))
check('image viewer: image bytes never logged', 'IHDR' not in service_logs('app'), 'image bytes in the app log')

# --- connections, ownership and secrets
conn = {'name': 'alice-s3', 'type': 's3', 'bucket': 'b', 's3_access_key_id': 'AKIAEXAMPLE',
        's3_secret_access_key': 'topsecret', 's3_region': 'us-west-2', 'owner': 'bob', 'id': 999}
status, c, _ = req('POST', '/api/connections/', A, conn)
check('create connection', status == 201 and c['id'] != 999, (status, c))
cid = c['id']
check('owner not settable on create', psql(f"select owner from cloud_connection where id={cid}") == 'alice')
check('secret not returned', c.get('s3_secret_access_key') is None, c)
status, lst, _ = req('GET', '/api/connections/', B)
check('bob does not see alice connection', status == 200 and all(x['id'] != cid for x in lst), lst)
status, body, _ = req('PATCH', f'/api/connections/{cid}', B, {'name': 'pwned', 'type': 's3'})
check('bob cannot patch alice connection', status == 404, (status, body))
status, body, _ = req('PATCH', f'/api/connections/{cid}', A, {'name': 'renamed', 'type': 's3', 'owner': 'bob', 's3_secret_access_key': ''})
check('patch ok', status == 200 and body['name'] == 'renamed', (status, body))
check('owner not settable on patch', psql(f"select owner from cloud_connection where id={cid}") == 'alice')
check('empty secret on patch keeps stored secret', psql(f"select s3_secret_access_key from cloud_connection where id={cid}") == 'topsecret')
status, body, _ = req('GET', '/api/connections/abc', A)
check('non-int id -> 404', status == 404, status)

# --- jobs must not use someone else's connection
job = {'description': 'x', 'src_cloud_id': cid, 'src_resource_path': '/b', 'dst_resource_path': '/home/bob/x', 'copy_links': True}
status, body, _ = req('POST', '/api/copy-jobs/', B, job)
check('bob cannot copy with alice connection', status == 404, (status, body))
status, body, _ = req('POST', '/api/hashsum-jobs/', B, {'src_cloud_id': cid, 'src_resource_path': '/b', 'dst_resource_path': '/home/bob', 'option_download': False})
check('bob cannot hashsum with alice connection', status == 404, (status, body))
status, body, _ = view(B, '/b/x.txt', cid)
check('bob cannot view a file with alice connection', status == 404, (status, body))
status, body, _ = chunk(B, '/b/x.txt', cid, from_end=True)
check('bob cannot page a file with alice connection', status == 404, (status, body))
status, body, _ = view_image(B, '/b/x.png', cid)
check('bob cannot view an image with alice connection', status == 404, (status, body))
status, body, _ = req('POST', '/api/system/files/', B, {'path': '/b', 'connection_id': cid})
check('bob cannot list with alice connection', status == 404, (status, body))

# --- local copy job
job = {'description': 'local copy', 'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst', 'copy_links': True}
status, cj, _ = req('POST', '/api/copy-jobs/', A, job)
check('create copy job', status in (200, 201), (status, cj))
cj = wait_job('copy-jobs', cj['id'], A)
check('copy job SUCCESS', cj['progress_state'] == 'SUCCESS' and cj['progress_current'] == 100, cj)
check('copy progress text returned', 'Transferred' in (cj.get('progress_text') or ''), cj.get('progress_text'))
owner = sh('app', "stat -c %U /home/alice/dst/f1.txt /home/alice/dst/sub/d.txt").stdout.split()
check('copied files owned by alice', owner == ['alice', 'alice'], owner)

job = {'description': 'forbidden', 'src_resource_path': '/home/bob', 'dst_resource_path': '/home/alice/stolen', 'copy_links': True}
status, cj2, _ = req('POST', '/api/copy-jobs/', A, job)
cj2 = wait_job('copy-jobs', cj2['id'], A)
check('copy of unreadable dir FAILED', cj2['progress_state'] == 'FAILED', cj2)
check('copy error text returned', bool(cj2.get('progress_error_text')), cj2)

job = {'description': 'relative', 'src_resource_path': '--config=/etc/shadow', 'dst_resource_path': '/home/alice/y', 'copy_links': True}
status, cj3, _ = req('POST', '/api/copy-jobs/', A, job)
cj3 = wait_job('copy-jobs', cj3['id'], A)
check('option-like local path rejected', cj3['progress_state'] == 'FAILED' and 'absolute' in (cj3.get('progress_error_text') or ''), cj3)

status, page, _ = req('GET', '/api/copy-jobs/?page=1&page_size=2', A)
check('copy job pagination', status == 200 and len(page['data']) == 2 and page['total'] == 3, page)
status, page, _ = req('GET', '/api/copy-jobs/', B)
check('bob sees no alice jobs', status == 200 and page['total'] == 0, page)

# --- rclone performance settings (utils/rclone_tuning.py). compose.yml sets
# MOTUZ_RCLONE_CHECKERS=16 and MOTUZ_RCLONE_MAX_TRANSFERS=48
status, perf, _ = req('GET', '/api/copy-jobs/performance/?dst_cloud_id=0', A)
fields = {f['name']: f for f in perf.get('fields', [])} if status == 200 else {}
check('performance settings of a local destination', status == 200
      and list(fields) == ['transfers', 'checkers', 'multi_thread_streams', 'multi_thread_cutoff']
      and fields['transfers']['max'] == 48 and fields['checkers']['default'] == 16 and perf['memory_budget'] == 8 * 2**30,
      (status, perf))
presets = {p['id']: p for p in perf.get('presets', [])} if status == 200 else {}
check('presets', list(presets) == ['default', 'small_files', 'large_files', 'maximum']
      and presets['small_files']['values'] == {'transfers': 32, 'checkers': 64}
      and presets['maximum']['values']['transfers'] <= 48, presets)
status, body, _ = req('GET', f'/api/copy-jobs/performance/?dst_cloud_id={cid}', B)
check('bob cannot read performance settings for alice connection', status == 404, (status, body))

job = {'description': 'many small files', 'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst-perf',
       'copy_links': True, 'performance': presets.get('small_files', {}).get('values')}
status, pj, _ = req('POST', '/api/copy-jobs/', A, job)
check('create copy job with the "Many small files" preset', status == 201 and pj['performance'] == {'transfers': 32, 'checkers': 64},
      (status, pj))
pj = wait_job('copy-jobs', pj['id'], A)
check('copy job with preset SUCCESS', pj['progress_state'] == 'SUCCESS', pj)
check('job detail returns the settings', pj.get('performance') == {'transfers': 32, 'checkers': 64}, pj.get('performance'))
check('settings stored on the job', json.loads(psql(f"select performance from copy_job where id={pj['id']}") or 'null')
      == {'transfers': 32, 'checkers': 64})
celery_log = service_logs('celery')
check('rclone got the preset flags (celery log)', '--transfers=32 --checkers=64 copyto /home/alice/src /home/alice/dst-perf' in celery_log,
      [line for line in celery_log.splitlines() if 'dst-perf' in line][-1:])
check('a job without settings: the installation default only (celery log)',
      '--contimeout=5m --checkers=16 copyto /home/alice/src /home/alice/dst ' in celery_log)

jobs_before = psql("select count(*) from copy_job")
for name, performance, expected in [
        ('flag injection in a value', {'transfers': '4 --config=/etc/shadow'}, 'whole number'),
        ('newline in a value', {'transfers': '4\n--rc'}, 'whole number'),
        ('an option as a value', {'multi_thread_cutoff': '--foo'}, 'size like 64M'),
        ('above the cap', {'transfers': 49}, 'at most 48'),
        ('installation-only setting', {'buffer_size': '1G'}, 'Unknown performance setting'),
        ('unknown setting', {'--config': '/etc/shadow'}, 'Unknown performance setting'),
        ('above the memory budget', {'transfers': 48, 'multi_thread_streams': 32}, 'memory'),
        ('not an object', '--transfers=64', '')]:
    status, body, _ = req('POST', '/api/copy-jobs/', A, dict(job, performance=performance))
    check(f'invalid performance rejected: {name}', status == 400 and expected in json.dumps(body), (status, body))
check('no job created for invalid settings', psql("select count(*) from copy_job") == jobs_before)

# --- integrity check
status, hj, _ = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst', 'option_download': False})
hj = wait_job('hashsum-jobs', hj['id'], A)
check('hashsum identical SUCCESS', hj['progress_state'] == 'SUCCESS' and json.loads(hj['progress_src_tree']) == [] and json.loads(hj['progress_dst_tree']) == [], hj)

sh('app', "sudo -u alice sh -c 'echo changed > /home/alice/dst/f2.txt; echo extra1 > /home/alice/dst/zz1.txt; echo extra2 > /home/alice/dst/zz2.txt'")
status, hj, _ = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst', 'option_download': False})
hj = wait_job('hashsum-jobs', hj['id'], A, timeout=60)
dst_tree = json.loads(hj.get('progress_dst_tree') or '[]')
dst_names = sorted(n['title'] for n in dst_tree)
check('hashsum with extra dst files finishes (no infinite loop)', hj['progress_state'] == 'SUCCESS', hj['progress_state'])
check('hashsum reports differences', dst_names == ['f2.txt', 'zz1.txt', 'zz2.txt'], dst_tree)
check('hashsum: rclone md5sum got the installation --checkers (celery log)',
      '--checkers=16 md5sum /home/alice/src' in service_logs('celery'))

status, hj, _ = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst-perf',
                                                      'option_download': False, 'performance': {'checkers': 24}})
check('hashsum with checkers accepted', status == 201 and hj.get('performance') == {'checkers': 24}, (status, hj))
hj = wait_job('hashsum-jobs', hj['id'], A)
check('hashsum with checkers SUCCESS and identical', hj['progress_state'] == 'SUCCESS' and json.loads(hj['progress_dst_tree']) == [], hj)
check("hashsum: rclone md5sum got the job's --checkers (celery log)", '--checkers=24 md5sum /home/alice/dst-perf' in service_logs('celery'))
for performance in ({'transfers': 4}, {'checkers': '8 --rc'}, {'checkers': 1000}):
    status, body, _ = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst',
                                                            'option_download': False, 'performance': performance})
    check(f'hashsum rejects performance {performance}', status == 400, (status, body))

# --- stop kills rclone
sh('app', "sudo -u alice python3 -c \"import os; os.makedirs('/home/alice/many', exist_ok=True); [open(f'/home/alice/many/{i}', 'w').write('x' * 4096) for i in range(60000)]\"")
job = {'description': 'long', 'src_resource_path': '/home/alice/many', 'dst_resource_path': '/home/alice/many_copy', 'copy_links': True}
status, lj, _ = req('POST', '/api/copy-jobs/', A, job)
time.sleep(4)
RCLONE_PIDS = "grep -lx rclone /proc/[0-9]*/comm 2>/dev/null"
running = sh('celery', RCLONE_PIDS).stdout.strip()
status, mid, _ = req('GET', f"/api/copy-jobs/{lj['id']}", A)
check('progress percent reported while running', 0 < mid['progress_current'] < 100, mid['progress_current'])
check('long copy is running', bool(running), running)
status, stopped, _ = req('PUT', f"/api/copy-jobs/{lj['id']}/stop/", A)
check('stop returns STOPPED', status == 202 and stopped['progress_state'] == 'STOPPED', (status, stopped))
time.sleep(4)
left = sh('celery', RCLONE_PIDS).stdout.strip()
check('rclone killed after stop', left == '', left)
status, after, _ = req('GET', f"/api/copy-jobs/{lj['id']}", A)
check('job stays STOPPED', after['progress_state'] == 'STOPPED', after['progress_state'])
status, hc, _ = req('POST', '/api/copy-jobs/', A, {'description': 'after stop', 'src_resource_path': '/home/alice/src', 'dst_resource_path': '/home/alice/dst2', 'copy_links': True})
hc = wait_job('copy-jobs', hc['id'], A)
check('worker still processes jobs after stop', hc['progress_state'] == 'SUCCESS', hc)

# --- refresh and logout
status, r, _ = req('POST', '/api/auth/refresh/', a['refresh'])
check('refresh', status == 200 and 'access' in r, (status, r))
status, body, _ = req('POST', '/api/auth/refresh/', A)
check('access token cannot refresh', status == 401, (status, body))
status, body, _ = req('POST', '/api/auth/logout/', r['refresh'])
check('logout', status == 200 and body['status'] == 'success', (status, body))
status, body, _ = req('POST', '/api/auth/refresh/', r['refresh'])
check('revoked refresh token rejected', status == 401, (status, body))

finish()
