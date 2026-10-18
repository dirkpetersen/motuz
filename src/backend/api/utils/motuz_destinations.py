"""
Which storage and path a copy writes to, for the overwrite policy
(managers/overwrite_policy.py, README "Overwriting existing files").

Copies overwrite existing files only below a path that an earlier Motuz copy job or
upload wrote to, by any user, on the same storage, unless MOTUZ_ALLOW_GENERAL_OVERWRITES
is true. This module finds the storage of a connection independent of its owner
(storage_key) and normalizes paths (normalized_path, ancestors). Stdlib only: config.py
imports it.
"""
import os
import posixpath
from urllib.parse import urlsplit


SETTING = 'MOTUZ_ALLOW_GENERAL_OVERWRITES'
LOCAL = 'local:'
_TRUE = ('true', '1', 'yes', 'on')
_FALSE = ('false', '0', 'no', 'off', '')


class OverwriteConfigError(ValueError):
    pass


def allow_general_overwrites(environ=None):
    """MOTUZ_ALLOW_GENERAL_OVERWRITES (default false); another value stops the app"""
    raw = (environ if environ is not None else os.environ).get(SETTING) or ''
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise OverwriteConfigError('{}={!r} must be true or false'.format(SETTING, raw))


def _host(url, default_scheme='https'):
    """scheme://host[:port]/path of a URL, lowercase scheme and host, default ports dropped"""
    url = (url or '').strip()
    if not url:
        return None
    parts = urlsplit(url if '://' in url else default_scheme + '://' + url)
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    if not host:
        return None
    scheme = parts.scheme.lower()
    if port is not None and (scheme, port) not in (('https', 443), ('http', 80)):
        host = '{}:{}'.format(host, port)
    return '{}://{}{}'.format(scheme, host, parts.path.rstrip('/'))


def _aws_partition(region):
    region = (region or '').strip().lower()
    if region.startswith('cn-'):
        return 'aws-cn'
    if region.startswith('us-gov-'):
        return 'aws-us-gov'
    return 'aws'


def storage_key(connection):
    """
    The storage a connection (None: the local filesystem) reads and writes, the same for
    every connection to it whoever owns it, e.g. 's3:aws', 's3:https://s3.lab.example',
    'azureblob:account', 'sftp:host:22'. Buckets and containers are the first segment of
    the path in Motuz (/bucket/dir), so they are not part of the key. None if the storage
    cannot be identified from what the connection stores (e.g. Dropbox, a profile whose
    endpoint comes from the user's home directory): nothing there counts as written by
    Motuz.
    """
    if connection is None:
        return LOCAL
    kind = connection.type
    profile = connection.subtype == 'profile'
    if kind == 's3':
        endpoint = _host(connection.s3_endpoint)
        if endpoint:
            return 's3:' + endpoint
        if profile: # ~/.aws or an rclone remote may name an endpoint
            return None
        if connection.s3_provider not in (None, '', 'AWS'):
            return None
        return 's3:' + _aws_partition(connection.s3_region)
    if kind == 'azureblob':
        if connection.subtype == 'sas': # the SAS URL may be for one container
            url = _host(connection.azure_sas_url)
            return 'azureblob:' + url if url else None
        if profile and connection.profile_source != 'azure-cli': # account from the rclone remote
            return None
        account = (connection.azure_account or '').strip().lower()
        return 'azureblob:' + account if account else None
    if kind == 'swift':
        auth = _host(connection.swift_auth)
        account = (connection.swift_tenant or connection.swift_user or '').strip()
        return 'swift:{}|{}'.format(auth, account) if auth and account else None
    if kind == 'google cloud storage':
        return 'gcs:' # bucket names are global
    if kind == 'sftp':
        host = (connection.sftp_host or '').strip().lower()
        port = (connection.sftp_port or '').strip() or '22'
        return 'sftp:{}:{}'.format(host, port) if host and port.isdigit() else None
    if kind == 'onedrive':
        drive = (connection.onedrive_drive_id or '').strip()
        return 'onedrive:' + drive if drive else None
    if kind == 'drive': # Google Drive: only shared drives and root folders have global ids
        team = (connection.gdrive_team_drive or '').strip()
        folder = (connection.gdrive_root_folder_id or '').strip()
        return 'drive:{}|{}'.format(team, folder) if team or folder else None
    if kind == 'webdav':
        url = _host(connection.webdav_url)
        return 'webdav:' + url if url else None
    return None # dropbox (the account is not stored), unknown types


def normalized_path(path, local):
    """
    The path as stored and compared: one leading slash, no trailing or double slashes.
    None for '.' or '..' segments (never resolved: /data/a/../b is refused, not /data/b),
    and for a local path that is not absolute.
    """
    if not isinstance(path, str) or '\x00' in path:
        return None
    if local and not path.startswith('/'):
        return None
    parts = [part for part in path.split('/') if part]
    if any(part in ('.', '..') for part in parts):
        return None
    return posixpath.join('/', *parts)


def ancestors(path):
    """`path` (normalized) and every folder above it, '/' included: /a/b -> /a/b, /a, /"""
    result = [path]
    while path != '/':
        path = posixpath.dirname(path)
        result.append(path)
    return result
