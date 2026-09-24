import logging

from flask import Blueprint, jsonify, request

from ..managers import token_broker_manager


# Deliberately not under /api: nginx only forwards /api and /swaggerui, so these
# routes are reachable only on the loopback HTTP socket (see wsgi.ini)
bp = Blueprint('internal', __name__, url_prefix='/internal')


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
