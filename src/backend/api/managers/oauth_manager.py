"""
"Sign in with Microsoft" for OneDrive / SharePoint connections, run by the server so
that users do not have to run `rclone config` themselves.

1. start():   creates an OauthFlow (state + PKCE verifier stay server side) and
              returns Microsoft's sign-in URL.
2. The user signs in. Microsoft redirects the browser to the redirect URI with ?code&state:
   - rclone's public app (default) only allows http://localhost:53682/, so the browser
     shows an error page and the user pastes its address into Motuz -> finish()
   - an own app registration can redirect to /api/oauth/onedrive/callback -> callback()
3. finish()/callback() exchange the code for a token and discover the user's drives
   (OneDrive, SharePoint libraries of followed sites). The token stays in the flow row.
4. connect(): the user picks a drive and Motuz creates the cloud connection. From then
   on the token broker keeps the token fresh, with the app registration that issued it
   (CloudConnection.onedrive_client_id).
"""
import base64
import datetime
import hashlib
import json
import logging
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app, request

from ..application import db
from ..exceptions import *
from ..models import CloudConnection, OauthFlow
from ..managers.auth_manager import token_required, get_logged_in_user


PROVIDER = 'onedrive'
CALLBACK_PATH = '/api/oauth/onedrive/callback'
# What rclone requests for its app (and what tenants consented to for it)
RCLONE_SCOPES = 'Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All Sites.Read.All offline_access'
# The delegated permissions an own app registration needs (README, "OneDrive: own app
# registration"); requesting more would ask for consent to more
OWN_APP_SCOPES = 'Files.ReadWrite.All Sites.Read.All offline_access User.Read'
FLOW_TTL = datetime.timedelta(minutes=15)

# rclone's public OneDrive app (backend/onedrive/onedrive.go); the secret is stored obscured
# there and revealed with `rclone reveal`, exactly like rclone itself does
RCLONE_CLIENT_ID = 'b15665d9-eda6-4092-8539-0eec376afd59'
RCLONE_OBSCURED_CLIENT_SECRET = '_JUdzh3LnKNqSPcf4Wu5fgMFIQOI8glZu_akYgR8yf6egowNBg-R'
RCLONE_REDIRECT_URI = 'http://localhost:53682/'

_HTTP_TIMEOUT = 30


def own_client_id():
    """Client id of Motuz's own app registration, or None if none is configured"""
    return current_app.config.get('ONEDRIVE_CLIENT_ID') or None


def own_client_credentials():
    """(client_id, client_secret) of the own app registration; the secret may be empty"""
    return own_client_id(), current_app.config.get('ONEDRIVE_CLIENT_SECRET') or ''


def uses_own_app():
    return own_client_id() is not None


def client_credentials():
    """
    (client_id, client_secret) of the app that new sign-ins use: the own app
    registration if one is configured, rclone's public app otherwise. Existing
    connections keep the app they were created with (CloudConnection.onedrive_client_id,
    see token_broker_manager.upstream_client_credentials).
    """
    if uses_own_app():
        return own_client_credentials()
    return RCLONE_CLIENT_ID, _reveal(RCLONE_OBSCURED_CLIENT_SECRET)


def redirect_uri():
    """The configured redirect URI applies to an own app only: rclone's app accepts
    nothing but its localhost address"""
    if uses_own_app():
        return current_app.config.get('ONEDRIVE_REDIRECT_URI') or RCLONE_REDIRECT_URI
    return RCLONE_REDIRECT_URI


def scopes():
    return OWN_APP_SCOPES if uses_own_app() else RCLONE_SCOPES


def redirect_mode():
    """'callback' if Microsoft redirects back to Motuz, 'paste' otherwise"""
    return 'callback' if redirect_uri().rstrip('/').endswith(CALLBACK_PATH) else 'paste'


@token_required
def start():
    owner = get_logged_in_user(request)
    _delete_expired_flows()

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip('=')

    client_id, _ = client_credentials()
    flow = OauthFlow(
        state=secrets.token_urlsafe(32),
        owner=owner,
        provider=PROVIDER,
        code_verifier=code_verifier,
        client_id=client_id,
    )
    db.session.add(flow)
    db.session.commit()

    query = urllib.parse.urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri(),
        'response_mode': 'query',
        'scope': scopes(),
        'state': flow.state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'prompt': 'select_account',
    })
    return {
        'authorize_url': '{}?{}'.format(current_app.config['ONEDRIVE_AUTH_URL'], query),
        'redirect_mode': redirect_mode(),
        'state': flow.state,
    }


@token_required
def finish(data):
    """Paste mode: `data['redirect_url']` is the address the browser was redirected to"""
    owner = get_logged_in_user(request)
    params = _parse_redirect(data.get('redirect_url') or '')

    flow = _pending_flow(params.get('state'))
    if flow is None or flow.owner != owner:
        raise HTTP_400_BAD_REQUEST('This sign-in has expired or belongs to another session. Please start again.')

    _complete_flow(flow, params)
    return _flow_result(flow)


def callback(args):
    """
    Callback mode: Microsoft redirects the browser here. There is no Motuz JWT on a
    redirect, so the owner comes from the flow created by start(). Returns the path
    to send the browser to.
    """
    flow = _pending_flow(args.get('state'))
    if flow is None:
        return '/clouds?oauth_error=expired'
    try:
        _complete_flow(flow, args)
    except HTTP_EXCEPTION as e:
        logging.error("OneDrive sign-in failed: {}".format(e.payload))
        return '/clouds?oauth_error=failed'
    return '/clouds?oauth_state={}'.format(urllib.parse.quote(flow.state))


