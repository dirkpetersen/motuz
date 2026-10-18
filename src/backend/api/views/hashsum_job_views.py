import logging
import random

from flask import request
from flask_restx import Resource, Namespace, fields

from ..managers import egress_manager, hashsum_job_manager
from .. import tasks
from ..exceptions import HTTP_EXCEPTION
from .copy_job_views import EgressEstimate, Performance, connection_id_arg


api = Namespace('hashsum-jobs', description='CheckJob related operations')

dto = api.model('hashsum-job', {
    'id': fields.String(readonly=True, example='a6cac16a63d05672555c884d38b8a3'),
    'src_cloud_id': fields.Integer(required=False, example=1),
    'src_resource_path': fields.String(required=True, example='/tmp'),
    'dst_cloud_id': fields.Integer(required=False, example=2),
    'dst_resource_path': fields.String(required=True, example='/temp'),

    'option_download': fields.Boolean(required=True, example=True),
    'notification_email': fields.String(required=False, example='hello@example.com'),
    'performance': Performance(required=False, example={'checkers': 64}), # only checkers
    'egress_estimate': EgressEstimate(required=False), # with option_download from AWS

    'progress_state': fields.String(readonly=True, example='PENDING'),
    'progress_current': fields.Integer(readonly=True, example=45),
    'progress_total': fields.Integer(readonly=True, example=100),
    'progress_execution_time': fields.Integer(readonly=True, example=3600),
    # Where the job runs (managers/worker_manager.annotate_location): 'central' (this
    # node) or a remote pool, and e.g. "starting worker (c7gn.2xlarge)"
    'pool': fields.String(readonly=True, example='aws'),
    'pool_status': fields.String(readonly=True, example='running on c7gn.2xlarge'),
    'progress_error': fields.String(readonly=True),

    'progress_src_tree': fields.String(readonly=True, example='[{title, children}]'),
    'progress_dst_tree': fields.String(readonly=True, example='[{title, children}]'),

    # Rabbitmq fields
    'progress_src_error_text': fields.String(readonly=True, example='Multi\nLine\nText'),
    'progress_dst_error_text': fields.String(readonly=True, example='Multi\nLine\nText'),
})


@api.route('/egress-estimate/')
class HashsumJobEgressEstimate(Resource):
    def get(self):
        """
        The AWS egress cost of an integrity check with download (New Hashsum Job
        dialog): every side on AWS is read out of it. Query: src_cloud_id,
        src_resource_path (repeatable), dst_cloud_id, dst_resource_path (repeatable);
        the answer as for /copy-jobs/egress-estimate/
        """
        src_cloud_id = connection_id_arg('src_cloud_id')
        dst_cloud_id = connection_id_arg('dst_cloud_id')
        try:
            return egress_manager.hashsum_estimate(
                src_cloud_id, request.args.getlist('src_resource_path'),
                dst_cloud_id, request.args.getlist('dst_resource_path'))
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


@api.route('/')
class HashsumJobList(Resource):

    @api.marshal_list_with(dto)
    def get(self):
        """
        List all Check Jobs
        """
        try:
            return hashsum_job_manager.list()
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))


    @api.expect(dto, validate=True)
    @api.marshal_with(dto, code=201)
    def post(self):
        """
        Create a new Check Job
        """
        try:
            return hashsum_job_manager.create(request.json), 201
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))



@api.route('/<int:id>')
@api.param('id', 'The Check Job Identifier')
@api.response(404, 'Check Job not found.')
class HashsumJob(Resource):

    @api.marshal_with(dto, code=200)
    def get(self, id):
        """
        Get a specific Check Job
        """
        try:
            return hashsum_job_manager.retrieve(id), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))



@api.route('/<int:id>/stop/')
@api.param('id', 'The Check Job Identifier')
@api.response(404, 'Check Job not found.')
class HashsumJob(Resource):

    @api.marshal_with(dto, code=202)
    def put(self, id):
        """
        Stop the Check Job
        """
        try:
            return hashsum_job_manager.stop(id), 202
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, str(e))
