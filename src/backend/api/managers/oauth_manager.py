"""
"Sign in with Microsoft" (OneDrive / SharePoint) and "Sign in with Google" (Google
Drive), run by the server so that users do not have to run `rclone config` themselves.
Each provider is a `Provider` definition (PROVIDERS); the flow is the same for all:

1. start():   creates an OauthFlow (state + PKCE verifier stay server side) and
              returns the provider's sign-in URL.
2. The user signs in. The provider redirects the browser to the redirect URI with ?code&state:
   - rclone's public apps only allow a loopback address on port 53682, so the browser
     shows an error page and the user pastes its address into Motuz -> finish()
   - an own app registration / OAuth client can redirect to
     /api/oauth/<provider>/callback -> callback()
3. finish()/callback() exchange the code for a token and discover the user's drives
   (OneDrive: OneDrive and SharePoint libraries of followed sites; Google: My Drive
   and shared drives). The token stays in the flow row.
4. connect(): the user picks a drive and Motuz creates the cloud connection. From then
   on the token broker keeps the token fresh, with the app that issued it
   (CloudConnection.<provider>_client_id, see token_broker_manager).
"""
import base64
import dataclasses
import datetime
import hashlib
import json
import logging
import secrets
import subprocess
import typing
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app, request

from ..application import db
from ..exceptions import *
from ..models import CloudConnection, OauthFlow
from ..managers.auth_manager import token_required, get_logged_in_user


FLOW_TTL = datetime.timedelta(minutes=15)
_HTTP_TIMEOUT = 30


@dataclasses.dataclass(frozen=True)
class Provider:
    """
    Everything that differs between OAuth providers. Settings come from the Flask
    config keys <config_prefix>_{CLIENT_ID,CLIENT_SECRET,REDIRECT_URI,AUTH_URL,TOKEN_URL}.
    """
    name: str               # URL segment: /api/oauth/<name>/..., OauthFlow.provider
    label: str              # "Microsoft", "Google"
    service: str            # "OneDrive", "Google Drive"
    connection_type: str    # CloudConnection.type (rclone backend)
    config_prefix: str
    token_column: str       # CloudConnection column with the rclone token JSON
    client_id_column: str   # CloudConnection column with the client that issued it
    # rclone's public app; the secret is stored obscured in rclone's source and revealed
    # with `rclone reveal`, exactly like rclone itself does
    rclone_client_id: str
    rclone_obscured_client_secret: str
    rclone_redirect_uri: str
    rclone_scopes: str
    own_app_scopes: str
    # Extra query parameters of the authorization URL
    auth_params: typing.Tuple[typing.Tuple[str, str], ...]
    # Whether the code exchange repeats the scope (Microsoft v2 endpoint)
    exchange_sends_scope: bool
    # Scope the token must have been granted (None: not checked)
    required_scope: typing.Optional[str]
    # (access_token) -> list of {'id', 'name', 'drive_type'}
    discover: typing.Callable
    # (drive, flow) -> dict of CloudConnection columns
    connection_fields: typing.Callable
    app_changed_message: str
    app_mismatch_description: str

    @property
    def callback_path(self):
        return '/api/oauth/{}/callback'.format(self.name)

    @property
    def token_url_key(self):
        return self.config_prefix + '_TOKEN_URL'

    def _config(self, key):
        return current_app.config.get('{}_{}'.format(self.config_prefix, key))

    def own_client_id(self):
        """Client id of Motuz's own app, or None if none is configured"""
        return self._config('CLIENT_ID') or None

    def own_client_credentials(self):
        """(client_id, client_secret) of the own app; the secret may be empty"""
        return self.own_client_id(), self._config('CLIENT_SECRET') or ''

    def uses_own_app(self):
        return self.own_client_id() is not None

    def client_credentials(self):
        """
        (client_id, client_secret) of the app that new sign-ins use: the own app if one
        is configured, rclone's public app otherwise. Existing connections keep the app
        they were created with (token_broker_manager.upstream_client_credentials).
        """
        if self.uses_own_app():
            return self.own_client_credentials()
        return self.rclone_client_id, _reveal(self.rclone_obscured_client_secret)

    def redirect_uri(self):
        """The configured redirect URI applies to an own app only: rclone's app accepts
        nothing but its loopback address"""
        if self.uses_own_app():
            return self._config('REDIRECT_URI') or self.rclone_redirect_uri
        return self.rclone_redirect_uri

    def scopes(self):
        return self.own_app_scopes if self.uses_own_app() else self.rclone_scopes

    def redirect_mode(self):
        """'callback' if the provider redirects back to Motuz, 'paste' otherwise"""
        return 'callback' if self.redirect_uri().rstrip('/').endswith(self.callback_path) else 'paste'

    def auth_url(self):
        return self._config('AUTH_URL')

    def token_url(self):
        return self._config('TOKEN_URL')


