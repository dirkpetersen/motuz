"""OAuth token broker checks (OneDrive) against the e2e stack and fake_ms.py on
127.0.0.1:5999, which must be freshly started (its first valid refresh token is REAL-1)."""
import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from common import BASE, CTX, FAKE_LOG, check, finish, psql, service_logs, set_fake_mode

BROKER = 'http://127.0.0.1:5001/internal/oauth/token'
LOG = FAKE_LOG


def api(method, path, token=None, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=120) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def broker(form, url=BROKER, context=None):
    r = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method='POST')
    try:
        with urllib.request.urlopen(r, timeout=60, context=context) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:80]


def stored_token(cid):
    return json.loads(psql(f"select onedrive_token from cloud_connection where id={cid}"))


def upstream_calls():
    if not os.path.exists(LOG):
        return []
    return [json.loads(line) for line in open(LOG)]


def reset_upstream_log():
    open(LOG, 'w').close()


set_fake_mode('ok')
reset_upstream_log()
status, a = api('POST', '/api/auth/login/', body={'username': 'alice', 'password': 'AlicePass1'})
A = a['access']

pasted = {'access_token': 'STALE', 'token_type': 'Bearer', 'refresh_token': 'REAL-1', 'expiry': '2020-01-01T00:00:00Z'}
status, conn = api('POST', '/api/connections/', A, {
    'name': 'my-onedrive', 'type': 'onedrive', 'onedrive_drive_id': 'b!fake', 'onedrive_drive_type': 'business',
    'onedrive_token': json.dumps(pasted)})
check('create onedrive connection', status == 201, (status, conn))
cid = conn['id']
check('token not returned by API', conn.get('onedrive_token') is None, conn)

# --- rclone (API process) refreshes through the broker
status, body = api('POST', '/api/system/files/', A, {'path': '/', 'connection_id': cid})
calls = upstream_calls()
check('ls fails at fake Graph as expected (no real account)', status == 400, (status, body))
check('rclone refresh went through broker to upstream exactly once', len(calls) == 1, calls)
check('upstream received the REAL refresh token', calls and calls[0]['form'].get('refresh_token') == 'REAL-1', calls)
check('upstream received refresh grant and client id', calls and calls[0]['form'].get('grant_type') == 'refresh_token' and calls[0]['form'].get('client_id'), calls)
check('upstream received rclone client secret intact', calls and calls[0]['form'].get('client_secret') == '<ok>', calls)
tok = stored_token(cid)
check('rotated token stored', tok['refresh_token'] == 'REAL-2' and tok['access_token'] == 'ACCESS-2', tok)
handle = psql(f"select token_broker_handle from cloud_connection where id={cid}")
check('handle assigned and opaque', len(handle) >= 40 and 'REAL' not in handle, handle)
logs = service_logs('app', 'celery')
check('no real or access tokens in logs', 'REAL-' not in logs and 'ACCESS-' not in logs and handle not in logs,
      [l for l in logs.splitlines() if 'REAL-' in l or 'ACCESS-' in l or handle in l][:3])

# --- cached token is served without calling upstream
reset_upstream_log()
status, body = broker({'grant_type': 'refresh_token', 'refresh_token': handle})
check('fresh cached token served', status == 200 and body['access_token'] == 'ACCESS-2' and body['expires_in'] > 3000, (status, body))
check('response never contains a refresh token', 'refresh_token' not in body, body)
check('no upstream call for cached token', upstream_calls() == [], upstream_calls())

# --- unknown handle
status, body = broker({'grant_type': 'refresh_token', 'refresh_token': 'guess'})
check('unknown handle rejected', status == 400 and body.get('error') == 'invalid_grant', (status, body))
status, body = broker({'grant_type': 'refresh_token', 'refresh_token': 'REAL-2'})
check('real refresh token is not accepted as a handle', status == 400, (status, body))
status, body = broker({'grant_type': 'client_credentials'})
check('other grants rejected', status == 400 and body.get('error') == 'unsupported_grant_type', (status, body))
check('no upstream calls for rejected requests', upstream_calls() == [], upstream_calls())

