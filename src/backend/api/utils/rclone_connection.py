import concurrent.futures
import functools
import json
import logging
import os
import re
import subprocess
from collections import defaultdict

from .abstract_connection import AbstractConnection, RcloneException, check_output, sudo_as, user_process_env
from . import disk_full
from . import kept_files
from . import local_credentials
from .file_times import rfc3339_to_iso_utc
from . import file_view
from . import image_view
from . import document_view
from . import rclone_obscure
from . import rclone_tuning
from . import s3_providers
from .copy_job_queue import CopyJobQueue
from .hashsum_job_queue import HashsumJobQueue


# The rclone that runs as the users: root's copy in the Docker image and on the EC2
# workers, the account's own copy in the systemd install, whose sudoers rule
# (bin/systemd/install.sh) allows exactly that path. The units set MOTUZ_RCLONE.
RCLONE_PATHS = ('/usr/local/bin/rclone', '/var/lib/motuz-runas/rclone')


def rclone_path(environ=None):
    path = (os.environ if environ is None else environ).get('MOTUZ_RCLONE') or RCLONE_PATHS[0]
    if path not in RCLONE_PATHS:
        raise RcloneException('MOTUZ_RCLONE={!r} must be one of {}'.format(path, ', '.join(RCLONE_PATHS)))
    return path


RCLONE = rclone_path()