# ---------------------------------------------------------------------------------------
# Microsoft OneDrive

def _discover_onedrive(access_token):
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


def _onedrive_connection_fields(drive, flow):
    return {
        'onedrive_token': flow.token,
        'onedrive_drive_id': drive['id'],
        'onedrive_drive_type': drive['drive_type'],
        # The app that issued the token: the broker refreshes it with the same app
        'onedrive_client_id': flow.client_id,
    }


ONEDRIVE = Provider(
    name='onedrive',
    label='Microsoft',
    service='OneDrive',
    connection_type='onedrive',
    config_prefix='ONEDRIVE',
    token_column='onedrive_token',
    client_id_column='onedrive_client_id',
    # backend/onedrive/onedrive.go
    rclone_client_id='b15665d9-eda6-4092-8539-0eec376afd59',
    rclone_obscured_client_secret='_JUdzh3LnKNqSPcf4Wu5fgMFIQOI8glZu_akYgR8yf6egowNBg-R',
    rclone_redirect_uri='http://localhost:53682/',
    # What rclone requests for its app (and what tenants consented to for it)
    rclone_scopes='Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All Sites.Read.All offline_access',
    # The delegated permissions an own app registration needs (README, "OneDrive: own app
    # registration"); requesting more would ask for consent to more
    own_app_scopes='Files.ReadWrite.All Sites.Read.All offline_access User.Read',
    auth_params=(('response_mode', 'query'), ('prompt', 'select_account')),
    exchange_sends_scope=True,
    required_scope=None,
    discover=_discover_onedrive,
    connection_fields=_onedrive_connection_fields,
    app_changed_message='The OneDrive app registration of Motuz has changed. Please start again.',
    app_mismatch_description=(
        'This OneDrive connection was created with a different app registration than '
        'the one Motuz uses now. Sign in with Microsoft again.'
    ),
)


# ---------------------------------------------------------------------------------------
# Google Drive

GDRIVE_SCOPE = 'https://www.googleapis.com/auth/drive'
_MAX_SHARED_DRIVE_PAGES = 10 # 100 shared drives each


def _google_error(body, status):
    error = body.get('error')
    if isinstance(error, dict):
        return error.get('message') or error.get('status') or status
    return body.get('error_description') or error or status


def _discover_gdrive(access_token):
    """The user's My Drive plus the shared drives they are a member of"""
    api = current_app.config['GDRIVE_API_URL']

    status, body = _request('GET', api + '/about?fields=user(displayName,emailAddress)', access_token=access_token)
    if status != 200:
        # e.g. the Drive API is not enabled in the own client's Google Cloud project
        raise HTTP_400_BAD_REQUEST('Signed in, but Google Drive cannot be accessed: {}'.format(_google_error(body, status)))
    user = body.get('user') or {}
    who = user.get('emailAddress') or user.get('displayName')
    drives = [{
        'id': 'root',
        'name': 'My Drive ({})'.format(who) if who else 'My Drive',
        'drive_type': 'my_drive',
    }]

    page_token = None
    for _ in range(_MAX_SHARED_DRIVE_PAGES):
        query = {'pageSize': 100, 'fields': 'nextPageToken,drives(id,name)'}
        if page_token:
            query['pageToken'] = page_token
        status, body = _request('GET', api + '/drives?' + urllib.parse.urlencode(query), access_token=access_token)
        if status != 200:
            logging.warning("Listing Google shared drives failed: {} {}".format(status, _google_error(body, status)))
            break
        for drive in body.get('drives', []):
            if drive.get('id') and drive['id'] != 'root':
                drives.append({
                    'id': drive['id'],
                    'name': 'Shared drive: {}'.format(drive.get('name') or drive['id']),
                    'drive_type': 'shared_drive',
                })
        page_token = body.get('nextPageToken')
        if not page_token:
            break
    return drives


