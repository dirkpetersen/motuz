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
"""
import logging
import re

from flask import current_app

from ..utils import file_view
from ..utils.abstract_connection import RcloneException
from ..utils.rclone_connection import RcloneConnection


CENTRAL = 'central'
POOL_RE = re.compile(r'^[a-z][a-z0-9-]{0,31}$')


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


def is_large(cloud_connection, path, config, estimate=estimate_size):
    size = estimate(cloud_connection, path, config['JOB_SIZE_TIMEOUT'])
    if size is None:
        return False
    total_bytes, files = size
    return total_bytes >= config['LARGE_JOB_BYTES'] or files >= config['LARGE_JOB_FILES']


def choose_pool(src_cloud, src_path, dst_cloud, config=None, estimate=estimate_size):
    """The pool for a job from `src_cloud`:`src_path` to `dst_cloud` (None = local)"""
    config = current_app.config if config is None else config
    if src_cloud is None or dst_cloud is None:
        pool = config['LOCAL_JOB_POOL']
    elif config['LARGE_JOB_POOL'] != CENTRAL and is_large(src_cloud, src_path, config, estimate):
        pool = config['LARGE_JOB_POOL']
    else:
        pool = CENTRAL
    if not valid_pool(pool):
        raise ValueError("Invalid pool name {!r} (MOTUZ_LOCAL_JOB_POOL / MOTUZ_LARGE_JOB_POOL)".format(pool))
    return pool


def rclone_flags(job):
    """
    Extra rclone options for a job (performance settings), passed to remote workers in
    the job ticket. None yet; the rclone settings work plugs in here.
    """
    return []