class RcloneConnection(AbstractConnection):
    def __init__(self):
        self._hashsum_job_queue = HashsumJobQueue()
        self._copy_job_queue = CopyJobQueue()

        self._job_status = defaultdict(functools.partial(defaultdict, str)) # Mapping from id to status dict

        self._job_text = defaultdict(str)
        self._job_error_text = defaultdict(str)
        self._job_percent = defaultdict(int)
        self._job_exitstatus = {}

        self._stop_events = {} # Mapping from id to threading.Event
        self._latest_job_id = 0


    def verify(self, data):
        try:
            credentials = self._formatCredentials(data, name='current')
        except RcloneException as e: # e.g. a profile that cannot be read from the user's home
            return {
                'result': False,
                'message': str(e)[-1000:],
            }
        user = data.owner
        bucket = getattr(data, 'bucket', None)
        if bucket is None:
            bucket = ''

        command = [
            *sudo_as(user, credentials),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            *VERIFY_FLAGS,
            'lsjson',
            'current:{}'.format(bucket),
        ]

        self._log_command(command, credentials)

        try:
            result = self._execute(command, credentials)
            return {
                'result': True,
                'message': 'Success',
            }
        except subprocess.CalledProcessError as e:
            returncode = e.returncode
            return {
                'result': False,
                'message': 'Exit status {}'.format(returncode),
                'retryable': True,
            }
        except RcloneException as e: # rclone failed and explained why on stderr
            message = str(e)[-1000:]
            return {
                'result': False,
                'message': message,
                'retryable': not is_permanent_error(message),
            }



    def ls(self, data, path):
        credentials = self._formatCredentials(data, name='current')
        user = data.owner
        command = [
            *sudo_as(user, credentials),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            'lsjson',
            'current:{}'.format(path),
        ]

        self._log_command(command, credentials)

        try:
            result = self._execute(command, credentials)
            files = _with_modified(json.loads(result))
            return {
                'files': files,
                'path': path,
            }
        except subprocess.CalledProcessError as e:
            raise RcloneException(str(e))



    def view(self, data, path):
        """
        The first MAX_VIEW_BYTES of a text file, read by rclone as the user with the
        connection's credentials (see file_view). `rclone cat` of a folder would print
        every file in it, so the path is checked with `lsjson --stat` first.
        """
        credentials, base, remote = self._view_base(data, path)
        size = self._view_stat(base, remote, credentials, path)

        command = base + ['cat', '--count', str(file_view.MAX_VIEW_BYTES + 1), remote]
        self._log_command(command, credentials)
        content = self._run_view_command(command, credentials, path)
        try:
            return file_view.view_result(path, content, size)
        except file_view.NotTextError:
            raise file_view.NotTextError("'{}' is not a text file".format(path.rstrip('/').split('/')[-1]))


    def view_chunk(self, data, path, request):
        """
        One chunk of a text file for the pager (file_view.ChunkRequest), read by rclone
        as the user with the connection's credentials: `lsjson --stat` (size; a folder
        is refused), `cat --offset --count` for the range and, unless the range starts
        at the beginning of the file, `cat --count` for the first bytes (text check).
        A forward read at the end of the file needs only `lsjson --stat`, and a follow
        read (request.skips_text_check) no text check.
        """
        credentials, base, remote = self._view_base(data, path)
        size = self._view_stat(base, remote, credentials, path)

        start, count = request.read_range()
        if size is None:
            if request.backward:
                raise file_view.ViewError("The size of '{}' is unknown, it can only be read from the start".format(path))
        else:
            if request.backward and request.before is not None and request.before > size:
                raise file_view.ViewError('Offset {} is beyond the end of the file ({} bytes)'.format(request.before, size))
            if not request.backward and request.offset >= size:
                # At the end (a follow poll without news) or past it (400, or for a
                # follow read the new size of a file that shrank): no `cat` at all
                return file_view.chunk_result(path, request, size, b'', request.offset, b'')
            if start < 0: # tail
                start = max(0, size + start)
            count = max(0, min(count, size - start))

        content = b''
        if count > 0:
            command = base + ['cat', '--offset', str(start), '--count', str(count), remote]
            self._log_command(command, credentials)
            content = self._run_view_command(command, credentials, path)

        head_needed = file_view.HEAD_CHECK_BYTES if size is None else min(size, file_view.HEAD_CHECK_BYTES)
        if start == 0 and len(content) >= head_needed:
            head = content[:file_view.HEAD_CHECK_BYTES]
        elif request.skips_text_check:
            head = b'' # following: the viewer checked the first bytes when it opened the file
        else:
            command = base + ['cat', '--count', str(file_view.HEAD_CHECK_BYTES), remote]
            self._log_command(command, credentials)
            head = self._run_view_command(command, credentials, path)

        try:
            return file_view.chunk_result(path, request, size, head, start, content)
        except file_view.NotTextError:
            raise file_view.NotTextError("'{}' is not a text file".format(path.rstrip('/').split('/')[-1]))


    def view_image(self, data, path, max_bytes):
        """
        (bytes, type) of a PNG, JPEG, GIF, WebP, HEIC/HEIF or AVIF image, read by rclone as the user with
        the connection's credentials: `lsjson --stat` (size; a folder is refused, and a
        file larger than `max_bytes` is refused before it is read), then
        `cat --count max_bytes+1`, so a file that grew meanwhile is refused too
        (see image_view).
        """
        name = path.rstrip('/').split('/')[-1]
        credentials, base, remote = self._view_base(data, path)
        size = self._view_stat(base, remote, credentials, path)
        image_view.check_size(name, size, max_bytes)

        command = base + ['cat', '--count', str(max_bytes + 1), remote]
        self._log_command(command, credentials)
        content = self._run_view_command(command, credentials, path)
        return image_view.image_result(name, content, size, max_bytes)


    def view_document(self, data, path, max_bytes, request):
        """
        A document for the document viewer, read by rclone as the user with the
        connection's credentials: (bytes, container, file size, start).
        `lsjson --stat` first (size; a folder is refused). The whole file is refused
        above `max_bytes` before any `cat`, then read with `cat --count max_bytes+1`.
        A range (PDFs only) is `cat --offset --count`, plus, unless it starts at the
        beginning, `cat --count 1024` for the type check, run at the same time.
        """
        name = path.rstrip('/').split('/')[-1]
        credentials, base, remote = self._view_base(data, path)
        size = self._view_stat(base, remote, credentials, path)

        if not request.is_range:
            document_view.check_size(name, size, max_bytes)
            command = base + ['cat', '--count', str(max_bytes + 1), remote]
            self._log_command(command, credentials)
            content = self._run_view_command(command, credentials, path)
            content, container = document_view.document_result(name, content, size, max_bytes)
            return content, container, size if size is not None else len(content), 0

        if size is None:
            raise file_view.ViewError("The size of '{}' is unknown; it cannot be read in ranges".format(path))
        document_view.check_range(name, request, size)
        count = min(request.length, size - request.offset)
        commands = [base + ['cat', '--offset', str(request.offset), '--count', str(count), remote]]
        head_in_range = request.offset == 0 and count >= min(size, document_view.HEAD_BYTES)
        if not head_in_range:
            commands.append(base + ['cat', '--count', str(document_view.HEAD_BYTES), remote])
        for command in commands:
            self._log_command(command, credentials)
        if len(commands) == 1:
            content = self._run_view_command(commands[0], credentials, path)
            head = content[:document_view.HEAD_BYTES]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self._run_view_command, command, credentials, path) for command in commands]
                content, head = [future.result() for future in futures]
        content, container = document_view.range_result(name, head, content)
        return content, container, size, request.offset


    def _view_base(self, data, path):
        """(credentials, rclone command prefix run as the user, remote path) for the viewer"""
        credentials = self._formatCredentials(data, name='current')
        user = data.owner
        remote = 'current:{}'.format(path) # never an option: always prefixed
        base = [
            *sudo_as(user, credentials),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
        ]
        return credentials, base, remote


    def _view_stat(self, base, remote, credentials, path):
        """
        The size of a file (None if the backend does not know it). A folder is refused:
        `rclone cat` of a folder would print every file in it.
        """
        command = base + ['lsjson', '--stat', remote]
        self._log_command(command, credentials)
        stdout = self._run_view_command(command, credentials, path)
        try:
            info = json.loads(stdout.decode('utf-8'))
        except ValueError:
            raise RcloneException("Could not read '{}'".format(path))
        if not isinstance(info, dict):
            raise file_view.NotFoundError("'{}' does not exist".format(path))
        if info.get('IsDir'):
            # Bucket storage (S3, Azure, GCS) reports a missing path as a virtual folder
            raise file_view.ViewError("'{}' is a folder or does not exist".format(path))
        size = info.get('Size')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = None
        return size


    def _run_view_command(self, command, credentials, path):
        """stdout of a viewer command (bytes); rclone's error text on failure, never contents"""
        returncode, stdout, stderr = file_view.run_limited(
            command, file_view.CLOUD_TIMEOUT, env=user_process_env(credentials))
        if returncode != 0:
            message = stderr.decode('utf-8', 'replace').strip()[-1000:]
            if 'not found' in message.lower():
                raise file_view.NotFoundError("'{}' does not exist".format(path))
            raise RcloneException(message or 'rclone failed with exit status {}'.format(returncode))
        return stdout


    def mkdir(self, data, path):
        credentials = self._formatCredentials(data, name='current')
        user = data.owner
        command = [
            *sudo_as(user, credentials),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            '--s3-no-check-bucket',
            '--s3-acl',
            'bucket-owner-full-control',
            'touch',
            'current:{}/.motuz_keep'.format(path),
        ]

        self._log_command(command, credentials)

        try:
            result = self._execute(command, credentials)
            return {
                'message': 'Success',
            }
        except subprocess.CalledProcessError as e:
            raise RcloneException(str(e))


    def size(self, data, path, timeout):
        """
        (bytes, files) below a path of a connection (`rclone size --json`, as the
        owner), used to route large cloud-to-cloud jobs (managers/job_routing.py).
        Raises file_view.ViewTimeoutError after `timeout` seconds and RcloneException
        if rclone fails.
        """
        credentials, base, remote = self._view_base(data, path)
        command = base + ['size', '--json', remote]
        self._log_command(command, credentials)
        returncode, stdout, stderr = file_view.run_limited(command, timeout, env=user_process_env(credentials))
        if returncode != 0:
            raise RcloneException(stderr.decode('utf-8', 'replace').strip()[-1000:]
                                  or 'rclone failed with exit status {}'.format(returncode))
        try:
            info = json.loads(stdout.decode('utf-8'))
            return int(info['bytes']), int(info['count'])
        except (ValueError, KeyError, TypeError):
            raise RcloneException('Unexpected output of rclone size')


    def measure(self, credentials, path, *, local, user, timeout, copy_links=False, missing_ok=False,
                extra_env=None):
        """
        (bytes, files) of a job's source or local destination (`rclone size --json` as
        `user`), for the free space check (utils/space_check.py): `credentials` are the
        remote `src` of a cloud side (RCLONE_CONFIG_SRC_*, from _formatCredentials or a
        worker's ticket), {} for a local path; `missing_ok`: (0, 0) if `path` does not
        exist. Local sources skip .snapshot/ like the copy does. Raises
        file_view.ViewTimeoutError after `timeout` seconds and RcloneException.
        """
        target = _local_path(path) if local else 'src:{}'.format(path)
        env = {**credentials, **(extra_env or {})}
        command = [
            *sudo_as(user, env),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            *(['--copy-links'] if copy_links else []),
            *(['--exclude=\\.snapshot/'] if local else []),
            'size',
            '--json',
            target,
        ]
        self._log_command(command, credentials)
        returncode, stdout, stderr = file_view.run_limited(command, timeout, env=user_process_env(env))
        if returncode == _RCLONE_DIRECTORY_NOT_FOUND and missing_ok:
            return 0, 0
        if returncode != 0:
            raise RcloneException(stderr.decode('utf-8', 'replace').strip()[-1000:]
                                  or 'rclone failed with exit status {}'.format(returncode))
        try:
            info = json.loads(stdout.decode('utf-8'))
            return int(info['bytes']), int(info['count'])
        except (ValueError, KeyError, TypeError):
            raise RcloneException('Unexpected output of rclone size')


    def upload_command(self, dst_data, user, path, size):
        """
        (command, environment) of `rclone rcat` that writes `size` bytes from its stdin
        to `path` of the connection `dst_data` (None: the local filesystem) as `user`:
        one file of an upload from the browser (utils/upload_pipes.py)
        """
        credentials = self._formatCredentials(dst_data, name='dst') if dst_data is not None else {}
        check_flags, check_env = destination_checks(dst_data)
        dst = _local_path(path) if dst_data is None else 'dst:{}'.format(path)
        # A full local disk: no retries (upload_manager fails the whole upload)
        env = {**credentials, **check_env, **(disk_full.FATAL_ENV if dst_data is None else {})}
        command = [
            *sudo_as(user, env),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            '--s3-no-check-bucket',
            '--s3-acl',
            'bucket-owner-full-control',
            '--contimeout=5m',
            *_checked_flags(check_flags),
            'rcat',
            '--size={}'.format(int(size)),
            dst,
        ]
        self._log_command(command, credentials)
        return command, user_process_env(env)


    def rename_local(self, user, src, dst):
        """Renames the local file `src` to `dst` (replacing it) as `user` (rclone moveto)"""
        command = [*sudo_as(user), RCLONE, '--config=/dev/null', 'moveto', _local_path(src), _local_path(dst)]
        self._log_command(command, {})
        self._execute(command)


    def remove_local(self, user, path):
        """Removes the local file `path` as `user` (rclone deletefile)"""
        command = [*sudo_as(user), RCLONE, '--config=/dev/null', 'deletefile', _local_path(path)]
        self._log_command(command, {})
        self._execute(command)


    def existing_names(self, dst_data, user, path, recursive, timeout):
        """
        The names in the folder `path` of a destination (None: the local filesystem),
        listed as `user`: files and folders (folders end with '/'), or with `recursive`
        all files below it as paths relative to `path`. Empty if `path` does not exist.
        Raises file_view.ViewTimeoutError after `timeout` seconds and RcloneException.
        """
        credentials = self._formatCredentials(dst_data, name='dst') if dst_data is not None else {}
        folder = _local_path(path) if dst_data is None else 'dst:{}'.format(path)
        command = [
            *sudo_as(user, credentials),
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            'lsf',
            *(['--recursive', '--files-only'] if recursive else ['--max-depth=1']),
            folder,
        ]
        self._log_command(command, credentials)
        returncode, stdout, stderr = file_view.run_limited(command, timeout, env=user_process_env(credentials))
        if returncode == _RCLONE_DIRECTORY_NOT_FOUND:
            return []
        if returncode != 0:
            raise RcloneException(stderr.decode('utf-8', 'replace').strip()[-1000:]
                                  or 'rclone failed with exit status {}'.format(returncode))
        return [line for line in stdout.decode('utf-8', 'replace').split('\n') if line]


    def copy(self,
            src_data,
            src_resource_path,
            dst_data,
            dst_resource_path,
            user,
            copy_links,
            job_id,
            performance=None,
            overwrite=True,
    ):
        """
        `performance`: the job's validated overrides (rclone_tuning), on top of the
        installation's MOTUZ_RCLONE_* defaults. `overwrite` False: existing destination
        files are kept (rclone_tuning.KEEP_EXISTING_FLAG, managers/overwrite_policy.py)
        """
        if src_data is None:
            _local_path(src_resource_path) # before reading any credentials
        credentials = {}
        if src_data is not None:
            credentials.update(self._formatCredentials(src_data, name='src'))
        if dst_data is not None:
            credentials.update(self._formatCredentials(dst_data, name='dst'))
        check_flags, check_env = destination_checks(dst_data)
        return self.copy_with_credentials(
            credentials,
            src_resource_path=src_resource_path,
            src_local=src_data is None,
            dst_resource_path=dst_resource_path,
            dst_local=dst_data is None,
            user=user,
            copy_links=copy_links,
            job_id=job_id,
            # One argv item per flag (--transfers=32), formatted from parsed values
            extra_flags=rclone_tuning.copy_flags(performance, dst_data.type if dst_data is not None else None,
                                                src_local=src_data is None, overwrite=overwrite)
                + check_flags,
            extra_env={**rclone_tuning.md5_env(), **check_env},
        )


    def copy_with_credentials(self,
            credentials,
            *,
            src_resource_path,
            src_local,
            dst_resource_path,
            dst_local,
            user,
            copy_links,
            job_id,
            extra_flags=(),
            extra_env=None,
    ):
        """
        Starts `rclone copyto` as `user` with ready-made remote configuration
        (`credentials`: RCLONE_CONFIG_SRC_* / RCLONE_CONFIG_DST_*, see
        _formatCredentials). The Celery task builds them from the connections here;
        a remote worker gets them in its job ticket (managers/worker_manager.py).

        @param extra_flags: further rclone options (each starting with --): the job's
                            performance flags (rclone_tuning.copy_flags), computed where
                            the installation's MOTUZ_RCLONE_* settings are, i.e. on the
                            central node, and passed in the ticket to remote workers
        @param extra_env: further process variables that are not remote configuration
                          (a remote worker's proxy and CA bundle)
        """
        credentials = dict(credentials)
        option_exclude_dot_snapshot = '' # HACKHACK: remove once https://github.com/rclone/rclone/issues/2425 is addressed

        if src_local:
            src = _local_path(src_resource_path)
            if os.path.isdir(src):
                option_exclude_dot_snapshot = '--exclude=\\.snapshot/'
        else:
            src = 'src:{}'.format(src_resource_path)

        local_flags = []
        if src_local and dst_local:
            # rclone checks a copy with the first hash both sides have, MD5 (local_to_local_env)
            extra_env = {**(extra_env or {}), **local_to_local_env()}
            local_flags = local_to_local_flags(copy_links)

        if dst_local:
            dst = _local_path(dst_resource_path)
            # A full disk is fatal for rclone, and CopyJobQueue stops it (utils/disk_full.py)
            extra_env = {**(extra_env or {}), **disk_full.FATAL_ENV}
        else:
            dst = 'dst:{}'.format(dst_resource_path)

        if copy_links:
            option_copy_links = '--copy-links'
        else:
            option_copy_links = ''

        # --ignore-existing: rclone reports the kept files to a new file of the job's, never
        # to stdout, where the report would make it redraw its progress after every file.
        # Made here, where rclone runs (never from a ticket: older tickets had --combined=-)
        extra_flags = [flag for flag in _checked_flags(extra_flags)
                       if flag.split('=', 1)[0] != kept_files.COMBINED_FLAG]
        kept_file = kept_files.new_path(job_id) if rclone_tuning.KEEP_EXISTING_FLAG in extra_flags else None
        if kept_file is not None:
            extra_flags.append(kept_files.combined_flag(kept_file))

        command = [
            *sudo_as(user, {**credentials, **(extra_env or {})}), # extra_env too (not logged)
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            '--s3-disable-checksum',
            '--s3-no-check-bucket',
            '--s3-acl',
            'bucket-owner-full-control',
            option_exclude_dot_snapshot,
            '--contimeout=5m',
            *_checked_flags(extra_flags),
            'copyto',
            src,
            dst,
            option_copy_links,
            *local_flags,
            '--progress',
            '--stats', '2s',
        ]

        command = [cmd for cmd in command if len(cmd) > 0]

        self._log_command(command, credentials)
        credentials.update(extra_env or {})

        try:
            self._copy_job_queue.push(command, credentials, job_id, local_dst=dst if dst_local else None,
                                      kept_file=kept_file, user=user)
        except RcloneException as e:
            raise RcloneException(str(e))

        return job_id


    def copy_text(self, job_id):
        return self._copy_job_queue.copy_text(job_id)

    def copy_error_text(self, job_id):
        return self._copy_job_queue.copy_error_text(job_id)

    def copy_percent(self, job_id):
        return self._copy_job_queue.copy_percent(job_id)

    def copy_kept(self, job_id):
        return self._copy_job_queue.copy_kept(job_id)

    def copy_stop(self, job_id):
        self._copy_job_queue.copy_stop(job_id)

    def copy_finished(self, job_id):
        return self._copy_job_queue.copy_finished(job_id)

    def copy_exitstatus(self, job_id):
        return self._copy_job_queue.copy_exitstatus(job_id)


    def md5sum(self,
            data,
            resource_path,
            user,
            job_id,
            download=False,
            performance=None,
            hash_type='md5',
            files=None,
    ):
        if data is None:
            _local_path(resource_path) # before reading any credentials
        credentials = {} if data is None else self._formatCredentials(data, name='src')
        return self.md5sum_with_credentials(
            credentials,
            resource_path=resource_path,
            local=data is None,
            user=user,
            job_id=job_id,
            download=download,
            extra_flags=rclone_tuning.hashsum_flags(performance), # --checkers
            extra_env=rclone_tuning.md5_env(),
            hash_type=hash_type,
            files=files,
        )


    def md5sum_with_credentials(self,
            credentials,
            *,
            resource_path,
            local,
            user,
            job_id,
            download=False,
            extra_flags=(),
            extra_env=None,
            hash_type='md5',
            files=None,
    ):
        """
        Starts `rclone md5sum` (or `rclone hashsum sha256|crc32c`, `hash_type`, see
        job_runner.hashsum_type) as `user`; the remote, if any, is `src` in `credentials`
        (RCLONE_CONFIG_SRC_*) for either side of an integrity check. `files`: only these
        paths (relative to `resource_path`, a folder), given to rclone on its standard
        input (--files-from-raw -), never as arguments (job_runner.run_hashsum's MD5
        fallback). See copy_with_credentials for the other parameters.
        """
        command_words = _HASHSUM_COMMANDS[hash_type] # KeyError for anything else
        file_list = None
        if files is not None:
            file_list = files_from_list(files)
        credentials = dict(credentials)
        option_exclude_dot_snapshot = '' # HACKHACK: remove once https://github.com/rclone/rclone/issues/2425 is addressed
        option_download = ''

        if local:
            src = _local_path(resource_path)
            download = False
            # Not with a list of files: rclone refuses other filters with --files-from-raw,
            # and the list comes from a run that excluded .snapshot already
            if os.path.isdir(src) and files is None:
                option_exclude_dot_snapshot = '--exclude=\\.snapshot/'
        else:
            src = 'src:{}'.format(resource_path)

        if download:
            option_download = '--download'

        command = [
            *sudo_as(user, {**credentials, **(extra_env or {})}), # extra_env too (not logged)
            RCLONE,
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            *_checked_flags(extra_flags),
            *command_words,
            src,
            option_exclude_dot_snapshot,
            option_download,
            *(('--files-from-raw', '-') if file_list is not None else ()),
        ]

        command = [cmd for cmd in command if len(cmd) > 0]

        self._log_command(command, credentials)
        credentials.update(extra_env or {})

        try:
            self._hashsum_job_queue.push(command, credentials, job_id, stdin=file_list, hash_type=hash_type)
        except RcloneException as e:
            raise RcloneException(str(e))

        return job_id


    def hashsum_text(self, job_id):
        return self._hashsum_job_queue.hashsum_text(job_id)

    def hashsum_error_text(self, job_id):
        return self._hashsum_job_queue.hashsum_error_text(job_id)

    def hashsum_percent(self, job_id):
        return self._hashsum_job_queue.hashsum_percent(job_id)

    def hashsum_stop(self, job_id):
        self._hashsum_job_queue.hashsum_stop(job_id)

    def hashsum_finished(self, job_id):
        return self._hashsum_job_queue.hashsum_finished(job_id)

    def hashsum_exitstatus(self, job_id):
        return self._hashsum_job_queue.hashsum_exitstatus(job_id)

    def hashsum_delete(self, job_id):
        return self._hashsum_job_queue.hashsum_delete(job_id)


    def terminate_all(self):
        self._copy_job_queue.terminate_all()
        self._hashsum_job_queue.terminate_all()


    def _log_command(self, command, credentials):
        sanitized_credentials = {}
        for key, value in credentials.items():
            if should_never_log_credential(key):
                sanitized_credentials[key] = '***'
            elif should_log_full_credential(key):
                sanitized_credentials[key] = mask_url_secrets(value)
            elif should_log_partial_credential(key):
                sanitized_credentials[key] = '***' + value[-4:]
            else:
                sanitized_credentials[key] = '***'

        bash_command = "{} {}".format(
            ' '.join("{}='{}'".format(key, value) for key, value in sanitized_credentials.items()),
            ' '.join(command),
        )
        logging.info(bash_command)
        return bash_command


    def _formatCredentials(self, data, name, token_broker=None):
        """
        Credentials are of the form
        RCLONE_CONFIG_CURRENT_TYPE=s3
            ^          ^        ^   ^
        [mandatory  ][name  ][key][value]

        @param token_broker: for OAuth connections of brokered types, a function
            (connection) -> (refresh_token, token_url) that replaces the loopback
            token broker, e.g. a remote worker's job-scoped HTTPS broker
            (managers/worker_manager.py). Default: the connection's handle and
            TOKEN_BROKER_URL.
        """

        prefix = "RCLONE_CONFIG_{}".format(name.upper())

        credentials = {}
        credentials['{}_TYPE'.format(prefix)] = data.type

        def _addCredential(env_key, data_key, *, value_functor=None):
            value = getattr(data, data_key, None)
            if value is not None and value != '':
                if value_functor is not None:
                    value = value_functor(value)
                credentials[env_key] = value


        def _addProfile():
            # Read from the owner's home directory now, as the owner (never stored)
            options, env = local_credentials.resolve(
                data.owner, data.type, data.profile_source, data.profile_name)
            credentials.update(env)
            for key, value in options.items():
                credentials['{}_{}'.format(prefix, key.upper())] = value


        if data.type == 's3':
            if data.subtype == 'profile':
                _addProfile()
            else:
                _addCredential(
                    '{}_ACCESS_KEY_ID'.format(prefix),
                    's3_access_key_id'
                )
                _addCredential(
                    '{}_SECRET_ACCESS_KEY'.format(prefix),
                    's3_secret_access_key'
                )
                if data.subtype == 'sts':
                    _addCredential(
                        '{}_SESSION_TOKEN'.format(prefix),
                        's3_session_token'
                    )
            # The connection's region and endpoint win over a profile's
            _addCredential(
                '{}_REGION'.format(prefix),
                's3_region'
            )
            if data.kms_encryption_key_arn:
                credentials['{}_SERVER_SIDE_ENCRYPTION'.format(prefix)] = "aws:kms"
                _addCredential(
                    '{}_SSE_KMS_KEY_ID'.format(prefix),
                    'kms_encryption_key_arn'
                )
            _addCredential(
                '{}_ENDPOINT'.format(prefix),
                's3_endpoint'
            )
            _addCredential(
                '{}_V2_AUTH'.format(prefix),
                's3_v2_auth'
            )
            provider = getattr(data, 's3_provider', None)
            if provider not in s3_providers.PROVIDER_VALUES:
                provider = None
            if '{}_PROVIDER'.format(prefix) in credentials and data.subtype == 'profile' \
                    and provider in (None, 'AWS', 'Other'):
                pass # from an rclone remote, which knows better than the defaults
            elif provider is not None:
                credentials['{}_PROVIDER'.format(prefix)] = provider
            else:
                credentials['{}_PROVIDER'.format(prefix)] = s3_providers.legacy_provider(
                    credentials.get('{}_ENDPOINT'.format(prefix)))
            # Chunked uploads (multipart) from 32 MiB on, or in one piece up to 5 GiB so
            # that S3 keeps the MD5 (ETag) Check Integrity needs (s3_providers)
            credentials['{}_UPLOAD_CUTOFF'.format(prefix)] = (
                s3_providers.UNCHUNKED_UPLOAD_CUTOFF if getattr(data, 's3_chunked', None) is False
                else s3_providers.CHUNKED_UPLOAD_CUTOFF)

        elif data.type == 'azureblob':
            if data.subtype == 'profile':
                _addProfile()
                if data.profile_source == 'azure-cli': # the login has no storage account
                    _addCredential(
                        '{}_ACCOUNT'.format(prefix),
                        'azure_account'
                    )
            elif data.subtype == 'sas':
                _addCredential(
                    '{}_SAS_URL'.format(prefix),
                    'azure_sas_url'
                )
            else:
                _addCredential(
                    '{}_ACCOUNT'.format(prefix),
                    'azure_account'
                )
                _addCredential(
                    '{}_KEY'.format(prefix),
                    'azure_key'
                )

        elif data.type == 'swift':
            _addCredential(
                '{}_USER'.format(prefix),
                'swift_user'
            )
            _addCredential(
                '{}_KEY'.format(prefix),
                'swift_key'
            )
            _addCredential(
                '{}_AUTH'.format(prefix),
                'swift_auth'
            )
            _addCredential(
                '{}_TENANT'.format(prefix),
                'swift_tenant'
            )

        elif data.type == 'google cloud storage':
            _addCredential(
                '{}_CLIENT_ID'.format(prefix),
                'gcp_client_id'
            )
            _addCredential(
                '{}_SERVICE_ACCOUNT_CREDENTIALS'.format(prefix),
                'gcp_service_account_credentials'
            )
            _addCredential(
                '{}_PROJECT_NUMBER'.format(prefix),
                'gcp_project_number'
            )
            _addCredential(
                '{}_OBJECT_ACL'.format(prefix),
                'gcp_object_acl'
            )
            _addCredential(
                '{}_BUCKET_ACL'.format(prefix),
                'gcp_bucket_acl'
            )

        elif data.type == 'sftp':
            # Apparently AWS SFTP buckets (really an SFTP interface to an S3 bucket)
            # do not support setting modification times after an upload.
            # So we are disabling this for all SFTP uploads.
            credentials['{}_SFTP_SET_MODTIME'.format(prefix)] = "false"
            _addCredential(
                '{}_HOST'.format(prefix),
                'sftp_host',
            )
            _addCredential(
                '{}_PORT'.format(prefix),
                'sftp_port',
            )
            _addCredential(
                '{}_USER'.format(prefix),
                'sftp_user',
            )
            _addCredential(
                '{}_PASS'.format(prefix),
                'sftp_pass',
                value_functor=self._obscure,
            )
            _addCredential(
                '{}_KEY_FILE'.format(prefix),
                'sftp_key_file',
            )

        elif data.type == 'dropbox':
            _addCredential(
                '{}_TOKEN'.format(prefix),
                'dropbox_token',
            )

        elif data.type == 'onedrive':
            from ..managers.token_broker_manager import broker_token
            brokered = broker_token(data, via=token_broker)
            if brokered is not None:
                credentials['{}_TOKEN'.format(prefix)], credentials['{}_TOKEN_URL'.format(prefix)] = brokered
            else:
                _addCredential(
                    '{}_TOKEN'.format(prefix),
                    'onedrive_token',
                )
            # Large chunks for big files; must be a multiple of 320 KiB
            credentials['{}_CHUNK_SIZE'.format(prefix)] = '50Mi'
            _addCredential(
                '{}_DRIVE_ID'.format(prefix),
                'onedrive_drive_id',
            )
            _addCredential(
                '{}_DRIVE_TYPE'.format(prefix),
                'onedrive_drive_type',
            )

        elif data.type == 'drive': # Google Drive
            from ..managers.token_broker_manager import broker_token
            brokered = broker_token(data, via=token_broker)
            if brokered is not None:
                credentials['{}_TOKEN'.format(prefix)], credentials['{}_TOKEN_URL'.format(prefix)] = brokered
            else:
                _addCredential(
                    '{}_TOKEN'.format(prefix),
                    'gdrive_token',
                )
            # The scope the token was issued for (Sign in with Google, `rclone config`)
            credentials['{}_SCOPE'.format(prefix)] = 'drive'
            # Own Google app: tell rclone its (public) client id, otherwise rclone warns that
            # its own shared client is being retired. The secret stays with the token broker,
            # which substitutes it when refreshing.
            if getattr(data, 'gdrive_client_id', None) and brokered is not None:
                credentials['{}_CLIENT_ID'.format(prefix)] = data.gdrive_client_id
            _addCredential(
                '{}_ROOT_FOLDER_ID'.format(prefix),
                'gdrive_root_folder_id',
            )
            _addCredential(
                '{}_TEAM_DRIVE'.format(prefix),
                'gdrive_team_drive',
            )
            credentials.update(_drive_tuning(prefix))

        elif data.type == 'webdav':
            _addCredential(
                '{}_URL'.format(prefix),
                'webdav_url',
            )
            _addCredential(
                '{}_USER'.format(prefix),
                'webdav_user',
            )
            _addCredential(
                '{}_PASS'.format(prefix),
                'webdav_pass',
                value_functor=self._obscure,
            )

        else:
            logging.error("Connection type unknown: {}".format(data.type))

        if getattr(data, 'skip_tls_verify', None) is True:
            credentials = _without_tls_verification(credentials, name)

        return credentials


    def _job_id_exists(self, job_id):
        return job_id in self._job_status

    def _obscure(self, password):
        """
        rclone's obscured form of a password, computed here (utils/rclone_obscure.py): the
        password must not appear on the command line of a process
        """
        return rclone_obscure.obscure(password)


    def _execute(self, command, env=None):
        if env is None:
            env = {}

        full_env = user_process_env(env)
        try:
            byteOutput = check_output(command, stderr=subprocess.PIPE, env=full_env)
            output = byteOutput.decode('UTF-8').rstrip()
            return output
        except subprocess.CalledProcessError as err:
            if (err.stderr is None):
                raise
            stderr = err.stderr.decode('UTF-8').strip()
            if len(stderr) == 0:
                raise
            raise RcloneException(stderr)


