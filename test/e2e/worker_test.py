"""Remote workers over HTTPS only (suite `worker`), against the e2e stack with
MOTUZ_LOCAL_JOB_POOL=onprem (run.sh configure_workers) and a fresh fake_ms.py.

A motuz-worker container (compose profile `worker`) sits on an internal Docker network
whose only way out is an HTTP CONNECT proxy that tunnels to motuz.test:443 (Traefik on
the host) and nothing else. It runs as an unprivileged `motuz` account with sudo, with
alice and bob and their homes like the stack. Checks: local copy and integrity-check
jobs run on the worker (job record, file owner), live progress, Stop kills rclone on the
worker, a OneDrive token refresh goes through the HTTPS broker with the job's token,
killing the worker expires its lease, and, acting as other workers from here: auth
separation, pool isolation, ticket and broker scope, revocation, bootstrap tokens and
rate limits. No secret may appear in any log."""
import json
import os
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request

from common import BASE, CTX, FAKE_LOG, WORK, check, compose, finish, psql, sh

SECRETS = [] # every secret this suite sees; none may appear in a log


def req(method, path, token=None, body=None, headers=None, form=None):
    data = None
    r = urllib.request.Request(BASE + path, method=method)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        r.add_header('Content-Type', 'application/x-www-form-urlencoded')
    elif body is not None:
        data = json.dumps(body).encode()
        r.add_header('Content-Type', 'application/json')
    r.data = data
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    for key, value in (headers or {}).items():
        r.add_header(key, value)
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and 'json' in resp.headers.get('Content-Type', '') else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def wc(*args, **kwargs):
    """compose with the worker suite's services (profile worker)"""
    return compose('--profile', 'worker', *args, **kwargs)


def worker_sh(cmd):
    return wc('exec', '-T', 'worker', 'sh', '-c', cmd)


def logs(*services):
    return wc('logs', '--no-color', *services).stdout


def manage(*args):
    command = 'cd /app/src/backend && . ./load-secrets.sh >/dev/null && python3 manage.py ' + ' '.join(shlex.quote(a) for a in args)
    result = compose('exec', '-T', 'app', 'bash', '-c', command)
    return result.returncode, result.stdout.strip(), result.stderr


def add_worker(name, pool):
    rc, out, err = manage('workers', 'add', name, '--pool', pool)
    secret = out.splitlines()[-1] if rc == 0 and out else ''
    check(f'manage.py workers add {name} --pool {pool} prints a secret', secret.startswith('mzw1.'), (rc, out, err[-500:]))
    SECRETS.append(secret)
    return secret


def remote_job(kind, job_id):
    """(state, worker name, pool) of a job's remote_job row"""
    row = psql(f"select r.state, coalesce(w.name, ''), r.pool from remote_job r left join worker w on w.id = r.worker_id "
               f"where r.job_type = '{kind}' and r.job_id = {job_id}")
    return tuple(row.split('|')) if row else None


def wait_job(kind, job_id, token, timeout=120):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        status, job = req('GET', f'/api/{kind}/{job_id}', token)
        if status == 200 and job['progress_state'] != 'PROGRESS':
            return job
        time.sleep(1)
    return job


RCLONE_PIDS = "grep -lx rclone /proc/[0-9]*/comm 2>/dev/null"


def rclone_on_worker():
    return worker_sh(RCLONE_PIDS).stdout.strip()


