"""
OAuth token broker for rclone.

rclone runs with `--config=/dev/null` and gets its OAuth token from environment
variables, so it can never persist a refreshed token: Motuz would keep the
originally pasted token forever, which eventually expires (OneDrive) or is
invalidated by the first refresh (providers with single-use refresh tokens).

Instead, rclone receives the current access token plus an opaque per-connection
handle in place of the refresh token, and its token URL points here. When rclone
needs a new access token it sends the handle; the broker locks the connection,
returns the cached access token if it is still fresh, and otherwise refreshes it
upstream with the real refresh token and stores the result. The real refresh
token never leaves the server, and concurrent jobs share one refresh.

OneDrive refresh tokens only work with the app registration that issued them
(CloudConnection.onedrive_client_id, see upstream_client_credentials): rclone's
public app, whose credentials rclone sends along, or Motuz's own app, whose
secret the broker adds and which rclone never gets.

The endpoint is registered outside of /api and answers only on the socket that
TOKEN_BROKER_URL points to (uWSGI's loopback HTTP socket 127.0.0.1:5001); Traefik
refuses /internal and forwards to :5000 only (see views/internal_views.py).
"""
import datetime
import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app
from sqlalchemy import update

from ..application import db
from ..models import CloudConnection
from . import oauth_manager


# Connection type -> (token column, config key of the upstream token URL)
BROKERED_TYPES = {
    'onedrive': ('onedrive_token', 'ONEDRIVE_TOKEN_URL'),
}

# Hand out a cached access token only if it stays valid at least this long
_MIN_REMAINING = datetime.timedelta(minutes=5)
_UPSTREAM_TIMEOUT = 30

APP_MISMATCH_DESCRIPTION = (
    'This OneDrive connection was created with a different app registration than '
    'the one Motuz uses now. Sign in with Microsoft again.'
)


class AppRegistrationMismatch(Exception):
    pass


def upstream_client_credentials(stored_client_id, forwarded, own_app, rclone_client_id):
    """
    Client credentials to refresh a OneDrive token with. A refresh token is only
    accepted from the app it was issued to, which the connection remembers.

    @param stored_client_id: CloudConnection.onedrive_client_id; None for connections
                             created from a pasted rclone token, i.e. rclone's app
    @param forwarded: (client_id, client_secret) that rclone sent; rclone always uses
                      its own public app
    @param own_app: (client_id, client_secret) of the configured own app registration,
                    or None if there is none
    @param rclone_client_id: client id of rclone's public app
    @return: (client_id, client_secret); either may be None/empty
    @raise AppRegistrationMismatch: the token belongs to an app that is not configured
                                    (any more)
    """
    if not stored_client_id or stored_client_id == rclone_client_id:
        return forwarded
    if own_app is not None and own_app[0] and stored_client_id == own_app[0]:
        # The own app's secret is added here, on the way upstream; rclone never sees it
        return own_app
    raise AppRegistrationMismatch(stored_client_id)


def broker_token(cloud_connection):
    """
    Returns (token_json, token_url) to pass to rclone for a stored connection, or
    None if the connection is not brokered (then the stored token is used as is).
    """
    columns = BROKERED_TYPES.get(cloud_connection.type)
    if columns is None or getattr(cloud_connection, 'id', None) is None:
        return None

    token = _parse_token(getattr(cloud_connection, columns[0], None))
    if token is None or not token.get('refresh_token'):
        return None

    handle = ensure_handle(cloud_connection)
    brokered = {
        'access_token': token.get('access_token', ''),
        'token_type': token.get('token_type', 'Bearer'),
        'refresh_token': handle,
        'expiry': token.get('expiry', '0001-01-01T00:00:00Z'),
    }
    return json.dumps(brokered), current_app.config['TOKEN_BROKER_URL']


