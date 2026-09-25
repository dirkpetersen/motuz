"""Credentials from the user's home directory (S3 and Azure Blob connections that use
~/.aws or rclone.conf of the logged-in user), against the e2e stack and Azurite.

Without MOTUZ_E2E_AWS_PROFILE the AWS keys in alice's home are fake and the checks that
need a real bucket are skipped. With it, the static keys of that profile from the local
~/.aws/credentials (or $AWS_SHARED_CREDENTIALS_FILE) are copied into alice's home and used
against MOTUZ_E2E_AWS_BUCKET (an existing bucket in MOTUZ_E2E_AWS_REGION, default
us-west-2); objects are written below a unique prefix and removed at the end.
Secrets are never printed; the checks only report whether they leaked."""
import configparser
import hashlib
import json
import os
import secrets
import string
import sys
import time
import urllib.error
import urllib.request

from common import BASE, CTX, check, compose, db_password, finish, psql, service_logs, sh, skip

AWS_PROFILE = os.environ.get('MOTUZ_E2E_AWS_PROFILE')
REGION = os.environ.get('MOTUZ_E2E_AWS_REGION', 'us-west-2')
NEEDS_AWS = 'needs MOTUZ_E2E_AWS_PROFILE and MOTUZ_E2E_AWS_BUCKET'
if AWS_PROFILE:
    BUCKET = os.environ.get('MOTUZ_E2E_AWS_BUCKET')
    if not BUCKET:
        sys.exit('MOTUZ_E2E_AWS_PROFILE is set, but MOTUZ_E2E_AWS_BUCKET (an existing test bucket) is not')
    local = configparser.ConfigParser(interpolation=None)
    local.read(os.environ.get('AWS_SHARED_CREDENTIALS_FILE', os.path.expanduser('~/.aws/credentials')))
    KEY_ID = local[AWS_PROFILE]['aws_access_key_id']
    SECRET = local[AWS_PROFILE]['aws_secret_access_key']
    del local
else:
    BUCKET = 'motuz-e2e-no-bucket'
    KEY_ID = 'AKIA' + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    SECRET = secrets.token_urlsafe(30)[:40]
PREFIX = 'motuz-e2e-{}-{}'.format(time.strftime('%Y%m%d%H%M%S'), secrets.token_hex(3))
PROFILE = 'e2e-static'  # profile name in alice's ~/.aws
AZURITE_KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='  # public dev key
FAKE_SSO_TOKEN = 'aoaFAKESSOACCESSTOKENmotuztest0123456789'
SENSITIVE = (SECRET, KEY_ID, AZURITE_KEY, FAKE_SSO_TOKEN)


def redact(detail):
    detail = str(detail)
    for s in SENSITIVE:
        detail = detail.replace(s, '<REDACTED>')
    return detail[:600]


def check_r(name, ok, detail=''):
    check(name, ok, redact(detail))


def req(method, path, token=None, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=180) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), raw
        except ValueError:
            return e.code, raw, raw


def write_as(user, path, content, mode='600'):
    """Writes a file in a home directory as its owner (content via stdin, never argv)"""
    out = sh('app', f"sudo -u {user} sh -c 'umask 077; mkdir -p \"$(dirname {path})\"; cat > {path}; chmod {mode} {path}'", content)
    assert out.returncode == 0, out.stderr


def wait_job(kind, job_id, token, timeout=300):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        status, job, _ = req('GET', f'/api/{kind}/{job_id}', token)
        if status == 200 and job['progress_state'] not in ('PROGRESS', 'PENDING', None):
            return job
        time.sleep(1)
    return job


def no_secret(raw):
    return all(s not in raw for s in SENSITIVE)


def verify(token, conn_id):
    """Like the edit dialog: the stored connection's fields plus its id"""
    status, stored, _ = req('GET', f'/api/connections/{conn_id}', token)
    data = {k: v for k, v in stored.items() if v is not None} if status == 200 else {'name': 'x', 'type': 's3'}
    data['id'] = conn_id
    return req('POST', '/api/connections/verify/', token, data)


