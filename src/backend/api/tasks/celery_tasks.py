import contextlib
import logging
import os
import signal
import time
import json

from .. import celery
from ..models import CopyJob, HashsumJob
from ..application import db

from ..utils.rclone_connection import RcloneConnection
from ..utils.email_utils import Email
from ..utils import job_runner


@contextlib.contextmanager
def _terminate_rclone_on_sigterm(connection):
    """
    Stopping a job revokes its task with terminate=True, which only sends SIGTERM to
    the worker process. rclone runs in its own process group, so kill it explicitly
    and then let the original SIGTERM handling proceed.
    """
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except ValueError: # Not in the main thread
        yield
        return

    def handler(signum, frame):
        connection.terminate_all()
        signal.signal(signal.SIGTERM, previous)
        os.kill(os.getpid(), signum)

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError: # Not in the main thread
        yield
        return

    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@celery.task(name='motuz.api.tasks.copy_job', bind=True)
def copy_job(self, task_id=None):
    try:
        start_time = time.time()

        copy_job = db.session.get(CopyJob, task_id)
        copy_job.progress_state = 'PROGRESS'
        db.session.commit()

        connection = RcloneConnection()
        with _terminate_rclone_on_sigterm(connection):
            exitstatus = _copy_job_run(self, copy_job, connection, task_id, start_time)

        if exitstatus == -1:
            logging.error("Copy Job did not set its status")
        copy_job.progress_state = job_runner.exit_state(exitstatus)

        copy_job.progress_current = 100
        copy_job.progress_execution_time = int(time.time() - start_time)
        db.session.commit()

        outcome = 'COMPLETED successfully' if copy_job.progress_state == 'SUCCESS' else 'FAILED'
        Email.send_notification(
            to=copy_job.notification_email,
            subject=f'Motuz Copy Job with ID {task_id} {outcome}!'
        )

        return {
            'text': connection.copy_text(task_id),
            'error_text': connection.copy_error_text(task_id)
        }
    except Exception as e:
        logging.exception(e)

        try:
            db.session.rollback()
            copy_job.progress_current = 100
            copy_job.progress_state = 'FAILED'
        except:
            pass

        try:
            copy_job.progress_execution_time = int(time.time() - start_time)
        except:
            pass

        try:
            db.session.commit()
        except:
            pass

        try:
            Email.send_notification(
                to=copy_job.notification_email,
                subject=f'Motuz Copy Job with ID {task_id} FAILED!'
            )
        except:
            pass

        return {
            'text': '',
            'error_text': str(e),
        }


def _copy_job_run(self, copy_job, connection, task_id, start_time):
    """Runs the copy, writing its progress once per second; returns rclone's exit status"""
    connection.copy(
        src_data=copy_job.src_cloud,
        src_resource_path=copy_job.src_resource_path,
        dst_data=copy_job.dst_cloud,
        dst_resource_path=copy_job.dst_resource_path,
        user=copy_job.owner,
        copy_links=copy_job.copy_links,
        job_id=task_id,
        performance=copy_job.performance,
    )

    def tick(percent, text, error_text):
        copy_job.progress_current = percent
        copy_job.progress_execution_time = int(time.time() - start_time)
        db.session.commit()

        self.update_state(state='PROGRESS', meta={
            'text': text,
            'error_text': error_text,
        })

    return job_runner.watch_copy(connection, task_id, tick)


