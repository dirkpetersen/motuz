"""
Uploads from the user's browser: files and folders dropped from the desktop onto a pane.

An upload is a copy job with `upload` set, shown in the job table like any other, and
one upload_file row per file. The browser
1. asks which of the dropped names exist in the destination folder (conflicts),
2. creates the job (create), in mode 'overwrite' or 'skip' (existing files are not
   uploaded),
3. sends each file in chunks of at most MAX_CHUNK_BYTES, in order (put_chunk). Each
   chunk is written to the file's `rclone rcat`, run as the user (utils/upload_pipes.py),
   so nothing is stored on the server and the OS checks the user's permissions. rcat
   writes a local file in place, so a local file goes to a hidden temporary name in the
   same folder and is renamed when it is complete: Stop never damages a file it replaces.

The data comes from the browser: if it stops sending (tab closed, connection lost), the
job fails after upload_pipes.IDLE_SECONDS (expire). A file that fails because the local
destination is full (utils/disk_full.py) fails the whole job at once and aborts its other
files. Only the central node runs uploads.
"""
import datetime
import logging
import posixpath
import re

from flask import request
from sqlalchemy import func, insert

from ..application import db
from ..exceptions import *
from ..models import CopyJob, UploadFile
from ..managers.auth_manager import token_required, get_logged_in_user
from ..managers import cloud_connection_manager
from ..utils import disk_full, file_view, upload_pipes
from ..utils.rclone_connection import RcloneConnection, RcloneException


MODES = ('overwrite', 'skip')
MAX_FILES = 100000
CHUNK_BYTES = 8 << 20       # what the browser sends: a few seconds even on a slow link
MAX_CHUNK_BYTES = 16 << 20
LIST_TIMEOUT = 300          # listing the destination for conflicts and skip mode
_MAX_PATH = 4096
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


@token_required
def conflicts(data):
    """{existing: [names]}: the dropped top-level `names` that exist in the destination"""
    owner = get_logged_in_user(request)
    dst_cloud = _owned_cloud(data.get('dst_cloud_id'))
    folder = _checked_folder(data.get('dst_resource_path'), dst_cloud)
    names = data.get('names')
    if not isinstance(names, list) or len(names) > MAX_FILES:
        raise HTTP_400_BAD_REQUEST('names must be a list of at most {} names'.format(MAX_FILES))
    names = {_checked_path(name) for name in names}
    if any('/' in name for name in names):
        raise HTTP_400_BAD_REQUEST('names must not contain "/"')
    existing = {entry.rstrip('/') for entry in _list(dst_cloud, owner, folder, recursive=False)}
    return {'existing': sorted(names & existing)}


@token_required
def create(data):
    """
    The upload job for `files` ([{path, size}], paths relative to the destination
    folder), plus the indexes of the files to send and the chunk size
    """
    owner = get_logged_in_user(request)
    dst_cloud = _owned_cloud(data.get('dst_cloud_id'))
    folder = _checked_folder(data.get('dst_resource_path'), dst_cloud)
    mode = data.get('mode')
    if mode not in MODES:
        raise HTTP_400_BAD_REQUEST('mode must be one of: {}'.format(', '.join(MODES)))
    files = _checked_files(data.get('files'))
    skipped = _existing_files(dst_cloud, owner, folder, [path for path, _ in files]) if mode == 'skip' else set()

    tops = sorted({path.split('/')[0] for path, _ in files})
    name = tops[0] + ('/' if any('/' in path for path, _ in files) else '') if len(tops) == 1 \
        else '{} items'.format(len(tops))
    job = CopyJob(
        description=data.get('description') or None,
        src_cloud_id=None,
        src_resource_path=name,
        dst_cloud_id=dst_cloud.id if dst_cloud is not None else None,
        dst_resource_path=folder,
        copy_links=False,
        upload={'files': len(files), 'bytes': sum(size for _, size in files), 'mode': mode, 'name': name},
        progress_current=0,
        progress_total=100,
        progress_state='PROGRESS',
        owner=owner,
    )
    db.session.add(job)
    db.session.flush()
    db.session.execute(insert(UploadFile), [{
        'copy_job_id': job.id,
        'index': index,
        'path': path,
        'size': size,
        'bytes_done': 0,
        'state': 'skipped' if path in skipped else 'pending',
    } for index, (path, size) in enumerate(files)])
    pending = [index for index, (path, _) in enumerate(files) if path not in skipped]
    if not pending:
        _finish_job(job)
    db.session.commit()
    logging.info('Copy job %s: upload of %s files (%s skipped) by %s', job.id, len(files), len(skipped), owner)
    return {'job': job, 'pending': pending, 'chunk_bytes': CHUNK_BYTES}


