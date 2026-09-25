"""Fake Microsoft identity platform + Graph for the Motuz e2e tests.
Usage: python3 fake_ms.py <expected_client_secret> [port (5999)]

/authorize: issues a code (remembers PKCE challenge, client, redirect) and 302s to redirect_uri
/token:     authorization_code (checks client, secret, redirect, PKCE) and refresh_token (rotates)
/graph/...: me/drive, me/drives, me/followedSites, sites/<id>/drives (bearer must be a live ACCESS-*)
Every request is appended to common.FAKE_LOG (test/e2e/.work/fake/requests.jsonl, or below
$MOTUZ_E2E_WORK); common.FAKE_MODE (mode.txt next to it) = "fail" makes /token fail.
The first valid refresh token is REAL-1; each refresh issues REAL-<n+1> and ACCESS-<n+1>."""
import base64, hashlib, json, os, secrets, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import FAKE_LOG as LOG, FAKE_MODE as MODE
EXPECTED_SECRET = sys.argv[1] if len(sys.argv) > 1 else ''  # client secret the exchange must carry
lock = threading.Lock()
st = {'n': 1, 'valid_refresh': {'REAL-1'}, 'access': set(), 'codes': {}}

def log(entry):
    with open(LOG, 'a') as f: f.write(json.dumps(entry) + '\n')

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == '/authorize':
            code = 'CODE-' + secrets.token_hex(4)
            with lock:
                st['codes'][code] = q
                log({'path': '/authorize', 'q': q})
            loc = q['redirect_uri'] + ('&' if '?' in q['redirect_uri'] else '?') + urllib.parse.urlencode({'code': code, 'state': q['state']})
            self.send_response(302); self.send_header('Location', loc); self.end_headers(); return
        if url.path.startswith('/graph/'):
            auth = self.headers.get('Authorization', '')
            with lock:
                log({'path': url.path, 'auth': auth[:20]})
                ok = auth.startswith('Bearer ') and auth[7:] in st['access']
            if not ok: return self.send(401, {'error': {'code': 'InvalidAuthenticationToken'}})
            p = url.path[len('/graph'):]
            if p == '/me/drive':
                return self.send(200, {'id': 'b!mydrive', 'name': 'OneDrive', 'driveType': 'business', 'owner': {'user': {'displayName': 'Alice Test'}}})
            if p == '/me/drives':
                return self.send(200, {'value': [{'id': 'b!mydrive', 'name': 'OneDrive', 'driveType': 'business'}]})
            if p == '/me/followedSites':
                return self.send(200, {'value': [{'id': 'site1', 'displayName': 'Lab Share'}, {'id': 'site2', 'displayName': 'Broken Site'}]})
            if p == '/sites/site1/drives':
                return self.send(200, {'value': [{'id': 'b!labdocs', 'name': 'Documents', 'driveType': 'documentLibrary'}]})
            return self.send(403, {'error': {'code': 'accessDenied'}})
        self.send(404, {})

    def do_POST(self):
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
        time.sleep(0.3)
        with lock:
            log({'path': self.path, 'form': {k: (v if k != 'client_secret' else ('<ok>' if v == EXPECTED_SECRET else '<WRONG>')) for k, v in form.items()}})
            mode = open(MODE).read().strip() if os.path.exists(MODE) else 'ok'
            if mode == 'fail':
                return self.send(400, {'error': 'invalid_grant', 'error_description': 'AADSTS70000: forced failure'})
            if form.get('grant_type') == 'authorization_code':
                req = st['codes'].pop(form.get('code'), None)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get('code_verifier', '').encode()).digest()).decode().rstrip('=')
                if (req is None or req['client_id'] != form.get('client_id') or req['redirect_uri'] != form.get('redirect_uri')
                        or req.get('code_challenge') != challenge or form.get('client_secret', '') != EXPECTED_SECRET):
                    return self.send(400, {'error': 'invalid_grant', 'error_description': 'AADSTS54005: bad code, client, redirect or PKCE'})
            elif form.get('grant_type') == 'refresh_token':
                if form.get('refresh_token') not in st['valid_refresh']:
                    return self.send(400, {'error': 'invalid_grant', 'error_description': 'AADSTS70000: refresh token invalid'})
                st['valid_refresh'].discard(form['refresh_token'])
            else:
                return self.send(400, {'error': 'unsupported_grant_type'})
            st['n'] += 1
            n = st['n']
            st['valid_refresh'].add(f'REAL-{n}'); st['access'].add(f'ACCESS-{n}')
            return self.send(200, {'token_type': 'Bearer', 'expires_in': 3600, 'access_token': f'ACCESS-{n}', 'refresh_token': f'REAL-{n}'})

ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2]) if len(sys.argv) > 2 else 5999), H).serve_forever()
