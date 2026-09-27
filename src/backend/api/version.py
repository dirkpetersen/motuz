"""
Version of Motuz as seen by remote workers (src/worker/motuz_worker.py).

A worker runs the job code of its own checkout (api.utils), so it must be the same
release as the central node: the central node reports both values when a worker signs
in, and the worker refuses to claim jobs if they differ. Bump VERSION with every
release that changes the job code (rclone command lines, output parsing) and
WORKER_PROTOCOL whenever the worker API (/api/workers/...) or the job ticket changes.
"""
VERSION = '0.3.0'
WORKER_PROTOCOL = 1
