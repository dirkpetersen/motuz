"""
AWS egress cost of copy jobs and integrity checks with download (utils/egress_cost.py):
the estimate the New Copy Job / New Hashsum Job dialogs show before the user confirms
it, and the check that a job reading out of AWS carries that confirmation.

rclone moves the data through the machine that runs the job, so the charge depends on
where the job runs (job_routing.route): this server (in the AWS region
CENTRAL_AWS_REGION, or not in AWS), a temporary EC2 worker (in the EC2 settings' region)
or a worker of another pool (not known to be in AWS).
"""
import logging
import time

from flask import current_app

from ..exceptions import HTTP_400_BAD_REQUEST
from ..managers.auth_manager import token_required
from ..managers import cloud_connection_manager, job_routing
from ..utils import egress_cost, file_view
from ..utils.rclone_connection import RcloneConnection, RcloneException

CONFIRM_MESSAGE = 'Confirm the AWS egress cost in the dialog'
MAX_PATHS = 100
# rclone size did not finish in time: routed as large, like job_routing.estimate_size
UNKNOWN_LARGE = (float('inf'), float('inf'))


def _owned_cloud(cloud_id):
    """The logged in user's connection (404 for anyone else's), None for the local filesystem"""
    if not cloud_id:
        return None
    return cloud_connection_manager.retrieve(cloud_id)


def runner(pool, config=None):
    """(AWS region or None, description) of the machine that runs a job of this pool"""
    config = current_app.config if config is None else config
    if pool == job_routing.CENTRAL:
        region = config.get('CENTRAL_AWS_REGION')
        if region:
            return region, 'this server (AWS {})'.format(region)
        return None, 'this server, which is not in AWS'
    ec2 = config.get('EC2')
    if ec2 is not None and ec2.enabled and pool == ec2.pool and ec2.region:
        return ec2.region, 'an EC2 worker in {}'.format(ec2.region)
    return None, 'a worker of the pool {}, which is not known to be in AWS'.format(pool)


class _Sizes:
    """`rclone size` of paths as the user, measured once each within one deadline"""

    def __init__(self):
        self.deadline = time.monotonic() + current_app.config.get('EGRESS_SIZE_TIMEOUT', 120)
        self.values = {}

    def of(self, connection, path):
        """(bytes, files); UNKNOWN_LARGE after the deadline, None if rclone failed"""
        key = (connection.id, path)
        if key not in self.values:
            self.values[key] = self._measure(connection, path)
        return self.values[key]

    def _measure(self, connection, path):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return UNKNOWN_LARGE
        try:
            return RcloneConnection().size(connection, path, remaining)
        except file_view.ViewTimeoutError:
            return UNKNOWN_LARGE
        except (RcloneException, file_view.ViewError) as e:
            logging.warning('Could not measure the size for an egress estimate: %s', e)
            return None

    @staticmethod
    def total(sizes):
        """(bytes, files) of all, or (None, None) if one is not known"""
        if not sizes or any(size is None or size == UNKNOWN_LARGE for size in sizes):
            return None, None
        return sum(size[0] for size in sizes), sum(size[1] for size in sizes)


def _paths(paths, required=True):
    paths = [path for path in (paths or []) if isinstance(path, str) and path]
    if required and not paths:
        raise HTTP_400_BAD_REQUEST('src_resource_path is required')
    if len(paths) > MAX_PATHS:
        raise HTTP_400_BAD_REQUEST('At most {} paths'.format(MAX_PATHS))
    return paths


def _route(src, path, dst, size):
    """The runner (region, description) of a job whose source has `size` (no new rclone size)"""
    return runner(job_routing.route(src, path, dst, estimate=lambda *_: size).pool)


def _hashsum_assessment(src, dst, runner_region, runs_on):
    """Each side on AWS is read to the machine that runs the check"""
    return egress_cost.worst([egress_cost.assess(side, None, runner_region, runs_on) for side in (src, dst)])


@token_required
def copy_estimate(src_cloud_id, src_paths, dst_cloud_id):
    """The estimate for copying `src_paths` of a connection to another (or the local filesystem)"""
    src, dst = _owned_cloud(src_cloud_id), _owned_cloud(dst_cloud_id)
    src_paths = _paths(src_paths)
    if not egress_cost.is_aws(src):
        return egress_cost.estimate(None)
    sizes = _Sizes()
    results = []
    for path in src_paths:
        # route measures the source only for cloud-to-cloud jobs with a large-job pool
        region, runs_on = runner(
            job_routing.route(src, path, dst, estimate=lambda *_, p=path: sizes.of(src, p)).pool)
        results.append((egress_cost.assess(src, dst, region, runs_on), runs_on))
    assessment, runs_on = max(results, key=lambda r: (egress_cost.RANK[r[0].kind], r[0].legs))
    if not egress_cost.needs_confirmation(assessment):
        return egress_cost.estimate(assessment, runs_on=runs_on)
    total = _Sizes.total([sizes.of(src, path) for path in src_paths])
    return egress_cost.estimate(assessment, *total, runs_on=runs_on)


