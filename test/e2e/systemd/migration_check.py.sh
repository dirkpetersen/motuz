# Run as root in the test VM by migration_test.sh (vm.sh root-script): checks the systemd
# install after `migrate_from_docker.sh import`, with the tokens and counts that
# migration_test.sh took from the docker install (/root/motuz-migration-tokens.json).
exec python3 - <<'EOF'
import json, ssl, subprocess, time, urllib.error, urllib.request

ctx = ssl._create_unverified_context()
before = json.load(open('/root/motuz-migration-tokens.json'))

def check(name, ok, detail=''):
    print('PASS' if ok else 'FAIL', name, '' if ok else detail)

def call(method, path, body=None, token=None):
    r = urllib.request.Request('https://localhost' + path, data=json.dumps(body).encode() if body is not None else None,
                               method=method)
    r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or 'null')
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]

status, _ = call('GET', '/api/connections/', token=before['access'])
check('an access token of the docker install works (same Flask key)', status == 200, status)
status, fresh = call('POST', '/api/auth/refresh/', token=before['refresh'])
check('a refresh token of the docker install works', status == 200 and 'access' in fresh, (status, fresh))
token = fresh['access'] if status == 200 else None
status, connections = call('GET', '/api/connections/', token=token)
check("alice's connections moved", status == 200 and sorted(c['name'] for c in connections) == before['connections'],
      (status, connections, before['connections']))
status, page = call('GET', '/api/copy-jobs/?page=1&page_size=100', token=token)
check("alice's copy jobs moved", status == 200 and page['total'] == before['copy_jobs'], (status, page))
status, page = call('GET', '/api/copy-jobs/?page=1&page_size=100', token=token)

settings = open('/var/lib/motuz/.config/motuz/motuz.env').read()
privacy = urllib.request.urlopen('https://localhost/privacy', context=ctx).read().decode()
check('settings moved (operator on /privacy)', 'E2E Lab &lt;R&amp;D&gt;' in privacy, settings[-500:])
check('the database is owned by motuz_user',
      subprocess.run(['runuser', '-u', 'motuz', '--', 'psql', '-h', '/run/user/%d/motuz-postgres' % int(subprocess.check_output(['id', '-u', 'motuz'])),
                      '-d', 'postgres', '-tAc', "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='motuz'"],
                     capture_output=True, text=True).stdout.strip() == 'motuz_user')

subprocess.run(['runuser', '-u', 'alice', '--', 'sh', '-c',
                'rm -rf /home/alice/migsrc /home/alice/migdst; mkdir -p /home/alice/migsrc/sub; echo one > /home/alice/migsrc/a.txt; echo two > /home/alice/migsrc/sub/b.txt'],
               check=True)
status, job = call('POST', '/api/copy-jobs/', {'description': 'after migration', 'src_resource_path': '/home/alice/migsrc',
                                               'dst_resource_path': '/home/alice/migdst', 'copy_links': True}, token)
job_id = job.get('id') if isinstance(job, dict) else None
for _ in range(60):
    status, job = call('GET', '/api/copy-jobs/%s' % job_id, token=token)
    if status == 200 and job['progress_state'] != 'PROGRESS':
        break
    time.sleep(1)
check('a copy job runs after the migration', status == 200 and job['progress_state'] == 'SUCCESS'
      and job_id is not None and job_id > before['copy_jobs'], job)
owner = subprocess.run(['stat', '-c', '%U', '/home/alice/migdst/sub/b.txt'], capture_output=True, text=True).stdout.strip()
check('the copy is owned by alice', owner == 'alice', owner)
EOF