def wait_for(predicate, timeout, interval=1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def worker_login(secret):
    status, body = req('POST', '/api/workers/auth', body={'secret': secret, 'version': 'test'})
    token = body.get('access_token') if status == 200 else None
    if token:
        SECRETS.append(token)
    return status, token


def upstream_calls():
    return [json.loads(line) for line in open(FAKE_LOG)] if os.path.exists(FAKE_LOG) else []


# ---------------------------------------------------------------- setup
status, a = req('POST', '/api/auth/login/', body={'username': 'alice', 'password': 'AlicePass1'})
A = a['access']
sh('app', "sudo -u alice sh -c 'mkdir -p /home/alice/wsrc/sub && echo one > /home/alice/wsrc/f1.txt && "
          "echo two > /home/alice/wsrc/f2.txt && echo three > /home/alice/wsrc/sub/d.txt'")
sh('app', "sudo -u alice python3 -c \"import os; os.makedirs('/home/alice/wmany', exist_ok=True); "
          "[open(f'/home/alice/wmany/{i}', 'w').write('x' * 4096) for i in range(60000)]\"")

W1 = add_worker('w1', 'onprem')
credential = os.path.join(WORK, 'worker', 'credential')
with open(credential, 'w') as f:
    f.write(W1 + '\n')
os.chmod(credential, 0o600)

result = wc('up', '-d', 'proxy', 'worker')
check('worker and proxy containers start', result.returncode == 0, result.stderr[-1000:])
signed_in = wait_for(lambda: 'signed in as worker w1 (pool onprem)' in logs('worker'), 90)
check('worker signs in through the proxy (https://motuz.test via CONNECT)', signed_in, logs('worker')[-2000:])
AGENT_USERS = ("for p in /proc/[0-9]*; do [ \"$(cat $p/comm 2>/dev/null)\" = python3 ] && grep -q motuz_worker $p/cmdline 2>/dev/null "
               "&& stat -c %U $p; done | sort -u")
check('worker runs as the unprivileged motuz account', worker_sh(AGENT_USERS).stdout.split() == ['motuz'], worker_sh(AGENT_USERS).stdout)

# ---------------------------------------------------------------- network: only 443 through the proxy
gateway = compose('--profile', 'worker', 'exec', '-T', 'proxy', 'getent', 'hosts', 'motuz.test').stdout.split()
gateway_ip = gateway[0] if gateway else '172.17.0.1'
probe = worker_sh(f"""python3 - <<'EOF'
import socket
def try_connect(host, port):
    try:
        socket.create_connection((host, port), 3).close(); return 'open'
    except OSError as e:
        return 'closed'
def via_proxy(target):
    s = socket.create_connection(('proxy', 3128), 3)
    s.sendall(('CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n' % (target, target)).encode())
    return s.recv(100).split(b'\\r\\n')[0].decode()
print(try_connect('{gateway_ip}', 443), try_connect('1.1.1.1', 443))
print(via_proxy('motuz.test:5000'), '|', via_proxy('motuz.test:5432'), '|', via_proxy('example.org:443'), '|', via_proxy('motuz.test:443'))
EOF""").stdout.strip().splitlines()
check('worker cannot connect anywhere directly (host :443, internet)', probe[:1] == ['closed closed'], probe)
check('proxy tunnels only motuz.test:443 (not :5000, :5432, other hosts)',
      len(probe) > 1 and probe[1] == 'HTTP/1.1 403 Forbidden | HTTP/1.1 403 Forbidden | HTTP/1.1 403 Forbidden | HTTP/1.1 200 Connection established',
      probe)

# ---------------------------------------------------------------- local copy on the worker
status, job = req('POST', '/api/copy-jobs/', A, {'description': 'on the worker', 'src_resource_path': '/home/alice/wsrc',
                                                   'dst_resource_path': '/home/alice/wdst', 'copy_links': True,
                                                   'performance': {'transfers': 7, 'checkers': 9}})
check('create local copy job (unchanged user API)', status == 201, (status, job))
job = wait_job('copy-jobs', job['id'], A)
check('copy job SUCCESS on the remote worker', job['progress_state'] == 'SUCCESS' and job['progress_current'] == 100, job)
check('job record: claimed and finished by worker w1 (pool onprem)', remote_job('copy', job['id']) == ('DONE', 'w1', 'onprem'),
      remote_job('copy', job['id']))
check('copy progress text returned by the API', 'Transferred' in (job.get('progress_text') or ''), job.get('progress_text'))
check('job says where it ran (pool onprem, worker w1)',
      (job.get('pool'), job.get('pool_status')) == ('onprem', 'ran on w1'), (job.get('pool'), job.get('pool_status')))
owners = worker_sh('stat -c %U /home/alice/wdst/f1.txt /home/alice/wdst/sub/d.txt').stdout.split()
check('copied files owned by alice (rclone ran as the owner via sudo)', owners == ['alice', 'alice'], owners)
check('rclone ran on the worker, not by celery, with the job\'s performance flags from the ticket',
      '--transfers=7 --checkers=9 copyto /home/alice/wsrc /home/alice/wdst' in logs('worker')
      and '/home/alice/wdst' not in logs('celery'), [l for l in logs('worker').splitlines() if 'copyto' in l][-1:])

status, bad = req('POST', '/api/copy-jobs/', A, {'description': 'relative', 'src_resource_path': '--config=/etc/shadow',
                                                   'dst_resource_path': '/home/alice/y', 'copy_links': True})
bad = wait_job('copy-jobs', bad['id'], A)
check('option-like local path rejected on the worker', bad['progress_state'] == 'FAILED'
      and 'absolute' in (bad.get('progress_error_text') or ''), bad)

# ---------------------------------------------------------------- integrity check on the worker
status, hj = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/wsrc', 'dst_resource_path': '/home/alice/wdst',
                                                    'option_download': False})
