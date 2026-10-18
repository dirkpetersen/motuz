import logging

from flask import request
from flask_restx import Resource, Namespace, fields

from ..exceptions import HTTP_EXCEPTION, public_error
from ..managers import schedule_manager
from .copy_job_views import EgressEstimate, Performance

api = Namespace('copy-schedules', description='Scheduled repeating copies (README "Scheduled copies")')


class ScheduleEgressEstimate(EgressEstimate):
    """
    The AWS egress cost the user confirmed for a schedule out of AWS: the first run's
    estimate (GET /copy-jobs/egress-estimate/) with confirmed: true, plus
    repeat_confirmed: true and the expected new data per run (new_bytes_per_run).
    Required for a schedule that reads out of AWS (else 400).
    """
    __schema_example__ = {'confirmed': True, 'repeat_confirmed': True, 'bytes': 340000000000,
                          'files': 1200, 'new_bytes_per_run': 34000000000}


schedule_dto = api.model('copy-schedule', {
    'id': fields.Integer(readonly=True, example=7),
    'description': fields.String(required=False, example='Nightly copy of the sequencer output'),
    'src_cloud_id': fields.Integer(required=False, example=1),
    'src_resource_path': fields.String(required=True, example='/data/run'),
    'dst_cloud_id': fields.Integer(required=False, example=2),
    'dst_resource_path': fields.String(required=True, example='/bucket/run'),
    'copy_links': fields.Boolean(required=False, example=False),
    'notification_email': fields.String(required=False, example='hello@example.com'),
    'performance': Performance(required=False, example={'transfers': 32}),

    # When it runs, all in UTC (utils/schedule.py)
    'frequency': fields.String(required=True, enum=['hourly', 'daily', 'weekly', 'monthly'], example='daily'),
    'minute': fields.Integer(required=True, min=0, max=59, example=30),
    'hour': fields.Integer(required=False, min=0, max=23, description='UTC; daily, weekly and monthly', example=2),
    'weekday': fields.Integer(required=False, min=0, max=6, description='0 = Monday ... 6 = Sunday (UTC); weekly', example=0),
    'day_of_month': fields.Integer(required=False, min=1, max=28, description='UTC; monthly', example=1),
    'schedule_text': fields.String(readonly=True, example='every day at 02:30 UTC'),

    'enabled': fields.Boolean(readonly=True, example=True),
    'next_run_at': fields.String(readonly=True, attribute='next_run_at_utc', example='2026-10-19T02:30:00Z'),
    'last_run_at': fields.String(readonly=True, attribute='last_run_at_utc', example='2026-10-18T02:30:00Z'),
    'last_job_id': fields.Integer(readonly=True, example=1234),
    'last_status': fields.String(readonly=True, example='Started job 1234 at 2026-10-18 02:30 UTC'),
    'skipped_runs': fields.Integer(readonly=True, example=0),
    'egress_estimate': ScheduleEgressEstimate(required=False),
    'owner': fields.String(readonly=True, example='alice'),
    'created_at': fields.String(readonly=True, attribute='created_at_utc', example='2026-10-18T01:00:00Z'),
})

create_dto = api.clone('copy-schedule-create', schedule_dto, {
    'run_now': fields.Boolean(required=False, example=True,
                              description='Also start the first run now (else at the next scheduled time)'),
})

update_dto = api.model('copy-schedule-update', {
    'enabled': fields.Boolean(required=False, example=False, description='false pauses, true resumes'),
})


@api.route('/')
class CopyScheduleList(Resource):
    @api.marshal_list_with(schedule_dto)
    def get(self):
        """
        List the schedules of the logged in user
        """
        try:
            return schedule_manager.list()
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, public_error(e))

    @api.expect(create_dto, validate=True)
    @api.marshal_with(schedule_dto, code=201)
    def post(self):
        """
        Create a schedule: its runs are copy jobs that only add new files (nothing is
        deleted or overwritten unless the server allows overwriting everywhere)
        """
        try:
            return schedule_manager.create(request.json), 201
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, public_error(e))


@api.route('/<int:id>')
@api.param('id', 'The Copy Schedule Identifier')
@api.response(404, 'Copy Schedule not found.')
class CopySchedule(Resource):
    @api.marshal_with(schedule_dto, code=200)
    def get(self, id):
        """
        Get a schedule
        """
        try:
            return schedule_manager.retrieve(id), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, public_error(e))

    @api.expect(update_dto, validate=True)
    @api.marshal_with(schedule_dto, code=200)
    def patch(self, id):
        """
        Pause ({"enabled": false}) or resume ({"enabled": true}) a schedule
        """
        try:
            return schedule_manager.update_schedule(id, request.json), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, public_error(e))

    @api.marshal_with(schedule_dto, code=200)
    def delete(self, id):
        """
        Delete a schedule (its copy jobs stay)
        """
        try:
            return schedule_manager.delete(id), 200
        except HTTP_EXCEPTION as e:
            api.abort(e.code, e.payload)
        except Exception as e:
            logging.exception(e, exc_info=True)
            api.abort(500, public_error(e))
