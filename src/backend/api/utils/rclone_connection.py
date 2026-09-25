import functools
import json
import logging
import os
import subprocess
from collections import defaultdict

from .abstract_connection import AbstractConnection, RcloneException, user_process_env
from . import local_credentials
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
            files = json.loads(result)
            return {
                'files': files,
                'path': path,
            }
        except subprocess.CalledProcessError as e:
            raise RcloneException(str(e))



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


    def copy(self,
            src_data,
            src_resource_path,
            dst_data,
            dst_resource_path,
            user,
            copy_links,
            job_id
    ):
        credentials = {}
        option_exclude_dot_snapshot = '' # HACKHACK: remove once https://github.com/rclone/rclone/issues/2425 is addressed

        if src_data is None: # Local
            src = _local_path(src_resource_path)
            if os.path.isdir(src):
                option_exclude_dot_snapshot = '--exclude=\\.snapshot/'
        else:
            credentials.update(self._formatCredentials(src_data, name='src'))
            src = 'src:{}'.format(src_resource_path)

        if dst_data is None: # Local
            dst = _local_path(dst_resource_path)
        else:
            credentials.update(self._formatCredentials(dst_data, name='dst'))
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
            'copyto',
            src,
            dst,
            option_copy_links,
            '--progress',
            '--stats', '2s',
        ]

        command = [cmd for cmd in command if len(cmd) > 0]

        self._log_command(command, credentials)

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
    ):
        credentials = {}
        option_exclude_dot_snapshot = '' # HACKHACK: remove once https://github.com/rclone/rclone/issues/2425 is addressed
        option_download = ''

        if data is None: # Local
            src = _local_path(resource_path)
            download = False
            if os.path.isdir(src):
                option_exclude_dot_snapshot = '--exclude=\\.snapshot/'
        else:
            credentials.update(self._formatCredentials(data, name='src'))
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
            'md5sum',
            src,
            option_exclude_dot_snapshot,
            option_download
        ]

        command = [cmd for cmd in command if len(cmd) > 0]

        self._log_command(command, credentials)

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


    def _formatCredentials(self, data, name):
        """
        Credentials are of the form
        RCLONE_CONFIG_CURRENT_TYPE=s3
            ^          ^        ^   ^
        [mandatory  ][name  ][key][value]
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
            brokered = broker_token(data)
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
            brokered = broker_token(data)
            if brokered is not None:
                credentials['{}_TOKEN'.format(prefix)], credentials['{}_TOKEN_URL'.format(prefix)] = brokered
            else:
                _addCredential(
                    '{}_TOKEN'.format(prefix),
                    'gdrive_token',
                )
            # The scope the token was issued for (Sign in with Google, `rclone config`)
            credentials['{}_SCOPE'.format(prefix)] = 'drive'
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
