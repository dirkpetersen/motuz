"""
Cloud credentials that users keep in their home directories.

Motuz offers these when a user creates an S3 or Azure Blob connection ("Found in your
home directory"). Only these fixed paths below the user's home are ever read:

    .aws/credentials, .aws/config        AWS profiles (static keys, roles, SSO)
    .aws/sso/cache/<sha1>.json           AWS SSO login state of an SSO profile
    .config/rclone/rclone.conf           rclone s3 / azureblob remotes
    .rclone.conf                         (legacy rclone location, if the above is missing)
    .azure/azureProfile.json             Azure CLI logins (tenants, subscriptions, user
                                         names; the tokens are in other files)

Security model
- Files are read only as the user, through `sudo -n -u <user>` like every other
  filesystem operation, so the OS enforces permissions. A symlink to a file the user
  cannot read (e.g. /etc/shadow) fails exactly as it would for the user.
- Discovery returns metadata only (profile name, kind, region, masked key id, Azure
  account). Secrets never leave the server.
- A connection stores only (profile_source, profile_name). `resolve()` reads the files
  again every time rclone runs, so rotated keys are picked up and no secret is stored
  in the database.
- rclone never gets the user's files. Motuz parses them and passes allowlisted values
  as RCLONE_CONFIG_* variables. Passing the files themselves (rclone env_auth) would
  make the AWS SDK execute `credential_process` commands, i.e. let any user run
  programs inside the Motuz containers. The only env_auth use is for SSO profiles,
  with a config file that Motuz writes from allowlisted SSO settings.
- Azure CLI logins (only if the image has the Azure CLI, build arg INSTALL_AZURE_CLI):
  rclone's use_az runs `az account get-access-token` as the user, with HOME and
  AZURE_CONFIG_DIR pointing at the user's ~/.azure (az reads and refreshes the MSAL
  token cache there, as the user). `azure_cli_env()` turns off what would run the
  user's code in the containers (az extensions from ~/.azure) and managed identity
  (the server's own identity).
"""
import configparser
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse

from .abstract_connection import RcloneException


class LocalCredentialsError(RcloneException):
    """User facing error. Messages never contain secret values."""


class _Unusable(Exception):
    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


AWS_CREDENTIALS = '.aws/credentials'
AWS_CONFIG = '.aws/config'
RCLONE_CONF = '.config/rclone/rclone.conf'
RCLONE_CONF_LEGACY = '.rclone.conf'
AZURE_PROFILE = '.azure/azureProfile.json'

SOURCES = ('aws', 'rclone', 'azure-cli')
MAX_FILE_SIZE = 1024 * 1024
READ_TIMEOUT = 20 # seconds

# Profile / remote names that may be stored in a connection. AWS SSO profile names end
# up in a config file and in an rclone option, so they are further limited (_SAFE_NAME).
PROFILE_NAME_RE = re.compile(r'^[A-Za-z0-9_.+@=,\- ]{1,128}$')
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_.+@=,\-]{1,128}$')
_SAFE_VALUE = re.compile(r'^[A-Za-z0-9_.:/@+=,#\-]{1,1024}$')
# Azure CLI logins are stored by tenant id; storage account names are 3-24 lower case
# letters and digits
_TENANT_ID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
STORAGE_ACCOUNT_RE = re.compile(r'^[a-z0-9]{3,24}$')

# Written config files for SSO profiles (no secrets: start URL, account, role, region)
SSO_CONFIG_DIR = os.environ.get('MOTUZ_SSO_CONFIG_DIR', '/tmp/motuz-aws-config')

# rclone remote options passed through to rclone, everything else is ignored. Never
# env_auth / use_msi: those would use the Motuz server's own identity. use_az is added
# by classify_rclone_remote when the Azure CLI is installed.
_RCLONE_S3_OPTIONS = (
    'provider', 'access_key_id', 'secret_access_key', 'session_token', 'region',
    'endpoint', 'location_constraint', 'force_path_style', 'v2_auth',
    'server_side_encryption', 'sse_kms_key_id', 'storage_class',
    'role_arn', 'role_session_name', 'role_session_duration', 'role_external_id',
)
_RCLONE_AZURE_OPTIONS = (
    'account', 'key', 'sas_url', 'connection_string', 'tenant', 'client_id',
    'client_secret', 'endpoint', 'use_emulator', 'access_tier',
)

