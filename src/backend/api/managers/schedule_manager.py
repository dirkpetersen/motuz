"""
Scheduled repeating copies (README "Scheduled copies"): a copy that runs every hour,
day, week or month (utils/schedule.py, all times UTC).

- Every run is a normal copy job made by copy_job_manager.create_for with the
  schedule's owner and parameters: the same ownership checks (404), free space check,
  routing to pools (remote workers unchanged) and performance validation.
- A run only adds new files: copyto never deletes, and the job's `overwrite` is False
  (rclone --ignore-existing) whatever the overwrite policy says for the destination,
  unless MOTUZ_ALLOW_GENERAL_OVERWRITES is true. Kept files are reported as usual.
- A schedule that reads out of AWS (managers/egress_manager.py, also depending on
  where the job runs) needs the confirmation of the first run's estimate and of the
  repeated runs (`repeat_confirmed`) when it is created (else 400); every run's job
  stores the schedule's estimate with `schedule_id`, without a new confirmation.
- The runner (`manage.py schedules run --loop`, started next to the Celery worker:
  deployment/docker/celery/celery-entrypoint.sh, bin/systemd/celery_worker.sh) calls
  tick() every minute. tick() claims due schedules with SELECT ... FOR UPDATE SKIP
  LOCKED and a compare-and-set of next_run_at (atomic on SQLite too), so a schedule
  runs once per due time even with two runners. A run whose previous job is still
  running is skipped (not stacked) and counted. A runner that was down runs a due
  schedule once, then continues at the next time after now (no catch-up).
"""
import datetime
import logging
import time

from flask import request
from sqlalchemy import update

from ..application import db
from ..exceptions import *
from ..models import CopyJob, CopySchedule
from ..managers.auth_manager import token_required, get_logged_in_user
from ..managers import copy_job_manager, egress_manager, worker_manager
from ..utils import egress_cost, schedule as schedule_utils


log = logging.getLogger(__name__)

REPEAT_CONFIRM_MESSAGE = 'Confirm the AWS egress cost of the repeated copies in the dialog'
TICK_SECONDS = 60
# Schedules run per tick at most (the rest follow in the next one)
MAX_RUNS_PER_TICK = 200
MAX_STATUS = 1000


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _message(e):
    """The text of an HTTP_EXCEPTION's payload"""
    if isinstance(e.payload, str):
        return e.payload
    if isinstance(e.payload, dict) and isinstance(e.payload.get('message'), str):
        return e.payload['message']
    return 'refused ({})'.format(e.code)


def annotate(schedules):
    """Adds the API's derived fields (`schedule_text`, UTC times as ISO 8601 with Z)"""
    for schedule in schedules:
        try:
            schedule.schedule_text = schedule_utils.describe(schedule_utils.spec_of(schedule))
        except schedule_utils.ScheduleError:
            schedule.schedule_text = None
        schedule.next_run_at_utc = schedule_utils.iso_utc(schedule.next_run_at)
        schedule.last_run_at_utc = schedule_utils.iso_utc(schedule.last_run_at)
        schedule.created_at_utc = schedule_utils.iso_utc(schedule.created_at)
    return schedules


# --- API (the logged in user's schedules; others' are 404)

@token_required
def list():
    owner = get_logged_in_user(request)
    return annotate(CopySchedule.query.filter_by(owner=owner).order_by(CopySchedule.id.desc()).all())


@token_required
def retrieve(id):
    return annotate([_owned(id, get_logged_in_user(request))])[0]


def _owned(id, owner):
    schedule = db.session.get(CopySchedule, id)
    if schedule is None or schedule.owner != owner:
        raise HTTP_404_NOT_FOUND('Copy Schedule with id {} not found'.format(id))
    return schedule


def _path(data, name):
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise HTTP_400_BAD_REQUEST('{} is required'.format(name))
    return value


def confirmed_egress(src_cloud, dst_cloud, raw, frequency):
    """
    The estimate stored on a new schedule (None if no AWS fee applies), or 400: a copy
    out of AWS needs the confirmation of the first run (egress_manager.confirmed_copy,
    which also depends on where the job runs) and of the repeated runs
    (`repeat_confirmed: true`, with the user's expected new data per run)
    """
    stored = egress_manager.confirmed_copy(src_cloud, dst_cloud, raw)
    if stored is None:
        return None
    if raw.get('repeat_confirmed') is not True:
        raise HTTP_400_BAD_REQUEST(REPEAT_CONFIRM_MESSAGE)
    stored.update(egress_cost.repeated(stored, raw.get('new_bytes_per_run'),
                                       schedule_utils.RUNS_PER_MONTH[frequency]))
    stored['repeat_confirmed'] = True
    return stored


