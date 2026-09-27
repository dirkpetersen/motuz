"""
Watching the rclone process of a copy or integrity-check (hashsum) job, shared by the
Celery tasks on the central node (tasks/celery_tasks.py) and motuz-worker on remote
workers (src/worker/motuz_worker.py). Starting rclone is RcloneConnection's job
(copy / md5sum, or *_with_credentials with a job ticket's remote configuration);
this module only polls it and turns its results into job states. It must not import
the Flask application: remote workers have neither a database nor the server's
configuration.
"""
import time

from .file_utils import generate_file_tree, remove_identical_branches


HASHSUM_SIDES = ('src', 'dst')


def exit_state(exitstatus):
    """progress_state of a job whose rclone exited with `exitstatus` (-1: never set)"""
    if exitstatus == -1:
        return 'UNSET'
    if exitstatus == 0:
        return 'SUCCESS'
    return 'FAILED'


def watch_copy(connection, run_id, tick, interval=1.0):
    """
    Calls tick(percent, text, error_text) every `interval` seconds while the copy runs.
    Returns rclone's exit status (-1 if it was never set).
    """
    while not connection.copy_finished(run_id):
        tick(
            connection.copy_percent(run_id),
            connection.copy_text(run_id),
            connection.copy_error_text(run_id),
        )
        time.sleep(interval)
    return connection.copy_exitstatus(run_id)


def watch_hashsum(connection, run_id, tick, interval=1.0):
    """
    Calls tick(percent, tree, error_text) every `interval` seconds while md5sum runs;
    `tree` is a function returning the file tree so far (building it costs time).
    Returns (exitstatus, tree, error_text) and frees the run's output.
    """
    def tree():
        return generate_file_tree(connection.hashsum_text(run_id))

    while not connection.hashsum_finished(run_id):
        tick(connection.hashsum_percent(run_id), tree, connection.hashsum_error_text(run_id))
        time.sleep(interval)

    result = (connection.hashsum_exitstatus(run_id), tree(), connection.hashsum_error_text(run_id))
    connection.hashsum_delete(run_id)
    return result


def hashsum_run_id(job_id, side):
    return '{}_{}'.format(job_id, side)


def run_hashsum(connection, job_id, start_side, tick, interval=1.0, should_stop=None):
    """
    An integrity check: md5sum of the source, then of the destination, compared.

    @param start_side: function (side, run_id) starting md5sum of 'src' or 'dst'
    @param tick: function (side, percent, tree, error_text) with the job's overall
                 percentage (the source is the first half)
    @param should_stop: optional function; when it returns True before a side starts,
                        the job ends as STOPPED
    @return: dict with 'state' (SUCCESS, FAILED, UNSET or STOPPED) and, for a failed
             side, 'failed_side' plus its '<side>_tree' and '<side>_error_text';
             on success 'src_tree' / 'dst_tree' (only the differing branches) and
             'src_error_text' / 'dst_error_text'
    """
    results = {}
    for side in HASHSUM_SIDES:
        if should_stop is not None and should_stop():
            return {'state': 'STOPPED'}
        offset = 50 if side == 'dst' else 0
        run_id = hashsum_run_id(job_id, side)
        start_side(side, run_id)
        exitstatus, tree, error_text = watch_hashsum(
            connection, run_id,
            lambda percent, tree, error_text, side=side, offset=offset:
                tick(side, int(percent * 0.5 + offset), tree, error_text),
            interval,
        )
        if exitstatus != 0:
            return {
                'state': exit_state(exitstatus),
                'failed_side': side,
                '{}_tree'.format(side): tree,
                '{}_error_text'.format(side): error_text,
            }
        results[side] = (tree, error_text)

    src_tree, dst_tree = remove_identical_branches(results['src'][0], results['dst'][0])
    return {
        'state': 'SUCCESS',
        'src_tree': src_tree,
        'dst_tree': dst_tree,
        'src_error_text': results['src'][1] or None,
        'dst_error_text': results['dst'][1] or None,
    }