@token_required
def put_chunk(job_id, index, offset, length, stream):
    """
    Writes one chunk (the request body, `length` bytes at `offset` of the file) of the
    file `index` of upload job `job_id`. The first chunk (offset 0) starts the file,
    also again after an error. 409 if the job no longer runs or the offset is not where
    the file is (`written` tells where to go on; None: start again at 0).
    """
    owner = get_logged_in_user(request)
    job = db.session.get(CopyJob, job_id)
    if job is None or job.owner != owner or not job.upload:
        raise HTTP_404_NOT_FOUND('Upload job with id {} not found'.format(job_id))
    if job.progress_state != 'PROGRESS':
        raise HTTP_409_CONFLICT({'message': 'The job is {}'.format(job.progress_state.lower()),
                                 'job_state': job.progress_state})
    upload_file = UploadFile.query.filter_by(copy_job_id=job.id, index=index).one_or_none()
    if upload_file is None:
        raise HTTP_404_NOT_FOUND('File {} of upload job {} not found'.format(index, job_id))
    if upload_file.state != 'pending':
        raise HTTP_409_CONFLICT({'message': 'The file is {}'.format(upload_file.state),
                                 'file_state': upload_file.state})
    if not 0 <= length <= MAX_CHUNK_BYTES:
        raise HTTP_413_PAYLOAD_TOO_LARGE('A chunk has at most {} bytes'.format(MAX_CHUNK_BYTES))

    key = (job.id, index)
    path = _join(job.dst_resource_path, upload_file.path)
    local = job.dst_cloud is None
    if offset == 0:
        command, env = RcloneConnection().upload_command(
            job.dst_cloud, owner, _temporary(path, key) if local else path, upload_file.size)
        try:
            upload_pipes.start(key, command, env, upload_file.size, by_signal=not local)
        except upload_pipes.PipeBusy as e:
            raise HTTP_503_SERVICE_UNAVAILABLE(str(e))
    try:
        pipe = upload_pipes.write(key, offset, stream, length)
        exited = False
    except upload_pipes.RcloneExited as e:
        pipe, exited = e.pipe, True
    except upload_pipes.PipeError as e:
        raise HTTP_409_CONFLICT({'message': str(e), 'written': e.written})

    full = None
    if exited or pipe.written == upload_file.size:
        returncode, error = upload_pipes.finish(key, pipe)
        if returncode is None: # Aborted, or the file was started again meanwhile
            raise HTTP_409_CONFLICT({'message': error, 'written': upload_pipes.written(key)})
        if returncode == 0 and local:
            try:
                RcloneConnection().rename_local(owner, _temporary(path, key), path)
            except RcloneException as e:
                returncode, error = 1, str(e)
        if returncode == 0:
            upload_file.state = 'done'
            upload_file.bytes_done = upload_file.size
        else:
            upload_file.state = 'failed'
            upload_file.error = error or 'rclone failed with exit status {}'.format(returncode)
            logging.info('Copy job %s: upload of %r failed: %s', job.id, upload_file.path, upload_file.error)
            full = disk_full.scan(upload_file.error) if local else None
    else:
        upload_file.bytes_done = pipe.written
    _update_progress(job)
    if full is not None:
        _fail_disk_full(job, *full)
    db.session.commit()
    if full is not None: # After the commit: a chunk that arrives now is refused
        upload_pipes.abort_job(job.id)

    if not UploadFile.query.filter_by(copy_job_id=job.id, state='pending').count():
        _finish_job(job)
        db.session.commit()
    return {
        'file_state': upload_file.state,
        'written': upload_file.bytes_done,
        'error': upload_file.error,
        'job_state': job.progress_state,
    }


