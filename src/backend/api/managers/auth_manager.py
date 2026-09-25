"""
Login tokens (flask-jwt-extended, JWT_IDENTITY_CLAIM 'identity' = the Unix user name).

- A login starts a session: every access and refresh token issued for it carries the
  same session id (claim `sid`). Lifetimes: JWT_ACCESS_TOKEN_EXPIRES and
  JWT_REFRESH_TOKEN_EXPIRES in config.py.
- /auth/refresh/ rotates the refresh token: the new pair keeps the sid, and the old
  refresh token is revoked with a short grace window (REFRESH_GRACE_SECONDS). Browser
  tabs share their tokens through localStorage (redux-persist), so two tabs can refresh
  with the same token at the same moment; both get a new pair. A rotated refresh token
  used after the grace window means it was copied (or a request was very late): the
  whole session is revoked.
- /auth/logout/ revokes the refresh token and the session, so every access token of
  the session stops working at once, also in other tabs.
- Every token is checked against the revoked_token table (its jti, and its sid).
  Tokens from before sessions existed have no sid; they expire on their own.
"""
import logging
import time
import uuid
from functools import wraps

from flask import current_app
from sqlalchemy.exc import IntegrityError
import flask_jwt_extended as flask_jwt

from ..models import RevokedToken
from ..application import db, jwt
from ..utils.pam import pam
from ..exceptions import *



def refresh_token_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            flask_jwt.verify_jwt_in_request(refresh=True)
        except Exception as e:
            raise HTTP_401_UNAUTHORIZED(str(e))

        return fn(*args, **kwargs)
    return wrapper



def token_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            flask_jwt.verify_jwt_in_request()
        except Exception as e:
            raise HTTP_401_UNAUTHORIZED(str(e))

        return fn(*args, **kwargs)
    return wrapper



# How long a rotated refresh token keeps working, for tabs that refresh concurrently
REFRESH_GRACE_SECONDS = 60
SESSION_CLAIM = 'sid'


@jwt.token_in_blocklist_loader
def _check_if_token_in_blocklist(jwt_header, jwt_payload):
    """
    This function is automatically loaded and it does not need to be called.
    https://flask-jwt-extended.readthedocs.io/en/stable/blocklist_and_token_revoking.html
    """
    return token_is_revoked(jwt_payload)


def _issue_tokens(identity, session_id):
    claims = {SESSION_CLAIM: session_id}
    return {
        'access': flask_jwt.create_access_token(identity=identity, additional_claims=claims),
        'refresh': flask_jwt.create_refresh_token(identity=identity, additional_claims=claims),
    }



@token_required
def get_logged_in_user(*args, **kwargs):
    return flask_jwt.get_jwt_identity()



def login_user(data):
    username = data['username']
    password = data['password']

    user_authentication = pam()
    user_authentication.authenticate(username, password)

    if user_authentication.code != 0:
        logging.error("Could not authenticate {}. Reason: `{}` (Code: {})".format(
            username, user_authentication.reason, user_authentication.code,
        ))
        raise HTTP_401_UNAUTHORIZED('No match for Username and Password.')

    return {
        'status': 'success',
        'message': 'Successfully logged in.',
        **_issue_tokens(username, str(uuid.uuid4())),
    }



@refresh_token_required
def refresh_token():
    """New access and refresh tokens; the old refresh token is revoked after a grace window"""
    token = flask_jwt.get_jwt()
    # Tokens from before sessions existed start one here
    session_id = token.get(SESSION_CLAIM) or str(uuid.uuid4())
    _add_revoked(token['jti'], 'refresh', token, token['exp'],
                 grace_until=int(time.time()) + REFRESH_GRACE_SECONDS)
    return {
        'status': 'success',
        'message': 'Successfully refreshed token.',
        **_issue_tokens(flask_jwt.get_jwt_identity(), session_id),
    }



@refresh_token_required
def logout_user():
    """Revokes the refresh token and its session, i.e. all access tokens of this login"""
    token = flask_jwt.get_jwt()
    revoke_token(token)
    if token.get(SESSION_CLAIM):
        revoke_session(token)
    clean_token_database()
    return {
        'status': 'success',
        'message': 'Successfully logged out.',
    }



def _add_revoked(jti, type, token, exp, grace_until=None):
    """Adds a revoked_token row. Returns False if the jti was already revoked."""
    try:
        db.session.add(RevokedToken(
            jti=str(jti),
            type=type,
            identity=token[current_app.config['JWT_IDENTITY_CLAIM']],
            exp=int(exp),
            grace_until=grace_until,
        ))
        db.session.commit()
        return True
    except IntegrityError:
        # Already revoked (e.g. rotated by a concurrent refresh): keep the first row
        db.session.rollback()
        return False



def revoke_token(token):
    """Revokes this token now (no grace window)"""
    revoked = RevokedToken.query.filter_by(jti=str(token['jti'])).first()
    if revoked is not None:
        if revoked.grace_until is not None:
            revoked.grace_until = None
            db.session.commit()
        return
    _add_revoked(token['jti'], token['type'], token, token['exp'])



def revoke_session(token):
    """
    Revokes every token of the token's session. The row outlives every token of the
    session: none was issued later than now, and none lives longer than a refresh token.
    """
    lifetime = current_app.config['JWT_REFRESH_TOKEN_EXPIRES']
    _add_revoked(token[SESSION_CLAIM], 'session', token, time.time() + lifetime.total_seconds())



def token_is_revoked(token):
    """
    Whether the token, or its session, has been revoked. A refresh token rotated less
    than REFRESH_GRACE_SECONDS ago still works; one used later revokes its session.
    """
    if 'jti' not in token:
        return True

    jti = str(token['jti'])
    session_id = token.get(SESSION_CLAIM)
    keys = [jti, str(session_id)] if session_id else [jti]
    rows = RevokedToken.query.filter(RevokedToken.jti.in_(keys)).all()

    now = time.time()
    revoked = False
    for row in rows:
        if row.jti == jti and row.grace_until is not None:
            if now <= row.grace_until:
                continue # Rotated moments ago, e.g. by another tab
            if session_id and token.get('type') == 'refresh':
                logging.warning("Reuse of a rotated refresh token of %s: revoking its session",
                                token.get(current_app.config['JWT_IDENTITY_CLAIM']))
                revoke_session(token)
        revoked = True
    return revoked



def clean_token_database():
    now_ts = int(time.time())

    try:
        # Opting for this version for performance (single round-trip)
        query = RevokedToken.__table__.delete().where(RevokedToken.exp < now_ts)
        db.session.execute(query)
        db.session.commit()
    except Exception as e:
        logging.error("Could not clean up the token database")
        logging.exception(e)
