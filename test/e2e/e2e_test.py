"""End-to-end checks against the e2e docker stack through Traefik (https://localhost):
login, local filesystem as the user, connection ownership and secrets, copy and
integrity-check jobs, stopping jobs, refresh and logout. Needs a fresh database."""
import json
import time
import urllib.error
import urllib.request

from common import BASE, CTX, check, finish, psql, sh


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
status, body, _ = req('POST', '/api/system/files/', A, {'path': '/home/bob', 'connection_id': 0})
check('alice cannot list bob home', status == 403, (status, body))
status, body, _ = req('POST', '/api/system/files/', A, {'path': '-la', 'connection_id': 0})
check('path "-la" not treated as option', status == 403, (status, body))
status, body, _ = req('POST', '/api/system/files/', A, {'path': '/home/alice/src/f1.txt/x', 'connection_id': 0})
check('bad path is an error, not 500', status in (400, 403), (status, body))

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
