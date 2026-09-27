import concurrent.futures
import functools
import json
import logging
import os
import subprocess
from collections import defaultdict

from .abstract_connection import AbstractConnection, RcloneException, user_process_env
from . import local_credentials
from .file_times import rfc3339_to_iso_utc
from . import file_view
from . import image_view
from . import document_view
from . import rclone_tuning
from .copy_job_queue import CopyJobQueue
from .hashsum_job_queue import HashsumJobQueue


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
            'sudo',
            '-E',
            '-u', user,
            '/usr/local/bin/rclone',
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
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
            }
        except RcloneException as e: # rclone failed and explained why on stderr
            return {
                'result': False,
                'message': str(e)[-1000:],
            }



    def ls(self, data, path):
        credentials = self._formatCredentials(data, name='current')
        user = data.owner
        command = [
            'sudo',
            '-E',
            '-u', user,
            '/usr/local/bin/rclone',
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
        (bytes, type) of a PNG, JPEG, GIF or WebP image, read by rclone as the user with
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
            'sudo',
            '-E',
            '-u', user,
            '/usr/local/bin/rclone',
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
            'sudo',
            '-E',
            '-u', user,
            '/usr/local/bin/rclone',
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


    def copy(self,
            src_data,
            src_resource_path,
            dst_data,
            dst_resource_path,
            user,
            copy_links,
            job_id,
            performance=None,
    ):
        """
        `performance`: the job's validated overrides (rclone_tuning), on top of the
        installation's MOTUZ_RCLONE_* defaults
        """
        if src_data is None:
            _local_path(src_resource_path) # before reading any credentials
        credentials = {}
        if src_data is not None:
            credentials.update(self._formatCredentials(src_data, name='src'))
        if dst_data is not None:
            credentials.update(self._formatCredentials(dst_data, name='dst'))
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
            extra_flags=rclone_tuning.copy_flags(performance, dst_data.type if dst_data is not None else None),
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

        if dst_local:
            dst = _local_path(dst_resource_path)
        else:
            dst = 'dst:{}'.format(dst_resource_path)

        if copy_links:
            option_copy_links = '--copy-links'
        else:
            option_copy_links = ''

        command = [
            'sudo',
            '-E',
            '-u', user,
            '/usr/local/bin/rclone',
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
            '--progress',
            '--stats', '2s',
        ]

        command = [cmd for cmd in command if len(cmd) > 0]

        self._log_command(command, credentials)
        credentials.update(extra_env or {})

        try:
            self._copy_job_queue.push(command, credentials, job_id)
        except RcloneException as e:
            raise RcloneException(str(e))

        return job_id


    def copy_text(self, job_id):
        return self._copy_job_queue.copy_text(job_id)

    def copy_error_text(self, job_id):
        return self._copy_job_queue.copy_error_text(job_id)

    def copy_percent(self, job_id):
        return self._copy_job_queue.copy_percent(job_id)

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
    ):
        """
        Starts `rclone md5sum` as `user`; the remote, if any, is `src` in `credentials`
        (RCLONE_CONFIG_SRC_*) for either side of an integrity check. See
        copy_with_credentials for the other parameters.
        """
        credentials = dict(credentials)
        option_exclude_dot_snapshot = '' # HACKHACK: remove once https://github.com/rclone/rclone/issues/2425 is addressed
        option_download = ''

        if local:
            src = _local_path(resource_path)
            download = False
            if os.path.isdir(src):
                option_exclude_dot_snapshot = '--exclude=\\.snapshot/'
        else:
            src = 'src:{}'.format(resource_path)

        if download:
            option_download = '--download'

        command = [
            'sudo',
            '-E',
            '-u', user,
            '/usr/local/bin/rclone',
            '--config=/dev/null',
            *_rate_limit_flags(credentials),
            *_checked_flags(extra_flags),
            'md5sum',
            src,
            option_exclude_dot_snapshot,
            option_download
        ]

        command = [cmd for cmd in command if len(cmd) > 0]

        self._log_command(command, credentials)
        credentials.update(extra_env or {})

        try:
            self._hashsum_job_queue.push(command, credentials, job_id)
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
                sanitized_credentials[key] = value
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
            if '{}_PROVIDER'.format(prefix) in credentials: # from an rclone remote
                pass
            elif '{}_ENDPOINT'.format(prefix) not in credentials:
                credentials['{}_PROVIDER'.format(prefix)] = 'AWS'
            else:
                credentials['{}_PROVIDER'.format(prefix)] = 'Other'

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

        return credentials


    def _job_id_exists(self, job_id):
        return job_id in self._job_status

    def _obscure(self, password):
        """
        Calls `rclone obscure password` and returns the result
        """
        return self._execute(["rclone", "obscure", password])


    def _execute(self, command, env=None):
        if env is None:
            env = {}

        full_env = user_process_env(env)
        try:
            byteOutput = subprocess.check_output(
                command,
                stderr=subprocess.PIPE,
                env=full_env
            )
            output = byteOutput.decode('UTF-8').rstrip()
            return output
        except subprocess.CalledProcessError as err:
            if (err.stderr is None):
                raise
            stderr = err.stderr.decode('UTF-8').strip()
            if len(stderr) == 0:
                raise
            raise RcloneException(stderr)


def _with_modified(files):
    """
    rclone lsjson entries plus `modified`: their ModTime (RFC 3339 with any offset)
    in ISO 8601 UTC, or None if unknown (see file_times)
    """
    for entry in files:
        entry['modified'] = rfc3339_to_iso_utc(entry.get('ModTime'))
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
