"""
Overwrite policy (README "Overwriting existing files"): a copy job or upload overwrites
existing files only at or below a path that an earlier Motuz copy job or upload wrote
to, by any user, on the same storage (utils/motuz_destinations.storage_key). Elsewhere
copy jobs run with rclone's --ignore-existing (new files are copied, existing ones are
never modified) and uploads only skip existing files. MOTUZ_ALLOW_GENERAL_OVERWRITES=true
allows overwriting everywhere, as before. Whether the user may write at all is decided
by the OS, as always (rclone runs as the user).

The registered path is the item a job wrote: a copy job's dst_resource_path
(<folder>/<source name>) and each top-level item of an upload, so that one copy into a
home directory does not make the whole home directory overwritable. A path counts from
the moment its job is created, also if the job fails or is stopped later.
"""
import datetime
import logging

from flask import current_app
from sqlalchemy.dialects import postgresql, sqlite

from ..application import db
from ..models import MotuzDestination
from ..utils.motuz_destinations import ancestors, normalized_path, storage_key


# Why a destination may (not) be overwritten, for the dialogs; never which user or job
# wrote it
GENERAL = 'general'
MOTUZ_DESTINATION = 'motuz_destination'
NOT_MOTUZ_DESTINATION = 'not_motuz_destination'
UNKNOWN_STORAGE = 'unknown_storage'
MESSAGES = {
    GENERAL: 'Existing files are overwritten when they changed (this server allows overwriting '
             'files you may write).',
    MOTUZ_DESTINATION: 'Existing files are overwritten when they changed (this destination was '
                       'written by Motuz before).',
    NOT_MOTUZ_DESTINATION: 'Existing files are kept (this destination was not written by Motuz '
                           'before); only new files are copied.',
    UNKNOWN_STORAGE: 'Existing files are kept (Motuz cannot tell whether it wrote this destination '
                     'before); only new files are copied.',
}


# Paths of kept files stored per job (copy_job.skipped_existing_sample); the job
# details show the first 20
KEPT_SAMPLE = 100
_MAX_KEPT_PATH = 4096


def record_kept(job, count, sample):
    """
    Stores the existing files a job kept (CopyJobQueue.copy_kept: `count` and the first
    paths), validated: from a remote worker they are data of the worker's report.
    Nothing if none were kept or the values are not a count and a list of paths.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0 or not isinstance(sample, list):
        return
    job.skipped_existing = count
    job.skipped_existing_sample = [path[:_MAX_KEPT_PATH] for path in sample[:KEPT_SAMPLE] if isinstance(path, str)]


def general_overwrites(config=None):
    return bool((config if config is not None else current_app.config).get('ALLOW_GENERAL_OVERWRITES', False))


def _location(dst_cloud, path):
    """(storage key, normalized path), or None if either cannot be determined"""
    key = storage_key(dst_cloud)
    path = normalized_path(path, local=dst_cloud is None)
    if key is None or path is None:
        return None
    return key, path


def is_motuz_destination(dst_cloud, path):
    """True if `path` of `dst_cloud` (None: local) is at or below a registered path"""
    location = _location(dst_cloud, path)
    if location is None:
        return False
    key, path = location
    # The path and its folders: a match on whole segments (/data/a never covers /data/ab),
    # one indexed query of at most one row per segment
    return db.session.query(
        MotuzDestination.query
        .filter(MotuzDestination.storage_key == key, MotuzDestination.path.in_(ancestors(path)))
        .exists()
    ).scalar()


def decide(dst_cloud, path, config=None):
    """(overwrite allowed, reason: one of MESSAGES) for a copy to `path` of `dst_cloud`"""
    if general_overwrites(config):
        return True, GENERAL
    if storage_key(dst_cloud) is None:
        return False, UNKNOWN_STORAGE
    if is_motuz_destination(dst_cloud, path):
        return True, MOTUZ_DESTINATION
    return False, NOT_MOTUZ_DESTINATION


def describe(dst_cloud, path):
    """The answer of GET /api/copy-jobs/overwrite-policy/: only the decision"""
    overwrite, reason = decide(dst_cloud, path)
    return {'overwrite': overwrite, 'reason': reason, 'message': MESSAGES[reason]}


def register(dst_cloud, path, job_type, job_id, owner):
    """
    Records that a job writes `path` of `dst_cloud`, in the caller's transaction (the
    caller commits). Nothing for an unknown storage, '/' and paths already covered.
    Two jobs registering the same path at once are fine (ON CONFLICT DO NOTHING).
    """
    location = _location(dst_cloud, path)
    if location is None or location[1] == '/' or is_motuz_destination(dst_cloud, path):
        return
    key, path = location
    dialect = postgresql if db.session.get_bind().dialect.name == 'postgresql' else sqlite
    db.session.execute(
        dialect.insert(MotuzDestination)
        .values(storage_key=key, path=path, job_type=job_type, job_id=job_id, owner=owner,
                created_at=datetime.datetime.utcnow())
        .on_conflict_do_nothing(index_elements=['storage_key', 'path'])
    )
    logging.info('Copy job %s: %s is a Motuz destination now', job_id, path)