def _gdrive_connection_fields(drive, flow):
    return {
        'gdrive_token': flow.token,
        # My Drive is rclone's default root; a shared drive is rclone's team_drive
        'gdrive_team_drive': drive['id'] if drive['drive_type'] == 'shared_drive' else None,
        'gdrive_root_folder_id': None,
        'gdrive_client_id': flow.client_id,
    }


GDRIVE = Provider(
    name='gdrive',
    label='Google',
    service='Google Drive',
    connection_type='drive',
    config_prefix='GDRIVE',
    token_column='gdrive_token',
    client_id_column='gdrive_client_id',
    # backend/drive/drive.go (rcloneClientID, rcloneEncryptedClientSecret, and
    # oauthutil.RedirectURL). rclone is retiring this shared client during 2026.
    rclone_client_id='202264815644.apps.googleusercontent.com',
    rclone_obscured_client_secret='eX8GpZTVx3vxMWVkuuBdDWmAUE6rGhTwVrvG9GhllYccSdj2-mvHVg',
    rclone_redirect_uri='http://127.0.0.1:53682/',
    rclone_scopes=GDRIVE_SCOPE,
    own_app_scopes=GDRIVE_SCOPE,
    # Google returns a refresh token only for offline access, and on a repeated sign-in
    # only when the consent screen is shown again
    auth_params=(('access_type', 'offline'), ('prompt', 'consent')),
    exchange_sends_scope=False,
    required_scope=GDRIVE_SCOPE,
    discover=_discover_gdrive,
    connection_fields=_gdrive_connection_fields,
    app_changed_message='The Google OAuth client of Motuz has changed. Please start again.',
    app_mismatch_description=(
        'This Google Drive connection was created with a different OAuth client than '
        'the one Motuz uses now. Sign in with Google again.'
    ),
)


PROVIDERS = {provider.name: provider for provider in (ONEDRIVE, GDRIVE)}
PROVIDERS_BY_CONNECTION_TYPE = {provider.connection_type: provider for provider in PROVIDERS.values()}


def provider(name):
    try:
        return PROVIDERS[name]
    except KeyError:
        raise HTTP_404_NOT_FOUND('Unknown sign-in provider')


# OneDrive names used before there were several providers (tests, token broker)
PROVIDER = ONEDRIVE.name
CALLBACK_PATH = ONEDRIVE.callback_path
RCLONE_SCOPES = ONEDRIVE.rclone_scopes
OWN_APP_SCOPES = ONEDRIVE.own_app_scopes
RCLONE_CLIENT_ID = ONEDRIVE.rclone_client_id
RCLONE_OBSCURED_CLIENT_SECRET = ONEDRIVE.rclone_obscured_client_secret
RCLONE_REDIRECT_URI = ONEDRIVE.rclone_redirect_uri


def own_client_id(p=ONEDRIVE):
    return p.own_client_id()

def own_client_credentials(p=ONEDRIVE):
    return p.own_client_credentials()

def uses_own_app(p=ONEDRIVE):
    return p.uses_own_app()

def client_credentials(p=ONEDRIVE):
    return p.client_credentials()

def redirect_uri(p=ONEDRIVE):
    return p.redirect_uri()

def scopes(p=ONEDRIVE):
    return p.scopes()

def redirect_mode(p=ONEDRIVE):
    return p.redirect_mode()


# ---------------------------------------------------------------------------------------
# The flow

@token_required
def start(provider_name=PROVIDER):
    p = provider(provider_name)
    owner = get_logged_in_user(request)
    _delete_expired_flows()

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip('=')

    client_id, _ = p.client_credentials()
    flow = OauthFlow(
        state=secrets.token_urlsafe(32),
        owner=owner,
        provider=p.name,
        code_verifier=code_verifier,
        client_id=client_id,
    )
    db.session.add(flow)
    db.session.commit()

    query = urllib.parse.urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': p.redirect_uri(),
        'scope': p.scopes(),
        'state': flow.state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        **dict(p.auth_params),
    })
    return {
        'authorize_url': '{}?{}'.format(p.auth_url(), query),
        'redirect_mode': p.redirect_mode(),
        'state': flow.state,
    }