@token_required
def create(data):
    """
    A new schedule of the logged in user; with `run_now` its first run starts at once
    (else at the next scheduled time). Nothing is stored if that run is refused.
    """
    owner = get_logged_in_user(request)
    data = data if isinstance(data, dict) else {}
    try:
        spec = schedule_utils.validate(data)
    except schedule_utils.ScheduleError as e:
        raise HTTP_400_BAD_REQUEST(str(e))
    src_cloud = copy_job_manager.owned_cloud(data.get('src_cloud_id'), owner)
    dst_cloud = copy_job_manager.owned_cloud(data.get('dst_cloud_id'), owner)
    src_path = _path(data, 'src_resource_path')
    dst_path = _path(data, 'dst_resource_path')
    performance = copy_job_manager.validate_performance(data.get('performance'), dst_cloud)
    egress_estimate = confirmed_egress(src_cloud, dst_cloud, data.get('egress_estimate'), spec['frequency'])

    now = _utcnow()
    schedule = CopySchedule(
        owner=owner,
        description=data.get('description'),
        src_cloud_id=src_cloud.id if src_cloud is not None else None,
        src_resource_path=src_path,
        dst_cloud_id=dst_cloud.id if dst_cloud is not None else None,
        dst_resource_path=dst_path,
        copy_links=bool(data.get('copy_links')),
        notification_email=data.get('notification_email') or None,
        performance=performance,
        enabled=True,
        next_run_at=schedule_utils.next_run(spec, now),
        skipped_runs=0,
        egress_estimate=egress_estimate,
        **{name: spec[name] for name in ('frequency',) + schedule_utils.ALL_FIELDS},
    )
    db.session.add(schedule)
    db.session.flush()
    log.info('Copy schedule %s of %s: %s', schedule.id, owner, schedule_utils.describe(spec))
    if data.get('run_now') is True:
        try:
            job = copy_job_manager.create_for(owner, _job_data(schedule), schedule=schedule)
        except HTTP_EXCEPTION:
            db.session.rollback()  # the schedule too: the dialog shows why
            raise
        schedule = db.session.get(CopySchedule, schedule.id)
        schedule.last_job_id = job.id
        schedule.last_run_at = now
        schedule.last_status = 'Started job {} when the schedule was created'.format(job.id)
    db.session.commit()
    return annotate([schedule])[0]


@token_required
def update_schedule(id, data):
    """Pause or resume (`enabled`); resuming continues at the next time after now"""
    schedule = _owned(id, get_logged_in_user(request))
    data = data if isinstance(data, dict) else {}
    if 'enabled' in data:
        enabled = data['enabled']
        if not isinstance(enabled, bool):
            raise HTTP_400_BAD_REQUEST('enabled must be true or false')
        if enabled and not schedule.enabled:
            schedule.next_run_at = schedule_utils.next_run(schedule_utils.spec_of(schedule), _utcnow())
        schedule.enabled = enabled
    db.session.commit()
    return annotate([schedule])[0]


@token_required
def delete(id):
    """Deletes the schedule; its jobs stay (and keep running if they do)"""
    schedule = _owned(id, get_logged_in_user(request))
    annotate([schedule])
    db.session.delete(schedule)
    db.session.commit()
    return schedule


# --- runner

def _job_data(schedule):
    return {
        'description': schedule.description,
        'src_cloud_id': schedule.src_cloud_id,
        'src_resource_path': schedule.src_resource_path,
        'dst_cloud_id': schedule.dst_cloud_id,
        'dst_resource_path': schedule.dst_resource_path,
        'copy_links': schedule.copy_links,
        'notification_email': schedule.notification_email,
        'performance': schedule.performance,
    }


def _status(schedule, text):
    schedule.last_status = text[:MAX_STATUS]