def handle_token_request(form, authorization):
    """
    Implements the refresh_token grant of an OAuth token endpoint for rclone.

    @param form: dict of the form-encoded request body
    @param authorization: flask request.authorization (client credentials may be sent
                          as HTTP basic auth instead of in the body)
    @return: (http status, response dict)
    """
    if form.get('grant_type') != 'refresh_token' or not form.get('refresh_token'):
        return 400, {'error': 'unsupported_grant_type'}

    handle = form['refresh_token']
    cloud_connection = (db.session.query(CloudConnection)
        .filter_by(token_broker_handle=handle)
        .with_for_update()
        .one_or_none()
    )
    if cloud_connection is None or cloud_connection.type not in BROKERED_TYPES:
        db.session.rollback()
        return 400, {'error': 'invalid_grant', 'error_description': 'Unknown token handle'}

    # Client credentials rclone sent, in the body or (RFC 6749 2.3.1) form-urlencoded
    # inside HTTP basic auth, which is what Go's oauth2 (used by rclone) does
    forwarded = (form.get('client_id'), form.get('client_secret'))
    if authorization is not None and authorization.username:
        forwarded = (
            forwarded[0] or urllib.parse.unquote_plus(authorization.username),
            forwarded[1] or (urllib.parse.unquote_plus(authorization.password) if authorization.password else None),
        )

    if cloud_connection.type == 'onedrive':
        own_app = oauth_manager.own_client_credentials() if oauth_manager.uses_own_app() else None
        try:
            client_id, client_secret = upstream_client_credentials(
                cloud_connection.onedrive_client_id, forwarded, own_app, oauth_manager.RCLONE_CLIENT_ID,
            )
        except AppRegistrationMismatch:
            db.session.rollback()
            logging.error("Token refresh for cloud connection {} refused: its token was issued to client {}, "
                "which is neither rclone's app nor the configured app registration ({})".format(
                cloud_connection.id, cloud_connection.onedrive_client_id, own_app[0] if own_app else 'none',
            ))
            return 400, {'error': 'invalid_grant', 'error_description': APP_MISMATCH_DESCRIPTION}
    else:
        client_id, client_secret = forwarded

    token_column, token_url_key = BROKERED_TYPES[cloud_connection.type]
    token = _parse_token(getattr(cloud_connection, token_column)) or {}

    now = datetime.datetime.now(datetime.timezone.utc)
    expiry = _parse_expiry(token.get('expiry'))
    if token.get('access_token') and expiry is not None and expiry - now > _MIN_REMAINING:
        db.session.rollback() # Release the lock
        return 200, _token_response(token, expiry, now)

    if not token.get('refresh_token'):
        db.session.rollback()
        return 400, {'error': 'invalid_grant', 'error_description': 'No refresh token stored for this connection'}

    # Forward rclone's request upstream, with the real refresh token and the client
    # credentials in the body (supported by all providers)
    upstream_form = {
        key: value for key, value in form.items()
        if value and key not in ('client_id', 'client_secret')
    }
    upstream_form['refresh_token'] = token['refresh_token']
    if client_id:
        upstream_form['client_id'] = client_id
    if client_secret:
        upstream_form['client_secret'] = client_secret

    status, body = _post_form(current_app.config[token_url_key], upstream_form)
    if status != 200 or not body.get('access_token'):
        db.session.rollback()
        logging.error("Token refresh for cloud connection {} failed: {} {}".format(
            cloud_connection.id, status, body.get('error'),
        ))
        return (status if 400 <= status < 500 else 502), {
            'error': body.get('error', 'server_error'),
            'error_description': body.get('error_description', 'Token refresh failed upstream'),
        }

    expires_in = int(body.get('expires_in') or 3600)
    expiry = now + datetime.timedelta(seconds=expires_in)
    token.update({
        'access_token': body['access_token'],
        'token_type': body.get('token_type', token.get('token_type', 'Bearer')),
        'expiry': expiry.isoformat().replace('+00:00', 'Z'),
    })
    if body.get('refresh_token'): # Rotated refresh token
        token['refresh_token'] = body['refresh_token']
    setattr(cloud_connection, token_column, json.dumps(token))
    db.session.commit()
    logging.info("Refreshed OAuth token for cloud connection {}".format(cloud_connection.id))

    return 200, _token_response(token, expiry, now)


def _token_response(token, expiry, now):
    # No refresh_token in the response: rclone keeps using its handle
    return {
        'access_token': token['access_token'],
        'token_type': token.get('token_type', 'Bearer'),
        'expires_in': max(int((expiry - now).total_seconds()), 0),
    }


def ensure_handle(cloud_connection):
    if cloud_connection.token_broker_handle:
        return cloud_connection.token_broker_handle

    # Only set it if still unset, so that concurrent callers agree on one handle
    db.session.execute(
        update(CloudConnection)
        .where(CloudConnection.id == cloud_connection.id)
        .where(CloudConnection.token_broker_handle.is_(None))
        .values(token_broker_handle=secrets.token_urlsafe(32))
    )
    db.session.commit()
    db.session.refresh(cloud_connection)
    return cloud_connection.token_broker_handle


def _parse_token(value):
    if not value:
        return None
    try:
        token = json.loads(value)
    except ValueError:
        return None
    return token if isinstance(token, dict) else None


def _parse_expiry(value):
    """
    Parses Go's RFC 3339 timestamps, e.g. 2026-09-24T22:39:52.486512262+02:00
    Returns an aware datetime or None (also for Go's zero time).
    """
    if not value or value.startswith('0001-01-01'):
        return None
    value = value.replace('Z', '+00:00')
    value = re.sub(r'(\.\d{6})\d+', r'\1', value) # Python supports microseconds only
    try:
        expiry = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    return expiry


def _post_form(url, form):
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(request, timeout=_UPSTREAM_TIMEOUT) as response:
            return response.status, _json_or_empty(response.read())
    except urllib.error.HTTPError as e:
        return e.code, _json_or_empty(e.read())
    except Exception as e:
        logging.exception(e)
        return 502, {'error': 'server_error', 'error_description': 'Token endpoint unreachable'}


def _json_or_empty(raw):
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
