"""
Which pool runs a new copy or integrity-check job.

- 'central': this node, with Celery, as always.
- any other pool ('onprem', 'aws', ...): the job is queued (models.RemoteJob) and a
  remote worker of that pool claims it over HTTPS (managers/worker_manager.py).

Rules (config.py, README "Remote workers (HTTPS only)"):
1. A job with a local path on either side runs where the files are: LOCAL_JOB_POOL
   (MOTUZ_LOCAL_JOB_POOL; default 'central', i.e. this node's mounts; 'onprem' when
   on-prem workers have the filesystems and this node, e.g. in AWS, does not).
2. A cloud-to-cloud job whose source has at least LARGE_JOB_BYTES bytes or
   LARGE_JOB_FILES files goes to LARGE_JOB_POOL (default 'central', i.e. off), e.g.
   'aws' for temporary EC2 workers. The size comes from estimate_size, a hook with a
   simple `rclone size` implementation for now.
3. Everything else runs centrally.

With temporary EC2 workers (MOTUZ_EC2_WORKERS=true, README "Temporary EC2 workers"),
LARGE_JOB_POOL is the EC2 pool and every job there gets its own instance. Such a worker
has none of the owner's files and reaches the internet on port 443 only, so a large job
goes there only if both connections are HTTPS APIs with stored credentials
(ec2_eligible); others run centrally. Local jobs never go there (config.py refuses
MOTUZ_LOCAL_JOB_POOL = the EC2 pool).
"""
import logging
import math
import re
from collections import namedtuple
from urllib.parse import urlsplit

from flask import current_app

from ..utils import file_view, rclone_tuning
from ..utils.abstract_connection import RcloneException
from ..utils.rclone_connection import RcloneConnection


CENTRAL = 'central'
POOL_RE = re.compile(r'^[a-z][a-z0-9-]{0,31}$')


# The pool of a job and the size of its source (None: not measured or unknown)
Route = namedtuple('Route', 'pool source_bytes source_files')

# Connection types an EC2 worker can reach (HTTPS APIs on port 443)
EC2_TYPES = ('s3', 'azureblob', 'google cloud storage', 'dropbox', 'onedrive', 'drive')


class SizeUnknown(Exception):
    pass


def valid_pool(pool):
    return isinstance(pool, str) and POOL_RE.match(pool) is not None


def estimate_size(cloud_connection, path, timeout):
    """
    (bytes, files) below `path` of a connection, or None if it cannot be determined.
    A listing that takes longer than `timeout` seconds counts as large.
    """
    try:
        return RcloneConnection().size(cloud_connection, path, timeout)
    except file_view.ViewTimeoutError:
        return (float('inf'), float('inf'))
    except (RcloneException, OSError) as e:
        logging.warning("Could not estimate the size of a job source: %s", e)
        return None


def _is_large(size, config):
    if size is None:
        return False
    total_bytes, files = size
    return total_bytes >= config['LARGE_JOB_BYTES'] or files >= config['LARGE_JOB_FILES']


def is_large(cloud_connection, path, config, estimate=estimate_size):
    return _is_large(estimate(cloud_connection, path, config['JOB_SIZE_TIMEOUT']), config)


def _finite(value):
    return int(value) if value is not None and not math.isinf(value) else None


def ec2_eligible(connection):
    """
    True if an EC2 worker can run a job with this connection: an HTTPS API on port 443
    (the workers' security group allows nothing else), and credentials that are stored
    in Motuz (a 'profile' connection reads them from the owner's home directory, which
    an EC2 worker does not have)
    """
    if connection is None or connection.type not in EC2_TYPES or connection.subtype == 'profile':
        return False
    if connection.type == 's3' and connection.s3_endpoint:
        endpoint = connection.s3_endpoint.strip()
        parts = urlsplit(endpoint if '://' in endpoint else 'https://' + endpoint)
        try:
            port = parts.port
        except ValueError:
            return False
        return parts.scheme == 'https' and port in (None, 443)
    return True


def route(src_cloud, src_path, dst_cloud, config=None, estimate=estimate_size):
    """The Route of a job from `src_cloud`:`src_path` to `dst_cloud` (None = local)"""
    config = current_app.config if config is None else config
    size = None
    if src_cloud is None or dst_cloud is None:
        pool = config['LOCAL_JOB_POOL']
    elif config['LARGE_JOB_POOL'] != CENTRAL:
        size = estimate(src_cloud, src_path, config['JOB_SIZE_TIMEOUT'])
        pool = config['LARGE_JOB_POOL'] if _is_large(size, config) else CENTRAL
    else:
        pool = CENTRAL
    if not valid_pool(pool):
        raise ValueError("Invalid pool name {!r} (MOTUZ_LOCAL_JOB_POOL / MOTUZ_LARGE_JOB_POOL)".format(pool))

    ec2 = config.get('EC2')
    if ec2 is not None and ec2.enabled and pool == ec2.pool:
        if src_cloud is None or dst_cloud is None: # config.py refuses this configuration
            raise ValueError("Jobs with a local path never run on EC2 workers (MOTUZ_LOCAL_JOB_POOL)")
        if not (ec2_eligible(src_cloud) and ec2_eligible(dst_cloud)):
            logging.info("Large job between %s and %s runs centrally: EC2 workers need HTTPS APIs with "
                         "stored credentials on both sides", src_cloud.type, dst_cloud.type)
            pool = CENTRAL
    return Route(pool, _finite(size[0]) if size else None, _finite(size[1]) if size else None)


def choose_pool(src_cloud, src_path, dst_cloud, config=None, estimate=estimate_size):
    """The pool for a job from `src_cloud`:`src_path` to `dst_cloud` (None = local)"""
    return route(src_cloud, src_path, dst_cloud, config, estimate).pool


def rclone_flags(job_type, job):
    """
    The job's rclone performance flags (utils/rclone_tuning.py: the job's overrides on
    top of this installation's MOTUZ_RCLONE_* settings), for the job ticket of a remote
    worker. They are computed here because the settings live in this node's
    environment; the worker passes them to rclone as they are (extra_flags), exactly
    where RcloneConnection.copy / md5sum put them for Celery jobs.
    """
    if job_type == 'copy':
        dst_cloud = job.dst_cloud
        return rclone_tuning.copy_flags(job.performance, dst_cloud.type if dst_cloud is not None else None)
    return rclone_tuning.hashsum_flags(job.performance)