# ---------------------------------------------------------------- fixtures in the homes
CREDENTIALS = f"""[{PROFILE}]
aws_access_key_id = {KEY_ID}
aws_secret_access_key = {SECRET}
"""
CONFIG = f"""[profile {PROFILE}]
region = {REGION}

[profile fake-sso]
sso_session = fh
sso_account_id = 123456789012
sso_role_name = ReadOnly
region = us-west-2

[sso-session fh]
sso_start_url = https://d-0000000000.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access

[profile role-mfa]
role_arn = arn:aws:iam::000000000000:role/motuz-no-such-role
source_profile = {PROFILE}
mfa_serial = arn:aws:iam::000000000000:mfa/nobody

[profile role-bogus]
role_arn = arn:aws:iam::000000000000:role/motuz-no-such-role
source_profile = {PROFILE}
region = {REGION}

[profile proc]
credential_process = /bin/sh -c 'touch /tmp/motuz-pwned-marker; echo {{}}'
"""
SSO_CACHE = '.aws/sso/cache/{}.json'.format(hashlib.sha1(b'fh').hexdigest())
RCLONE_CONF = f"""[azurite]
type = azureblob
account = devstoreaccount1
key = {AZURITE_KEY}
endpoint = http://127.0.0.1:10000/devstoreaccount1

[s3remote]
type = s3
provider = AWS
access_key_id = {KEY_ID}
secret_access_key = {SECRET}
region = {REGION}

[msi]
type = azureblob
account = someaccount
use_msi = true
"""
ENCRYPTED_RCLONE_CONF = """# Encrypted rclone configuration File

RCLONE_ENCRYPT_V0:
XVzEo1Y2CZxDD2Cq3kv8e6Z8NzPrCw8tFmrQyZ5iYk1lzAe7l3wP0Zb0Z2sdf8cLkT3y
"""

print('AWS:', f'profile {AWS_PROFILE}, bucket {BUCKET}, prefix {PREFIX}' if AWS_PROFILE else 'fake keys, checks against S3 skipped')
write_as('alice', '/home/alice/.aws/credentials', CREDENTIALS)
write_as('alice', '/home/alice/.aws/config', CONFIG)
write_as('alice', f'/home/alice/{SSO_CACHE}', json.dumps({
    'startUrl': 'https://d-0000000000.awsapps.com/start', 'region': 'us-west-2',
    'accessToken': FAKE_SSO_TOKEN, 'expiresAt': '2020-01-01T00:00:00Z'}))
write_as('alice', '/home/alice/.config/rclone/rclone.conf', RCLONE_CONF)
write_as('bob', '/home/bob/.config/rclone/rclone.conf', ENCRYPTED_RCLONE_CONF)
sh('app', 'rm -f /tmp/motuz-pwned-marker'); sh('celery', 'rm -f /tmp/motuz-pwned-marker')
out = sh('app', "sudo -u alice /usr/local/bin/rclone --config /home/alice/.config/rclone/rclone.conf mkdir azurite:motuztest")
check_r('azurite container created', out.returncode == 0, out.stderr)

A = req('POST', '/api/auth/login/', body={'username': 'alice', 'password': 'AlicePass1'})[1]['access']
B = req('POST', '/api/auth/login/', body={'username': 'bob', 'password': 'BobPass1'})[1]['access']

# ---------------------------------------------------------------- discovery
status, found, raw = req('GET', '/api/connections/local-credentials/?type=s3', A)
by = {(p['source'], p['name']): p for p in (found or {}).get('profiles', [])}
check_r('discovery s3 200', status == 200, (status, raw))
check('discovery: no secret, no full key id, no SSO token in raw JSON', no_secret(raw))
p = by.get(('aws', PROFILE), {})
check_r('static profile: static keys, region, masked key id, usable',
        (p.get('kind'), p.get('region'), p.get('access_key_id'), p.get('usable')) == ('static', REGION, '****' + KEY_ID[-4:], True), p)
p = by.get(('aws', 'fake-sso'), {})
check_r('fake SSO: not usable, "SSO login expired: run aws sso login"', p.get('kind') == 'sso' and not p.get('usable')
        and 'SSO login expired: run `aws sso login --profile fake-sso` on a cluster node' == p.get('reason'), p)
