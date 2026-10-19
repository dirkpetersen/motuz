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
from .s3_providers import CRC32C_PROVIDERS


HASHSUM_SIDES = ('src', 'dst')


# The hashes an integrity check can compare (rclone_connection._HASHSUM_COMMANDS)
HASH_TYPES = ('md5', 'sha256', 'crc32c')

# Paths of files that could not be checked stored per job (hashsum_job.not_checkable_sample)
NOT_CHECKABLE_SAMPLE = 100


def side_kind(local, connection_type=None, s3_provider=None):
    """
    What hashes one side of a check has: 'local', 's3' (an S3 connection at a provider
    that stores a CRC32C: s3_providers.CRC32C_PROVIDERS, i.e. Amazon S3; `s3_provider`
    as rclone gets it, s3_providers.effective_provider) or 'other' (clouds with an MD5
    only, including the other S3 providers: a CRC32C check would do one HEAD per object
    for nothing)
    """
    if local:
        return 'local'
    return 's3' if connection_type == 's3' and s3_provider in CRC32C_PROVIDERS else 'other'


def hashsum_type(src_kind, dst_kind, download=False, crc32c_supported=None):
    """
    The hash an integrity check compares:
    - SHA-256 if both sides are local (the CPU's SHA instructions make it about twice
      as fast as MD5 per stream);
    - CRC32C if one side is Amazon S3 (side_kind 's3') and the other local or Amazon
      S3, without download, and this node's rclone has the hash (`crc32c_supported`, a function called only then): S3
      returns the CRC32C it stored (one HEAD per object, nothing downloaded); objects
      without one are compared by MD5 (run_hashsum);
    - else MD5, the hash that clouds record (S3's ETag, ...)
    """
    if src_kind == 'local' and dst_kind == 'local':
        return 'sha256'
    kinds = {src_kind, dst_kind}
    if ('s3' in kinds and kinds <= {'local', 's3'} and not download
            and crc32c_supported is not None and crc32c_supported()):
        return 'crc32c'
    return 'md5'


def hashsum_outcome(src_tree, dst_tree, not_checkable=0):
    """The outcome in the notification email of a successful check"""
    if src_tree or dst_tree:
        return 'Files are DIFFERENT!'
    if not_checkable:
        return 'Files are IDENTICAL, but {} could not be checked!'.format(not_checkable)
    return 'Files are IDENTICAL!'


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
    exitstatus, entries, error_text = _watch_entries(connection, run_id, tick, interval)
    return exitstatus, generate_file_tree(entries), error_text


def _watch_entries(connection, run_id, tick, interval):
    """watch_hashsum, returning the run's {Name, md5chksum} entries instead of the tree"""
    def tree():
        return generate_file_tree(list(connection.hashsum_text(run_id)))

    while not connection.hashsum_finished(run_id):
        tick(connection.hashsum_percent(run_id), tree, connection.hashsum_error_text(run_id))
        time.sleep(interval)

    result = (connection.hashsum_exitstatus(run_id), list(connection.hashsum_text(run_id)),
              connection.hashsum_error_text(run_id))
    connection.hashsum_delete(run_id)
    return result


def hashsum_run_id(job_id, side):
    return '{}_{}'.format(job_id, side)