_KIND_LABELS = {
    'static': 'static keys',
    'session': 'session token',
    'role': 'role',
    'sso': 'SSO',
    'process': 'credential process',
    'keys': 'access keys',
    'env_auth': 'env_auth',
    'account_key': 'account key',
    'sas_url': 'SAS URL',
    'connection_string': 'connection string',
    'service_principal': 'service principal',
    'emulator': 'emulator',
    'azure_cli': 'Azure CLI login',
    'unsupported': 'unsupported',
}


# ---------------------------------------------------------------- reading as the user

# Runs as the user. Opens each file non-blocking (a FIFO cannot hang it), accepts only
# regular files up to the size limit and prints {relpath: {content|error}} as JSON.
_READER = r'''
import json, os, stat, sys
cap, home = int(sys.argv[1]), sys.argv[2]
out = {}
for rel in sys.argv[3:]:
    try:
        fd = os.open(os.path.join(home, rel), os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except FileNotFoundError:
        out[rel] = {"error": "missing"}; continue
    except NotADirectoryError:
        out[rel] = {"error": "missing"}; continue
    except PermissionError:
        out[rel] = {"error": "permission denied"}; continue
    except OSError as e:
        out[rel] = {"error": e.strerror or "cannot open"}; continue
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            out[rel] = {"error": "not a regular file"}; continue
        if st.st_size > cap:
            out[rel] = {"error": "too large"}; continue
        chunks, size = [], 0
        while size <= cap:
            chunk = os.read(fd, cap + 1 - size)
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
        if size > cap:
            out[rel] = {"error": "too large"}; continue
        out[rel] = {"content": b"".join(chunks).decode("utf-8", "replace")}
    except OSError as e:
        out[rel] = {"error": e.strerror or "cannot read"}
    finally:
        os.close(fd)
sys.stdout.write(json.dumps(out))
'''


def _python():
    for candidate in ('/usr/local/bin/python3', '/usr/bin/python3'):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which('python3') or 'python3'


def home_directory(user):
    from .local_connection import _homepath_with_impersonation
    try:
        output = _homepath_with_impersonation(user)
    except (subprocess.CalledProcessError, OSError) as e:
        logging.error("Could not resolve the home directory of %s: %s", user, e)
        raise LocalCredentialsError('Could not find your home directory')
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    home = lines[-1] if lines else ''
    if not home.startswith('/') or home == '/':
        raise LocalCredentialsError('Could not find your home directory')
    return home


def read_home_files(user, relpaths, home=None):
    """
    Reads fixed relative paths below the user's home directory, as the user.
    Returns (home, {relpath: {'content': str} | {'error': str}}).
    """
    if home is None:
        home = home_directory(user)
    for rel in relpaths:
        if rel.startswith('/') or '..' in rel.split('/'):
            raise ValueError('Not a relative path below the home directory: {}'.format(rel))

    command = ['sudo', '-n', '-u', user, '--', 'env']
    # sudo drops LD_LIBRARY_PATH, which the image's python needs when /etc is the host's
    if os.environ.get('LD_LIBRARY_PATH'):
        command.append('LD_LIBRARY_PATH={}'.format(os.environ['LD_LIBRARY_PATH']))
    command += [_python(), '-I', '-S', '-c', _READER, str(MAX_FILE_SIZE), home, *relpaths]

    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=READ_TIMEOUT)
    except subprocess.TimeoutExpired:
        logging.error("Reading credential files of %s timed out", user)
        raise LocalCredentialsError('Reading your home directory timed out')
    if result.returncode != 0:
        # stderr of sudo / the reader, never file contents
        logging.error("Reading credential files of %s failed (%s): %s", user, result.returncode,
                      result.stderr.decode('utf-8', 'replace')[-500:])
        raise LocalCredentialsError('Could not read your home directory')
    try:
        files = json.loads(result.stdout.decode('utf-8'))
    except ValueError:
        raise LocalCredentialsError('Could not read your home directory')
    return home, files


# ---------------------------------------------------------------- parsing

def _parse_ini(content):
    """Returns {section: {key: value}} (keys lower case), or raises _ParseError."""
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
        delimiters=('=',),
        comment_prefixes=('#', ';'),
        inline_comment_prefixes=None,
        empty_lines_in_values=False,
        default_section='\x00motuz-no-default',
    )
    parser.optionxform = lambda option: option.strip().lower()
    try:
        parser.read_string(content.lstrip('﻿'))
    except configparser.Error:
        # The exception text quotes the offending line, which may be a secret
        raise _ParseError()
    return {
        section.strip(): {key: value.strip() for key, value in parser.items(section) if value is not None}
        for section in parser.sections()
    }


