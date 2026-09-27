"""
The API of remote workers (managers/worker_manager.py), public under /api/workers
behind Traefik like the user API, but a separate blueprint outside flask-restx:
no Swagger entries, and only worker credentials are accepted (never user tokens).

POST /api/workers/auth                       {secret | bootstrap_token, version} -> access token
POST /api/workers/auth/refresh               (worker token) -> new access token
POST /api/workers/claim                      (worker token) {pool, capabilities, wait} -> 200 ticket | 204
POST /api/workers/jobs/<ticket>/progress     (worker token + X-Motuz-Ticket) -> {action}
POST /api/workers/jobs/<ticket>/finish       (worker token + X-Motuz-Ticket)
POST /api/workers/oauth/token                OAuth refresh for rclone (job broker token)
"""
import logging

from flask import Blueprint, jsonify, request

from ..exceptions import HTTP_EXCEPTION
from ..managers import worker_manager
from ..utils.rate_limit import RateLimiter


bp = Blueprint('workers', __name__, url_prefix='/api/workers')

# Per client address, and per worker for claims
AUTH_LIMIT = RateLimiter(limit=20, window_seconds=60)
CLAIM_LIMIT = RateLimiter(limit=60, window_seconds=60)
BROKER_LIMIT = RateLimiter(limit=120, window_seconds=60)

MAX_BODY = 1024 * 1024
MAX_FINISH_BODY = 256 * 1024 * 1024 # hashsum trees of large jobs


def _json(body, status=200):
    response = jsonify(body)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response


def _error(status, message):
    return _json({'error': message if isinstance(message, str) else str(message)}, status)


def _body(limit=MAX_BODY):
    if request.content_length is not None and request.content_length > limit:
        return None
    return request.get_json(silent=True) or {}


def _handle(fn, limit=MAX_BODY):
    try:
        if request.content_length is not None and request.content_length > limit:
            return _error(413, 'Request too large')
        return fn()
    except HTTP_EXCEPTION as e:
        return _error(e.code, e.payload)
    except Exception as e:
        logging.exception(e)
        return _error(500, 'Internal error')


def _rate_limited(limiter, key):
    if not limiter.allow(key):
        worker_manager.audit.warning("rate limit: %s", key[0])
        return _error(429, 'Too many requests')
    return None


@bp.route('/auth', methods=['POST'])
def auth():
    limited = _rate_limited(AUTH_LIMIT, ('auth', request.remote_addr))
    if limited:
        return limited
    return _handle(lambda: _json(worker_manager.sign_in(_body())))


@bp.route('/auth/refresh', methods=['POST'])
def auth_refresh():
    limited = _rate_limited(AUTH_LIMIT, ('auth', request.remote_addr))
    if limited:
        return limited

    def run():
        worker = worker_manager.authenticate(request.headers.get('Authorization'))
        return _json(worker_manager.refresh(worker))
    return _handle(run)


@bp.route('/claim', methods=['POST'])
def claim():
    def run():
        worker = worker_manager.authenticate(request.headers.get('Authorization'))
        limited = _rate_limited(CLAIM_LIMIT, ('claim', worker.id))
        if limited:
            return limited
        ticket = worker_manager.claim(worker, _body())
        if ticket is None:
            return '', 204
        return _json(ticket)
    return _handle(run)


@bp.route('/jobs/<int:ticket_id>/progress', methods=['POST'])
def progress(ticket_id):
    def run():
        worker = worker_manager.authenticate(request.headers.get('Authorization'))
        return _json(worker_manager.progress(
            worker, ticket_id, request.headers.get('X-Motuz-Ticket'), _body()))
    return _handle(run)


@bp.route('/jobs/<int:ticket_id>/finish', methods=['POST'])
def finish(ticket_id):
    def run():
        worker = worker_manager.authenticate(request.headers.get('Authorization'))
        return _json(worker_manager.finish(
            worker, ticket_id, request.headers.get('X-Motuz-Ticket'), _body(MAX_FINISH_BODY)))
    return _handle(run, limit=MAX_FINISH_BODY)


@bp.route('/oauth/token', methods=['POST'])
def oauth_token():
    """OAuth token endpoint for rclone on remote workers (see worker_manager)"""
    limited = _rate_limited(BROKER_LIMIT, ('broker', request.remote_addr))
    if limited:
        return limited
    try:
        status, body = worker_manager.handle_worker_token_request(
            request.form.to_dict(), request.authorization,
        )
    except Exception as e:
        logging.exception(e)
        status, body = 500, {'error': 'server_error'}
    return _json(body, status)
