import logging

from flask import request, redirect
from flask_restx import Resource, Namespace, fields

from ..managers import oauth_manager
from ..exceptions import HTTP_EXCEPTION
from .cloud_connection_views import dto as connection_dto


api = Namespace('oauth', description='Sign in to cloud providers (OneDrive, Google Drive) from the browser')

start_dto = api.model('oauth-start', {
    'authorize_url': fields.String(example='https://login.microsoftonline.com/common/oauth2/v2.0/authorize?...'),
    'redirect_mode': fields.String(example='paste', description="'paste': paste the address the browser was redirected to; 'callback': Motuz receives it"),
    'state': fields.String(),
})

drive_dto = api.model('oauth-drive', {
    'id': fields.String(example='b!AbCd'),
    'name': fields.String(example='OneDrive (Jane Doe)'),
    'drive_type': fields.String(example='business', description="OneDrive: business, personal, documentLibrary; Google: my_drive, shared_drive"),
})

flow_dto = api.model('oauth-flow', {
    'state': fields.String(),
    'drives': fields.List(fields.Nested(drive_dto)),
    'default_drive_id': fields.String(),
})

finish_dto = api.model('oauth-finish', {
    'redirect_url': fields.String(required=True, example='http://localhost:53682/?code=...&state=...'),
})

connect_dto = api.model('oauth-connect', {
    'state': fields.String(required=True),
    'drive_id': fields.String(required=True),
    'name': fields.String(required=False, example='My OneDrive'),
})


def _call(fn, *args):
    try:
        return fn(*args)
    except HTTP_EXCEPTION as e:
        api.abort(e.code, e.payload)
    except Exception as e:
        logging.exception(e, exc_info=True)
        api.abort(500, str(e))


# /api/oauth/<provider>/...: 'onedrive' (Sign in with Microsoft) or 'gdrive' (Sign in
# with Google), see oauth_manager.PROVIDERS. Unknown providers get 404.
_PROVIDER_DOC = "'onedrive' or 'gdrive'"


@api.route('/<string:provider>/start/')
@api.param('provider', _PROVIDER_DOC)
class OauthStart(Resource):
    @api.marshal_with(start_dto)
    def post(self, provider):
        """Start signing in; open `authorize_url` in a new tab"""
        return _call(oauth_manager.start, provider), 200


@api.route('/<string:provider>/finish/')
@api.param('provider', _PROVIDER_DOC)
class OauthFinish(Resource):
    @api.expect(finish_dto, validate=True)
    @api.marshal_with(flow_dto)
    def post(self, provider):
        """Complete the sign-in with the address the browser was redirected to, list the drives"""
        return _call(oauth_manager.finish, provider, request.json), 200


@api.route('/<string:provider>/callback')
@api.param('provider', _PROVIDER_DOC)
class OauthCallback(Resource):
    @api.doc(security=[])
    def get(self, provider):
        """Redirect target of an own app registration / OAuth client (no Motuz login on a redirect)"""
        return redirect(_call(oauth_manager.callback, provider, request.args))


@api.route('/<string:provider>/flows/<string:state>/')
@api.param('provider', _PROVIDER_DOC)
class OauthFlow(Resource):
    @api.marshal_with(flow_dto)
    def get(self, provider, state):
        """Drives of a sign-in completed through the callback"""
        return _call(oauth_manager.retrieve, provider, state), 200


@api.route('/<string:provider>/connect/')
@api.param('provider', _PROVIDER_DOC)
class OauthConnect(Resource):
    @api.expect(connect_dto, validate=True)
    @api.marshal_with(connection_dto, code=201)
    def post(self, provider):
        """Create the connection for the chosen drive"""
        return _call(oauth_manager.connect, provider, request.json), 201