class _ParseError(Exception):
    pass


def mask(value):
    if not value:
        return None
    return '****' + value[-4:]


def parse_aws(credentials_text, config_text):
    """
    Returns (profiles, sso_sessions): {name: settings}. The credentials file wins over
    the config file for the same key, like in the AWS CLI.
    """
    profiles, sso_sessions = {}, {}
    if config_text:
        for section, values in _parse_ini(config_text).items():
            if section == 'default':
                name = 'default'
            elif section.startswith('profile '):
                name = section[len('profile '):].strip()
            elif section.startswith('sso-session '):
                sso_sessions[section[len('sso-session '):].strip()] = values
                continue
            else:
                continue # [services ...] and prefix-less sections are not profiles
            profiles.setdefault(name, {}).update(values)
    if credentials_text:
        for section, values in _parse_ini(credentials_text).items():
            profiles.setdefault(section, {}).update(values)
    return profiles, sso_sessions


def _aws_static(settings):
    key_id = settings.get('aws_access_key_id')
    secret = settings.get('aws_secret_access_key')
    if not key_id or not secret:
        return None
    options = {'access_key_id': key_id, 'secret_access_key': secret}
    token = settings.get('aws_session_token') or settings.get('aws_security_token')
    if token:
        options['session_token'] = token
    return options


def sso_cache_path(settings):
    """
    Relative path of the AWS SSO token cache of a profile, keyed like the AWS CLI and
    SDKs: sha1 of the sso-session name, or of the start URL for legacy profiles.
    """
    key = settings.get('sso_session') or settings.get('sso_start_url') or ''
    return '.aws/sso/cache/{}.json'.format(hashlib.sha1(key.encode('utf-8')).hexdigest())


def _aws_sso_config(name, settings, sso_sessions):
    """The allowlisted SSO settings of a profile as an AWS config file."""
    account, role = settings.get('sso_account_id'), settings.get('sso_role_name')
    if not account or not role:
        raise _Unusable('sso', 'sso_account_id and sso_role_name are missing')
    if not _SAFE_NAME.match(name):
        raise _Unusable('sso', 'profile names with spaces or special characters are not supported for SSO')

    profile = {'sso_account_id': account, 'sso_role_name': role}
    if settings.get('region'):
        profile['region'] = settings['region']

    session_name = settings.get('sso_session')
    session = None
    if session_name:
        session = sso_sessions.get(session_name)
        if not session or not session.get('sso_start_url') or not session.get('sso_region'):
            raise _Unusable('sso', 'sso-session "{}" is missing or incomplete in ~/.aws/config'.format(session_name))
        if not _SAFE_NAME.match(session_name):
            raise _Unusable('sso', 'unsupported characters in the sso-session name')
        profile['sso_session'] = session_name
        session = {key: session[key] for key in ('sso_start_url', 'sso_region', 'sso_registration_scopes') if session.get(key)}
    else:
        if not settings.get('sso_start_url') or not settings.get('sso_region'):
            raise _Unusable('sso', 'sso_start_url and sso_region are missing')
        profile['sso_start_url'] = settings['sso_start_url']
        profile['sso_region'] = settings['sso_region']

    for value in list(profile.values()) + list((session or {}).values()):
        if not _SAFE_VALUE.match(value):
            raise _Unusable('sso', 'unsupported characters in the SSO settings')

    lines = ['[profile {}]'.format(name)] + ['{} = {}'.format(k, v) for k, v in profile.items()]
    if session is not None:
        lines += ['', '[sso-session {}]'.format(session_name)] + ['{} = {}'.format(k, v) for k, v in session.items()]
    return '\n'.join(lines) + '\n'