@token_required
def retrieve(state):
    """Drives of a completed flow (callback mode)"""
    owner = get_logged_in_user(request)
    flow = _pending_flow(state)
    if flow is None or flow.owner != owner or flow.token is None:
        raise HTTP_404_NOT_FOUND('Sign-in not found or expired')
    return _flow_result(flow)


@token_required
def connect(data):
    owner = get_logged_in_user(request)
    flow = _pending_flow(data.get('state'))
    if flow is None or flow.owner != owner or flow.token is None:
        raise HTTP_400_BAD_REQUEST('This sign-in has expired. Please start again.')

    drives = json.loads(flow.drives or '[]')
    drive = next((d for d in drives if d['id'] == data.get('drive_id')), None)
    if drive is None:
        raise HTTP_400_BAD_REQUEST('Please choose one of the listed drives')

    name = (data.get('name') or '').strip() or drive['name']
    cloud_connection = CloudConnection(
        name=name,
        owner=owner,
        type='onedrive',
        onedrive_token=flow.token,
        onedrive_drive_id=drive['id'],
        onedrive_drive_type=drive['drive_type'],
        # The app that issued the token: the broker refreshes it with the same app
        onedrive_client_id=flow.client_id,
    )
    db.session.add(cloud_connection)
    db.session.delete(flow)
    db.session.commit()
    return cloud_connection


def _complete_flow(flow, params):
    if params.get('error'):
        raise HTTP_400_BAD_REQUEST('Microsoft sign-in failed: {}'.format(
            params.get('error_description') or params['error']))
    if not params.get('code'):
        raise HTTP_400_BAD_REQUEST('The pasted address does not contain a sign-in code')

    client_id, client_secret = client_credentials()
    if flow.client_id != client_id:
        # The configured app changed (redeploy) after this sign-in started
        raise HTTP_400_BAD_REQUEST('The OneDrive app registration of Motuz has changed. Please start again.')
    form = {
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'code': params['code'],
        'redirect_uri': redirect_uri(),
        'code_verifier': flow.code_verifier,
        'scope': scopes(),
    }
    if client_secret:
        form['client_secret'] = client_secret
    status, body = _request('POST', current_app.config['ONEDRIVE_TOKEN_URL'], form=form)
    if status != 200 or not body.get('access_token') or not body.get('refresh_token'):
        logging.error("OneDrive code exchange failed: {} {}".format(status, body.get('error')))
        raise HTTP_400_BAD_REQUEST('Microsoft did not accept the sign-in: {}'.format(
            body.get('error_description', body.get('error', 'unknown error')).split('\r\n')[0]))

    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(body.get('expires_in') or 3600))
    token = {
        'access_token': body['access_token'],
        'token_type': body.get('token_type', 'Bearer'),
        'refresh_token': body['refresh_token'],
        'expiry': expiry.isoformat().replace('+00:00', 'Z'),
    }

    flow.token = json.dumps(token)
    flow.drives = json.dumps(_discover_drives(body['access_token']))
    db.session.commit()


def _discover_drives(access_token):
    """OneDrive(s) of the user plus the document libraries of SharePoint sites they follow"""
    graph = current_app.config['GRAPH_URL']
    drives = {}

    def add(drive, site=None):
        if not drive.get('id') or drive['id'] in drives:
            return
        owner = ((drive.get('owner') or {}).get('user') or {}).get('displayName')
        name = drive.get('name') or 'OneDrive'
        if site:
            name = '{} - {}'.format(site, name)
        elif owner:
            name = '{} ({})'.format(name, owner)
        drives[drive['id']] = {
            'id': drive['id'],
            'name': name,
            'drive_type': drive.get('driveType', 'business'),
        }

    status, body = _request('GET', graph + '/me/drive', access_token=access_token)
    if status == 200:
        add(body)
    status, body = _request('GET', graph + '/me/drives', access_token=access_token)
    for drive in body.get('value', []) if status == 200 else []:
        add(drive)

    status, body = _request('GET', graph + '/me/followedSites?$select=id,displayName', access_token=access_token)
    for site in (body.get('value', []) if status == 200 else [])[:50]:
        site_status, site_body = _request('GET', '{}/sites/{}/drives'.format(graph, site['id']), access_token=access_token)
        for drive in site_body.get('value', []) if site_status == 200 else []:
            add(drive, site=site.get('displayName'))

    if not drives:
        raise HTTP_400_BAD_REQUEST('Signed in, but no OneDrive was found for this account')
    return list(drives.values())


def _flow_result(flow):
    drives = json.loads(flow.drives or '[]')
    return {
        'state': flow.state,
        'drives': drives,
        'default_drive_id': drives[0]['id'] if drives else None,
    }


def _pending_flow(state):
    if not state:
        return None
    flow = OauthFlow.query.filter_by(state=state, provider=PROVIDER).one_or_none()
    if flow is None or flow.created_at < datetime.datetime.utcnow() - FLOW_TTL:
        return None
    return flow


def _delete_expired_flows():
    OauthFlow.query.filter(OauthFlow.created_at < datetime.datetime.utcnow() - FLOW_TTL).delete()
    db.session.commit()


def _parse_redirect(redirect_url):
    """Accepts the full redirect address, or just its query string"""
    redirect_url = redirect_url.strip()
    query = urllib.parse.urlsplit(redirect_url).query if '://' in redirect_url else redirect_url.lstrip('?')
    return {key: values[0] for key, values in urllib.parse.parse_qs(query).items()}


def _request(method, url, form=None, access_token=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {'Accept': 'application/json'}
    if form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    if access_token:
        headers['Authorization'] = 'Bearer ' + access_token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except Exception as e:
        logging.exception(e)
        return 502, {'error': 'unreachable'}


def _json(raw):
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _reveal(obscured):
    return subprocess.check_output(['rclone', 'reveal', obscured]).decode().strip()