hj = wait_job('hashsum-jobs', hj['id'], A)
check('hashsum job SUCCESS on the worker, identical', hj['progress_state'] == 'SUCCESS'
      and json.loads(hj['progress_src_tree']) == [] and json.loads(hj['progress_dst_tree']) == [], hj)
check('hashsum job record: worker w1', remote_job('hashsum', hj['id']) == ('DONE', 'w1', 'onprem'), remote_job('hashsum', hj['id']))
check("md5sum on the worker got the installation's --checkers=16 (MOTUZ_RCLONE_CHECKERS of the central node)",
      '--checkers=16 md5sum /home/alice/wsrc' in logs('worker'), [l for l in logs('worker').splitlines() if 'md5sum' in l][-1:])
sh('app', "sudo -u alice sh -c 'echo changed > /home/alice/wdst/f2.txt; echo extra > /home/alice/wdst/zz.txt'")
status, hj = req('POST', '/api/hashsum-jobs/', A, {'src_resource_path': '/home/alice/wsrc', 'dst_resource_path': '/home/alice/wdst',
                                                    'option_download': False})
hj = wait_job('hashsum-jobs', hj['id'], A)
names = sorted(n['title'] for n in json.loads(hj.get('progress_dst_tree') or '[]'))
check('hashsum on the worker reports the differences', hj['progress_state'] == 'SUCCESS' and names == ['f2.txt', 'zz.txt'], (names, hj['progress_state']))

# ---------------------------------------------------------------- live progress and stop
status, lj = req('POST', '/api/copy-jobs/', A, {'description': 'long', 'src_resource_path': '/home/alice/wmany',
                                                  'dst_resource_path': '/home/alice/wmany_copy', 'copy_links': True})
mid = wait_for(lambda: (lambda s, j: j if s == 200 and 0 < j['progress_current'] < 100 and 'Transferred' in (j.get('progress_text') or '') else None)(
    *req('GET', f"/api/copy-jobs/{lj['id']}", A)), 40)
check('live progress (percent and text) of the remote job in the API', bool(mid), mid)
running = rclone_on_worker()
check('rclone runs in the worker container', bool(running), running)
check('... and not in the celery container', sh('celery', RCLONE_PIDS).stdout.strip() == '', sh('celery', RCLONE_PIDS).stdout)
status, stopped = req('PUT', f"/api/copy-jobs/{lj['id']}/stop/", A)
check('stop returns STOPPED', status == 202 and stopped['progress_state'] == 'STOPPED', (status, stopped))
gone = wait_for(lambda: rclone_on_worker() == '', 30)
check('rclone on the worker is gone after stop', gone, rclone_on_worker())
time.sleep(4)
status, after = req('GET', f"/api/copy-jobs/{lj['id']}", A)
check('job stays STOPPED; its ticket ended', after['progress_state'] == 'STOPPED' and remote_job('copy', lj['id'])[0] == 'DONE',
      (after['progress_state'], remote_job('copy', lj['id'])))

