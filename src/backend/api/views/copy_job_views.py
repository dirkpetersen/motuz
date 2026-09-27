import logging

from flask import request
from flask_restx import Resource, Namespace, fields, reqparse

from ..exceptions import HTTP_EXCEPTION
from ..managers import copy_job_manager

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