def _parse_expiry(value):
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace('UTC', 'Z')
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def sso_login_state(cache_file, name, now=None):
    """
    Checks the SSO token cache (never returns tokens). Returns a note for the user, or
    raises _Unusable if Motuz cannot use the login.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    login = 'run `aws sso login --profile {}` on a cluster node'.format(name)
    if not cache_file or 'content' not in cache_file:
        raise _Unusable('sso', 'Not signed in to AWS SSO: {}'.format(login))
    try:
        token = json.loads(cache_file['content'])
    except ValueError:
        raise _Unusable('sso', 'SSO login cache is unreadable: {}'.format(login))
    if not isinstance(token, dict):
        raise _Unusable('sso', 'SSO login cache is unreadable: {}'.format(login))
    expires = _parse_expiry(token.get('expiresAt'))
    if expires is not None and expires > now + datetime.timedelta(minutes=5):
        return 'SSO login valid until {:%Y-%m-%d %H:%M} UTC'.format(expires.astimezone(datetime.timezone.utc))
    registration = _parse_expiry(token.get('registrationExpiresAt'))
    if token.get('refreshToken') and token.get('clientId') and registration and registration > now:
        return 'SSO login expired, it is renewed automatically when used'
    raise _Unusable('sso', 'SSO login expired: {}'.format(login))


def resolve_aws_profile(name, profiles, sso_sessions):
    """
    Returns (kind, options, sso_config): rclone options for static/session/role
    profiles, or the config file text for SSO profiles. Raises _Unusable, or returns
    None for profiles without credentials (e.g. only a region).
    """
    settings = profiles.get(name)
    if settings is None:
        return None

    if settings.get('role_arn'):
        if settings.get('mfa_serial'):
            raise _Unusable('role', 'needs an MFA code for each session, which Motuz cannot ask for')
        source = settings.get('source_profile')
        if not source:
            if settings.get('credential_source'):
                raise _Unusable('role', 'credential_source would use the Motuz server\'s own identity')
            raise _Unusable('role', 'only roles with a source_profile are supported')
        source_settings = profiles.get(source)
        if source_settings is None:
            raise _Unusable('role', 'source_profile "{}" not found'.format(source))
        base = _aws_static(source_settings)
        if base is None or (source != name and source_settings.get('role_arn')):
            raise _Unusable('role', 'source_profile "{}" must have access keys (role chains, SSO and processes are not supported)'.format(source))
        options = dict(base)
        options['role_arn'] = settings['role_arn']
        if settings.get('role_session_name'):
            options['role_session_name'] = settings['role_session_name']
        if settings.get('external_id'):
            options['role_external_id'] = settings['external_id']
        if (settings.get('duration_seconds') or '').isdigit():
            options['role_session_duration'] = '{}s'.format(int(settings['duration_seconds']))
        return 'role', options, None

    static = _aws_static(settings)
    if static is not None:
        return ('session' if 'session_token' in static else 'static'), static, None

    if settings.get('sso_session') or settings.get('sso_start_url') or settings.get('sso_account_id'):
        return 'sso', None, _aws_sso_config(name, settings, sso_sessions)

    if settings.get('credential_process'):
        raise _Unusable('process', 'Motuz does not run credential_process programs on its server')

    if settings.get('web_identity_token_file'):
        raise _Unusable('unsupported', 'web identity tokens are not supported')

    return None


def _truthy(value):
    return str(value or '').strip().lower() in ('true', '1', 'yes', 'on')


def parse_rclone(content):
    """Returns {remote: settings}. Raises LocalCredentialsError for encrypted files."""
    if 'RCLONE_ENCRYPT_V0:' in content:
        raise LocalCredentialsError('rclone.conf is encrypted, Motuz cannot import it')
    return _parse_ini(content)


def classify_rclone_remote(conn_type, settings):
    """
    Returns (kind, options) for an rclone remote of the connection type, None for
    other remote types. Raises _Unusable.
    """
    remote_type = settings.get('type')
    if conn_type == 's3' and remote_type == 's3':
        options = {key: settings[key] for key in _RCLONE_S3_OPTIONS if settings.get(key)}
        if options.get('access_key_id') and options.get('secret_access_key'):
            return 'keys', options
        if _truthy(settings.get('env_auth')):
            raise _Unusable('env_auth', 'uses env_auth; choose the matching AWS profile instead')
        raise _Unusable('unsupported', 'has no access keys')

    if conn_type == 'azureblob' and remote_type == 'azureblob':
        options = {key: settings[key] for key in _RCLONE_AZURE_OPTIONS if settings.get(key)}
        if _truthy(settings.get('use_msi')) or _truthy(settings.get('env_auth')):
            raise _Unusable('unsupported', 'managed identity / env_auth would use the Motuz server\'s own identity')
        if _truthy(settings.get('use_az')):
            if not azure_cli_installed():
                raise _Unusable('azure_cli', AZURE_CLI_MISSING)
            if not options.get('account'):
                raise _Unusable('azure_cli', 'uses the Azure CLI but has no account')
            if options.get('tenant') and not _TENANT_ID.match(options['tenant']):
                raise _Unusable('azure_cli', 'tenant must be a tenant id')
            # Only the account, tenant and endpoint: never a key or client secret next to it
            options = {key: options[key] for key in ('account', 'tenant', 'endpoint', 'access_tier') if options.get(key)}
            options['use_az'] = 'true'
            return 'azure_cli', options
        if options.get('connection_string'):
            return 'connection_string', options
        if options.get('sas_url'):
            return 'sas_url', options
        if options.get('account') and options.get('key'):
            return 'account_key', options
        if options.get('account') and options.get('tenant') and options.get('client_id') and options.get('client_secret'):
            return 'service_principal', options
        if _truthy(options.get('use_emulator')):
            return 'emulator', options
        raise _Unusable('unsupported', 'this kind of Azure authentication is not supported by Motuz')

    return None


def _azure_account(kind, options):
    if kind == 'connection_string':
        for part in options['connection_string'].split(';'):
            key, _, value = part.partition('=')
            if key.strip().lower() == 'accountname':
                return value.strip() or None
        return None
    if kind == 'sas_url':
        host = urllib.parse.urlsplit(options['sas_url']).hostname or ''
        return host.split('.')[0] or None
    if kind == 'emulator':
        return options.get('account') or 'devstoreaccount1'
    return options.get('account')


# ---------------------------------------------------------------- Azure CLI

AZURE_CLI_MISSING = ('the Azure CLI is not installed on this Motuz server '
                     '(image build arg INSTALL_AZURE_CLI=true)')
_MANAGED_IDENTITY_USERS = ('systemAssignedIdentity', 'userAssignedIdentity')
# Points az at a closed port for managed identity (App Service style variables win over
# the instance metadata service), so a login can never become the server's identity
_NO_MANAGED_IDENTITY = 'http://127.0.0.1:9/motuz-no-managed-identity'


def azure_cli_installed():
    """Whether rclone (use_az) finds `az` on the PATH it runs with"""
    from .abstract_connection import user_process_env
    return shutil.which('az', path=user_process_env()['PATH']) is not None


def azure_cli_env(home):
    """
    Environment for rclone with use_az, which runs `az account get-access-token` as the
    user (rclone's process runs as the user, never as root). az reads and refreshes the
    user's own ~/.azure. Everything else from ~/.azure that could run code in the
    containers is switched off: extensions (Python packages in ~/.azure/cliextensions
    or the dev_sources of ~/.azure/config; environment variables win over that file)
    and dynamic extension installs, telemetry (a helper process) and ~/.local
    site-packages (.pth files are code). The az launcher runs Python with -I.
    """
    return {
        'HOME': home, # sudo -E keeps root's HOME otherwise
        'AZURE_CONFIG_DIR': os.path.join(home, '.azure'),
        'AZURE_EXTENSION_DIR': '/nonexistent/motuz-no-az-extensions',
        'AZURE_EXTENSION_DEV_SOURCES': '',
        'AZURE_EXTENSION_USE_DYNAMIC_INSTALL': 'no',
        'AZURE_CORE_COLLECT_TELEMETRY': 'false',
        'AZURE_CORE_SURVEY_MESSAGE': 'false',
        'AZURE_CORE_ONLY_SHOW_ERRORS': 'true',
        'AZURE_CORE_NO_COLOR': 'true',
        # az starts some helpers (telemetry) with plain `python`, not -I
        'PYTHONNOUSERSITE': '1',
        'IDENTITY_ENDPOINT': _NO_MANAGED_IDENTITY,
        'IDENTITY_HEADER': 'none',
        'MSI_ENDPOINT': _NO_MANAGED_IDENTITY,
        'MSI_SECRET': 'none',
    }


def parse_azure_profile(content):
    """
    Azure CLI logins from ~/.azure/azureProfile.json, by tenant id (a login can span
    several tenants, and rclone asks az for a token of one tenant). Returns
    {tenant_id: {'user', 'subscriptions', 'reason'}}, where reason is set for logins
    Motuz cannot use. The file has no tokens (they are in the MSAL cache). Raises
    _ParseError.
    """
    try:
        data = json.loads(content.lstrip('\ufeff')) # az writes it with a BOM
    except ValueError:
        raise _ParseError()
    subscriptions = data.get('subscriptions') if isinstance(data, dict) else None
    if not isinstance(subscriptions, list):
        raise _ParseError()

    logins = {}
    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            continue
        tenant = subscription.get('tenantId')
        if not isinstance(tenant, str) or not _TENANT_ID.match(tenant):
            continue
        user = subscription.get('user') if isinstance(subscription.get('user'), dict) else {}
        login = logins.setdefault(tenant.lower(), {
            'user': str(user.get('name') or '')[:128] or None,
            'subscriptions': [],
            'reason': None,
        })
        name = subscription.get('name')
        if isinstance(name, str) and name and not name.startswith('N/A('):
            login['subscriptions'].append(name[:64])
        if user.get('assignedIdentityInfo') or user.get('name') in _MANAGED_IDENTITY_USERS:
            login['reason'] = 'a managed identity login would use the Motuz server\'s own identity'
        elif subscription.get('environmentName', 'AzureCloud') != 'AzureCloud':
            login['reason'] = login['reason'] or 'only the public Azure cloud is supported'
    return logins


def _azure_cli_note(login):
    parts = []
    if login['user']:
        parts.append('signed in as {}'.format(login['user']))
    if login['subscriptions']:
        parts.append('subscriptions: {}'.format(', '.join(sorted(set(login['subscriptions']))[:5])))
    return '; '.join(parts) or None


def _discover_azure_cli(files, profiles_out, notes):
    entry = files.get(AZURE_PROFILE)
    note = _file_note('~/' + AZURE_PROFILE, entry)
    if note:
        notes.append(note)
    if not entry or 'content' not in entry:
        return
    try:
        logins = parse_azure_profile(entry['content'])
    except _ParseError:
        notes.append('~/{} could not be parsed'.format(AZURE_PROFILE))
        return
    installed = azure_cli_installed()
    for tenant in sorted(logins):
        login = logins[tenant]
        reason = login['reason'] or (None if installed else AZURE_CLI_MISSING)
        profiles_out.append(_entry(
            'azure-cli', tenant, 'azure_cli', usable=reason is None, reason=reason,
            note=_azure_cli_note(login), file='~/' + AZURE_PROFILE,
        ))


# ---------------------------------------------------------------- discovery

def _file_note(label, entry):
    if entry is None or 'content' in entry or entry.get('error') == 'missing':
        return None
    return '{}: {}'.format(label, entry['error'])


def _rclone_file(files):
    entry = files.get(RCLONE_CONF)
    if entry and entry.get('error') == 'missing':
        entry = files.get(RCLONE_CONF_LEGACY)
        return entry, '~/' + RCLONE_CONF_LEGACY
    return entry, '~/' + RCLONE_CONF


def _entry(source, name, kind, *, usable, reason=None, note=None, region=None, access_key_id=None,
           account=None, file=None):
    return {
        'source': source,
        'name': name,
        'kind': kind,
        'label': _KIND_LABELS.get(kind, kind),
        'region': region or None,
        'access_key_id': mask(access_key_id),
        'account': account or None,
        'usable': usable,
        'reason': reason,
        'note': note,
        'file': file,
    }


def _discover_aws(user, home, files, profiles_out, notes):
    creds, config = files.get(AWS_CREDENTIALS), files.get(AWS_CONFIG)
    for rel, entry in ((AWS_CREDENTIALS, creds), (AWS_CONFIG, config)):
        note = _file_note('~/' + rel, entry)
        if note:
            notes.append(note)
    try:
        profiles, sso_sessions = parse_aws(
            (creds or {}).get('content'),
            (config or {}).get('content'),
        )
    except _ParseError:
        notes.append('~/.aws/credentials or ~/.aws/config could not be parsed')
        return

    found = []
    for name in sorted(profiles):
        settings = profiles[name]
        region = settings.get('region')
        source_file = '~/' + (AWS_CREDENTIALS if settings.get('aws_access_key_id') else AWS_CONFIG)
        try:
            resolved = resolve_aws_profile(name, profiles, sso_sessions)
        except _Unusable as e:
            found.append(_entry('aws', name, e.kind, usable=False, reason=e.reason, region=region,
                                access_key_id=settings.get('aws_access_key_id'), file=source_file))
            continue
        if resolved is None:
            continue
        kind, options, _ = resolved
        key_id = (options or {}).get('access_key_id')
        found.append(_entry('aws', name, kind, usable=True, region=region, access_key_id=key_id, file=source_file))

    # SSO profiles: check the login state in the token cache
    sso = [(entry, sso_cache_path(profiles[entry["name"]])) for entry in found
           if entry['kind'] == 'sso' and entry['usable']]
    if sso:
        try:
            _, caches = read_home_files(user, sorted({path for _, path in sso}), home=home)
        except LocalCredentialsError:
            caches = {}
        for entry, path in sso:
            try:
                entry['note'] = sso_login_state(caches.get(path), entry['name'])
            except _Unusable as e:
                entry['usable'], entry['reason'] = False, e.reason

    for entry in found:
        if not PROFILE_NAME_RE.match(entry['name']) and entry['usable']:
            entry['usable'], entry['reason'] = False, 'unsupported characters in the profile name'
    profiles_out.extend(found)


def _discover_rclone(conn_type, files, profiles_out, notes):
    entry, label = _rclone_file(files)
    note = _file_note(label, entry)
    if note:
        notes.append(note)
    if not entry or 'content' not in entry:
        return
    try:
        remotes = parse_rclone(entry['content'])
    except LocalCredentialsError:
        notes.append('{} is encrypted, Motuz cannot import it'.format(label))
        return
    except _ParseError:
        notes.append('{} could not be parsed'.format(label))
        return

    for name in sorted(remotes):
        settings = remotes[name]
        try:
            classified = classify_rclone_remote(conn_type, settings)
        except _Unusable as e:
            profiles_out.append(_entry('rclone', name, e.kind, usable=False, reason=e.reason,
                                       region=settings.get('region'), file=label))
            continue
        if classified is None:
            continue
        kind, options = classified
        usable = bool(PROFILE_NAME_RE.match(name))
        profiles_out.append(_entry(
            'rclone', name, kind,
            usable=usable,
            reason=None if usable else 'unsupported characters in the remote name',
            region=options.get('region'),
            access_key_id=options.get('access_key_id'),
            account=_azure_account(kind, options) if conn_type == 'azureblob' else None,
            file=label,
        ))


def discover(user, conn_type):
    """Profiles for the New Connection dialog. Metadata only, never secrets."""
    if conn_type == 's3':
        relpaths = [AWS_CREDENTIALS, AWS_CONFIG, RCLONE_CONF, RCLONE_CONF_LEGACY]
    elif conn_type == 'azureblob':
        relpaths = [RCLONE_CONF, RCLONE_CONF_LEGACY, AZURE_PROFILE]
    else:
        raise LocalCredentialsError('Only S3 and Azure Blob connections can use local credentials')

    home, files = read_home_files(user, relpaths)
    profiles, notes = [], []
    if conn_type == 's3':
        _discover_aws(user, home, files, profiles, notes)
    _discover_rclone(conn_type, files, profiles, notes)
    if conn_type == 'azureblob':
        _discover_azure_cli(files, profiles, notes)
    return {'profiles': profiles, 'notes': notes}


# ---------------------------------------------------------------- use at run time

def validate_reference(conn_type, source, name):
    if conn_type not in ('s3', 'azureblob'):
        raise LocalCredentialsError('Only S3 and Azure Blob connections can use local credentials')
    if (source not in SOURCES or (source == 'aws' and conn_type != 's3')
            or (source == 'azure-cli' and conn_type != 'azureblob')):
        raise LocalCredentialsError('Unknown credential source')
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        raise LocalCredentialsError('Invalid profile name')
    if source == 'azure-cli' and not _TENANT_ID.match(name):
        raise LocalCredentialsError('Invalid Azure tenant id')


def resolve(user, conn_type, source, name, *, materialize=True):
    """
    Reads the user's files (as the user) and returns (options, env): rclone options for
    the remote (without the RCLONE_CONFIG_<REMOTE>_ prefix) and extra process variables.
    Raises LocalCredentialsError with a message for the user.

    Azure CLI logins ('azure-cli', name = tenant id) have no storage account: the
    connection's azure_account is added by RcloneConnection._formatCredentials.
    """
    validate_reference(conn_type, source, name)

    if source == 'azure-cli':
        if not azure_cli_installed():
            raise LocalCredentialsError('Azure CLI login: {}'.format(AZURE_CLI_MISSING))
        home, files = read_home_files(user, [AZURE_PROFILE])
        entry = files.get(AZURE_PROFILE) or {}
        if 'content' not in entry:
            if entry.get('error', 'missing') != 'missing':
                raise LocalCredentialsError('~/{}: {}'.format(AZURE_PROFILE, entry['error']))
            raise LocalCredentialsError('No Azure CLI login in your home directory: run `az login` on a cluster node')
        try:
            logins = parse_azure_profile(entry['content'])
        except _ParseError:
            raise LocalCredentialsError('~/{} could not be parsed'.format(AZURE_PROFILE))
        login = logins.get(name.lower())
        if login is None:
            raise LocalCredentialsError(
                'No Azure CLI login for tenant {0}: run `az login --tenant {0}` on a cluster node'.format(name))
        if login['reason']:
            raise LocalCredentialsError('Azure CLI login for tenant {}: {}'.format(name, login['reason']))
        return {'use_az': 'true', 'tenant': name.lower()}, azure_cli_env(home)

    if source == 'aws':
        home, files = read_home_files(user, [AWS_CREDENTIALS, AWS_CONFIG])
        file_errors = [note for note in (_file_note('~/' + rel, files.get(rel)) for rel in (AWS_CREDENTIALS, AWS_CONFIG)) if note]
        try:
            profiles, sso_sessions = parse_aws(
                (files.get(AWS_CREDENTIALS) or {}).get('content'),
                (files.get(AWS_CONFIG) or {}).get('content'),
            )
            resolved = resolve_aws_profile(name, profiles, sso_sessions)
        except _ParseError:
            raise LocalCredentialsError('~/.aws/credentials or ~/.aws/config could not be parsed')
        except _Unusable as e:
            raise LocalCredentialsError('AWS profile "{}": {}'.format(name, e.reason))
        if resolved is None:
            raise LocalCredentialsError('AWS profile "{}" not found in your home directory{}'.format(
                name, ' ({})'.format('; '.join(file_errors)) if file_errors else ''))

        kind, options, sso_config = resolved
        if kind != 'sso':
            region = profiles[name].get('region')
            if region:
                options['region'] = region
            if profiles[name].get('endpoint_url'):
                options['endpoint'] = profiles[name]['endpoint_url']
            return options, {}

        cache_rel = sso_cache_path(profiles[name])
        _, caches = read_home_files(user, [cache_rel], home=home)
        try:
            sso_login_state(caches.get(cache_rel), name)
        except _Unusable as e:
            raise LocalCredentialsError('AWS profile "{}": {}'.format(name, e.reason))
        path = write_sso_config(sso_config) if materialize else '/dev/null'
        options = {'env_auth': 'true', 'profile': name, 'shared_credentials_file': path}
        if profiles[name].get('region'):
            options['region'] = profiles[name]['region']
        env = {
            # rclone runs with sudo -E, so HOME would be root's. The AWS SDK finds the
            # SSO token cache (and writes refreshed tokens) below the user's HOME.
            'HOME': home,
            'AWS_CONFIG_FILE': path,
            'AWS_SHARED_CREDENTIALS_FILE': '/dev/null',
            'AWS_EC2_METADATA_DISABLED': 'true',
        }
        return options, env

    # rclone remote
    home, files = read_home_files(user, [RCLONE_CONF, RCLONE_CONF_LEGACY])
    entry, label = _rclone_file(files)
    if not entry or 'content' not in entry:
        if entry and entry.get('error') != 'missing':
            raise LocalCredentialsError('{}: {}'.format(label, entry['error']))
        raise LocalCredentialsError('rclone remote "{}" not found in your home directory'.format(name))
    try:
        remotes = parse_rclone(entry['content'])
    except _ParseError:
        raise LocalCredentialsError('{} could not be parsed'.format(label))
    settings = remotes.get(name)
    try:
        classified = classify_rclone_remote(conn_type, settings) if settings is not None else None
    except _Unusable as e:
        raise LocalCredentialsError('rclone remote "{}": {}'.format(name, e.reason))
    if classified is None:
        raise LocalCredentialsError('rclone remote "{}" ({}) not found in your home directory'.format(name, conn_type))
    kind, options = classified
    return dict(options), (azure_cli_env(home) if kind == 'azure_cli' else {})


def write_sso_config(content):
    """
    Writes the (secret-free) SSO config file that rclone reads as the user. Files are
    named by their content hash in a root-owned directory that others cannot list.
    """
    directory = SSO_CONFIG_DIR
    try:
        os.mkdir(directory, 0o711)
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise LocalCredentialsError('Unsafe directory for AWS SSO configuration: {}'.format(directory))
    os.chmod(directory, 0o711)

    path = os.path.join(directory, hashlib.sha256(content.encode('utf-8')).hexdigest() + '.ini')
    if not os.path.exists(path):
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return path