check_r('role with MFA: not usable', by.get(('aws', 'role-mfa'), {}).get('usable') is False and 'MFA' in by[('aws', 'role-mfa')]['reason'], by.get(('aws', 'role-mfa')))
check_r('role with static source_profile: usable', by.get(('aws', 'role-bogus'), {}).get('usable') is True, by.get(('aws', 'role-bogus')))
check_r('credential_process: not usable', by.get(('aws', 'proc'), {}).get('usable') is False and by[('aws', 'proc')]['kind'] == 'process', by.get(('aws', 'proc')))
check_r('rclone s3 remote listed', by.get(('rclone', 's3remote'), {}).get('usable') is True and by[('rclone', 's3remote')]['access_key_id'] == '****' + KEY_ID[-4:], by.get(('rclone', 's3remote')))
check('azure remotes not listed for s3', ('rclone', 'azurite') not in by)

status, found_b, raw_b = req('GET', '/api/connections/local-credentials/?type=s3', B)
check_r('bob sees none of alice\'s profiles', status == 200 and found_b['profiles'] == [] and PROFILE not in raw_b, raw_b)
check_r('bob: encrypted rclone.conf reported', '~/.config/rclone/rclone.conf is encrypted, Motuz cannot import it' in found_b['notes'], raw_b)
status, _, _ = req('GET', '/api/connections/local-credentials/?type=swift', A)
check('discovery rejects other types', status == 400, status)
status, _, _ = req('GET', '/api/connections/local-credentials/?type=s3')
check('discovery needs a token', status == 401, status)

# ---------------------------------------------------------------- create + use a connection
conn = {'name': 'alice-profile', 'type': 's3', 'subtype': 'profile', 'profile_source': 'aws', 'profile_name': PROFILE,
        'bucket': BUCKET, 's3_region': REGION, 's3_access_key_id': 'AKIASHOULDBEDROPPED0', 's3_secret_access_key': 'dropme'}
status, c, raw = req('POST', '/api/connections/', A, conn)
check_r('create connection from profile', status == 201 and c['subtype'] == 'profile' and c['profile_name'] == PROFILE, (status, raw))
cid = c['id']
row = psql(f"select coalesce(s3_access_key_id,'-'), coalesce(s3_secret_access_key,'-'), profile_source, profile_name from cloud_connection where id={cid}")
check_r('no credentials stored in the database', row == f'-|-|aws|{PROFILE}', row)
check('create response has no secret', no_secret(raw))

S3 = f'/{BUCKET}/{PREFIX}'
if AWS_PROFILE:
    status, body, raw = verify(A, cid)
    check_r('verify profile connection', status == 200 and body['result'] is True, raw)
    status, body, raw = req('POST', '/api/connections/verify/', A, {k: v for k, v in conn.items() if not k.startswith('s3_a') and k != 's3_secret_access_key'})
    check_r('verify from the new-connection form', status == 200 and body['result'] is True, raw)

    status, ls, raw = req('POST', '/api/system/files/', A, {'path': f'/{BUCKET}', 'connection_id': cid})
    check_r('ls bucket', status == 200 and isinstance(ls.get('files'), list), raw)
    status, body, raw = req('POST', '/api/system/files/mkdir/', A, {'path': f'{S3}/made-by-motuz', 'connection_id': cid})
    check_r('mkdir in bucket', status == 200, raw)

    sh('app', "sudo -u alice sh -c 'mkdir -p /home/alice/credsrc/sub; for i in 1 2 3; do head -c 200000 /dev/urandom > /home/alice/credsrc/f$i.bin; done; echo deep > /home/alice/credsrc/sub/d.txt'")
    status, cj, raw = req('POST', '/api/copy-jobs/', A, {'description': 'to s3', 'src_resource_path': '/home/alice/credsrc',
                                                        'dst_cloud_id': cid, 'dst_resource_path': f'{S3}/copy', 'copy_links': True})
    cj = wait_job('copy-jobs', cj['id'], A)
    check_r('copy local -> S3 SUCCESS', cj['progress_state'] == 'SUCCESS', cj)
    status, ls, raw = req('POST', '/api/system/files/', A, {'path': f'{S3}/copy', 'connection_id': cid})
    check_r('S3 listing shows copied files', status == 200 and sorted(f['Name'] for f in ls['files']) == ['f1.bin', 'f2.bin', 'f3.bin', 'sub'], raw)
    status, cj, raw = req('POST', '/api/copy-jobs/', A, {'description': 'from s3', 'src_cloud_id': cid, 'src_resource_path': f'{S3}/copy',
                                                        'dst_resource_path': '/home/alice/credback', 'copy_links': True})
    cj = wait_job('copy-jobs', cj['id'], A)
    check_r('copy S3 -> local SUCCESS', cj['progress_state'] == 'SUCCESS', cj)
    out = sh('app', "sudo -u alice diff -r /home/alice/credsrc /home/alice/credback && echo SAME")
    check_r('round trip identical', out.stdout.strip() == 'SAME', out.stdout + out.stderr)
    status, hj, raw = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/credsrc', 'dst_cloud_id': cid,
                                                           'dst_resource_path': f'{S3}/copy', 'option_download': False})
    hj = wait_job('hashsum-jobs', hj['id'], A)
    check_r('hashsum local vs S3 SUCCESS and identical', hj['progress_state'] == 'SUCCESS' and json.loads(hj['progress_src_tree']) == []
            and json.loads(hj['progress_dst_tree']) == [], {k: hj.get(k) for k in ('progress_state', 'progress_src_tree', 'progress_dst_tree', 'progress_src_error_text', 'progress_dst_error_text')})