def stop(job):
    """Stops the upload job `job` (copy_job_manager.stop): aborts its files"""
    upload_pipes.abort_job(job.id)


def expire(owner):
    """
    Fails the running uploads of `owner` whose browser sent nothing for
    upload_pipes.IDLE_SECONDS (copy_job_manager.list and retrieve)
    """
    limit = datetime.datetime.utcnow() - datetime.timedelta(seconds=upload_pipes.IDLE_SECONDS)
    jobs = (CopyJob.query
            .filter(CopyJob.owner == owner, CopyJob.upload.isnot(None),
                    CopyJob.progress_state == 'PROGRESS', CopyJob.updated_at < limit)
            .all())
    for job in jobs:
        upload_pipes.abort_job(job.id)
        done = UploadFile.query.filter_by(copy_job_id=job.id, state='done').count()
        job.progress_state = 'FAILED'
        job.progress_error = ('The browser stopped sending the upload (the tab was closed or the '
                              'connection was lost). {} of {} files were uploaded.'.format(done, job.upload['files']))
    if jobs:
        db.session.commit()


def describe(job):
    """(progress text, error text) of an upload job for the job details"""
    rows = dict(db.session.query(UploadFile.state, func.count()).filter_by(copy_job_id=job.id)
                .group_by(UploadFile.state).all())
    sent = _bytes_sent(job)
    total = _bytes_to_send(job)
    text = 'Uploaded:  {} / {}\nFiles:     {} of {} done, {} skipped, {} failed\nFrom:      your browser ({})'.format(
        _size(sent), _size(total), rows.get('done', 0), job.upload['files'] - rows.get('skipped', 0),
        rows.get('skipped', 0), rows.get('failed', 0), job.upload['mode'])
    failed = (UploadFile.query.filter_by(copy_job_id=job.id, state='failed')
              .order_by(UploadFile.index).limit(20).all())
    errors = '\n'.join('{}: {}'.format(f.path, f.error) for f in failed)
    if rows.get('failed', 0) > len(failed):
        errors += '\n... and {} more'.format(rows['failed'] - len(failed))
    if job.progress_error: # Why the job failed (full disk, browser gone) first
        errors = job.progress_error + ('\n\n' + errors if errors else '')
    return text, errors


def _update_progress(job):
    total = _bytes_to_send(job)
    job.progress_current = 100 if not total else min(100, int(100 * _bytes_sent(job) / total))
    job.progress_execution_time = int((datetime.datetime.utcnow() - job.created_at).total_seconds())
    job.updated_at = datetime.datetime.utcnow()  # expire() looks at it


def _fail_disk_full(job, name, line):
    """Fails the job: its local destination is full (`name`: disk_full.FULL or QUOTA)"""
    if job.progress_state != 'PROGRESS':
        return
    done = UploadFile.query.filter_by(copy_job_id=job.id, state='done').count()
    job.progress_state = 'FAILED'
    job.progress_error = disk_full.message(name, job.dst_resource_path, done, 'uploaded', line)
    logging.warning('Copy job %s: upload stopped, the local destination is full (%s)', job.id, name)


def _finish_job(job):
    """SUCCESS if every file is done or skipped, else FAILED with the count"""
    if job.progress_state != 'PROGRESS':
        return
    failed = UploadFile.query.filter_by(copy_job_id=job.id, state='failed').count()
    _update_progress(job)
    if failed:
        job.progress_state = 'FAILED'
        job.progress_error = '{} of {} files could not be uploaded'.format(failed, job.upload['files'])
    else:
        job.progress_state = 'SUCCESS'
        job.progress_current = 100


