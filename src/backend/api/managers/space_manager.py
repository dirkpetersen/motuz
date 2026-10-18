"""
The free space limit on the central node (utils/space_check.py, README "Free space
limit"): copy_job_manager.create and upload_manager.create call it before they create
the job. A destination on the local filesystem that would be more than
MAX_FILL_PERCENT full afterwards is a 409 with a message for the user (not 507: werkzeug,
and so flask-restx's abort, has no exception for it); otherwise the result is the job's
note (`space_check`), None when nothing was checked (a cloud
destination, the limit off, or a remote worker's local filesystem: the worker checks
before rclone starts, with the percentage from the ticket).
"""
import logging

from flask import current_app

from ..exceptions import HTTP_409_CONFLICT
from ..managers import job_routing
from ..utils import space_check
from ..utils.abstract_connection import RcloneException
from ..utils.rclone_connection import RcloneConnection


def check_copy(owner, src_cloud, src_path, dst_cloud, dst_path, copy_links):
    """The note of a new copy job from `src_cloud`:`src_path` to `dst_cloud`:`dst_path` (None: local)"""
    config = current_app.config
    max_percent = config['MAX_FILL_PERCENT']
    if dst_cloud is not None or max_percent >= space_check.NO_CHECK:
        return None
    if config['LOCAL_JOB_POOL'] != job_routing.CENTRAL: # the destination is on a remote worker
        return None
    connection = RcloneConnection()
    try:
        credentials = connection._formatCredentials(src_cloud, name='src') if src_cloud is not None else {}
    except RcloneException as e: # e.g. a profile that cannot be read: the job fails with the reason
        return space_check.not_checked(e)
    try:
        return space_check.check_copy(
            connection, owner, src_credentials=credentials, src_path=src_path, src_local=src_cloud is None,
            dst_path=dst_path, copy_links=bool(copy_links), max_percent=max_percent,
            timeout=config['JOB_SIZE_TIMEOUT'])
    except space_check.NotEnoughSpace as e:
        logging.info('Copy job of %s to %s refused: %s', owner, dst_path, e)
        raise HTTP_409_CONFLICT(str(e))


def check_upload(owner, dst_cloud, folder, to_add):
    """The note of a new upload of `to_add` bytes to `dst_cloud`:`folder` (uploads run here)"""
    config = current_app.config
    if dst_cloud is not None:
        return None
    try:
        return space_check.check_upload(owner, folder, to_add, config['MAX_FILL_PERCENT'], config['JOB_SIZE_TIMEOUT'])
    except space_check.NotEnoughSpace as e:
        logging.info('Upload of %s to %s refused: %s', owner, folder, e)
        raise HTTP_409_CONFLICT(str(e))