else:
    # rclone runs with the (fake) keys from alice's home: AWS rejects them
    status, body, raw = verify(A, cid)
    check_r('fake keys: verify runs rclone and fails', status == 200 and body['result'] is False, raw)
    for name in ('verify profile connection', 'verify from the new-connection form', 'ls bucket', 'mkdir in bucket',
                 'copy local -> S3 SUCCESS', 'S3 listing shows copied files', 'copy S3 -> local SUCCESS', 'round trip identical',
                 'hashsum local vs S3 SUCCESS and identical'):
        skip(name, NEEDS_AWS)

# ---------------------------------------------------------------- bob
status, body, _ = req('GET', f'/api/connections/{cid}', B)
check('bob cannot read alice connection', status == 404, status)
status, body, _ = req('POST', '/api/system/files/', B, {'path': f'/{BUCKET}', 'connection_id': cid})
check('bob cannot ls with alice connection', status == 404, (status, body))
status, body, _ = verify(B, cid)
check('bob cannot verify alice connection', status == 404, (status, body))
status, body, _ = req('POST', '/api/copy-jobs/', B, {'description': 'x', 'src_cloud_id': cid, 'src_resource_path': f'{S3}/copy',
                                                    'dst_resource_path': '/home/bob/stolen', 'copy_links': True})
check('bob cannot copy with alice connection', status == 404, (status, body))
status, body, raw = req('POST', '/api/connections/', B, dict(conn, name='bob-steal'))
check_r('bob cannot create a connection with alice\'s profile name (resolved in bob\'s home)', status == 400 and 'not found' in raw, (status, raw))
status, body, raw = req('POST', '/api/connections/verify/', B, {k: v for k, v in conn.items() if k in ('name', 'type', 'subtype', 'profile_source', 'profile_name', 'bucket', 's3_region')})
check_r(f'bob verify with profile {PROFILE} fails (bob has none)', status == 200 and body['result'] is False, raw)

# ---------------------------------------------------------------- unreadable file, symlink to /etc/shadow, FIFO
sh('app', 'chmod 000 /home/alice/.aws/credentials')
status, found, raw = req('GET', '/api/connections/local-credentials/?type=s3', A)
check_r('mode 000: reported as permission denied, profile skipped', '~/.aws/credentials: permission denied' in found['notes']
        and not any(p['name'] == PROFILE and p['source'] == 'aws' for p in found['profiles']), raw)
status, body, raw = verify(A, cid)
check_r('mode 000: verify fails (not read as root)', body['result'] is False and 'permission denied' in body['message'], raw)
sh('app', 'chmod 600 /home/alice/.aws/credentials')
sh('app', "sudo -u alice sh -c 'mv /home/alice/.aws/credentials /home/alice/.aws/credentials.real; ln -s /etc/shadow /home/alice/.aws/credentials'")
status, found, raw = req('GET', '/api/connections/local-credentials/?type=s3', A)
check_r('symlink to /etc/shadow: permission denied, nothing from it', '~/.aws/credentials: permission denied' in found['notes']
        and 'root:' not in raw, raw)
status, body, raw = verify(A, cid)
check_r('symlink to /etc/shadow: verify fails', body['result'] is False and 'permission denied' in body['message'], raw)
sh('app', "sudo -u alice sh -c 'rm /home/alice/.aws/credentials; mv /home/alice/.aws/credentials.real /home/alice/.aws/credentials'")
sh('app', "sudo -u bob sh -c 'mkdir -p /home/bob/.aws; mkfifo /home/bob/.aws/credentials'")
t0 = time.time()
status, found, raw = req('GET', '/api/connections/local-credentials/?type=s3', B)
check_r('FIFO as ~/.aws/credentials: no hang, reported', status == 200 and time.time() - t0 < 10
        and '~/.aws/credentials: not a regular file' in found['notes'], (time.time() - t0, raw))