@celery.task(name='motuz.api.tasks.hashsum_job', bind=True)
def hashsum_job(self, task_id):
    """
    @return : dict {
        "progress_src_tree",
        "progress_src_error_text",
        "progress_dst_tree",
        "progress_dst_error_text",
    }
    """
    try:
        start_time = time.time()

        hashsum_job = db.session.get(HashsumJob, task_id)
        hashsum_job.progress_state = 'PROGRESS'
        db.session.commit()

        connection = RcloneConnection()
        with _terminate_rclone_on_sigterm(connection):
            result = _hashsum_job_run(self, hashsum_job, connection, start_time)

        side = result.get('failed_side')
        if side is not None:
            if result['state'] == 'UNSET':
                logging.error("Hashsum Job did not set its status")
            hashsum_job.progress_state = result['state']
            hashsum_job.progress_current = 100
            hashsum_job.progress_execution_time = int(time.time() - start_time)
            setattr(hashsum_job, f'progress_{side}_error', result[f'{side}_error_text'])
            db.session.commit()
            Email.send_notification(
                to=hashsum_job.notification_email,
                subject=f'Motuz Integrity Check Job with ID {task_id} FAILED!'
            )
            return {
                f'progress_{side}_tree': result[f'{side}_tree'],
                f'progress_{side}_error_text': result[f'{side}_error_text'],
            }

        progress_src_tree = result['src_tree']
        progress_dst_tree = result['dst_tree']

        self.update_state(state='PROGRESS', meta={}) # Clearing rabbitmq

        hashsum_job.progress_state = 'SUCCESS'
        hashsum_job.progress_current = 100
        hashsum_job.progress_execution_time = int(time.time() - start_time)
        hashsum_job.progress_src_error = result['src_error_text']
        hashsum_job.progress_dst_error = result['dst_error_text']

        try:
            hashsum_job.progress_src_tree = json.dumps(progress_src_tree)
        except Exception as e:
            logging.error("Could not save progress_src_tree to DB")
            logging.exception(e)

        try:
            hashsum_job.progress_dst_tree = json.dumps(progress_dst_tree)
        except Exception as e:
            logging.error("Could not save progress_dst_tree to DB")
            logging.exception(e)

        db.session.commit()

        if len(progress_src_tree) == 0 and len(progress_dst_tree) == 0:
            integrity_outcome_message = "Files are IDENTICAL!"
        else:
            integrity_outcome_message = "Files are DIFFERENT!"

        Email.send_notification(
            to=hashsum_job.notification_email,
            subject=f'Motuz Integrity Check Job with ID {task_id} completed! {integrity_outcome_message}'
        )

        return {}

    except Exception as e:
        logging.exception(e)

        try:
            db.session.rollback()
            hashsum_job.progress_current = 100
            hashsum_job.progress_state = 'FAILED'
        except:
            pass

        try:
            hashsum_job.progress_execution_time = int(time.time() - start_time)
        except:
            pass

        try:
            db.session.commit()
        except:
            pass

        try:
            Email.send_notification(
                to=hashsum_job.notification_email,
                subject=f'Motuz Integrity Check Job with ID {task_id} FAILED!'
            )
        except:
            pass

        return {
            'error_text': str(e),
        }


@celery.task(name='motuz.api.tasks.ec2_dispatch')
def ec2_dispatch():
    """
    Launches temporary EC2 workers for queued jobs of the EC2 pool (managers/
    ec2_launcher.py) right after such a job was created, so that creating the job does
    not wait for AWS. The reaper loop (manage.py ec2 reap --loop) does the same every
    MOTUZ_EC2_REAP_INTERVAL, e.g. while this task waits behind long copy jobs.
    """
    from ..managers import ec2_launcher
    try:
        launched = ec2_launcher.dispatch()
        return {'launched': [row.instance_id or row.client_token for row in launched]}
    finally:
        db.session.remove()


def _hashsum_job_run(self, hashsum_job, connection, start_time):
    """md5sum of both sides (job_runner.run_hashsum), writing progress once per second"""
    def start_side(side, run_id):
        connection.md5sum(
            data=getattr(hashsum_job, f'{side}_cloud'),
            resource_path=getattr(hashsum_job, f'{side}_resource_path'),
            user=hashsum_job.owner,
            job_id=run_id,
            download=hashsum_job.option_download,
            performance=hashsum_job.performance,
        )

    def tick(side, percent, tree, error_text):
        hashsum_job.progress_current = percent
        hashsum_job.progress_execution_time = int(time.time() - start_time)
        db.session.commit()

        self.update_state(state='PROGRESS', meta={
            f'progress_{side}_tree': tree(),
            f'progress_{side}_error_text': error_text,
        })

    return job_runner.run_hashsum(connection, hashsum_job.id, start_side, tick)
