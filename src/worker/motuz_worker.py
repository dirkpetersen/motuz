#!/usr/bin/env python3
"""
motuz-worker: runs Motuz copy and integrity-check jobs on a remote worker that can
only make outbound HTTPS connections (optionally through an HTTP proxy) to the central
Motuz node. No database access, no inbound ports. README, "Remote workers (HTTPS only)".

Loop: sign in with the worker credential -> check version and mounts -> long-poll
POST /api/workers/claim -> run the job's rclone as the job's owner with the job
ticket's configuration (the same code as the central Celery worker: api.utils.
rclone_connection, copy/hashsum queues, job_runner) -> report progress every few
seconds (renews the lease; "stop" kills rclone) -> finish -> claim again.

Configuration (environment, e.g. the systemd unit's EnvironmentFile):
  MOTUZ_CENTRAL_URL             https://motuz.example.org (required)
  MOTUZ_WORKER_CREDENTIAL_FILE  file with the worker secret from `manage.py workers add`,
                                mode 600 (default ~/.config/motuz-worker/credential)
  MOTUZ_WORKER_POOL             expected pool of the credential (optional check)
  MOTUZ_REQUIRED_PATHS          comma separated mount points that must be mounted
                                before jobs are claimed, e.g. /home,/fh/fast
  MOTUZ_CA_BUNDLE               CA bundle (PEM) for the central node and rclone, which
                                gets it as SSL_CERT_FILE: must include the public roots
  HTTPS_PROXY, NO_PROXY         HTTP proxy for everything (the agent and rclone)
  MOTUZ_WORKER_JOB_TYPES        copy,hashsum (default: both)

Ephemeral mode (temporary cloud workers): --bootstrap-token-file <file> (or
--bootstrap-token <t>, or MOTUZ_BOOTSTRAP_TOKEN) --once: exchange the single-use token,
run the one job it is bound to, exit.

Exit codes: 0 done, 1 error, 3 --once without a job, 78 configuration error (the
systemd unit does not restart then).
"""
import argparse
import json
import logging
import os
import random
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('MOTUZ_BACKEND_DIR') or os.path.join(HERE, '..', 'backend'))

from api.utils import job_runner  # noqa: E402  (Flask-free, see api/__init__.py)
from api.utils.abstract_connection import RcloneException  # noqa: E402
from api.utils.rclone_connection import RcloneConnection  # noqa: E402
from api.version import VERSION, WORKER_PROTOCOL  # noqa: E402

EXIT_ERROR = 1
EXIT_NO_JOB = 3
EXIT_CONFIG = 78
RCLONE = '/usr/local/bin/rclone' # RcloneConnection runs this path
CLAIM_WAIT = 25
PROXY_VARIABLES = ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy')

log = logging.getLogger('motuz-worker')


class ConfigError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__('{} {}'.format(status, message))
        self.status = status
        self.message = message


class Shutdown(Exception):
    pass


# ------------------------------------------------------------------ configuration