# ---------------------------------------------------------------- key rotation is picked up
if AWS_PROFILE:
    write_as('alice', '/home/alice/.aws/credentials', CREDENTIALS.replace(SECRET, SECRET[:-4] + ('AAAA' if SECRET[-4:] != 'AAAA' else 'BBBB')))
    status, body, raw = verify(A, cid)
    check_r('wrong key in file: verify fails', body['result'] is False, raw)
    write_as('alice', '/home/alice/.aws/credentials', CREDENTIALS)
    status, body, raw = verify(A, cid)
    check_r('key restored: verify works again', body['result'] is True, raw)
else:
    skip('wrong key in file: verify fails', NEEDS_AWS)
    skip('key restored: verify works again', NEEDS_AWS)

# ---------------------------------------------------------------- credential_process is never run
status, body, raw = req('POST', '/api/connections/', A, dict(conn, name='proc', profile_name='proc'))
check_r('cannot create a connection from a credential_process profile', status == 400 and 'credential_process' in raw, (status, raw))
write_as('alice', '/home/alice/.aws/credentials', '')
write_as('alice', '/home/alice/.aws/config', CONFIG.replace(f'[profile {PROFILE}]\nregion = {REGION}',
         f"[profile {PROFILE}]\nregion = {REGION}\ncredential_process = /bin/sh -c 'touch /tmp/motuz-pwned-marker; echo {{}}'"))
status, body, raw = verify(A, cid)
check_r('keys swapped for credential_process later: verify refuses', body['result'] is False and 'credential_process' in body['message'], raw)
status, cj, raw = req('POST', '/api/copy-jobs/', A, {'description': 'proc', 'src_resource_path': '/home/alice',
                                                    'dst_cloud_id': cid, 'dst_resource_path': f'{S3}/proc', 'copy_links': True})
cj = wait_job('copy-jobs', cj['id'], A)
check_r('... and a copy job fails', cj['progress_state'] == 'FAILED', cj)
markers = sh('app', 'ls /tmp/motuz-pwned-marker 2>&1').stdout + sh('celery', 'ls /tmp/motuz-pwned-marker 2>&1').stdout
check('credential_process was never executed (no marker in app or celery)', markers.count('No such file') == 2, markers)
write_as('alice', '/home/alice/.aws/credentials', CREDENTIALS)
write_as('alice', '/home/alice/.aws/config', CONFIG)

# ---------------------------------------------------------------- role and SSO wiring
status, c2, raw = req('POST', '/api/connections/', A, dict(conn, name='role', profile_name='role-bogus'))
check_r('create role connection', status == 201, raw)
status, body, raw = verify(A, c2['id'])
check_r('role connection: rclone calls AssumeRole (fails for a missing role)', body['result'] is False and 'AssumeRole' in body['message'], raw)
status, body, raw = req('POST', '/api/connections/', A, dict(conn, name='sso', profile_name='fake-sso'))
check_r('expired SSO profile cannot be used', status == 400 and 'SSO login expired' in raw, raw)
write_as('alice', f'/home/alice/{SSO_CACHE}', json.dumps({
    'startUrl': 'https://d-0000000000.awsapps.com/start', 'region': 'us-west-2',
    'accessToken': FAKE_SSO_TOKEN, 'expiresAt': '2099-01-01T00:00:00Z'}))
status, found, raw = req('GET', '/api/connections/local-credentials/?type=s3', A)
p = {x['name']: x for x in found['profiles']}.get('fake-sso', {})
check_r('valid SSO login: usable with note', p.get('usable') is True and 'valid until 2099' in (p.get('note') or ''), p)
status, c3, raw = req('POST', '/api/connections/', A, dict(conn, name='sso', profile_name='fake-sso'))
check_r('create SSO connection', status == 201, raw)
status, body, raw = verify(A, c3['id'])
check_r('SSO connection: rclone (as alice) uses the SSO token from alice\'s cache (rejected by AWS)',
        body['result'] is False and ('sso' in body['message'].lower() or 'GetRoleCredentials' in body['message']), raw)