# ---------------------------------------------------------------- OneDrive token refresh through the HTTPS broker
open(FAKE_LOG, 'w').close()
pasted = {'access_token': 'STALE', 'token_type': 'Bearer', 'refresh_token': 'REAL-1', 'expiry': '2020-01-01T00:00:00Z'}
status, conn = req('POST', '/api/connections/', A, {'name': 'od-worker', 'type': 'onedrive', 'onedrive_drive_id': 'b!fake',
                                                     'onedrive_drive_type': 'business', 'onedrive_token': json.dumps(pasted)})
check('create onedrive connection', status == 201, (status, conn))
cid = conn['id']
status, oj = req('POST', '/api/copy-jobs/', A, {'description': 'from onedrive', 'src_cloud_id': cid, 'src_resource_path': '/Documents',
                                                 'dst_resource_path': '/home/alice/od', 'copy_links': True})
oj = wait_job('copy-jobs', oj['id'], A, timeout=150)
calls = [c for c in upstream_calls() if c.get('form', {}).get('grant_type') == 'refresh_token']
check('onedrive job ran on the worker (ends FAILED at Graph, unreachable through the proxy)',
      oj['progress_state'] == 'FAILED' and remote_job('copy', oj['id'])[1] == 'w1', (oj['progress_state'], remote_job('copy', oj['id'])))
check("the worker's rclone refreshed through the HTTPS broker: one upstream refresh with the real token",
      len(calls) == 1 and calls[0]['form'].get('refresh_token') == 'REAL-1' and calls[0]['form'].get('client_secret') == '<ok>', calls)
stored = json.loads(psql(f"select onedrive_token from cloud_connection where id={cid}"))
check('rotated token stored centrally', stored['refresh_token'] == 'REAL-2' and stored['access_token'] == 'ACCESS-2', stored)
worker_log, app_log, proxy_log = logs('worker'), logs('app'), logs('proxy')
check('rclone on the worker got the public HTTPS broker URL',
      "RCLONE_CONFIG_SRC_TOKEN_URL='https://motuz.test/api/workers/oauth/token'" in worker_log,
      [l for l in worker_log.splitlines() if 'TOKEN_URL' in l][-1:])
check('broker use is audited with the ticket', re.search(r'broker refresh: ticket \d+ \(copy:{}\) side src, connection {}, worker w1'.format(oj['id'], cid), app_log),
      [l for l in app_log.splitlines() if 'broker' in l][-3:])
allowed = set(re.findall(r'ALLOW (\S+ \S+)', proxy_log))
check('proxy tunnelled only CONNECT motuz.test:443', allowed == {'CONNECT motuz.test:443'}, allowed)
check("proxy refused rclone's direct Graph connection", 'DENY CONNECT graph.microsoft.com:443' in proxy_log,
      [l for l in proxy_log.splitlines() if 'DENY' in l][-3:])

# ---------------------------------------------------------------- killing the worker expires its lease
status, kj = req('POST', '/api/copy-jobs/', A, {'description': 'killed', 'src_resource_path': '/home/alice/wmany',
                                                  'dst_resource_path': '/home/alice/wmany_kill', 'copy_links': True})
claimed = wait_for(lambda: (remote_job('copy', kj['id']) or ('',))[0] == 'RUNNING' and rclone_on_worker(), 30)
check('long job running on the worker', bool(claimed), remote_job('copy', kj['id']))
killed_at = time.time()
wc('kill', 'worker')
kj = wait_job('copy-jobs', kj['id'], A, timeout=90)
check('killed worker: the job fails once its lease (20 s) expires', kj['progress_state'] == 'FAILED'
      and 15 <= time.time() - killed_at < 90, (kj['progress_state'], round(time.time() - killed_at)))
