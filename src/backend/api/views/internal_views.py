import logging
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, jsonify, request

from ..managers import token_broker_manager


# Deliberately not under /api. Traefik refuses /internal (router rule in
# deployment/docker/traefik/dynamic/motuz.yml), and these routes only answer on the
# loopback socket that TOKEN_BROKER_URL points to (uWSGI http-socket 127.0.0.1:5001)
bp = Blueprint('internal', __name__, url_prefix='/internal')


def _broker_port():
    url = urlsplit(current_app.config['TOKEN_BROKER_URL'])
    return str(url.port or (443 if url.scheme == 'https' else 80))


@bp.before_request
def only_on_broker_socket():
    """Second line of defense next to the Traefik rule: Traefik forwards to :5000,
    rclone uses :5001. uWSGI and the Flask dev server set SERVER_PORT to the port of
    the socket that accepted the connection, never from the Host header (and wsgi.py
    does not let ProxyFix touch it)."""
    if request.environ.get('SERVER_PORT') != _broker_port():
        abort(404)


@bp.route('/oauth/token', methods=['POST'])
def oauth_token():
    """OAuth token endpoint used by rclone, see managers/token_broker_manager.py"""
    try:
        status, body = token_broker_manager.handle_token_request(
            request.form.to_dict(), request.authorization,
        )
    except Exception as e:
        logging.exception(e)
        status, body = 500, {'error': 'server_error'}
    response = jsonify(body)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response