@token_required
def hashsum_estimate(src_cloud_id, src_paths, dst_cloud_id, dst_paths):
    """
    The estimate of an integrity check with download: each side on AWS is read to the
    machine that runs the check (job_routing.route, as for the copy)
    """
    src, dst = _owned_cloud(src_cloud_id), _owned_cloud(dst_cloud_id)
    src_paths = _paths(src_paths)
    dst_paths = _paths(dst_paths, required=False)
    if not (egress_cost.is_aws(src) or egress_cost.is_aws(dst)):
        return egress_cost.estimate(None)
    sizes = _Sizes()
    results = []
    for i, path in enumerate(src_paths):
        size = (lambda *_, p=path: sizes.of(src, p)) if src is not None else (lambda *_: None)
        region, runs_on = runner(job_routing.route(src, path, dst, estimate=size).pool)
        assessment = _hashsum_assessment(src, dst, region, runs_on)
        # The sides whose read is not free
        read = []
        for side, side_path in ((src, path), (dst, dst_paths[i] if i < len(dst_paths) else None)):
            side_assessment = egress_cost.assess(side, None, region, runs_on)
            if egress_cost.needs_confirmation(side_assessment):
                read.append((side, side_path))
        results.append((assessment, runs_on, read))
    assessment, runs_on, _ = max(results, key=lambda r: (egress_cost.RANK[r[0].kind], r[0].legs))
    if not egress_cost.needs_confirmation(assessment):
        return egress_cost.estimate(assessment, runs_on=runs_on)
    reads = [(side, side_path) for _, _, read in results for side, side_path in read]
    total = _Sizes.total([sizes.of(side, side_path) if side_path else None for side, side_path in reads])
    return egress_cost.estimate(assessment, *total, runs_on=runs_on)


def _candidate_runners(src, dst):
    """
    The runners a new job may get: the size decides between the small and the large
    pool, and the creation measures it again, so both count
    """
    return {_route(src, None, dst, size) for size in (None, UNKNOWN_LARGE)}


def _size_of(raw):
    """The size the user confirmed, for routing (unknown: large)"""
    if isinstance(raw, dict):
        size_bytes, files = raw.get('bytes'), raw.get('files')
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (size_bytes, files)):
            return size_bytes, files
    return UNKNOWN_LARGE


def _confirmed(assess, src, dst, raw):
    """
    The stored estimate (None if no AWS fee applies on any runner the job may get), or
    400 if one needs a confirmation and the request has none
    """
    if not any(egress_cost.needs_confirmation(assess(*r)) for r in _candidate_runners(src, dst)):
        return None
    region, runs_on = _route(src, None, dst, _size_of(raw))
    assessment = assess(region, runs_on)
    if not egress_cost.needs_confirmation(assessment):  # a smaller size than measured
        assessment = egress_cost.worst(assess(*r) for r in _candidate_runners(src, dst))
    stored = egress_cost.confirmed(assessment, raw, runs_on)
    if stored is None:
        raise HTTP_400_BAD_REQUEST(CONFIRM_MESSAGE)
    return stored


def confirmed_copy(src_cloud, dst_cloud, raw):
    """
    The confirmed estimate to store on a new copy job (None if no AWS fee applies), or
    400 if the job reads out of AWS and the request does not confirm the cost
    """
    if not egress_cost.is_aws(src_cloud):
        return None
    return _confirmed(lambda region, runs_on: egress_cost.assess(src_cloud, dst_cloud, region, runs_on),
                      src_cloud, dst_cloud, raw)


def confirmed_hashsum(src_cloud, dst_cloud, option_download, raw):
    """The same for an integrity check: only with download from a side on AWS"""
    if not option_download or not (egress_cost.is_aws(src_cloud) or egress_cost.is_aws(dst_cloud)):
        return None
    return _confirmed(lambda region, runs_on: _hashsum_assessment(src_cloud, dst_cloud, region, runs_on),
                      src_cloud, dst_cloud, raw)