def _bytes_sent(job):
    return (db.session.query(func.coalesce(func.sum(UploadFile.bytes_done), 0))
            .filter(UploadFile.copy_job_id == job.id, UploadFile.state != 'skipped').scalar())


def _bytes_to_send(job):
    return (db.session.query(func.coalesce(func.sum(UploadFile.size), 0))
            .filter(UploadFile.copy_job_id == job.id, UploadFile.state != 'skipped').scalar())


def _owned_cloud(cloud_id):
    """The user's connection (else 404), None for the local filesystem (None or 0)"""
    return cloud_connection_manager.retrieve(cloud_id) if cloud_id else None


def _checked_files(files):
    """[(path, size)] of the request's files, validated"""
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise HTTP_400_BAD_REQUEST('files must be a list of 1 to {} files'.format(MAX_FILES))
    result, seen = [], set()
    for entry in files:
        if not isinstance(entry, dict):
            raise HTTP_400_BAD_REQUEST('Each file must be an object with path and size')
        path, size = _checked_path(entry.get('path')), entry.get('size')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise HTTP_400_BAD_REQUEST('Invalid size of {!r}'.format(path))
        if path in seen:
            raise HTTP_400_BAD_REQUEST('{!r} is in the list twice'.format(path))
        seen.add(path)
        result.append((path, size))
    # A file cannot also be a folder of another file
    parents = set()
    for path in seen:
        parts = path.split('/')
        parents.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    clash = seen & parents
    if clash:
        raise HTTP_400_BAD_REQUEST('{!r} is a file and a folder'.format(sorted(clash)[0]))
    return result


def _checked_path(path):
    """A path relative to the destination folder: no empty, '.' or '..' parts"""
    if not isinstance(path, str) or not path or len(path) > _MAX_PATH or _CONTROL.search(path):
        raise HTTP_400_BAD_REQUEST('Invalid file name: {!r}'.format(path if not isinstance(path, str) else path[:200]))
    for part in path.split('/'):
        if part in ('', '.', '..') or len(part.encode('utf-8')) > 255:
            raise HTTP_400_BAD_REQUEST('Invalid file name: {!r}'.format(path[:200]))
    return path


def _checked_folder(folder, dst_cloud):
    """The destination folder: absolute for the local filesystem"""
    if not isinstance(folder, str) or len(folder) > _MAX_PATH or _CONTROL.search(folder):
        raise HTTP_400_BAD_REQUEST('Invalid destination folder')
    if dst_cloud is None and not folder.startswith('/'):
        raise HTTP_400_BAD_REQUEST('The destination folder must be an absolute path')
    return folder


def _join(folder, path):
    return posixpath.join(folder, path) if folder else path


def _temporary(path, key):
    """The hidden name a local file is written to before it is renamed to `path`"""
    return posixpath.join(posixpath.dirname(path), '.motuz-upload-{}-{}'.format(*key))


def _list(dst_cloud, owner, folder, recursive):
    try:
        return RcloneConnection().existing_names(dst_cloud, owner, folder, recursive, LIST_TIMEOUT)
    except file_view.ViewTimeoutError:
        raise HTTP_504_GATEWAY_TIMEOUT('Listing the destination took longer than {} seconds'.format(LIST_TIMEOUT))
    except RcloneException as e:
        raise HTTP_400_BAD_REQUEST('Could not list the destination: {}'.format(e))


def _existing_files(dst_cloud, owner, folder, paths):
    """The `paths` (relative to `folder`) that exist as files in the destination"""
    tops = {path.split('/')[0] for path in paths}
    existing = set()
    for entry in _list(dst_cloud, owner, folder, recursive=False):
        name = entry.rstrip('/')
        if name not in tops:
            continue
        if entry.endswith('/'):
            existing.update(name + '/' + below for below in _list(dst_cloud, owner, _join(folder, name), recursive=True))
        else:
            existing.add(name)
    return existing & set(paths)


def _size(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return '{:.1f} {}'.format(n, unit) if unit != 'B' else '{} B'.format(n)
        n /= 1024
