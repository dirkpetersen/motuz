import json
import logging

from flask import request

from .. import tasks
from ..application import db
from ..exceptions import *
from ..models import HashsumJob
from ..managers.auth_manager import token_required, get_logged_in_user
from ..managers.cloud_connection_manager import owned_cloud_id


@token_required
def list(page_size=50, offset=0):
    owner = get_logged_in_user(request)

    hashsum_jobs = (HashsumJob.query
        .filter_by(owner=owner)
        .order_by(HashsumJob.id.desc())
        .limit(page_size)
        .all()
    )
    return hashsum_jobs


@token_required
def create(data):
    owner = get_logged_in_user(request)

    hashsum_job = HashsumJob(**{
        'src_cloud_id': owned_cloud_id(data.get('src_cloud_id')),
        'src_resource_path': data.get('src_resource_path', None),
        'dst_cloud_id': owned_cloud_id(data.get('dst_cloud_id')),
        'dst_resource_path': data.get('dst_resource_path', None),

        'option_download': data.get('option_download', None),
        'notification_email': data.get('notification_email', None),

        'progress_current': 0,
        'progress_total': 100,
        'progress_state': "PROGRESS",
        'owner': owner
    })

    db.session.add(hashsum_job)
    db.session.commit()

    task_id = hashsum_job.id
    try:
        tasks.hashsum_job.apply_async(task_id=str(task_id), kwargs={
            'task_id': task_id,
        })
    except Exception as e:
        # Otherwise the job would stay in PROGRESS forever
        hashsum_job.progress_state = 'FAILED'
        hashsum_job.progress_error = 'Could not queue the job: {}'.format(e)
        db.session.commit()
        raise

    return hashsum_job


@token_required
def retrieve(id):
    hashsum_job = db.session.get(HashsumJob, id)

    if hashsum_job is None:
        raise HTTP_404_NOT_FOUND('Hashsum Job with id {} not found'.format(id))

    owner = get_logged_in_user(request)

    if hashsum_job.owner != owner:
        raise HTTP_404_NOT_FOUND('Hashsum Job with id {} not found'.format(id))

    for _ in range(2): # Sometimes Rabbitmq closes the connection, just retry
        try:
            task = tasks.hashsum_job.AsyncResult(str(hashsum_job.id))

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

    return hashsum_job


@token_required
def stop(id):
    hashsum_job = retrieve(id)

    task = tasks.hashsum_job.AsyncResult(str(hashsum_job.id))
    task.revoke(terminate=True)

    hashsum_job = db.session.get(HashsumJob, id) # Avoid race conditions
    if hashsum_job.progress_state == 'PROGRESS':
        hashsum_job.progress_state = 'STOPPED'
        db.session.commit()

    return hashsum_job