# The rclone command of each hash of an integrity check (job_runner.hashsum_type)
_HASHSUM_COMMANDS = {'md5': ('md5sum',), 'sha256': ('hashsum', 'sha256'), 'crc32c': ('hashsum', 'crc32c')}


def files_from_list(names):
    """
    The --files-from-raw list of `names` (paths relative to the checked folder, as
    rclone printed them): one per line, no "./" prefix; a name that cannot be one line
    is an error (job_runner.run_hashsum never passes one: rclone prints them on one line)
    """
    lines = []
    for name in names:
        if not isinstance(name, str) or not name or any(c in name for c in '\n\r\0'):
            raise RcloneException('Invalid file name for the MD5 check')
        lines.append(name)
    return ''.join(line + '\n' for line in lines).encode('utf-8', errors='surrogateescape')


_supported_hashes = {} # rclone path -> frozenset, see supported_hashes


def supported_hashes(rclone=None, timeout=30):
    """
    The hashes this node's rclone knows (`rclone hashsum` without arguments lists them),
    cached per rclone path once it answered; an empty set if it could not be asked. Run
    as this account (no user data, no configuration), like node_check.rclone_version.
    """
    rclone = rclone or RCLONE
    if rclone in _supported_hashes:
        return _supported_hashes[rclone]
    try:
        result = subprocess.run([rclone, '--config=/dev/null', 'hashsum'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout, env=user_process_env())
    except (OSError, subprocess.SubprocessError) as e:
        logging.warning('Could not ask rclone for its hashes: %s', e)
        return frozenset()
    hashes = parse_supported_hashes(result.stdout)
    if result.returncode == 0 and hashes:
        _supported_hashes[rclone] = hashes
    return hashes


def parse_supported_hashes(output):
    """The hashes listed in the output of `rclone hashsum` ("Supported hashes are:", "- md5", ...)"""
    lines = (output or '').splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == 'Supported hashes are:')
    except StopIteration:
        return frozenset()
    hashes = set()
    for line in lines[start + 1:]:
        match = re.match(r'^\s*-\s*([a-z0-9]+)\s*$', line)
        if match is None:
            break
        hashes.add(match[1])
    return frozenset(hashes)


