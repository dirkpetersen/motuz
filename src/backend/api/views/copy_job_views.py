import logging

from flask import request
from flask_restx import Resource, Namespace, fields, reqparse

from ..exceptions import HTTP_EXCEPTION
from ..managers import copy_job_manager, upload_manager

api = Namespace('copy-jobs', description='CopyJob related operations')


class Performance(fields.Raw):
    """
    rclone performance overrides of a job, e.g. {"transfers": 32, "s3_chunk_size": "64M"}.
    Only the names in utils/rclone_tuning.PER_JOB; the manager validates them. null or {}:
    the server's defaults.
    """
    __schema_type__ = ['object', 'null']
    __schema_example__ = {'transfers': 32, 'checkers': 64}

job_dto = api.model('copy-job', {
    'id': fields.Integer(readonly=True, example=1234),
    'description': fields.String(required=True, example='Task Description'),
    'src_cloud_id': fields.Integer(required=False, example=1),
    'src_resource_path': fields.String(required=True, example='/tmp'),
    'dst_cloud_id': fields.Integer(required=False, example=2),
    'dst_resource_path': fields.String(required=True, example='/trash'),

    'copy_links': fields.Boolean(required=True, example=True),
    'notification_email': fields.String(required=False, example='hello@example.com'),
    'performance': Performance(required=False, example={'transfers': 32, 'multi_thread_streams': 16, 's3_chunk_size': '64Mi'}),

    'owner': fields.String(required=False, example='owner'),

    'progress_state': fields.String(readonly=True, example='PENDING'),
    'progress_text': fields.String(readonly=True, example='Multi\nLine\nText'),
    'progress_error_text': fields.String(readonly=True, example='Multi\nLine\nText'),
    'progress_current': fields.Integer(readonly=True, example=45),
    'progress_total': fields.Integer(readonly=True, example=100),
    'progress_error': fields.String(readonly=True),
    'progress_execution_time': fields.Integer(readonly=True, example=3600),
    # Where the job runs (managers/worker_manager.annotate_location): 'central' (this
    # node) or a remote pool, and e.g. "starting worker (c7gn.2xlarge)"
    'pool': fields.String(readonly=True, example='aws'),
    'pool_status': fields.String(readonly=True, example='running on c7gn.2xlarge'),
    # Set for an upload from the browser (managers/upload_manager.py)
    'upload': fields.Raw(readonly=True, example={'files': 3, 'bytes': 1048576, 'mode': 'skip', 'name': 'photos/'}),
})

upload_dto = api.model('copy-job-upload', {
    'job': fields.Nested(job_dto),
    'pending': fields.List(fields.Integer, description='Indexes of the files to send, in order'),
    'chunk_bytes': fields.Integer(description='Bytes per chunk', example=8388608),
})

list_dto = api.model('copy-job-list', {
    'data': fields.List(fields.Nested(job_dto)),
    'total': fields.Integer(example=100),
    'page': fields.Integer(example=1),
    'pages': fields.Integer(example=10)
})

performance_parser = reqparse.RequestParser()
performance_parser.add_argument('dst_cloud_id', help='Destination connection (none or 0: local filesystem)', type=int, default=0)

list_arg_parser = reqparse.RequestParser()
list_arg_parser.add_argument('page', help='Current page', type=int, default=1)
list_arg_parser.add_argument('page_size', help='Maximum number of records to return per page', type=int, default=50)


@api.route('/')
class CopyJobList(Resource):
    @api.marshal_list_with(list_dto)
    @api.expect(list_arg_parser)
    def get(self):
        """
        List all Copy Jobs
        """
        try:
            return copy_job_manager.list(
                page_size=int(request.args.get('page_size', 50)),
                page=int(request.args.get('page', 1))
            )
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))

    @api.expect(job_dto, validate=True)
    @api.marshal_with(job_dto, code=201)
    def post(self):
        """
        Create a new Copy Job
        """
        try:
            return copy_job_manager.create(request.json), 201
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/performance/')
class CopyJobPerformance(Resource):
    @api.expect(performance_parser)
    def get(self):
        """
        Performance fields, their limits, the presets and the memory budget of this
        server for a destination connection (New Copy Job dialog)
        """
        dst_cloud_id = request.args.get('dst_cloud_id') or '0'
        if not dst_cloud_id.isdigit():
            api.abort(400, 'dst_cloud_id must be a connection id')
        try:
            return copy_job_manager.performance(int(dst_cloud_id))
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/<int:id>')
@api.param('id', 'The Copy Job Identifier')
@api.response(404, 'Copy Job not found.')
class CopyJob(Resource):
    @api.marshal_with(job_dto, code=200)
    def get(self, id):
        """
        Get a specific Copy Job
        """
        try:
            return copy_job_manager.retrieve(id), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/<int:id>/stop/')
@api.param('id', 'The Copy Job Identifier')
@api.response(404, 'Copy Job not found.')
class CopyJob(Resource):
    @api.marshal_with(job_dto, code=202)
    def put(self, id):
        """
        Stop the Copy Job
        """
        try:
            return copy_job_manager.stop(id), 202
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


def _abort(e):
    """api.abort with the manager's message, or its fields if it gave a dict"""
    if isinstance(e.payload, dict):
        api.abort(e.code, **e.payload)
    api.abort(e.code, e.payload)


@api.route('/uploads/conflicts/')
class UploadConflicts(Resource):
    def post(self):
        """
        Which of the top-level names of a drop from the desktop exist in the destination
        folder: {dst_cloud_id, dst_resource_path, names} -> {existing}
        """
        try:
            return upload_manager.conflicts(request.get_json(silent=True) or {})
        except HTTP_EXCEPTION as e:
            _abort(e)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/uploads/')
class Uploads(Resource):
    @api.marshal_with(upload_dto, code=201)
    def post(self):
        """
        Create an upload from the browser: {dst_cloud_id, dst_resource_path, mode
        ('overwrite' or 'skip'), files: [{path, size}]}. The browser then sends the
        pending files with PUT /copy-jobs/<id>/uploads/<index>.
        """
        try:
            return upload_manager.create(request.get_json(silent=True) or {}), 201
        except HTTP_EXCEPTION as e:
            _abort(e)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/<int:id>/uploads/<int:index>')
@api.param('id', 'The Copy Job Identifier')
@api.param('index', 'The index of the file in the upload')
class UploadChunk(Resource):
    def put(self, id, index):
        """
        One chunk of a file of an upload (the body, application/octet-stream) at
        ?offset=<bytes>; offset 0 starts the file
        """
        offset = request.args.get('offset', '')
        if not offset.isdigit():
            api.abort(400, 'offset must be a number of bytes')
        if request.content_length is None and 'chunked' in request.headers.get('Transfer-Encoding', ''):
            api.abort(411, 'Content-Length is required')
        try:
            return upload_manager.put_chunk(id, index, int(offset), request.content_length or 0, request.stream)
        except HTTP_EXCEPTION as e:
            _drain_chunk()
            _abort(e)
        except Exception as e:
            logging.exception(e, exc_info=True)
            _drain_chunk()
            api.abort(500, str(e))


def _drain_chunk():
    """
    Reads the rest of a refused chunk: an answer before the body is read ends the
    connection, and the browser sees a network error instead of e.g. 409 (job stopped)
    """
    if (request.content_length or 0) > upload_manager.MAX_CHUNK_BYTES:
        return
    try:
        while request.stream.read(1 << 20):
            pass
    except Exception:
        pass