class Config:
    def __init__(self, env, args):
        self.central_url = (args.central_url or env.get('MOTUZ_CENTRAL_URL') or '').rstrip('/')
        if not self.central_url:
            raise ConfigError('MOTUZ_CENTRAL_URL is not set')
        if not self.central_url.startswith('https://') and env.get('MOTUZ_WORKER_ALLOW_HTTP') != '1':
            raise ConfigError('MOTUZ_CENTRAL_URL must be an https:// address')
        self.credential_file = os.path.expanduser(
            env.get('MOTUZ_WORKER_CREDENTIAL_FILE') or '~/.config/motuz-worker/credential')
        self.pool = env.get('MOTUZ_WORKER_POOL') or None
        self.required_paths = [p.strip() for p in (env.get('MOTUZ_REQUIRED_PATHS') or '').replace(':', ',').split(',') if p.strip()]
        self.ca_bundle = env.get('MOTUZ_CA_BUNDLE') or None
        if self.ca_bundle and not os.path.isfile(self.ca_bundle):
            raise ConfigError('MOTUZ_CA_BUNDLE {} does not exist'.format(self.ca_bundle))
        self.job_types = [t.strip() for t in (env.get('MOTUZ_WORKER_JOB_TYPES') or 'copy,hashsum').split(',') if t.strip()]
        self.bootstrap_token = args.bootstrap_token or env.get('MOTUZ_BOOTSTRAP_TOKEN') or None
        if args.bootstrap_token_file:
            with open(args.bootstrap_token_file) as f:
                self.bootstrap_token = f.read().strip()
        self.once = args.once
        self.once_wait = int(env.get('MOTUZ_WORKER_ONCE_WAIT') or 600)
        # rclone runs with an allowlisted environment (user_process_env): it gets the
        # proxy and the CA bundle explicitly
        self.rclone_env = {key: env[key] for key in PROXY_VARIABLES if env.get(key)}
        if self.ca_bundle:
            self.rclone_env['SSL_CERT_FILE'] = os.path.abspath(self.ca_bundle)

    def secret(self):
        """The worker secret; the file must be private to this account"""
        try:
            info = os.stat(self.credential_file)
        except OSError as e:
            raise ConfigError('Cannot read the credential file {}: {}'.format(self.credential_file, e.strerror))
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError('The credential file {} must have mode 600'.format(self.credential_file))
        if info.st_uid != os.getuid():
            raise ConfigError('The credential file {} must belong to this account'.format(self.credential_file))
        with open(self.credential_file) as f:
            secret = f.read().strip()
        if not secret:
            raise ConfigError('The credential file {} is empty'.format(self.credential_file))
        return secret


def check_host(config):
    """Problems that keep this worker from running jobs: [message]"""
    problems = []
    for path in config.required_paths:
        if not os.path.isdir(path):
            problems.append('required path {} does not exist'.format(path))
        elif not os.path.ismount(path):
            problems.append('required path {} is not mounted'.format(path))
    if not os.access(RCLONE, os.X_OK):
        problems.append('{} is missing'.format(RCLONE))
    return problems


def rclone_version():
    try:
        output = subprocess.run([RCLONE, 'version'], capture_output=True, text=True, timeout=10).stdout
        return output.split('\n')[0].strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ the central node

class Central:
    """JSON over HTTPS with the worker's access token; honours HTTPS_PROXY / NO_PROXY"""

    def __init__(self, config):
        self.config = config
        context = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(), # from the environment
            urllib.request.HTTPSHandler(context=context),
        )
        self.token = None
        self.token_expires = 0
        self.info = None
        self.uses_bootstrap = bool(config.bootstrap_token)
        self.stop = None # function: True when retries should give up (shutdown)

    def _raw(self, method, path, body=None, headers=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.config.central_url + path, data=data, method=method)
        request.add_header('Content-Type', 'application/json')
        request.add_header('Accept', 'application/json')
        request.add_header('User-Agent', 'motuz-worker/{}'.format(VERSION))
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw).get('error', raw[:200])
            except (ValueError, AttributeError):
                message = raw[:200].decode('utf-8', 'replace')
            return e.code, {'error': message}

    def call(self, method, path, body=None, *, auth=True, ticket=None, headers=None, timeout=60, retry=True, stop=None):
        """
        Returns (status, body) for 2xx; raises ApiError for other answers. Network
        errors, 429 and 5xx are retried with backoff while `retry`; Shutdown is raised
        instead of the next retry once `stop()` (default: self.stop) is true.
        """
        stop = stop or self.stop
        delay = 1
        reauthenticated = False
        while True:
            headers = dict(headers or {})
            if auth:
                headers['Authorization'] = 'Bearer ' + self.access_token()
            if ticket:
                headers['X-Motuz-Ticket'] = ticket
            try:
                status, answer = self._raw(method, path, body, headers, timeout)
            except (urllib.error.URLError, OSError, ssl.SSLError, ValueError) as e:
                status, answer = None, {'error': str(getattr(e, 'reason', e))}
            if status is not None and 200 <= status < 300:
                return status, answer
            message = (answer or {}).get('error', '')
            if status == 401 and auth and not reauthenticated:
                reauthenticated = True
                self.token = None # expired or refused: sign in again (raises if revoked)
                continue
            if status is not None and status < 500 and status != 429:
                raise ApiError(status, message)
            if not retry:
                raise ApiError(status or 0, message or 'unreachable')
            log.warning('%s %s: %s; retrying in %ss', method, path, status or message, delay)
            _sleep(delay, stop)
            delay = min(delay * 2, 60) + random.random()

    def access_token(self):
        if self.token is None or time.time() > self.token_expires - 60:
            self.sign_in()
        return self.token

    def sign_in(self):
        if self.token is not None and self.info and self.info['worker'].get('ephemeral'):
            # No secret to sign in with again: renew the access token while it is valid
            _, answer = self.call('POST', '/api/workers/auth/refresh', {}, auth=False,
                                  headers={'Authorization': 'Bearer ' + self.token})
        elif self.uses_bootstrap:
            if self.info is not None: # the token was spent; an ephemeral worker cannot sign in again
                raise ApiError(401, 'the access token of this ephemeral worker expired')
            _, answer = self.call('POST', '/api/workers/auth', {
                'bootstrap_token': self.config.bootstrap_token, 'version': VERSION,
                'hostname': socket.gethostname(),
            }, auth=False)
        else:
            _, answer = self.call('POST', '/api/workers/auth', {
                'secret': self.config.secret(), 'version': VERSION, 'hostname': socket.gethostname(),
            }, auth=False)
        self.token = answer['access_token']
        self.token_expires = time.time() + int(answer.get('expires_in') or 300)
        first = self.info is None
        self.info = answer
        if first:
            worker = answer['worker']
            log.info('signed in as worker %s (pool %s%s) at %s', worker['name'], worker['pool'],
                     ', ephemeral, job {}'.format(worker.get('bound_job') or 'any') if worker.get('ephemeral') else '',
                     self.config.central_url)