def run_hashsum(connection, job_id, start_side, tick, interval=1.0, should_stop=None,
                hash_type='md5', start_fallback=None):
    """
    An integrity check: md5sum (or `hash_type`) of the source, then of the destination,
    compared.

    @param start_side: function (side, run_id) starting md5sum of 'src' or 'dst'
    @param tick: function (side, percent, tree, error_text) with the job's overall
                 percentage (the source is the first half)
    @param should_stop: optional function; when it returns True before a side starts,
                        the job ends as STOPPED
    @param hash_type: 'crc32c': files on both sides of which one has no CRC32C (S3
                      objects uploaded without one, or multipart COMPOSITE) are then
                      compared by MD5, with start_fallback(side, run_id, names) (names:
                      the paths relative to the side's path, or None for the whole path
                      when the side is a single file); files without a comparable MD5
                      either are "not checkable": in neither tree, counted
    @return: dict with 'state' (SUCCESS, FAILED, UNSET or STOPPED) and, for a failed
             side, 'failed_side' plus its '<side>_tree' and '<side>_error_text';
             on success 'src_tree' / 'dst_tree' (only the differing branches),
             'src_error_text' / 'dst_error_text', 'hash_type', 'md5_fallback' (files
             compared by MD5 in a CRC32C check) and 'not_checkable' ({count, sample})
    """
    crc32c = hash_type == 'crc32c'
    share = 45 if crc32c else 50 # a CRC32C check keeps the last 10% for the MD5 fallback
    entries, errors = {}, {}
    for side in HASHSUM_SIDES:
        if should_stop is not None and should_stop():
            return {'state': 'STOPPED'}
        offset = share if side == 'dst' else 0
        run_id = hashsum_run_id(job_id, side)
        start_side(side, run_id)
        exitstatus, side_entries, error_text = _watch_entries(
            connection, run_id,
            lambda percent, tree, error_text, offset=offset, side=side:
                tick(side, int(percent * share / 100 + offset), tree, error_text),
            interval,
        )
        if exitstatus != 0:
            return _failed(side, exitstatus, side_entries, error_text)
        entries[side] = {entry['Name']: entry['md5chksum'] for entry in side_entries}
        errors[side] = error_text

    fallback, not_checkable = [], []
    if crc32c:
        fallback = sorted(name for name in entries['src'].keys() & entries['dst'].keys()
                          if not entries['src'][name] or not entries['dst'][name])
    if fallback:
        md5 = {}
        for side in HASHSUM_SIDES:
            if should_stop is not None and should_stop():
                return {'state': 'STOPPED'}
            offset = 90 if side == 'src' else 95
            run_id = hashsum_run_id(job_id, side + '_md5')
            done_tree = generate_file_tree(_entry_list(entries[side]))
            # A single file cannot be combined with a list of files (rclone): its md5sum
            names = fallback if len(entries[side]) > 1 else None
            start_fallback(side, run_id, names)
            exitstatus, side_entries, error_text = _watch_entries(
                connection, run_id,
                lambda percent, tree, error_text, offset=offset, side=side, done_tree=done_tree:
                    tick(side, int(percent * 5 / 100 + offset), lambda: done_tree, error_text),
                interval,
            )
            if exitstatus != 0:
                return _failed(side, exitstatus, _entry_list(entries[side]),
                               '\n'.join(text for text in (errors[side], error_text) if text))
            md5[side] = {entry['Name']: entry['md5chksum'] for entry in side_entries}
            if error_text:
                errors[side] = '\n'.join(text for text in (errors[side], error_text) if text)
        for name in fallback:
            src_md5, dst_md5 = md5['src'].get(name), md5['dst'].get(name)
            if src_md5 and dst_md5:
                entries['src'][name], entries['dst'][name] = src_md5, dst_md5
            else: # neither identical nor different: left out of both trees
                not_checkable.append(name)
                del entries['src'][name], entries['dst'][name]

    src_tree, dst_tree = remove_identical_branches(
        generate_file_tree(_entry_list(entries['src'])), generate_file_tree(_entry_list(entries['dst'])))
    return {
        'state': 'SUCCESS',
        'src_tree': src_tree,
        'dst_tree': dst_tree,
        'src_error_text': errors['src'] or None,
        'dst_error_text': errors['dst'] or None,
        'hash_type': hash_type,
        'md5_fallback': len(fallback) - len(not_checkable),
        'not_checkable': {'count': len(not_checkable), 'sample': not_checkable[:NOT_CHECKABLE_SAMPLE]},
    }


def _entry_list(entries):
    return [{'Name': name, 'md5chksum': value} for name, value in entries.items()]


def _failed(side, exitstatus, side_entries, error_text):
    return {
        'state': exit_state(exitstatus),
        'failed_side': side,
        '{}_tree'.format(side): generate_file_tree(list(side_entries)),
        '{}_error_text'.format(side): error_text,
    }


_MAX_PATH = 4096


def record_hash_result(job, result):
    """
    Stores a check's hash_type, md5_fallback and not_checkable ({count, sample}, from
    run_hashsum, or a remote worker's report: validated) on the hashsum job `job`
    """
    hash_type = result.get('hash_type')
    if hash_type in HASH_TYPES:
        job.hash_type = hash_type
    fallback = result.get('md5_fallback')
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback >= 0:
        job.md5_fallback = fallback
    not_checkable = result.get('not_checkable')
    if isinstance(not_checkable, dict):
        count, sample = not_checkable.get('count'), not_checkable.get('sample')
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0 and isinstance(sample, list):
            job.not_checkable = count
            job.not_checkable_sample = [path[:_MAX_PATH] for path in sample[:NOT_CHECKABLE_SAMPLE]
                                        if isinstance(path, str)]