check('the job says why', 'lease expired' in (kj.get('progress_error') or '') and 'w1' in (kj.get('progress_error') or ''), kj.get('progress_error'))
check('its ticket ended', remote_job('copy', kj['id']) == ('DONE', 'w1', 'onprem'), remote_job('copy', kj['id']))

# ---------------------------------------------------------------- acting as other workers (the real one is down)
W2, W3, W4 = add_worker('w2', 'onprem'), add_worker('w3', 'aws'), add_worker('w4', 'onprem')
status, T2 = worker_login(W2)
check('w2 signs in', status == 200 and T2, status)
T3, T4 = worker_login(W3)[1], worker_login(W4)[1]

status, body = req('POST', '/api/workers/claim', A, {'pool': 'onprem', 'wait': 0})
check('user token refused by worker endpoints', status == 401, (status, body))
status, body = req('GET', '/api/copy-jobs/', T2)
check('worker token refused by user endpoints', status == 401, (status, body))
status, body = req('POST', '/api/system/files/home/', T2, {'connection_id': 0})
check('worker token cannot list files', status == 401, (status, body))

status, j1 = req('POST', '/api/copy-jobs/', A, {'description': 'n1', 'src_resource_path': '/home/alice/wsrc',
                                                  'dst_resource_path': '/home/alice/n1', 'copy_links': True})
status, body = req('POST', '/api/workers/claim', T3, {'pool': 'onprem', 'wait': 0})
check('aws worker cannot claim from pool onprem', status == 403, (status, body))
status, body = req('POST', '/api/workers/claim', T3, {'pool': 'aws', 'wait': 1})
check('aws worker gets no onprem job', status == 204, (status, body))
status, K1 = req('POST', '/api/workers/claim', T2, {'pool': 'onprem', 'wait': 5})
check('w2 claims the queued job', status == 200 and K1['job']['id'] == j1['id'], (status, K1))
SECRETS.append(K1.get('ticket_token', '-'))
check('ticket: only this job, its owner and paths', K1['job']['owner'] == 'alice' and K1['job']['src'] == {'path': '/home/alice/wsrc', 'local': True}
      and set(K1) >= {'ticket_id', 'ticket_token', 'expires_at', 'lease_seconds'}, K1)
status, j2 = req('POST', '/api/copy-jobs/', A, {'description': 'n2', 'src_resource_path': '/home/alice/wsrc',
                                                  'dst_resource_path': '/home/alice/n2', 'copy_links': True})
status, K2 = req('POST', '/api/workers/claim', T4, {'pool': 'onprem', 'wait': 5})
check('w4 claims the next job', status == 200 and K2['job']['id'] == j2['id'], (status, K2))
SECRETS.append(K2.get('ticket_token', '-'))
status, body = req('POST', '/api/workers/claim', T2, {'pool': 'onprem', 'wait': 1})
check('a running job is never handed out again', status == 204, (status, body))

progress = lambda token, ticket_id, ticket: req('POST', f'/api/workers/jobs/{ticket_id}/progress', token, {'percent': 1},
                                               headers={'X-Motuz-Ticket': ticket})
check("w2 cannot report on w4's job with its own ticket", progress(T2, K2['ticket_id'], K1['ticket_token'])[0] == 403)
check("w2 cannot use w4's ticket", progress(T2, K2['ticket_id'], K2['ticket_token'])[0] == 403)
check('w2 reports on its own job', progress(T2, K1['ticket_id'], K1['ticket_token']) [1].get('action') == 'continue')
status, body = req('GET', f"/api/workers/jobs/{K2['ticket_id']}", T2)
check('no endpoint returns a ticket again', status in (404, 405), status)

status, j3 = req('POST', '/api/copy-jobs/', A, {'description': 'n3', 'src_cloud_id': cid, 'src_resource_path': '/Documents',
                                                  'dst_resource_path': '/home/alice/n3', 'copy_links': True})
