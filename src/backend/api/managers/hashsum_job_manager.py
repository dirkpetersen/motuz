import json
import logging

from flask import request

from .. import tasks
from ..application import db
from ..exceptions import *
from ..models import HashsumJob
from ..managers.auth_manager import token_required, get_logged_in_user
from ..managers import cloud_connection_manager, egress_manager, job_routing, worker_manager
from ..utils import rclone_tuning


@token_required
def list(page_size=50, offset=0):
    owner = get_logged_in_user(request)
    worker_manager.expire_leases()

    hashsum_jobs = (HashsumJob.query
        .filter_by(owner=owner)
        .order_by(HashsumJob.id.desc())
        .limit(page_size)
        .all()
    )
    return worker_manager.annotate_location('hashsum', hashsum_jobs)


@token_required
def create(data):
    owner = get_logged_in_user(request)
    # The logged in user's connections (404 for anyone else's), None: local filesystem
    src_cloud = cloud_connection_manager.retrieve(data['src_cloud_id']) if data.get('src_cloud_id') else None
    dst_cloud = cloud_connection_manager.retrieve(data['dst_cloud_id']) if data.get('dst_cloud_id') else None

    hashsum_job = HashsumJob(**{
        'src_cloud_id': src_cloud.id if src_cloud is not None else None,
        'src_resource_path': data.get('src_resource_path', None),
        'dst_cloud_id': dst_cloud.id if dst_cloud is not None else None,
        'dst_resource_path': data.get('dst_resource_path', None),

        'option_download': data.get('option_download', None),
        'notification_email': data.get('notification_email', None),
        'performance': _validate_performance(data.get('performance')),
        'egress_estimate': egress_manager.confirmed_hashsum(
            src_cloud, dst_cloud, data.get('option_download'), data.get('egress_estimate')),

        'progress_current': 0,
        'progress_total': 100,
        'progress_state': "PROGRESS",
        'owner': owner
    })

    db.session.add(hashsum_job)
    db.session.commit()

    task_id = hashsum_job.id
    try:
        route = job_routing.route(hashsum_job.src_cloud, hashsum_job.src_resource_path, hashsum_job.dst_cloud)
        if route.pool == job_routing.CENTRAL:
            tasks.hashsum_job.apply_async(task_id=_celery_task_id(task_id), kwargs={
                'task_id': task_id,
            })
        else: # a remote worker of that pool claims it (managers/worker_manager.py)
            worker_manager.queue_job('hashsum', hashsum_job, route.pool, route)
    except Exception as e:
        # Otherwise the job would stay in PROGRESS forever
        hashsum_job.progress_state = 'FAILED'
        hashsum_job.progress_error = 'Could not queue the job: {}'.format(e)
        db.session.commit()
        raise

    worker_manager.annotate_location('hashsum', [hashsum_job])
    return hashsum_job


def _validate_performance(raw):
    """Integrity checks only take `checkers` (rclone md5sum's parallelism); None = default"""
    try:
        values = rclone_tuning.validate_overrides(raw, None, only=('checkers',))
    except rclone_tuning.TuningError as e:
        raise HTTP_400_BAD_REQUEST(str(e))
    return values or None


@token_required
def retrieve(id):
    hashsum_job = db.session.get(HashsumJob, id)

    if hashsum_job is None:
        raise HTTP_404_NOT_FOUND('Hashsum Job with id {} not found'.format(id))

    owner = get_logged_in_user(request)

    if hashsum_job.owner != owner:
        raise HTTP_404_NOT_FOUND('Hashsum Job with id {} not found'.format(id))

    worker_manager.expire_leases()
    if worker_manager.apply_remote_progress('hashsum', hashsum_job):
        for field in ('progress_src_tree', 'progress_dst_tree'):
            if getattr(hashsum_job, field, None) is None:
                setattr(hashsum_job, field, '[]')
        worker_manager.annotate_location('hashsum', [hashsum_job])
        return hashsum_job

    for _ in range(2): # Sometimes Rabbitmq closes the connection, just retry
        try:
            task = _async_result(hashsum_job.id)

            for field in (
                'progress_src_tree',
                'progress_dst_tree',
            ):
                value = task.info.get(field, None)
                if value is not None:
                    try:
                        value = json.dumps(value)
                    except:
                        logging.error("Could not parse {} as json".format(value))

                    setattr(hashsum_job, field, value)

                if getattr(hashsum_job, field, None) is None:
                    setattr(hashsum_job, field, '[]')

            for field in (
                'progress_src_error_text',
                'progress_dst_error_text',
            ):
                value = task.info.get(field, None)
                if value is not None:
                    setattr(hashsum_job, field, value)

            break
        except Exception:
            pass
    else:
        logging.error("Rabbitmq closed the connection. Failing silently")

    worker_manager.annotate_location('hashsum', [hashsum_job])
    return hashsum_job


@token_required
def stop(id):
    hashsum_job = retrieve(id)

    if not worker_manager.request_stop('hashsum', hashsum_job.id):
        task = _async_result(hashsum_job.id)
        task.revoke(terminate=True)

    hashsum_job = db.session.get(HashsumJob, id) # Avoid race conditions
    if hashsum_job.progress_state == 'PROGRESS':
        hashsum_job.progress_state = 'STOPPED'
        db.session.commit()

    worker_manager.annotate_location('hashsum', [hashsum_job])
    return hashsum_job


def _celery_task_id(job_id):
    # Copy and hashsum jobs have separate id sequences, so the Celery task id needs a
    # prefix: revoking copy job 4 must not discard hashsum job 4
    return 'hashsum-{}'.format(job_id)


def _async_result(job_id):
    task = tasks.hashsum_job.AsyncResult(_celery_task_id(job_id))
    if task.state == 'PENDING': # Unknown id: job created before the prefix existed
        legacy = tasks.hashsum_job.AsyncResult(str(job_id))
        if legacy.state != 'PENDING':
            return legacy
    return task