# --- concurrent refreshes collapse into one upstream refresh (single-use refresh tokens stay safe)
tok['expiry'] = '2020-01-01T00:00:00Z'
psql("update cloud_connection set onedrive_token='{}' where id={}".format(json.dumps(tok), cid))
reset_upstream_log()
with concurrent.futures.ThreadPoolExecutor(10) as pool:
    answers = list(pool.map(lambda _: broker({'grant_type': 'refresh_token', 'refresh_token': handle,
                                                'client_id': 'rclone-client'}), range(10)))
check('10 concurrent refreshes all succeed', all(s == 200 for s, _ in answers), answers)
check('all get the same new access token', {b['access_token'] for _, b in answers} == {'ACCESS-3'}, answers)
check('exactly one upstream refresh', len(upstream_calls()) == 1, upstream_calls())
check('refresh token rotated once', stored_token(cid)['refresh_token'] == 'REAL-3', stored_token(cid))

# --- upstream failure is passed through and nothing is stored
tok = stored_token(cid)
tok['expiry'] = '2020-01-01T00:00:00Z'
psql("update cloud_connection set onedrive_token='{}' where id={}".format(json.dumps(tok), cid))
set_fake_mode('fail')
status, body = broker({'grant_type': 'refresh_token', 'refresh_token': handle})
check('upstream invalid_grant passed to rclone', status == 400 and body.get('error') == 'invalid_grant', (status, body))
check('stored token unchanged on failure', stored_token(cid)['refresh_token'] == 'REAL-3', stored_token(cid))
set_fake_mode('ok')

# --- celery worker (copy job) also refreshes through the broker
reset_upstream_log()
status, job = api('POST', '/api/copy-jobs/', A, {'description': 'from onedrive', 'src_cloud_id': cid,
                                                  'src_resource_path': '/Documents', 'dst_resource_path': '/home/alice/od',
                                                  'copy_links': True})
for _ in range(60):
    status, job = api('GET', f"/api/copy-jobs/{job['id']}", A)
    if job['progress_state'] != 'PROGRESS':
        break
    time.sleep(1)
calls = upstream_calls()
check('copy job ends FAILED at fake Graph', job['progress_state'] == 'FAILED', job['progress_state'])
check('celery refresh went through broker once with real token', len(calls) == 1 and calls[0]['form']['refresh_token'] == 'REAL-3', calls)
check('rotated again after job', stored_token(cid)['refresh_token'] == 'REAL-4', stored_token(cid))
ps = service_logs('celery')
check('rclone got --tpslimit and broker URL (celery log)', '--tpslimit 10' in ps and '127.0.0.1:5001/internal/oauth/token' in ps,
      [l for l in ps.splitlines() if 'rclone' in l][-1:])

# --- broker is not reachable through Traefik (router rule) nor on the proxied socket :5000 (app)
reset_upstream_log()
status, body = broker({'grant_type': 'refresh_token', 'refresh_token': handle}, url=BASE + '/internal/oauth/token', context=CTX)
check('broker not exposed via Traefik', status == 404 and not isinstance(body, dict), (status, body))
check('no upstream call via Traefik', upstream_calls() == [], upstream_calls())

# --- edit without re-pasting the token keeps it; verify with id uses stored secret
status, body = api('PATCH', f'/api/connections/{cid}', A, {'name': 'renamed-od', 'type': 'onedrive', 'onedrive_token': None,
                                                           'onedrive_drive_id': 'b!fake', 'onedrive_drive_type': 'business'})
check('patch with null token accepted', status == 200, (status, body))
check('token kept on edit', stored_token(cid)['refresh_token'] == 'REAL-4', stored_token(cid))
status, body = api('POST', '/api/connections/verify/', A, {'id': cid, 'name': 'renamed-od', 'type': 'onedrive',
                                                           'onedrive_token': None, 'onedrive_drive_id': 'b!fake',
                                                           'onedrive_drive_type': 'business'})
check('verify with stored token runs (fails at fake Graph)', status == 200 and body.get('result') is False, (status, body))
status, lst = api('POST', '/api/auth/login/', body={'username': 'bob', 'password': 'BobPass1'})
status, body = api('POST', '/api/connections/verify/', lst['access'], {'id': cid, 'name': 'x', 'type': 'onedrive'})
check("bob cannot verify with alice's stored token", status == 404, (status, body))

finish()