status, K3 = req('POST', '/api/workers/claim', T2, {'pool': 'onprem', 'wait': 5})
SECRETS.append(K3.get('ticket_token', '-'))
env = K3['job']['src'].get('rclone_env', {}) if status == 200 else {}
rclone_token = json.loads(env.get('RCLONE_CONFIG_SRC_TOKEN', '{}'))
broker_token = rclone_token.get('refresh_token', '')
SECRETS.append(broker_token)
handle = psql(f"select coalesce(token_broker_handle, '') from cloud_connection where id={cid}")
check('onedrive ticket: job-scoped broker token, no handle, no real refresh token',
      broker_token.startswith(f"mzr1.{K3.get('ticket_id')}.src.") and 'REAL-' not in json.dumps(K3)
      and (not handle or handle not in json.dumps(K3)), rclone_token.get('refresh_token', '')[:12])
open(FAKE_LOG, 'w').close()
broker = lambda token: req('POST', '/api/workers/oauth/token', form={'grant_type': 'refresh_token', 'refresh_token': token})
status, body = broker(broker_token)
check('HTTPS broker answers with the job token (cached access token)', status == 200 and body.get('access_token') == 'ACCESS-2'
      and 'refresh_token' not in body, (status, body))
prefix, ticket_id, side, secret = (broker_token.split('.') + ['', '', '', ''])[:4]
check("broker refuses the job's other side", broker('.'.join((prefix, ticket_id, 'dst', secret)))[0] == 400)
check("broker refuses the token for another job's ticket", broker('.'.join((prefix, str(K1['ticket_id']), 'src', secret)))[0] == 400)
check('broker refuses the ticket token', broker(K3.get('ticket_token', 'x'))[0] == 400)
if handle:
    check('public broker refuses the loopback handle', broker(handle)[0] == 400)
check('loopback broker still not exposed', req('POST', '/internal/oauth/token', form={'grant_type': 'refresh_token',
                                                                                      'refresh_token': broker_token})[0] == 404)
status, body = req('POST', f"/api/workers/jobs/{K3['ticket_id']}/finish", T2, {'state': 'FAILED', 'error_text': 'test'},
                   headers={'X-Motuz-Ticket': K3['ticket_token']})
check('w2 finishes its job', status == 200, (status, body))
check('broker token dead after the job ended', broker(broker_token)[0] == 400)
check('no upstream refresh for any of these', upstream_calls() == [], upstream_calls())
status, body = req('POST', f"/api/workers/jobs/{K3['ticket_id']}/progress", T2, {}, headers={'X-Motuz-Ticket': K3['ticket_token']})
check('ended ticket: 410 Gone', status == 410, (status, body))

rc, out, err = manage('workers', 'revoke', 'w2')
check('manage.py workers revoke w2', rc == 0, (rc, out, err[-300:]))
check('revoked worker: access token refused', progress(T2, K1['ticket_id'], K1['ticket_token'])[0] == 401)
check('revoked worker: secret refused', worker_login(W2)[0] == 401)
status, j1 = req('GET', f"/api/copy-jobs/{j1['id']}", A)
check("revoked worker's running job failed", j1['progress_state'] == 'FAILED' and 'revoked' in (j1.get('progress_error') or ''), j1)
req('POST', f"/api/workers/jobs/{K2['ticket_id']}/finish", T4, {'state': 'SUCCESS'}, headers={'X-Motuz-Ticket': K2['ticket_token']})

# ---------------------------------------------------------------- ephemeral worker: bootstrap token, --once
status, bj = req('POST', '/api/copy-jobs/', A, {'description': 'ephemeral', 'src_resource_path': '/home/alice/wsrc',
                                                  'dst_resource_path': '/home/alice/wboot', 'copy_links': True})
