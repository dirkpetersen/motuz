import logging
import os

from flask import request

from .. import tasks
from ..application import db
from ..exceptions import *
from ..models import CopyJob
from ..managers.auth_manager import token_required, get_logged_in_user
from ..managers import cloud_connection_manager
from ..utils import rclone_tuning
from ..managers import job_routing, worker_manager
from ..utils.email_utils import Email


@token_required
def list(page_size=50, page=1):
    owner = get_logged_in_user(request)
    worker_manager.expire_leases()
    try:
        query = (CopyJob.query
                 .filter_by(owner=owner)
                 .order_by(CopyJob.id.desc())
                 .paginate(page=page,
                           per_page=page_size,
                           error_out=False)
                 )
    except Exception as e:
        logging.exception(e, exc_info=True)
        for address in os.environ.get('MOTUZ_ALERT_ADDRESS', '').split(','):
            Email.send_notification(
                to=address.strip() or None,
                subject='Motuz: Error listing copy jobs',
                body=str(e),
            )
        raise HTTP_500_INTERNAL_SERVER_ERROR(str(e))

    return {
        'data': worker_manager.annotate_location('copy', query.items),
        'total': query.total,
        'page': query.page,
        'pages': query.pages
    }


def _owned_cloud(cloud_id):
    """
    The connection with this id if the logged in user owns it (else 404), None for the
    local filesystem (None or 0)
    """
    if not cloud_id:
        return None
    return cloud_connection_manager.retrieve(cloud_id)


def validate_performance(raw, dst_cloud):
    """
    The job's validated rclone performance overrides as stored (None = the defaults),
    or 400 with a message for the user
    """
    dst_type = dst_cloud.type if dst_cloud is not None else None
    try:
        values = rclone_tuning.validate_overrides(raw, dst_type)
    except rclone_tuning.TuningError as e:
        raise HTTP_400_BAD_REQUEST(str(e))
    return rclone_tuning.to_api(values) or None


@token_required
def performance(dst_cloud_id=None):
    """Fields, limits, presets and memory budget for a destination (New Copy Job dialog)"""
    dst_cloud = _owned_cloud(dst_cloud_id)
    return rclone_tuning.describe(dst_cloud.type if dst_cloud is not None else None)


@token_required
def create(data):
    owner = get_logged_in_user(request)

    src_cloud = _owned_cloud(data.get('src_cloud_id'))
    dst_cloud = _owned_cloud(data.get('dst_cloud_id'))

    copy_job = CopyJob(**{
        'description': data.get('description', None),
        'src_cloud_id': src_cloud.id if src_cloud is not None else None,
        'src_resource_path': data.get('src_resource_path', None),
        'dst_cloud_id': dst_cloud.id if dst_cloud is not None else None,
        'dst_resource_path': data.get('dst_resource_path', None),

        'copy_links': data.get('copy_links', None),
        'notification_email': data.get('notification_email', None),
        'performance': validate_performance(data.get('performance'), dst_cloud),

        'progress_current': 0,
        'progress_total': 100,
        'progress_state': "PROGRESS",
        'owner': owner
    })

    db.session.add(copy_job)
    db.session.commit()

    task_id = copy_job.id
    try:
        route = job_routing.route(copy_job.src_cloud, copy_job.src_resource_path, copy_job.dst_cloud)
        if route.pool == job_routing.CENTRAL:
            tasks.copy_job.apply_async(task_id=_celery_task_id(task_id), kwargs={
                'task_id': task_id,
            })
        else: # a remote worker of that pool claims it (managers/worker_manager.py)
            worker_manager.queue_job('copy', copy_job, route.pool, route)
    except Exception as e:
        # Otherwise the job would stay in PROGRESS forever
        copy_job.progress_state = 'FAILED'
        copy_job.progress_error = 'Could not queue the job: {}'.format(e)
        db.session.commit()
        raise

    worker_manager.annotate_location('copy', [copy_job])
    return copy_job


@token_required
def retrieve(id):
    copy_job = db.session.get(CopyJob, id)

    if copy_job is None:
        raise HTTP_404_NOT_FOUND('Copy Job with id {} not found'.format(id))

    owner = get_logged_in_user(request)

    if copy_job.owner != owner:
        raise HTTP_404_NOT_FOUND('Copy Job with id {} not found'.format(id))

    worker_manager.expire_leases()
    if worker_manager.apply_remote_progress('copy', copy_job):
        worker_manager.annotate_location('copy', [copy_job])
        return copy_job

    for _ in range(2):  # Sometimes rabbitmq closes the connection!
        try:
            task = _async_result(copy_job.id)
            copy_job.progress_text = task.info.get('text', '')
            copy_job.progress_error_text = task.info.get('error_text', '')
            break
        except Exception:
            pass
    else:
        logging.error("Rabbitmq closed the connection. Failing silently")

    worker_manager.annotate_location('copy', [copy_job])
    return copy_job


@token_required
def stop(id):
    copy_job = retrieve(id)

    if not worker_manager.request_stop('copy', copy_job.id):
        task = _async_result(copy_job.id)
        task.revoke(terminate=True)

    copy_job = db.session.get(CopyJob, id)  # Avoid race conditions
    if copy_job.progress_state == 'PROGRESS':
        copy_job.progress_state = 'STOPPED'
        db.session.commit()

    worker_manager.annotate_location('copy', [copy_job])
    return copy_job


def _celery_task_id(job_id):
    # Copy and hashsum jobs have separate id sequences, so the Celery task id needs a
    # prefix: revoking copy job 4 must not discard hashsum job 4
    return 'copy-{}'.format(job_id)


def _async_result(job_id):
    task = tasks.copy_job.AsyncResult(_celery_task_id(job_id))
    if task.state == 'PENDING': # Unknown id: job created before the prefix existed
        legacy = tasks.copy_job.AsyncResult(str(job_id))
        if legacy.state != 'PENDING':
            return legacy
    return task
