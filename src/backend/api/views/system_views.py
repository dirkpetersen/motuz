import logging

from flask import request
from flask_restx import Resource, Namespace, fields

from ..managers import system_manager
from ..exceptions import HTTP_EXCEPTION


api = Namespace('system', description='System related operations')


dto = api.model('system', {
    'connection_id': fields.Integer(required=True, example=2),
    'path': fields.String(required=True, example='/usr/bin/'),
})


@api.route('/files/')
class SystemFiles(Resource):
    @api.expect(dto, validate=True)
    def post(self):
        """
        List all files for a particular URI.
        """
        try:
            return system_manager.ls(request.json), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/files/home/')
class SystemFilesHome(Resource):
    def post(self):
        """
        List all files for local home
        """
        try:
            return system_manager.lshome(), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


view_dto = api.model('system-file-view', {
    'path': fields.String(example='/home/alice/notes.txt'),
    'content': fields.String(description='The text, at most the first 1 MiB'),
    'truncated': fields.Boolean(description='True if the file is larger than what is returned'),
    'size': fields.Integer(description='Size of the whole file in bytes'),
    'encoding': fields.String(example='utf-8'),
})


@api.route('/files/view/')
class SystemFilesView(Resource):
    @api.expect(dto, validate=True)
    @api.marshal_with(view_dto, code=200)
    @api.response(403, 'The user cannot read the file')
    @api.response(404, 'No such file (or connection)')
    @api.response(415, 'Not a text file')
    def post(self):
        """
        The first 1 MiB of a text file, read as the logged-in user (read-only viewer).
        """
        try:
            return system_manager.view(request.json), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/files/mkdir/')
class SystemFilesMkdir(Resource):
    @api.expect(dto, validate=True)
    def post(self):
        """
        Crete a new directory at a particular URI.
        """
        try:
            return system_manager.mkdir(request.json), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))



@api.route('/uid/')
class SystemUid(Resource):
    def get(self):
        """
        Get the `uid` of the currently logged in user.
        """
        try:
            return system_manager.get_uid(), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/info/')
class SystemUid(Resource):
    def get(self):
        """
        Get information about current system
        """
        try:
            return system_manager.get_info(), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))
