import logging

from flask import Response, request
from flask_restx import Resource, Namespace, fields

from ..managers import system_manager
from ..exceptions import HTTP_EXCEPTION
from ..utils import image_view


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


chunk_dto = api.model('system-file-view-chunk-request', {
    'connection_id': fields.Integer(required=True, example=0, description='0 for the local filesystem'),
    'path': fields.String(required=True, example='/home/alice/big.log'),
    'offset': fields.Integer(description='Read forward from this byte (default 0)'),
    'before': fields.Integer(description='Read the chunk that ends at this byte (backward, the previous chunk)'),
    'from_end': fields.Boolean(description='Read the last chunk of the file (tail)'),
    'length': fields.Integer(description='At most this many bytes (256 to 262144, the default)'),
    'follow': fields.Boolean(description='Follow mode (with offset): an incomplete last line is withheld, and '
                                         'an offset beyond the end returns the new (smaller) size instead of 400'),
})

chunk_result_dto = api.model('system-file-view-chunk', {
    'path': fields.String(example='/home/alice/big.log'),
    'content': fields.String(description='Whole lines of text (a line longer than a chunk is split)'),
    'offset': fields.Integer(description='First byte of the file the content covers'),
    'end': fields.Integer(description='Byte after the last byte the content covers'),
    'size': fields.Integer(description='Size of the whole file in bytes'),
    'bof': fields.Boolean(description='The content starts at the beginning of the file'),
    'eof': fields.Boolean(description='The content ends at the end of the file'),
    'encoding': fields.String(example='utf-8'),
})


@api.route('/files/view/chunk/')
class SystemFilesViewChunk(Resource):
    @api.expect(chunk_dto, validate=True)
    @api.marshal_with(chunk_result_dto, code=200)
    @api.response(400, 'Invalid offset or length, a folder, not a regular file')
    @api.response(403, 'The user cannot read the file')
    @api.response(404, 'No such file (or connection)')
    @api.response(415, 'Not a text file')
    def post(self):
        """
        One chunk of whole lines of a text file, read as the logged-in user (the pager of the viewer).
        """
        try:
            return system_manager.view_chunk(request.json), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/files/view/image/')
class SystemFilesViewImage(Resource):
    @api.expect(dto, validate=True)
    @api.produces(list(image_view.TYPES))
    @api.response(200, 'The image (image/png, image/jpeg, image/gif or image/webp)')
    @api.response(400, 'A folder, not a regular file, a relative local path')
    @api.response(403, 'The user cannot read the file')
    @api.response(404, 'No such file (or connection)')
    @api.response(413, 'Larger than MOTUZ_VIEW_IMAGE_MAX_BYTES')
    @api.response(415, 'Not a PNG, JPEG, GIF or WebP image (SVG is refused)')
    def post(self):
        """
        The bytes of a PNG, JPEG, GIF or WebP image, read as the logged-in user (image viewer).
        The type is detected from the file's first bytes, never from its name.
        """
        try:
            content, mime, path = system_manager.view_image(request.json)
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))
        return Response(content, status=200, headers=image_view.response_headers(path, mime),
                        direct_passthrough=True)


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