def _sleep(seconds, stop=None):
    end = time.time() + seconds
    while time.time() < end:
        if stop is not None and stop():
            raise Shutdown()
        time.sleep(min(0.5, max(end - time.time(), 0)))


# ------------------------------------------------------------------ running a job

class TicketConnection:
    """A connection built from a ticket's non-secret fields (profile connections)"""
    def __init__(self, fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        return None


def side_credentials(entry):
    """rclone configuration (RCLONE_CONFIG_*) of one side of a ticket"""
    if entry.get('local'):
        return {}
    remote = entry['remote']
    if remote not in ('src', 'dst'):
        raise RcloneException('Invalid remote in the job ticket')
    if 'profile_connection' in entry:
        # Credentials from the owner's home directory, read here, as the owner, like
        # the central node reads them for its own jobs (utils/local_credentials.py)
        return RcloneConnection()._formatCredentials(TicketConnection(entry['profile_connection']), remote)
    env = entry.get('rclone_env') or {}
    prefix = 'RCLONE_CONFIG_{}_'.format(remote.upper())
    for key, value in env.items():
        if not key.startswith(prefix) or not isinstance(value, str):
            raise RcloneException('Invalid rclone configuration in the job ticket')
    return dict(env)


class JobRun:
    def __init__(self, central, config, ticket, shutdown):
        self.central = central
        self.config = config
        self.ticket = ticket
        self.job = ticket['job']
        self.shutdown = shutdown
        self.connection = RcloneConnection()
        self.stop_requested = False
        self.abandoned = False # the central node ended the job (lease, revoked)
        self.interval = max(1, int(ticket.get('progress_interval') or 5))
        self.last_report = 0
        self.name = '{} job {} of {} (ticket {})'.format(self.job['type'], self.job['id'], self.job['owner'], ticket['ticket_id'])

    # -------------------------------------------------------- reporting
    def report(self, body):
        if self.abandoned or time.time() - self.last_report < self.interval:
            return
        self.last_report = time.time()
        try:
            _, answer = self.central.call(
                'POST', '/api/workers/jobs/{}/progress'.format(self.ticket['ticket_id']), body,
                ticket=self.ticket['ticket_token'], timeout=30, retry=False)
        except Shutdown:
            return
        except ApiError as e:
            if e.status in (401, 403, 404, 410):
                log.error('%s: the central node ended the job (%s); stopping rclone', self.name, e.message)
                self.abandoned = True
                self.terminate()
            else: # unreachable: rclone goes on, the next report retries (the lease allows for it)
                log.warning('%s: progress not reported: %s', self.name, e)
            return
        if answer.get('action') == 'stop' and not self.stop_requested:
            log.info('%s: stopped by the user', self.name)
            self.stop_requested = True
            self.terminate()

    def terminate(self):
        self.connection.terminate_all()

    def check_shutdown(self):
        if self.shutdown() and not getattr(self, '_shutting_down', False):
            self._shutting_down = True
            log.warning('%s: shutting down, stopping rclone', self.name)
            self.terminate()

    # -------------------------------------------------------- jobs
    def run(self):
        log.info('%s: started', self.name)
        start = time.time()
        try:
            if self.job['type'] == 'copy':
                result = self.copy()
            elif self.job['type'] == 'hashsum':
                result = self.hashsum()
            else:
                result = {'state': 'FAILED', 'error_text': 'Unknown job type {}'.format(self.job['type'])}
        except Exception as e: # e.g. a relative local path, a profile that cannot be read
            log.error('%s: %s', self.name, e)
            result = {'state': 'FAILED', 'error_text': str(e)[-10000:]}
        if self.stop_requested:
            result['state'] = 'STOPPED'
        elif getattr(self, '_shutting_down', False):
            result['state'] = 'FAILED'
            result['error_text'] = ((result.get('error_text') or '') + '\nmotuz-worker on {} was shut down'.format(socket.gethostname())).strip()
        if self.abandoned:
            log.info('%s: ended by the central node after %ds', self.name, time.time() - start)
            return
        self.finish(result)
        log.info('%s: %s after %ds', self.name, result['state'], time.time() - start)

    def finish(self, result):
        try:
            self.central.call('POST', '/api/workers/jobs/{}/finish'.format(self.ticket['ticket_id']), result,
                              ticket=self.ticket['ticket_token'], timeout=120, retry=True)
        except (ApiError, Shutdown) as e:
            log.error('%s: could not report the result: %s', self.name, e)

    def copy(self):
        job, options = self.job, self.job.get('options') or {}
        run_id = job['id']
        self.connection.copy_with_credentials(
            {**side_credentials(job['src']), **side_credentials(job['dst'])},
            src_resource_path=job['src']['path'],
            src_local=bool(job['src'].get('local')),
            dst_resource_path=job['dst']['path'],
            dst_local=bool(job['dst'].get('local')),
            user=job['owner'],
            copy_links=bool(options.get('copy_links')),
            job_id=run_id,
            extra_flags=self.ticket.get('rclone_flags') or [],
            extra_env=self.config.rclone_env,
        )

        def tick(percent, text, error_text):
            self.check_shutdown()
            self.report({'percent': percent, 'text': text, 'error_text': error_text})

        exitstatus = job_runner.watch_copy(self.connection, run_id, tick)
        return {
            'state': job_runner.exit_state(exitstatus),
            'exit_status': exitstatus,
            'text': self.connection.copy_text(run_id),
            'error_text': self.connection.copy_error_text(run_id),
        }

    def hashsum(self):
        job, options = self.job, self.job.get('options') or {}

        def start_side(side, run_id):
            entry = job[side]
            self.connection.md5sum_with_credentials(
                side_credentials(entry),
                resource_path=entry['path'],
                local=bool(entry.get('local')),
                user=job['owner'],
                job_id=run_id,
                download=bool(options.get('download')),
                extra_flags=self.ticket.get('rclone_flags') or [],
                extra_env=self.config.rclone_env,
            )

        def tick(side, percent, tree, error_text):
            self.check_shutdown()
            self.report({'percent': percent, 'side': side, 'error_text': error_text})

        result = job_runner.run_hashsum(self.connection, job['id'], start_side, tick,
                                        should_stop=lambda: self.stop_requested or self.abandoned or self.shutdown())
        body = {'state': result['state']}
        for side in job_runner.HASHSUM_SIDES:
            for key in ('{}_tree'.format(side), '{}_error_text'.format(side)):
                if key in result:
                    body[key] = result[key]
        return body


# ------------------------------------------------------------------ main loop

class Worker:
    def __init__(self, config):
        self.config = config
        self.central = Central(config)
        self._shutdown = False
        self.central.stop = self.shutdown
        self.local_rclone_version = rclone_version()

    def shutdown(self):
        return self._shutdown

    def request_shutdown(self, signum, frame):
        if not self._shutdown:
            log.info('signal %s: finishing', signum)
        self._shutdown = True

    def ready(self):
        """Signed in, same version as the central node, mounts present: None or a reason"""
        problems = check_host(self.config)
        if problems:
            return '; '.join(problems)
        self.central.access_token()
        info = self.central.info
        server = info.get('server') or {}
        if server.get('protocol') != WORKER_PROTOCOL or server.get('version') != VERSION:
            return 'version mismatch: this worker is {} (protocol {}), the central node {} (protocol {})'.format(
                VERSION, WORKER_PROTOCOL, server.get('version'), server.get('protocol'))
        if self.config.pool and info['worker']['pool'] != self.config.pool:
            raise ConfigError('the credential belongs to pool {}, not MOTUZ_WORKER_POOL={}'.format(
                info['worker']['pool'], self.config.pool))
        if server.get('rclone_version') and server.get('rclone_version') != self.local_rclone_version:
            log.warning('rclone differs from the central node: %s here, %s there',
                        self.local_rclone_version, server.get('rclone_version'))
        return None

    def run(self):
        waiting_since = time.time()
        last_problem = None
        while not self._shutdown:
            try:
                problem = self.ready()
                if problem:
                    if problem != last_problem:
                        log.error('not claiming jobs: %s', problem)
                        last_problem = problem
                    if self.config.once:
                        return EXIT_CONFIG
                    _sleep(60, self.shutdown)
                    continue
                if last_problem:
                    log.info('ready again')
                    last_problem = None
                status, ticket = self.central.call('POST', '/api/workers/claim', {
                    'pool': self.central.info['worker']['pool'],
                    'capabilities': {
                        'job_types': self.config.job_types,
                        'version': VERSION,
                        'rclone_version': self.local_rclone_version,
                        'hostname': socket.gethostname(),
                    },
                    'wait': CLAIM_WAIT,
                }, timeout=CLAIM_WAIT + 35)
                if status == 204 or not ticket:
                    if self.config.once and time.time() - waiting_since > self.config.once_wait:
                        log.error('no job to claim')
                        return EXIT_NO_JOB
                    continue
                JobRun(self.central, self.config, ticket, self.shutdown).run()
                if self.config.once:
                    return 0
                waiting_since = time.time()
            except Shutdown:
                break
            except ConfigError:
                raise
            except ApiError as e:
                if e.status == 401: # revoked credential, used bootstrap token
                    log.error('refused by the central node: %s', e.message)
                    return EXIT_CONFIG
                log.error('%s', e)
                _sleep(10, self.shutdown)
            except Exception as e:
                log.exception(e)
                _sleep(10, self.shutdown)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Motuz remote worker (outbound HTTPS only)')
    parser.add_argument('--central-url', help='overrides MOTUZ_CENTRAL_URL')
    parser.add_argument('--bootstrap-token', help='single-use bootstrap token (ephemeral worker)')
    parser.add_argument('--bootstrap-token-file', help='file with the bootstrap token (not visible in ps)')
    parser.add_argument('--once', action='store_true', help='run one job, then exit')
    parser.add_argument('--check', action='store_true', help='check configuration, mounts, sign-in and version, then exit')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
    try:
        config = Config(os.environ, args)
        worker = Worker(config)
        signal.signal(signal.SIGTERM, worker.request_shutdown)
        signal.signal(signal.SIGINT, worker.request_shutdown)
        if args.check:
            problem = worker.ready()
            if problem:
                log.error('%s', problem)
                return EXIT_CONFIG
            log.info('ready: version %s, pool %s', VERSION, worker.central.info['worker']['pool'])
            return 0
        log.info('motuz-worker %s starting (central %s)', VERSION, config.central_url)
        return worker.run()
    except ConfigError as e:
        log.error('configuration: %s', e)
        return EXIT_CONFIG
    except ApiError as e:
        log.error('refused by the central node: %s', e.message)
        return EXIT_CONFIG if e.status in (400, 401, 403) else EXIT_ERROR


if __name__ == '__main__':
    sys.exit(main())
