#!/usr/bin/env python3
"""A login and a small local copy job through the API of a Motuz install (README, "Deploy
with an AI assistant (Claude Code)"; used by the playbook .claude/skills/motuz-deploy).

    bin/systemd/smoke_test.py USER PASSWORD_FILE SRC_PATH DST_PATH [BASE_URL]

Signs in as USER (the password is read from PASSWORD_FILE, never from the command line and
never printed), copies the folder or file SRC_PATH to DST_PATH as that user (both local paths
the user can access), waits for the job and prints PASS/FAIL lines. BASE_URL defaults to
https://127.0.0.1 (the self-signed certificate is accepted: it only talks to this host).
Exit status 0 when everything passed.
"""
import json
import ssl
import sys
import time
import urllib.error
import urllib.request


def main(argv):
    if len(argv) not in (5, 6):
        print(__doc__)
        return 2
    user, pw_file, src, dst = argv[1:5]
    base = argv[5].rstrip('/') if len(argv) == 6 else 'https://127.0.0.1'
    ctx = ssl.create_default_context()
    if base.startswith('https://127.0.0.1') or base.startswith('https://localhost'):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with open(pw_file, encoding='utf-8') as f:
        password = f.read().rstrip('\n')
    failures = 0

    def call(method, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(base + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, context=ctx, timeout=60) as response:
                return response.status, json.loads(response.read() or b'null')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'null')
            except ValueError:
                return e.code, None

    def check(name, ok, detail=''):
        nonlocal failures
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ' ' + str(detail)))
        failures += 0 if ok else 1

    status, info = call('GET', '/api/system/info/')
    check('the API is healthy', status == 200 and 'healthy' in json.dumps(info), (status, info))
    status, tokens = call('POST', '/api/auth/login/', {'username': user, 'password': password})
    check('login as %s' % user, status == 200 and isinstance(tokens, dict) and 'access' in tokens, status)
    if status != 200 or not isinstance(tokens, dict):
        print('%d/4 passed' % (2 - failures))
        return 1
    token = tokens['access']
    status, job = call('POST', '/api/copy-jobs/', {'description': 'deploy check', 'src_resource_path': src,
                                                    'dst_resource_path': dst}, token)
    check('copy job created', status in (200, 201) and isinstance(job, dict) and 'id' in job, (status, job))
    if isinstance(job, dict) and 'id' in job:
        for _ in range(120):
            status, job = call('GET', '/api/copy-jobs/%s' % job['id'], token=token)
            if status == 200 and job.get('progress_state') not in ('PROGRESS', 'PENDING', None):
                break
            time.sleep(1)
        check('copy job finished', status == 200 and job.get('progress_state') == 'SUCCESS',
              (status, job.get('progress_state'), job.get('progress_error_text')))
    call('POST', '/api/auth/logout/', token=token)
    print('%d/%d passed' % (4 - failures, 4))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