rc, token, err = manage('workers', 'bootstrap', '--pool', 'onprem', '--job', f"copy:{bj['id']}", '--ttl', '5m')
check('manage.py workers bootstrap prints a token', rc == 0 and token.startswith('mzb1.'), (rc, err[-300:]))
SECRETS.append(token)
result = wc('run', '--rm', '-e', 'MOTUZ_BOOTSTRAP_TOKEN=' + token, '-e', 'MOTUZ_WORKER_CREDENTIAL_FILE=/nonexistent', 'worker', '--once', timeout=180)
bj = wait_job('copy-jobs', bj['id'], A, timeout=30)
state = remote_job('copy', bj['id'])
check('motuz-worker --once with a bootstrap token runs its job and exits 0', result.returncode == 0 and bj['progress_state'] == 'SUCCESS',
      (result.returncode, bj['progress_state'], result.stdout[-1500:]))
check('... as an ephemeral worker of the pool', state and state[0] == 'DONE' and state[1].startswith('onprem-ephemeral-'), state)
status, body = req('POST', '/api/workers/auth', body={'bootstrap_token': token})
check('bootstrap token is single use', status == 401, (status, body))

# ---------------------------------------------------------------- the revoked real worker is refused
rc, out, err = manage('workers', 'revoke', 'w1')
result = wc('run', '--rm', 'worker', timeout=120)
check('revoked worker w1: refused and exits 78 (no restart loop)', result.returncode == 78
      and 'refused by the central node' in (result.stdout + result.stderr), (result.returncode, (result.stdout + result.stderr)[-800:]))
rc, out, err = manage('workers', 'list')
check('manage.py workers list shows revoked and last seen', re.search(r'w1\s+pool=onprem\s+revoked\s+last seen 20', out) is not None, out)

# ---------------------------------------------------------------- temporary EC2 workers off
# MOTUZ_EC2_WORKERS is unset in this stack: jobs of other pools never start instances
rc, out, err = manage('ec2', 'reap')
check('manage.py ec2 reap: EC2 workers are off by default', rc != 0 and 'EC2 workers are off' in err, (rc, out, err[-300:]))
rc, out, err = manage('ec2', 'status')
check('manage.py ec2 status works without AWS', rc == 0 and out.startswith('EC2 workers off (pool aws'), (rc, out, err[-300:]))
check('ec2_worker table exists (migration e4b8c2d6f1a3) and is empty', psql('select count(*) from ec2_worker') == '0',
      psql('select count(*) from ec2_worker'))
check('no EC2 reaper loop in the celery container', sh('celery', "grep -la '[e]c2' /proc/[0-9]*/cmdline").stdout.strip() == '',
      sh('celery', "grep -la '[e]c2' /proc/[0-9]*/cmdline").stdout)
check('queued jobs keep their source size column (NULL for local jobs)',
      psql("select count(*) from remote_job where source_bytes is not null") == '0',
      psql("select count(*) from remote_job where source_bytes is not null"))

# ---------------------------------------------------------------- rate limit and logs
codes = [req('POST', '/api/workers/auth', body={'secret': 'mzw1.1.wrong'})[0] for _ in range(21)]
check('worker sign-in is rate limited (429)', codes[0] == 401 and 429 in codes, codes)
all_logs = wc('logs', '--no-color').stdout
leaked = [s[:12] for s in SECRETS if s and len(s) > 20 and s in all_logs]
check('no worker secret, access token, ticket, broker or bootstrap token in any log', leaked == [], leaked)
check('no OAuth tokens in any log', 'REAL-' not in all_logs and 'ACCESS-' not in all_logs,
      [l for l in all_logs.splitlines() if 'REAL-' in l or 'ACCESS-' in l][:3])
check('worker auth, claims and ticket use are audited', all(s in app_log + logs('app') for s in (
    'worker sign-in: worker=w1', 'job claimed: copy:', 'ticket refused: worker w2', 'claim refused: worker w3')),
    [l for l in logs('app').splitlines() if 'motuz.audit' in l or 'worker' in l][-5:])

wc('stop', 'worker', 'proxy')
finish()