def _claim(now):
    """
    The next due schedule, now marked as run (next_run_at moved on and committed), or
    None if none is due. Another runner may take a schedule at the same time: SKIP
    LOCKED on PostgreSQL, and the compare-and-set of next_run_at everywhere, give each
    due time to one runner only. Returns False if another runner won a schedule.
    """
    row = db.session.execute(
        db.select(CopySchedule.id, CopySchedule.next_run_at)
        .where(CopySchedule.enabled.is_(True), CopySchedule.next_run_at <= now)
        .order_by(CopySchedule.next_run_at, CopySchedule.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        db.session.rollback()
        return None
    schedule = db.session.get(CopySchedule, row.id)
    try:
        next_at = schedule_utils.next_run(schedule_utils.spec_of(schedule), now)
    except schedule_utils.ScheduleError as e:  # changed in the database by hand
        result = db.session.execute(update(CopySchedule).where(CopySchedule.id == row.id).values(
            enabled=False, last_status='Paused: invalid schedule ({})'.format(e)[:MAX_STATUS]))
        db.session.commit()
        return False
    result = db.session.execute(
        update(CopySchedule)
        .where(CopySchedule.id == row.id, CopySchedule.next_run_at == row.next_run_at,
               CopySchedule.enabled.is_(True))
        .values(next_run_at=next_at, last_run_at=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    if result.rowcount != 1:
        return False
    return db.session.get(CopySchedule, row.id)


def _previous_running(schedule):
    if not schedule.last_job_id:
        return None
    job = db.session.get(CopyJob, schedule.last_job_id)
    if job is not None and job.progress_state == 'PROGRESS':
        return job
    return None


def run_claimed(schedule, now):
    """Runs a claimed schedule once: 'started', 'skipped' or 'refused'"""
    at = '{:%Y-%m-%d %H:%M} UTC'.format(now)
    previous = _previous_running(schedule)
    if previous is not None:
        schedule.skipped_runs = (schedule.skipped_runs or 0) + 1
        _status(schedule, 'Skipped the run of {}: job {} was still running'.format(at, previous.id))
        db.session.commit()
        log.info('Copy schedule %s: skipped, job %s still running', schedule.id, previous.id)
        return 'skipped'
    schedule_id, last_job_id = schedule.id, schedule.last_job_id
    try:
        job = copy_job_manager.create_for(schedule.owner, _job_data(schedule), schedule=schedule)
    except HTTP_EXCEPTION as e:
        db.session.rollback()
        schedule = db.session.get(CopySchedule, schedule_id)
        message = _message(e)
        if message == egress_manager.CONFIRM_MESSAGE:
            # The AWS fee changed since the schedule was confirmed (e.g. jobs now run
            # elsewhere): nothing more runs without the user's new confirmation
            schedule.enabled = False
            _status(schedule, 'Paused at {}: the copy now reads out of AWS at a cost that was not '
                              'confirmed; create the schedule again to confirm it'.format(at))
        else:
            _status(schedule, 'Not run at {}: {}'.format(at, message))
        db.session.commit()
        log.info('Copy schedule %s: not run: %s', schedule_id, message)
        return 'refused'
    except Exception as e:
        # E.g. the broker was down: create_for marked its job FAILED
        log.exception('Copy schedule %s: the run failed', schedule_id)
        db.session.rollback()
        schedule = db.session.get(CopySchedule, schedule_id)
        job = (CopyJob.query.filter(CopyJob.schedule_id == schedule_id, CopyJob.id > (last_job_id or 0))
               .order_by(CopyJob.id.desc()).first())
        if job is not None:
            schedule.last_job_id = job.id
        _status(schedule, 'Failed at {}: {}'.format(at, error_text(e)))
        db.session.commit()
        return 'refused'
    schedule = db.session.get(CopySchedule, schedule_id)
    schedule.last_job_id = job.id
    _status(schedule, 'Started job {} at {}'.format(job.id, at))
    db.session.commit()
    log.info('Copy schedule %s: started copy job %s', schedule_id, job.id)
    return 'started'


def tick(now=None):
    """
    One pass of the runner (manage.py schedules tick): runs every due schedule once.
    Returns [(schedule id, 'started' | 'skipped' | 'refused')].
    """
    now = now or _utcnow()
    worker_manager.expire_leases()  # a remote job whose worker died is not "still running"
    outcomes = []
    for _ in range(MAX_RUNS_PER_TICK):
        schedule = _claim(now)
        if schedule is None:
            break
        if schedule is False:
            continue
        outcomes.append((schedule.id, run_claimed(schedule, now)))
    return outcomes


def run_loop(interval=TICK_SECONDS, stop=None):
    """manage.py schedules run --loop: tick() at the start of every minute, forever"""
    log.info('Copy schedule runner: every %ss', interval)
    while stop is None or not stop():
        try:
            outcomes = tick()
            if outcomes:
                log.info('Copy schedule runner: %s', ', '.join('{} {}'.format(*o) for o in outcomes))
        except Exception as e:
            log.exception(e)
            db.session.rollback()
        finally:
            db.session.remove()
        time.sleep(interval - time.time() % interval + 1)