@token_required
def finish(provider_name, data):
    """Paste mode: `data['redirect_url']` is the address the browser was redirected to"""
    p = provider(provider_name)
    owner = get_logged_in_user(request)
    params = _parse_redirect(data.get('redirect_url') or '')

    flow = _pending_flow(p, params.get('state'))
    if flow is None or flow.owner != owner:
        raise HTTP_400_BAD_REQUEST('This sign-in has expired or belongs to another session. Please start again.')

    _complete_flow(p, flow, params)
    return _flow_result(flow)


def callback(provider_name, args):
    """
    Callback mode: the provider redirects the browser here. There is no Motuz JWT on a
    redirect, so the owner comes from the flow created by start(). Returns the path
    to send the browser to.
    """
    p = provider(provider_name)
    # OneDrive keeps its original landing URL; the frontend defaults to it
    suffix = '' if p is ONEDRIVE else '&oauth_provider={}'.format(p.name)
    flow = _pending_flow(p, args.get('state'))
    if flow is None:
        return '/clouds?oauth_error=expired' + suffix
    try:
        _complete_flow(p, flow, args)
    except HTTP_EXCEPTION as e:
        logging.error("{} sign-in failed: {}".format(p.service, e.payload))
        return '/clouds?oauth_error=failed' + suffix
    return '/clouds?oauth_state={}'.format(urllib.parse.quote(flow.state)) + suffix


@token_required
def retrieve(provider_name, state):
    """Drives of a completed flow (callback mode)"""
    p = provider(provider_name)
    owner = get_logged_in_user(request)
    flow = _pending_flow(p, state)
    if flow is None or flow.owner != owner or flow.token is None:
        raise HTTP_404_NOT_FOUND('Sign-in not found or expired')
    return _flow_result(flow)


@token_required
def connect(provider_name, data):
    p = provider(provider_name)
    owner = get_logged_in_user(request)
    flow = _pending_flow(p, data.get('state'))
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
        type=p.connection_type,
        **p.connection_fields(drive, flow),
    )
    db.session.add(cloud_connection)
    db.session.delete(flow)
    db.session.commit()
    return cloud_connection


def _complete_flow(p, flow, params):
    if params.get('error'):
        raise HTTP_400_BAD_REQUEST('{} sign-in failed: {}'.format(
            p.label, params.get('error_description') or params['error']))
    if not params.get('code'):
        raise HTTP_400_BAD_REQUEST('The pasted address does not contain a sign-in code')

    client_id, client_secret = p.client_credentials()
    if flow.client_id != client_id:
        # The configured app changed (redeploy) after this sign-in started
        raise HTTP_400_BAD_REQUEST(p.app_changed_message)
    form = {
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'code': params['code'],
        'redirect_uri': p.redirect_uri(),
        'code_verifier': flow.code_verifier,
    }
    if p.exchange_sends_scope:
        form['scope'] = p.scopes()
    if client_secret:
        form['client_secret'] = client_secret
    status, body = _request('POST', p.token_url(), form=form)
    if status != 200 or not body.get('access_token') or not body.get('refresh_token'):
        logging.error("{} code exchange failed: {} {}".format(p.service, status, body.get('error')))
        raise HTTP_400_BAD_REQUEST('{} did not accept the sign-in: {}'.format(
            p.label, body.get('error_description', body.get('error', 'unknown error')).split('\r\n')[0]))
    if p.required_scope and body.get('scope') and p.required_scope not in body['scope'].split():
        # Google lets users untick requested permissions on the consent screen
        raise HTTP_400_BAD_REQUEST('Motuz needs access to your {}. Please start again and allow it.'.format(p.service))

    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(body.get('expires_in') or 3600))
    token = {
        'access_token': body['access_token'],
        'token_type': body.get('token_type', 'Bearer'),
        'refresh_token': body['refresh_token'],
        'expiry': expiry.isoformat().replace('+00:00', 'Z'),
    }

    flow.token = json.dumps(token)
    flow.drives = json.dumps(p.discover(body['access_token']))
    db.session.commit()


def _flow_result(flow):
    drives = json.loads(flow.drives or '[]')
    return {
        'state': flow.state,
        'drives': drives,
        'default_drive_id': drives[0]['id'] if drives else None,
    }


def _pending_flow(p, state):
    if not state:
        return None
    flow = OauthFlow.query.filter_by(state=state, provider=p.name).one_or_none()
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
