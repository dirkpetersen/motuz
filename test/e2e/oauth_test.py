"""'Sign in with Microsoft' (OneDrive) checks against the e2e stack and fake_ms.py.
PHASE=paste: rclone's public app, the user pastes the redirect address (default).
PHASE=callback: own app registration (MOTUZ_ONEDRIVE_CLIENT_ID=motuz-own-app), Microsoft
redirects to Motuz's callback. run.sh reconfigures the app and the fake for each phase."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from common import BASE, CTX, FAKE_LOG, check, finish, psql, set_prefix

PHASE = os.environ.get('PHASE', 'paste')
LOG = FAKE_LOG
set_prefix(f'[{PHASE}] ')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def api(method, path, token=None, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=60) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def get_no_redirect(url, context=None):
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=CTX))
    try:
        opener.open(url, timeout=30)
        return None, None
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Location')


def calls(path):
    if not os.path.exists(LOG):
        return []
    return [json.loads(l) for l in open(LOG) if json.loads(l)['path'] == path]


open(LOG, 'w').close()
_, a = api('POST', '/api/auth/login/', body={'username': 'alice', 'password': 'AlicePass1'})
_, b = api('POST', '/api/auth/login/', body={'username': 'bob', 'password': 'BobPass1'})
A, B = a['access'], b['access']
AUTH_PREFIX = 'http://127.0.0.1:5999/authorize?'

status, start = api('POST', '/api/oauth/onedrive/start/', A, {})
check('start', status == 200 and start['authorize_url'].startswith(AUTH_PREFIX), (status, start))
q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(start['authorize_url']).query).items()}
expected_client = 'b15665d9-eda6-4092-8539-0eec376afd59' if PHASE == 'paste' else 'motuz-own-app'
check('authorize url: client, PKCE S256, offline_access, state', q['client_id'] == expected_client and q['code_challenge_method'] == 'S256'
      and 'offline_access' in q['scope'] and 'Files.ReadWrite.All' in q['scope'] and q['state'] == start['state'], q)
check('redirect mode', start['redirect_mode'] == ('paste' if PHASE == 'paste' else 'callback'), start)
check('no verifier in authorize url', 'code_verifier' not in start['authorize_url'], start['authorize_url'])

status, unauth = api('POST', '/api/oauth/onedrive/start/', None, {})
check('start requires login', status == 401, status)

# "Microsoft" sign-in: follow the authorize URL, get the redirect with code+state
status, location = get_no_redirect(start['authorize_url'])
check('fake Microsoft redirects with code', status == 302 and 'code=' in location, (status, location))

if PHASE == 'paste':
    check('redirects to rclone localhost URI', location.startswith('http://localhost:53682/?'), location)
    status, body = api('POST', '/api/oauth/onedrive/finish/', B, {'redirect_url': location})
    check("bob cannot finish alice's sign-in", status == 400, (status, body))
    status, body = api('POST', '/api/oauth/onedrive/finish/', A, {'redirect_url': 'http://localhost:53682/?error=access_denied&error_description=User+declined&state=' + start['state']})
    check('declined sign-in reported', status == 400 and 'declined' in json.dumps(body), (status, body))
    status, flow = api('POST', '/api/oauth/onedrive/finish/', A, {'redirect_url': location})
else:
    check('redirects to Motuz callback', location.startswith(BASE + '/api/oauth/onedrive/callback?'), location)
    status, landing = get_no_redirect(location)
    check('callback (no JWT) redirects to /clouds?oauth_state', status == 302 and landing.startswith('/clouds?oauth_state='), (status, landing))
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(landing).query)['oauth_state'][0]
    status, body = api('GET', f'/api/oauth/onedrive/flows/{state}/', B)
    check("bob cannot read alice's flow", status == 404, (status, body))
    status, flow = api('GET', f'/api/oauth/onedrive/flows/{state}/', A)
    status2, landing2 = get_no_redirect(BASE + '/api/oauth/onedrive/callback?code=x&state=unknown')
    check('callback with unknown state', status2 == 302 and 'oauth_error' in landing2, (status2, landing2))

check('drives discovered', status == 200 and [d['id'] for d in flow['drives']] == ['b!mydrive', 'b!labdocs'], (status, flow))
check('drive names and types', flow['drives'][0]['name'] == 'OneDrive (Alice Test)' and flow['drives'][1] == {'id': 'b!labdocs', 'name': 'Lab Share - Documents', 'drive_type': 'documentLibrary'}, flow)
check('no token in response', 'REAL-' not in json.dumps(flow) and 'ACCESS-' not in json.dumps(flow), flow)
exchange = calls('/token')
check('code exchange: grant, client secret, PKCE verified by fake', len(exchange) == 1 and exchange[0]['form']['grant_type'] == 'authorization_code'
      and exchange[0]['form'].get('client_secret') == '<ok>', exchange)

status, body = api('POST', '/api/oauth/onedrive/connect/', B, {'state': flow['state'], 'drive_id': 'b!labdocs'})
check("bob cannot connect alice's flow", status == 400, (status, body))
status, body = api('POST', '/api/oauth/onedrive/connect/', A, {'state': flow['state'], 'drive_id': 'b!nope'})
check('unknown drive rejected', status == 400, (status, body))
status, conn = api('POST', '/api/oauth/onedrive/connect/', A, {'state': flow['state'], 'drive_id': 'b!labdocs', 'name': ''})
check('connect creates connection', status == 201 and conn['type'] == 'onedrive' and conn['name'] == 'Lab Share - Documents'
      and conn['onedrive_drive_id'] == 'b!labdocs' and conn['onedrive_drive_type'] == 'documentLibrary', (status, conn))
check('token not returned', conn.get('onedrive_token') is None, conn)
stored = json.loads(psql(f"select onedrive_token from cloud_connection where id={conn['id']}"))
check('rclone-format token stored', set(stored) == {'access_token', 'token_type', 'refresh_token', 'expiry'} and stored['refresh_token'].startswith('REAL-')
      and stored['expiry'].endswith('Z'), stored)
check('owner is alice', psql(f"select owner from cloud_connection where id={conn['id']}") == 'alice')
check('flow deleted after connect', psql("select count(*) from oauth_flow where state='{}'".format(flow['state'])) == '0')
status, body = api('POST', '/api/oauth/onedrive/connect/', A, {'state': flow['state'], 'drive_id': 'b!labdocs'})
check('flow cannot be reused', status == 400, (status, body))
if PHASE == 'paste':
    status, body = api('POST', '/api/oauth/onedrive/finish/', A, {'redirect_url': location})
    check('pasted address cannot be replayed', status == 400, (status, body))

# Expired flows are rejected
status, s2 = api('POST', '/api/oauth/onedrive/start/', A, {})
psql("update oauth_flow set created_at = now() - interval '1 hour' where state='{}'".format(s2['state']))
status, body = api('POST', '/api/oauth/onedrive/finish/', A, {'redirect_url': 'http://localhost:53682/?code=x&state=' + s2['state']})
check('expired flow rejected', status == 400, (status, body))

# The broker refreshes the new connection's token with the right client
psql("update cloud_connection set onedrive_token='{}' where id={}".format(json.dumps({**stored, 'expiry': '2020-01-01T00:00:00Z'}), conn['id']))
open(LOG, 'w').close()
status, body = api('POST', '/api/system/files/', A, {'path': '/', 'connection_id': conn['id']})
refresh = calls('/token')
check('rclone refresh through broker', len(refresh) == 1 and refresh[0]['form']['grant_type'] == 'refresh_token'
      and refresh[0]['form']['refresh_token'] == stored['refresh_token'], refresh)
check('refresh uses ' + expected_client, refresh and refresh[0]['form']['client_id'] == expected_client
      and refresh[0]['form'].get('client_secret') == '<ok>', refresh)

finish()