def crc32c_supported(rclone=None):
    """rclone can hash CRC32C (and then reads the CRC32C S3 stored, job_runner.hashsum_type)"""
    return 'crc32c' in supported_hashes(rclone)

_metadata_copy_supported = {}

# The first fork release whose local backend, run as a user other than root, sets only
# the ownership that user may set (else --metadata fails every file of another user)
# and keeps the setgid bit of directories
METADATA_COPY_RELEASE = re.compile(r'^rclone v\S*-md5simd\.(\d+)$')
METADATA_COPY_MIN = 6


def metadata_copy_supported(rclone=None, timeout=30):
    """
    Whether this node's rclone can copy POSIX metadata between local paths as a user
    (local_to_local_flags): the pinned release of github.com/dirkpetersen/rclone from
    v1.76.0-md5simd.6 on (`rclone version`, cached per rclone path once it answered)
    """
    rclone = rclone or RCLONE
    if rclone in _metadata_copy_supported:
        return _metadata_copy_supported[rclone]
    try:
        result = subprocess.run([rclone, '--config=/dev/null', 'version'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout, env=user_process_env())
    except (OSError, subprocess.SubprocessError) as e:
        logging.warning('Could not ask rclone for its version: %s', e)
        return False
    first = (result.stdout or '').split('\n')[0].strip()
    if result.returncode != 0 or not first.startswith('rclone '):
        return False
    match = METADATA_COPY_RELEASE.match(first)
    _metadata_copy_supported[rclone] = bool(match) and int(match[1]) >= METADATA_COPY_MIN
    return _metadata_copy_supported[rclone]


def local_to_local_flags(copy_links, rclone=None):
    """
    rclone flags of a copy between two local paths that keep POSIX metadata, if this
    rclone can (metadata_copy_supported; else none, as before): --metadata (permission
    bits including the executable bit, the group if the user is a member, atime/mtime,
    user.* extended attributes; the setgid bit of directories; never the owner, the copy
    belongs to the user) and, unless symlinks are followed (--copy-links), --links
    (symlinks stay symlinks instead of being skipped). Never
    --local-metadata-restore-special-bits: setuid/setgid files and the sticky bit are
    stripped, as rclone may run as root (root login helper). Not kept: POSIX ACLs, hard
    links, sparseness, empty directories (copyto has no --create-empty-src-dirs).
    """
    if not metadata_copy_supported(rclone):
        return []
    return ['--metadata'] if copy_links else ['--metadata', '--links']


def local_to_local_env(rclone=None):
    """
    The hash rclone checks a copy between two local paths with (copy_with_credentials):
    CRC32C if this rclone has it (SSE 4.2 / ARMv8 CRC instructions; measured 3 GB, 16
    transfers: 0.4 CPU-s, as fast as no check), else SHA-256 (SHA instructions: 3.5
    CPU-s), never rclone's default MD5 (7.8 CPU-s, 50% longer)
    """
    return {'RCLONE_LOCAL_HASHES': 'crc32c' if crc32c_supported(rclone) else 'sha256'}

# rclone's exit status for "directory not found" (docs: rclone.org/docs/#exit-code)
_RCLONE_DIRECTORY_NOT_FOUND = 3


def destination_checks(dst_data):
    """
    rclone flags and process variables for skipping checksum checks of uploads to the
    destination connection `dst_data` (None: local), only S3 on AWS or Ceph, which verify
    Content-MD5 on every request themselves (s3_providers):
    - 'post_copy': --ignore-checksum, no MD5 comparison after each file (rclone's
      flag is global, so only ever for this destination's copy jobs)
    - 'crc32c' (AWS): also RCLONE_S3_PART_CHECKSUM=crc32c, each part of a multipart
      upload carries its CRC32C instead of its MD5, which S3 verifies just the same and
      which costs almost no CPU
    - 'all': also RCLONE_S3_PART_CHECKSUM=none and AWS_REQUEST_CHECKSUM_CALCULATION=
      when_required, i.e. no checksum with the parts, no CRC32 and, for uploads in one
      piece, no Content-MD5: S3 no longer verifies the data
    --s3-part-checksum is an option of the rclone build at github.com/dirkpetersen/rclone
    (branch s3-md5-simd); a stock rclone ignores the variable and sends the parts' MD5s.
    It applies to every S3 remote, but only uploads use it, and a copy uploads only to
    the destination.
    """
    if dst_data is None or getattr(dst_data, 'type', None) != 's3':
        return [], {}
    if getattr(dst_data, 's3_provider', None) not in s3_providers.CHECK_SKIPPING_PROVIDERS:
        return [], {}
    skip = getattr(dst_data, 's3_skip_checks', None)
    if skip == s3_providers.SKIP_POST_COPY:
        return ['--ignore-checksum'], {}
    if skip == s3_providers.SKIP_CRC32C and dst_data.s3_provider in s3_providers.CRC32C_PROVIDERS:
        return ['--ignore-checksum'], {'RCLONE_S3_PART_CHECKSUM': 'crc32c'}
    if skip == s3_providers.SKIP_ALL:
        return ['--ignore-checksum'], {'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required',
                                       'RCLONE_S3_PART_CHECKSUM': 'none'}
    return [], {}


# Process variables a job ticket may carry for rclone (destination_checks,
# rclone_tuning.md5_env), with the values a worker accepts
JOB_ENV_VALUES = {
    'AWS_REQUEST_CHECKSUM_CALCULATION': ('when_required',),
    'RCLONE_S3_PART_CHECKSUM': ('crc32c', 'none'),
    'RCLONE_MD5_SIMD': ('true',),
    'RCLONE_MD5_SIMD_SERVERS': tuple(str(n) for n in range(1, 17)),
}


def checked_job_env(env):
    """A job ticket's process variables for rclone, only those of JOB_ENV_VALUES"""
    env = dict(env or {})
    for key, value in env.items():
        if value not in JOB_ENV_VALUES.get(key, ()):
            raise RcloneException('Invalid rclone environment in the job ticket: {}'.format(key))
    return env


# "Test connection" makes one quick attempt: the dialog repeats it (at most 5 times,
# showing each error at once) only while the error may be transient. rclone's default
# of 10 low-level retries with backoff took minutes to report e.g. a certificate error.
VERIFY_FLAGS = ('--low-level-retries=1', '--retries=1', '--contimeout=15s', '--timeout=30s')

# Errors a retry cannot fix (wrong certificate, credentials, bucket or host name)
_PERMANENT_ERRORS = (
    'x509:',
    'tls:',
    'certificate',
    'InvalidAccessKeyId',
    'SignatureDoesNotMatch',
    'AccessDenied',
    'NoSuchBucket',
    'StatusCode: 401',
    'StatusCode: 403',
    'StatusCode: 404',
    'no such host',
    'invalid_grant',
    'AuthenticationFailed',
    'AuthorizationFailure',
)


def is_permanent_error(message):
    """True if rclone's error message names an error that trying again cannot fix"""
    return any(marker in message for marker in _PERMANENT_ERRORS)


# Suffix of the remote that holds a connection's real configuration when its TLS
# certificate is not verified (see _without_tls_verification)
_TLS_SKIPPING_SUFFIX = 'tlsoff'


def _without_tls_verification(credentials, name):
    """
    The configuration of remote `name` with TLS certificate verification turned off for
    that remote only. rclone's --no-check-certificate is global (a copy would also stop
    checking the other side), and an RCLONE_CONFIG_<NAME>_OVERRIDE.<FLAG> variable has a
    dot in its name. So the configuration moves to the remote `<name>tlsoff`, and `name`
    becomes an alias of it with a per-remote override (rclone connection string), so
    paths like `src:bucket/key` keep working: everything is still plain RCLONE_CONFIG_*
    variables, also in a remote worker's ticket.
    """
    prefix = 'RCLONE_CONFIG_{}_'.format(name.upper())
    inner = name + _TLS_SKIPPING_SUFFIX
    inner_prefix = 'RCLONE_CONFIG_{}_'.format(inner.upper())
    result = {}
    for key, value in credentials.items():
        if key.startswith(prefix):
            key = inner_prefix + key[len(prefix):]
        result[key] = value
    result[prefix + 'TYPE'] = 'alias'
    result[prefix + 'REMOTE'] = '{},override.no_check_certificate=true:'.format(inner)
    return result


# The ModTime rclone gives directories of bucket storage (S3 prefixes and the like),
# which have no modification time of their own; a pane would show "26 years ago"
_PLACEHOLDER_DIR_TIME = '2000-01-01T00:00:00Z'


def _with_modified(files):
    """
    rclone lsjson entries plus `modified`: their ModTime (RFC 3339 with any offset)
    in ISO 8601 UTC, or None if unknown (see file_times), also for a directory with
    rclone's placeholder time
    """
    for entry in files:
        modified = rfc3339_to_iso_utc(entry.get('ModTime'))
        if entry.get('IsDir') and modified == _PLACEHOLDER_DIR_TIME:
            modified = None
        entry['modified'] = modified
    return files


def _drive_tuning(prefix):
    """
    Google Drive settings of a remote, following rclone's Drive documentation:
    - Google's default quota is 10 API transactions per second per client id, so pace
      API calls at the documented default of one per 100ms (also --tpslimit below)
    - larger upload chunks are faster (each is buffered in memory, per transfer; must
      be a power of 2); the default is 8Mi
    - Drive allows about 750 GiB of uploads per user and day: fail the job when it is
      reached instead of retrying for hours
    """
    return {
        '{}_PACER_MIN_SLEEP'.format(prefix): '100ms',
        '{}_CHUNK_SIZE'.format(prefix): '64Mi',
        '{}_STOP_ON_UPLOAD_LIMIT'.format(prefix): 'true',
    }


# Transactions per second rclone may make when a remote is of this type
_TPS_LIMITS = {
    'onedrive': 10, # Microsoft Graph throttles aggressively (HTTP 429)
    'drive': 10,    # Google's default quota per client id (rclone docs, "Making your own client_id")
}


def _rate_limit_flags(credentials):
    """
    Stay below the API limits of throttling providers (--tpslimit applies to the
    whole rclone process, i.e. both remotes of a copy)
    """
    limits = [
        _TPS_LIMITS[value] for key, value in credentials.items()
        if key.endswith('_TYPE') and value in _TPS_LIMITS
    ]
    if limits:
        return ['--tpslimit', str(min(limits))]
    return []


def _checked_flags(flags):
    """
    Extra rclone options (performance settings, from the job ticket on a remote
    worker): each must be an option, never a positional argument such as a remote
    """
    flags = list(flags or ())
    for flag in flags:
        if not isinstance(flag, str) or not flag.startswith('--') or '\x00' in flag:
            raise RcloneException("Not an rclone option: {!r}".format(flag))
    return flags


def _local_path(path):
    """
    Local paths are passed to rclone as positional arguments, so they must be
    absolute to never be interpreted as an option (e.g. "--config=...")
    """
    if not path or not path.startswith('/'):
        raise RcloneException("Local path must be absolute: '{}'".format(path))
    return path


def should_never_log_credential(key):
    """
    Secrets whose names end with an allowlisted suffix (the SAS URL ends in '_URL')
    """
    return any(key.endswith(suffix) for suffix in (
        '_SAS_URL',
        '_CONNECTION_STRING',
        '_CLIENT_SECRET',
        '_ROLE_EXTERNAL_ID',
    ))


# Process variables (not RCLONE_CONFIG_*) that are safe to log
_LOGGABLE_VARIABLES = frozenset((
    'HOME',
    'AWS_CONFIG_FILE',
    'AWS_SHARED_CREDENTIALS_FILE',
    'AWS_EC2_METADATA_DISABLED',
    'AZURE_CONFIG_DIR',
))


_URL_USERINFO = re.compile(r'(?<=://)[^/?#\s]*@')
_URL_QUERY = re.compile(r'\?[^#\s]*')


def mask_url_secrets(value):
    """
    `https://user:pass@host/x?token=y` -> `https://***@host/x?***`: a URL (WebDAV, S3
    endpoint) may carry credentials in its user info or query; other values are unchanged
    """
    if not isinstance(value, str) or '://' not in value:
        return value
    return _URL_QUERY.sub('?***', _URL_USERINFO.sub('***@', value))


def should_log_full_credential(key):
    """
    Returns true if we should log the value of the credential given the key (name) of the credential
    For robustness, prefer an allowlist over a blocklist
    """

    suffix_allowlist = [
        # generic
        '_TYPE',

        # s3
        '_PROVIDER',
        '_REGION',
        '_ENDPOINT',
        '_V2_AUTH',
        '_ROLE_ARN',
        '_ROLE_SESSION_NAME',
        '_ROLE_SESSION_DURATION',
        '_ENV_AUTH',
        '_PROFILE',
        '_SHARED_CREDENTIALS_FILE',
        '_FORCE_PATH_STYLE',
        '_UPLOAD_CUTOFF',

        # azureblob
        '_ACCOUNT',
        '_USE_EMULATOR',
        '_USE_AZ',

        # swift
        '_USER',
        '_AUTH',
        '_TENANT',

        # google cloud storage
        '_PROJECT_NUMBER',
        '_OBJECT_ACL',
        '_BUCKET_ACL',

        # sftp
        '_HOST',
        '_PORT',
        '_USER',
        '_KEY_FILE',

        # dropbox

        # onedrive
        '_DRIVE_ID',
        '_DRIVE_TYPE',
        '_CHUNK_SIZE',

        # drive (Google Drive); the token URL is the broker's (ends in '_URL')
        '_SCOPE',
        '_PACER_MIN_SLEEP',
        '_STOP_ON_UPLOAD_LIMIT',

        # webdav
        '_URL',
        '_USER',

        # alias (a connection that skips TLS verification, _without_tls_verification)
        '_REMOTE',
    ]

    return key in _LOGGABLE_VARIABLES or any(key.endswith(suffix) for suffix in suffix_allowlist)


def should_log_partial_credential(key):
    """
    Returns true if we should log the last 4 characters the value of the credential
    given the key (name) of the credential.
    For robustness, prefer an allowlist over a blocklist
    """

    suffix_allowlist = [
        # s3
        '_ACCESS_KEY_ID',

        # google cloud storage
        '_CLIENT_ID',

        # drive: rclone treats folder and shared drive ids as sensitive
        '_ROOT_FOLDER_ID',
        '_TEAM_DRIVE',
    ]

    return any(key.endswith(suffix) for suffix in suffix_allowlist)



def main():
    """
    Can run as
    export MOTUZ_REGION='<add-here>'
    export MOTUZ_ACCESS_KEY_ID='<add-here>'
    export MOTUZ_SECRET_ACCESS_KEY='<add-here>'

    python -m utils.rclone_connection
    """

    import time
    import os

    class CloudConnection:
        pass

    data = CloudConnection()
    data.__dict__ = {
        'type': 's3',
        'owner': 'aicioara',
        's3_region': os.environ['MOTUZ_REGION'],
        's3_access_key_id': os.environ['MOTUZ_ACCESS_KEY_ID'],
        's3_secret_access_key': os.environ['MOTUZ_SECRET_ACCESS_KEY'],
    }

    connection = RcloneConnection()

    # result = connection.ls(data, '/motuz-test/')
    # print(result)
    # return

    import random
    import json
    id = connection.md5sum(
        data,
        'motuz-test/test/',
        'aicioara',
        random.randint(1, 10000000),
        download=True,
    )
    while not connection.hashsum_finished(id):
        print(json.dumps(connection.hashsum_text(id)))
        time.sleep(1)
    print(json.dumps(connection.hashsum_text(id)))

    # connection.copy(
    #     src_data=None, # Local
    #     src_resource_path='/tmp/motuz/mb_blob.bin',
    #     dst_data=data,
    #     dst_resource_path='/fh-ctr-mofuz-test/hello/world/{}'.format(random.randint(10, 10000)),
    # )

    # while not connection.copy_finished(job_id):
    #     print(connection.copy_percent(job_id))
    #     time.sleep(0.1)



if __name__ == '__main__':
    main()