out = sh('app', 'ls -ld /tmp/motuz-aws-config; ls -l /tmp/motuz-aws-config; cat /tmp/motuz-aws-config/*.ini')
check_r('SSO config file: root-owned dir 0711, file 0644, no token', 'drwx--x--x' in out.stdout and FAKE_SSO_TOKEN not in out.stdout
        and 'credential_process' not in out.stdout, out.stdout)

# ---------------------------------------------------------------- rclone remotes: S3 and Azure (Azurite)
status, c4, raw = req('POST', '/api/connections/', A, dict(conn, name='rclone-s3', profile_source='rclone', profile_name='s3remote'))
check_r('create from rclone s3 remote', status == 201, raw)
if AWS_PROFILE:
    status, body, raw = verify(A, c4['id'])
    check_r('rclone s3 remote verifies', body['result'] is True, raw)
else:
    skip('rclone s3 remote verifies', NEEDS_AWS)

status, found, raw = req('GET', '/api/connections/local-credentials/?type=azureblob', A)
by = {p['name']: p for p in found['profiles']}
check_r('azure discovery: azurite account key', by.get('azurite', {}).get('kind') == 'account_key' and by['azurite']['account'] == 'devstoreaccount1' and by['azurite']['usable'], raw)
check_r('azure discovery: managed identity not usable', by.get('msi', {}).get('usable') is False, raw)
check('azure discovery: no secrets', no_secret(raw))
az = {'name': 'alice-azurite', 'type': 'azureblob', 'subtype': 'profile', 'profile_source': 'rclone', 'profile_name': 'azurite', 'bucket': 'motuztest'}
status, c5, raw = req('POST', '/api/connections/', A, az)
check_r('create azure connection from rclone remote', status == 201, raw)
status, body, raw = verify(A, c5['id'])
check_r('azure connection verifies (Azurite)', body['result'] is True, raw)
sh('app', "sudo -u alice sh -c 'mkdir -p /home/alice/azsrc/sub; for i in 1 2 3; do head -c 200000 /dev/urandom > /home/alice/azsrc/f$i.bin; done; echo deep > /home/alice/azsrc/sub/d.txt'")
status, cj, raw = req('POST', '/api/copy-jobs/', A, {'description': 'to azure', 'src_resource_path': '/home/alice/azsrc',
                                                    'dst_cloud_id': c5['id'], 'dst_resource_path': '/motuztest/copy', 'copy_links': True})
cj = wait_job('copy-jobs', cj['id'], A)
check_r('copy local -> Azure SUCCESS', cj['progress_state'] == 'SUCCESS', cj)
status, hj, raw = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/azsrc', 'dst_cloud_id': c5['id'],
                                                       'dst_resource_path': '/motuztest/copy', 'option_download': False})
hj = wait_job('hashsum-jobs', hj['id'], A)
check_r('hashsum local vs Azure identical', hj['progress_state'] == 'SUCCESS' and json.loads(hj['progress_src_tree']) == [] and json.loads(hj['progress_dst_tree']) == [], hj)
status, body, raw = req('POST', '/api/connections/', A, dict(az, name='msi', profile_name='msi'))
check_r('cannot create from a managed-identity remote', status == 400, raw)
status, body, raw = req('POST', '/api/connections/', A, dict(az, profile_source='aws', profile_name=PROFILE))
check_r('aws profile cannot back an azure connection', status == 400, raw)

# ---------------------------------------------------------------- nothing in logs or the database
logs = service_logs('app', 'celery')
check('no secret, key id or token in app/celery logs', no_secret(logs), 'leak')
check('logs show masked key id', '***' + KEY_ID[-4:] in logs)
dump = compose('exec', '-T', 'database', 'pg_dump', f'postgresql://motuz_user:{db_password()}@127.0.0.1:5432/motuz').stdout
check('no secret in a database dump', len(dump) > 1000 and no_secret(dump), len(dump))

sh('app', 'rm -f /home/bob/.aws/credentials')
if AWS_PROFILE:  # the test objects in the bucket
    out = sh('app', f"sudo -u alice /usr/local/bin/rclone --config /home/alice/.config/rclone/rclone.conf purge s3remote:{BUCKET}/{PREFIX}")
    print('cleanup', f's3://{BUCKET}/{PREFIX}:', 'removed' if out.returncode == 0 else redact(out.stderr))
finish()
