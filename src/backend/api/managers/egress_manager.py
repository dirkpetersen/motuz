"""
AWS egress cost of copy jobs and integrity checks with download (utils/egress_cost.py):
the estimate the New Copy Job / New Hashsum Job dialogs show before the user confirms
it, and the check that a job reading out of AWS carries that confirmation.
"""
import logging
import time

from flask import current_app

from ..exceptions import HTTP_400_BAD_REQUEST
from ..managers.auth_manager import token_required
from ..managers import cloud_connection_manager
from ..utils import egress_cost, file_view
from ..utils.rclone_connection import RcloneConnection, RcloneException

CONFIRM_MESSAGE = 'Confirm the AWS egress cost in the dialog'
MAX_PATHS = 100


def _owned_cloud(cloud_id):
    """The logged in user's connection (404 for anyone else's), None for the local filesystem"""
    if not cloud_id:
        return None
    return cloud_connection_manager.retrieve(cloud_id)


def _measure(sides):
    """
    (bytes, files) of all paths of the (connection, paths) pairs, read with `rclone size`
    as the user within EGRESS_SIZE_TIMEOUT seconds in all; None if any path could not
    be measured in time
    """
    deadline = time.monotonic() + current_app.config.get('EGRESS_SIZE_TIMEOUT', 120)
    total_bytes = total_files = 0
    for connection, paths in sides:
        for path in paths:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                size_bytes, files = RcloneConnection().size(connection, path, remaining)
            except file_view.ViewTimeoutError:
                return None
            except (RcloneException, file_view.ViewError) as e:
                logging.warning('Could not measure the size for an egress estimate: %s', e)
                return None
            total_bytes += size_bytes
            total_files += files
    return total_bytes, total_files


def _paths(paths):
    paths = [path for path in (paths or []) if isinstance(path, str) and path]
    if not paths:
        raise HTTP_400_BAD_REQUEST('src_resource_path is required')
    if len(paths) > MAX_PATHS:
        raise HTTP_400_BAD_REQUEST('At most {} paths'.format(MAX_PATHS))
    return paths


def _region_known(src, dst):
    return egress_cost.region(src) is not None and (not egress_cost.is_aws(dst) or egress_cost.region(dst) is not None)


@token_required
def copy_estimate(src_cloud_id, src_paths, dst_cloud_id):
    """The estimate for copying `src_paths` of a connection to another (or the local filesystem)"""
    src, dst = _owned_cloud(src_cloud_id), _owned_cloud(dst_cloud_id)
    src_paths = _paths(src_paths)
    kind = egress_cost.kind(src, dst)
    if kind is None:
        return egress_cost.estimate(None)
    size = _measure([(src, src_paths)]) if egress_cost.needs_confirmation(kind) else None
    return egress_cost.estimate(kind, *(size or (None, None)), region_known=_region_known(src, dst))


def _hashsum_sides(src, src_paths, dst, dst_paths):
    """The sides of an integrity check with download that are read out of AWS"""
    return [(connection, paths) for connection, paths in ((src, src_paths), (dst, dst_paths))
            if egress_cost.is_aws(connection)]


@token_required
def hashsum_estimate(src_cloud_id, src_paths, dst_cloud_id, dst_paths):
    """
    The estimate of an integrity check with download: each side on AWS is read to the
    server that runs the check, which counts as leaving AWS
    """
    src, dst = _owned_cloud(src_cloud_id), _owned_cloud(dst_cloud_id)
    src_paths = _paths(src_paths)
    dst_paths = [path for path in (dst_paths or []) if isinstance(path, str) and path][:MAX_PATHS]
    sides = _hashsum_sides(src, src_paths, dst, dst_paths)
    if not sides:
        return egress_cost.estimate(None)
    size = _measure(sides)
    return egress_cost.estimate(egress_cost.INTERNET, *(size or (None, None)))


def confirmed_copy(src_cloud, dst_cloud, raw):
    """
    The confirmed estimate to store on a new copy job (None if no AWS fee applies), or
    400 if the job reads out of AWS and the request does not confirm the cost
    """
    return _confirmed(egress_cost.kind(src_cloud, dst_cloud), raw)


def confirmed_hashsum(src_cloud, dst_cloud, option_download, raw):
    """The same for an integrity check: only with download from a side on AWS"""
    if not option_download or not (egress_cost.is_aws(src_cloud) or egress_cost.is_aws(dst_cloud)):
        return None
    return _confirmed(egress_cost.INTERNET, raw)


def _confirmed(kind, raw):
    if not egress_cost.needs_confirmation(kind):
        return None
    stored = egress_cost.confirmed(kind, raw)
    if stored is None:
        raise HTTP_400_BAD_REQUEST(CONFIRM_MESSAGE)
    return stored
