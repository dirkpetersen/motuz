"""
Code changes for the install without docker (bin/systemd): sudo prefixes that the one
sudoers rule allows, the home directory from NSS, the broker/PAM/login-helper settings,
login refusals, the node check before jobs, and Traefik's flags from docker-compose.yml.
"""
import importlib.util
import json
import os
import pwd
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from flask import Flask

from api.exceptions import HTTP_401_UNAUTHORIZED
from api.managers import auth_manager
from api.utils import abstract_connection, local_connection, local_credentials, node_check
from api.utils.abstract_connection import sudo_as

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
BACKEND = os.path.join(REPO, 'src', 'backend')


class TestSudoPrefix(unittest.TestCase):

    def test_preserves_exactly_the_given_variables(self):
        with mock.patch.dict(os.environ, {'LANG': 'C.UTF-8', 'MOTUZ_FLASK_SECRET_KEY': 'secret',
                                          'MOTUZ_DATABASE_PASSWORD': 'pw', 'HOME': '/root'}):
            os.environ.pop('LC_ALL', None)
            os.environ.pop('TZ', None)
            command = sudo_as('alice', {'RCLONE_CONFIG_SRC_TYPE': 's3', 'RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY': 'k'})
        self.assertEqual(command, ['sudo', '-n', '--preserve-env=LANG,RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY,RCLONE_CONFIG_SRC_TYPE',
                                   '-u', 'alice'])

    def test_no_variables(self):
        with mock.patch.object(abstract_connection, '_INHERITED_VARIABLES', ('PATH',)):
            self.assertEqual(sudo_as('bob'), ['sudo', '-n', '-u', 'bob'])

    def test_extra_env_of_a_remote_worker_is_preserved(self):
        # the remote worker gives rclone its proxy and CA bundle as extra_env
        from api.utils.rclone_connection import RcloneConnection
        connection = RcloneConnection()
        pushed = {}
        with mock.patch.object(connection._copy_job_queue, 'push',
                               side_effect=lambda command, env, job_id: pushed.update(command=command, env=env)):
            connection.copy_with_credentials({}, src_resource_path='/a', src_local=True, dst_resource_path='/b',
                                             dst_local=True, user='alice', copy_links=False, job_id=1,
                                             extra_env={'HTTPS_PROXY': 'http://proxy:3128', 'SSL_CERT_FILE': '/etc/motuz-worker/ca.pem'})
        preserve = [c for c in pushed['command'] if c.startswith('--preserve-env=')][0]
        self.assertIn('HTTPS_PROXY', preserve.split('=', 1)[1].split(','))
        self.assertIn('SSL_CERT_FILE', preserve.split('=', 1)[1].split(','))
        self.assertEqual(pushed['env']['HTTPS_PROXY'], 'http://proxy:3128')

    def test_never_minus_E(self):
        # sudo-rs (Ubuntu 26.04's sudo) ignores -E; -E would also pass everything
        self.assertNotIn('-E', sudo_as('alice', {'RCLONE_CONFIG_CURRENT_TYPE': 'local'}))


class TestAbsoluteCommands(unittest.TestCase):
    """The sudoers rule allows /usr/local/bin/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env"""

    def test_ls_and_mkdir(self):
        with mock.patch.object(local_connection, 'check_output', return_value=b'') as run:
            local_connection._ls_with_impersonation('/data', 'alice')
            local_connection._mkdir_with_impersonation('/data/new', 'alice')
        ls, mkdir = run.call_args_list[0][0][0], run.call_args_list[1][0][0]
        self.assertEqual(ls[:5], ['sudo', '-n', '-u', 'alice', '/usr/bin/ls'])
        self.assertEqual(mkdir[:5], ['sudo', '-n', '-u', 'alice', '/usr/bin/mkdir'])
        self.assertEqual(ls[-2:], ['--', '/data'])

    def test_every_sudo_command_is_allowed(self):
        allowed = {'/usr/local/bin/rclone', '/usr/bin/ls', '/usr/bin/mkdir', '/usr/bin/env'}
        source = ''
        for name in ('local_connection.py', 'rclone_connection.py', 'local_credentials.py', 'file_view.py'):
            with open(os.path.join(BACKEND, 'api', 'utils', name)) as f:
                source += f.read()
        self.assertNotIn("'-i'", source) # no login shells
        self.assertNotIn("'-E'", source)
        self.assertEqual({local_connection.LS, local_connection.MKDIR, local_connection.ENV}, allowed - {'/usr/local/bin/rclone'})
        from api.utils import rclone_connection
        self.assertEqual(rclone_connection.RCLONE, '/usr/local/bin/rclone')


class TestSignalMask(unittest.TestCase):
    """uWSGI blocks signals in its worker threads; sudo-rs keeps an inherited mask and hangs"""

    def in_blocking_thread(self, function):
        import signal
        import threading
        result = {}

        def run():
            signal.pthread_sigmask(signal.SIG_BLOCK, set(signal.Signals) - {signal.SIGKILL, signal.SIGSTOP})
            result['value'] = function()
            result['mask_after'] = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(30)
        return result

    @unittest.skipUnless(os.path.exists('/proc/self/status'), 'needs /proc')
    def test_children_start_with_no_blocked_signals(self):
        from api.utils import file_view
        command = ['grep', '^SigBlk:', '/proc/self/status']
        for name, function in (
                ('check_output', lambda: abstract_connection.check_output(command).decode()),
                ('run', lambda: abstract_connection.run(command, timeout=10).stdout.decode()),
                ('run_limited', lambda: file_view.run_limited(command, 10)[1].decode())):
            result = self.in_blocking_thread(function)
            self.assertEqual(result['value'].split()[-1], '0' * 16, name)
            self.assertGreater(len(result['mask_after']), 30, name) # the thread's own mask is restored


class TestHomeDirectory(unittest.TestCase):

    def test_from_the_user_database_without_a_shell(self):
        entry = pwd.struct_passwd(('alice', 'x', 1501, 1501, '', '/home/alice', '/bin/bash'))
        with mock.patch.object(local_connection.pwd, 'getpwnam', return_value=entry), \
                mock.patch.object(local_connection, 'check_output', side_effect=AssertionError('no subprocess')):
            self.assertEqual(local_connection._homepath_with_impersonation('alice'), '/home/alice')
            self.assertEqual(local_credentials.home_directory('alice'), '/home/alice')

    def test_unknown_user(self):
        with mock.patch.object(local_connection.pwd, 'getpwnam', side_effect=KeyError('nobody-here')):
            with self.assertRaises(local_credentials.LocalCredentialsError):
                local_credentials.home_directory('nobody-here')

    def test_root_home_is_refused_for_credentials(self):
        entry = pwd.struct_passwd(('x', 'x', 1600, 1600, '', '/', '/bin/sh'))
        with mock.patch.object(local_connection.pwd, 'getpwnam', return_value=entry):
            with self.assertRaises(local_credentials.LocalCredentialsError):
                local_credentials.home_directory('x')


def config_values(env):
    """Settings of api.config in a fresh interpreter with `env`"""
    base = {'MOTUZ_FLASK_SECRET_KEY': 'k', 'MOTUZ_DATABASE_PROTOCOL': 'postgresql', 'MOTUZ_DATABASE_USER': 'u',
            'MOTUZ_DATABASE_PASSWORD': 'p', 'MOTUZ_DATABASE_NAME': 'motuz', 'MOTUZ_DATABASE_HOST': '127.0.0.1:5432',
            'PATH': os.environ['PATH']}
    code = ('import json; from api.config import Config as C; print(json.dumps([C.CELERY_BROKER_URL, '
            'C.CELERY_BROKER_TRANSPORT_OPTIONS, C.PAM_SERVICE, C.AUTH_HELPER]))')
    out = subprocess.run([sys.executable, '-c', code], cwd=BACKEND, env=dict(base, **env), capture_output=True, text=True)
    return json.loads(out.stdout)


class TestSettings(unittest.TestCase):

    def test_defaults_are_the_docker_install(self):
        self.assertEqual(config_values({}), ['amqp://', {}, 'login', None])
        self.assertEqual(config_values({'MOTUZ_CELERY_BROKER_URL': '', 'MOTUZ_AUTH_HELPER': ' '}), ['amqp://', {}, 'login', None])

    def test_systemd_install(self):
        url = 'redis+socket://:pw@/run/user/999/motuz-redis/redis.sock'
        broker, options, service, helper = config_values({'MOTUZ_CELERY_BROKER_URL': url, 'MOTUZ_PAM_SERVICE': 'motuz',
                                                          'MOTUZ_AUTH_HELPER': '/run/motuz-auth.sock'})
        self.assertEqual((broker, service, helper), (url, 'motuz', '/run/motuz-auth.sock'))
        # a waiting task is not redelivered while long jobs keep the worker busy
        self.assertEqual(options, {'visibility_timeout': 30 * 24 * 3600})

    def test_celery_gets_the_broker(self):
        code = 'from api.application import celery; print(celery.conf.broker_url, celery.conf.broker_transport_options)'
        base = {'MOTUZ_FLASK_SECRET_KEY': 'k', 'MOTUZ_DATABASE_PROTOCOL': 'postgresql', 'MOTUZ_DATABASE_USER': 'u',
                'MOTUZ_DATABASE_PASSWORD': 'p', 'MOTUZ_DATABASE_NAME': 'motuz', 'MOTUZ_DATABASE_HOST': '127.0.0.1:5432',
                'PATH': os.environ['PATH'], 'MOTUZ_CELERY_BROKER_URL': 'redis+socket://:pw@/tmp/r.sock',
                'MOTUZ_CELERY_VISIBILITY_TIMEOUT': '7200'}
        out = subprocess.run([sys.executable, '-c', code], cwd=BACKEND, env=base, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "redis+socket://:pw@/tmp/r.sock {'visibility_timeout': 7200}", out.stderr[-500:])


class TestLogin(unittest.TestCase):

    def setUp(self):
        self.app = Flask('login-test')
        self.app.config.update(JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256', PAM_SERVICE='motuz')
        from api.application import jwt
        jwt.init_app(self.app)

    def login(self, username, **config):
        self.app.config.update(config)
        with self.app.test_request_context():
            return auth_manager.login_user({'username': username, 'password': 'pw'})

    def test_pam_service_setting(self):
        pam = mock.MagicMock()
        pam.return_value.code = 0
        with mock.patch.object(auth_manager, 'pam', pam), \
                mock.patch.object(auth_manager, '_issue_tokens', return_value={}), \
                mock.patch.object(auth_manager, '_login_refused', return_value=None):
            self.login('alice', AUTH_HELPER=None)
        pam.return_value.authenticate.assert_called_once_with('alice', 'pw', service='motuz')

    def test_login_helper_instead_of_pam(self):
        pam = mock.MagicMock()
        with mock.patch.object(auth_manager, 'pam', pam), \
                mock.patch.object(auth_manager, '_issue_tokens', return_value={}), \
                mock.patch.object(auth_manager, '_login_refused', return_value=None), \
                mock.patch.object(auth_manager.auth_helper, 'check_password', side_effect=[True, False]) as check:
            self.login('alice', AUTH_HELPER='/run/motuz-auth.sock')
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                self.login('alice', AUTH_HELPER='/run/motuz-auth.sock')
        check.assert_called_with('/run/motuz-auth.sock', 'alice', 'pw')
        pam.assert_not_called()

    def test_root_and_the_service_account_are_refused(self):
        pam = mock.MagicMock()
        pam.return_value.code = 0
        me = pwd.getpwuid(os.geteuid()).pw_name
        with mock.patch.object(auth_manager, 'pam', pam):
            for user in ('root', me):
                with self.assertRaises(HTTP_401_UNAUTHORIZED):
                    self.login(user, AUTH_HELPER=None)
        pam.assert_not_called()
        self.assertIsNone(auth_manager._login_refused('no-such-user-motuz'))


class TestNodeCheck(unittest.TestCase):

    def test_required_paths(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        env = {'MOTUZ_REQUIRED_PATHS': '/nonexistent/motuz, /:' + tmp + ',relative'}
        problems = node_check.required_paths_problems(env)
        self.assertEqual(len(problems), 3, problems)
        self.assertIn('/nonexistent/motuz is not a directory', problems[0])
        self.assertIn(tmp + ' is not a mount point', problems[1])
        self.assertIn('relative is not an absolute path', problems[2])
        self.assertEqual(node_check.required_paths_problems({}), [])

    def test_hung_mount(self):
        def hang(path, mount):
            time.sleep(5)
        with mock.patch.object(node_check, '_check_path', side_effect=hang):
            problems = node_check.required_paths_problems({'MOTUZ_REQUIRED_PATHS': '/fh/fast'}, timeout=0.2)
        self.assertEqual(problems, ['/fh/fast does not respond (hung mount?)'])

    def test_script_refuses_to_start(self):
        env = dict(os.environ, MOTUZ_REQUIRED_PATHS='/nonexistent/motuz')
        out = subprocess.run([sys.executable, '-I', os.path.join(BACKEND, 'api', 'utils', 'node_check.py'), 'start'],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn('/nonexistent/motuz is not a directory', out.stderr)
        self.assertIn('refusing to start the worker', out.stderr)
        env['MOTUZ_REQUIRED_PATHS'] = '/'
        out = subprocess.run([sys.executable, '-I', os.path.join(BACKEND, 'api', 'utils', 'node_check.py'), 'start'],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_versions(self):
        version = node_check.local_version(REPO)
        self.assertEqual(len(version['code']), 16)
        self.assertEqual(node_check.code_version(REPO), version['code'])
        self.assertEqual(node_check.version_problems(version, dict(version)), [])
        other = dict(version, code='0' * 16, rclone='v0.0.1')
        self.assertEqual(len(node_check.version_problems(version, other)), 2)
        self.assertEqual(node_check.version_problems(version, None), ['the expected version is unknown'])

    def test_jobs_fail_on_a_node_without_its_mounts(self):
        from api.tasks import celery_tasks
        with mock.patch.dict(os.environ, {'MOTUZ_REQUIRED_PATHS': '/nonexistent/motuz'}):
            with self.assertRaises(RuntimeError) as cm:
                celery_tasks._ensure_node_ready()
        self.assertIn('cannot run jobs: /nonexistent/motuz is not a directory', str(cm.exception))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop('MOTUZ_REQUIRED_PATHS', None)
            celery_tasks._ensure_node_ready()


class TestTraefikArgs(unittest.TestCase):

    def setUp(self):
        spec = importlib.util.spec_from_file_location('traefik_args', os.path.join(REPO, 'bin', 'systemd', 'traefik_args.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_the_compose_flags_with_this_install(self):
        with open(os.path.join(REPO, 'docker-compose.yml')) as f:
            flags = self.module.compose_flags(f.read())
        self.assertIn('--entryPoints.websecure.http.encodedCharacters.allowEncodedSlash=false', flags)
        out = self.module.systemd_flags(flags, '/var/lib/motuz/motuz/deployment/docker/traefik/dynamic',
                                        '/var/lib/motuz/data/traefik/acme.json', {'MOTUZ_ACME_EMAIL': 'a@example.org'})
        self.assertEqual(len(out), len(flags))
        self.assertIn('--providers.file.directory=/var/lib/motuz/motuz/deployment/docker/traefik/dynamic', out)
        self.assertIn('--certificatesResolvers.letsencrypt.acme.storage=/var/lib/motuz/data/traefik/acme.json', out)
        self.assertIn('--certificatesResolvers.letsencrypt.acme.email=a@example.org', out)
        self.assertIn('--certificatesResolvers.letsencrypt.acme.caServer=https://acme-v02.api.letsencrypt.org/directory', out)
        self.assertFalse(any('#' in f or '${' in f or '/etc/traefik' in f for f in out))

    def test_refuses_what_it_cannot_pass_safely(self):
        with self.assertRaises(ValueError):
            self.module.systemd_flags(['--entryPoints.web.address=:80'], '/d', '/a', {})
        with self.assertRaises(ValueError):
            self.module.systemd_flags(['--x=${MOTUZ_ACME_EMAIL}'], '/d', '/a', {'MOTUZ_ACME_EMAIL': 'a b'})


class TestDistroModules(unittest.TestCase):
    """Every bin/systemd/distro/<ID>.sh defines what install.sh, deploy.sh and uninstall.sh use"""

    MODULES = {'ubuntu': ('ubuntu', '26.04'), 'amzn': ('amzn', '2027')}
    VARIABLES = ('DISTRO_NAME', 'PG_BINDIR', 'REDIS_SERVER', 'REDIS_CLI', 'PAM_TEMPLATE', 'DISTRO_SERVICES',
                 'DISTRO_PG_SERVICE', 'DISTRO_REDIS_SERVICE', 'DEFAULT_POSTGRES', 'DEFAULT_REDIS')
    FUNCTIONS = ('distro_supported', 'distro_install_packages', 'distro_install_worker_packages', 'distro_firewall_hint',
                 'distro_system_postgres_prepare')

    def module(self, name, os_id, version):
        script = ('REPO_DIR="$1"; source "$1/bin/systemd/distro/$2.sh"; ID="$3"; VERSION_ID="$4"; '
                  'for v in ' + ' '.join(self.VARIABLES) + ' DISTRO_PYTHON; do printf "%s=%s\\n" "$v" "${!v:-}"; done; '
                  'for f in ' + ' '.join(self.FUNCTIONS) + '; do [ "$(type -t "$f")" = function ] && echo "fn=$f"; done; '
                  'distro_supported && echo supported=1 || echo supported=0')
        out = subprocess.run(['bash', '-c', script, 'bash', REPO, name, os_id, version], capture_output=True, text=True, check=True)
        values = dict(line.split('=', 1) for line in out.stdout.splitlines())
        values['functions'] = [line[3:] for line in out.stdout.splitlines() if line.startswith('fn=')]
        return values

    def test_the_contract(self):
        self.assertEqual(sorted(f[:-3] for f in os.listdir(os.path.join(REPO, 'bin', 'systemd', 'distro'))), sorted(self.MODULES))
        for name, (os_id, version) in self.MODULES.items():
            with self.subTest(name):
                values = self.module(name, os_id, version)
                for variable in self.VARIABLES:
                    self.assertTrue(values[variable], variable)
                self.assertEqual(sorted(values['functions']), sorted(self.FUNCTIONS))
                self.assertEqual(values['supported'], '1')
                self.assertTrue(os.path.isfile(values['PAM_TEMPLATE']), values['PAM_TEMPLATE'])
                self.assertTrue(values['PG_BINDIR'].startswith('/') and values['REDIS_SERVER'].startswith('/'))
                # the unit's placeholders are replaced with these paths (install_units)
                self.assertNotIn('|', values['PG_BINDIR'] + values['REDIS_SERVER'])

    def test_only_its_release(self):
        self.assertEqual(self.module('amzn', 'amzn', '2023')['supported'], '0')
        self.assertEqual(self.module('ubuntu', 'ubuntu', '24.04')['supported'], '0')
        self.assertEqual(self.module('amzn', 'ubuntu', '2027')['supported'], '0')

    def test_amazon_linux(self):
        values = self.module('amzn', 'amzn', '2027')
        # no RabbitMQ or Redis packages on AL2027: Valkey is the broker; its own Python 3.14
        self.assertEqual(values['REDIS_SERVER'], '/usr/bin/valkey-server')
        self.assertEqual(values['DISTRO_PYTHON'], '/usr/bin/python3.14')
        self.assertIn('valkey.service', values['DISTRO_SERVICES'].split())
        with open(values['PAM_TEMPLATE']) as f:
            pam = [line.split() for line in f if line.strip() and not line.startswith('#')]
        self.assertEqual(pam, [['auth', 'include', 'password-auth'], ['account', 'include', 'password-auth']])
        # Ubuntu keeps uv's Python 3.12 (versions.env)
        self.assertEqual(self.module('ubuntu', 'ubuntu', '26.04')['DISTRO_PYTHON'], '')

    def test_defaults_per_distribution(self):
        # Ubuntu uses the distribution's PostgreSQL and Redis services, Amazon Linux its private instances
        ubuntu, amzn = self.module('ubuntu', 'ubuntu', '26.04'), self.module('amzn', 'amzn', '2027')
        self.assertEqual((ubuntu['DEFAULT_POSTGRES'], ubuntu['DEFAULT_REDIS']), ('system', 'system'))
        self.assertEqual((amzn['DEFAULT_POSTGRES'], amzn['DEFAULT_REDIS']), ('private', 'private'))
        self.assertEqual(ubuntu['DISTRO_PG_SERVICE'], 'postgresql.service')
        self.assertEqual(ubuntu['DISTRO_REDIS_SERVICE'], 'redis-server.service')


def bash_lib(script, **env):
    """Runs `script` with bin/systemd/_lib.sh and _system_services.sh sourced"""
    code = 'set -euo pipefail; REPO_DIR=%s; source %s/bin/systemd/_lib.sh; source %s/bin/systemd/_system_services.sh; %s' % (
        REPO, REPO, REPO, script)
    return subprocess.run(['bash', '-c', code], capture_output=True, text=True, env=dict(os.environ, **env))


class TestServiceModes(unittest.TestCase):
    """Where PostgreSQL and the broker live: private (the account's), system (the distribution's), external"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ['rm', '-rf', self.tmp])
        self.settings = os.path.join(self.tmp, 'motuz.env')
        self.pgdata = os.path.join(self.tmp, 'pg')

    def write(self, text):
        with open(self.settings, 'w') as f:
            f.write(text)

    def mode(self, function, *args):
        out = bash_lib('%s %s' % (function, ' '.join(args)))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_database_mode_explicit(self):
        for mode in ('private', 'system', 'external'):
            self.write('MOTUZ_DATABASE_MODE="%s"\nMOTUZ_DATABASE_HOST=127.0.0.1:5432\n' % mode)
            self.assertEqual(self.mode('database_mode', self.settings, self.pgdata), mode)

    def test_database_mode_inferred_for_installs_without_the_setting(self):
        self.write('MOTUZ_DATABASE_HOST=127.0.0.1:5432\n')
        self.assertEqual(self.mode('database_mode', self.settings, self.pgdata), 'unset')
        os.makedirs(self.pgdata)
        with open(os.path.join(self.pgdata, 'PG_VERSION'), 'w') as f:
            f.write('18\n')
        self.assertEqual(self.mode('database_mode', self.settings, self.pgdata), 'private')  # an existing install keeps its cluster
        self.write('MOTUZ_DATABASE_HOST=db.example.org:5432\n')
        self.assertEqual(self.mode('database_mode', self.settings, self.pgdata), 'external')

    def test_invalid_mode_is_refused(self):
        self.write('MOTUZ_DATABASE_MODE=docker\n')
        out = bash_lib('database_mode %s %s' % (self.settings, self.pgdata))
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('use private, system or external', out.stderr)

    def test_broker_mode(self):
        self.write('')
        self.assertEqual(self.mode('broker_mode', self.settings, self.tmp), 'unset')
        with open(os.path.join(self.tmp, 'redis.conf'), 'w') as f:
            f.write('port 0\n')
        self.assertEqual(self.mode('broker_mode', self.settings, self.tmp), 'private')
        self.write('MOTUZ_BROKER_MODE=system\n')
        self.assertEqual(self.mode('broker_mode', self.settings, self.tmp), 'system')

    def test_existing_installs_never_switch_silently(self):
        pick = lambda option, existing, default: bash_lib('pick_mode PostgreSQL MOTUZ_DATABASE_MODE "%s" %s %s' % (option, existing, default))
        # no option: the existing mode wins over the distribution's default
        self.assertEqual(pick('', 'private', 'system').stdout.strip(), 'private')
        self.assertEqual(pick('', 'system', 'private').stdout.strip(), 'system')
        self.assertEqual(pick('', 'external', 'system').stdout.strip(), 'external')
        # a fresh install: the option, else the default
        self.assertEqual(pick('', 'unset', 'system').stdout.strip(), 'system')
        self.assertEqual(pick('private', 'unset', 'system').stdout.strip(), 'private')
        # explicit private -> system is allowed; system -> private and anything for an external database are refused
        self.assertEqual(pick('system', 'private', 'private').stdout.strip(), 'system')
        refused = pick('private', 'system', 'system')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('already runs as a system service', refused.stderr)
        self.assertNotEqual(pick('system', 'external', 'system').returncode, 0)

    def test_units_depend_on_a_database_service_only_when_it_is_private(self):
        def install(db, broker):
            dest = tempfile.mkdtemp(dir=self.tmp)
            out = bash_lib('PG_BINDIR=/usr/lib/postgresql/18/bin; REDIS_SERVER=/usr/bin/redis-server; install_units %s %s %s' % (dest, db, broker))
            self.assertEqual(out.returncode, 0, out.stderr)
            files = {}
            for root, _, names in os.walk(dest):
                for name in names:
                    with open(os.path.join(root, name)) as f:
                        files[os.path.relpath(os.path.join(root, name), dest)] = f.read()
            return files

        files = install('private', 'private')
        self.assertIn('motuz-postgres.service', files)
        self.assertIn('motuz-redis.service', files)
        self.assertIn('Requires=motuz-postgres.service', files['motuz-app.service.d/10-postgres.conf'])
        self.assertIn('Requires=motuz-postgres.service', files['motuz-celery.service.d/10-postgres.conf'])
        self.assertIn('Wants=motuz-postgres.service', files['motuz.target.d/10-postgres.conf'])
        self.assertIn('Wants=motuz-redis.service', files['motuz.target.d/10-redis.conf'])
        self.assertIn('/usr/lib/postgresql/18/bin/postgres', files['motuz-postgres.service'])

        files = install('system', 'system')  # also the external database: nothing local is wanted, required or started
        self.assertEqual(sorted(files), ['motuz-app.service', 'motuz-celery.service', 'motuz-traefik.service', 'motuz.target'])
        for name, text in files.items():
            text = ''.join(line for line in text.splitlines(True) if not line.startswith('#'))
            self.assertNotIn('motuz-postgres', text, name)
            self.assertNotIn('motuz-redis', text, name)

        files = install('system', 'private')
        self.assertNotIn('motuz-postgres.service', files)
        self.assertIn('motuz-redis.service', files)
        self.assertNotIn('motuz-postgres.service.d', ' '.join(files))
        self.assertNotIn('10-postgres.conf', ' '.join(files))

    def test_units_in_the_repository_do_not_name_the_database_or_broker_services(self):
        for name in ('motuz-app.service', 'motuz-celery.service', 'motuz.target'):
            with open(os.path.join(REPO, 'deployment', 'systemd', 'user', name)) as f:
                text = ''.join(line for line in f if not line.startswith('#'))
            self.assertNotIn('motuz-postgres', text, name)
            self.assertNotIn('motuz-redis', text, name)

    def test_system_redis_acl_line(self):
        # the generated config never contains the password, only its SHA-256, and denies the dangerous commands
        with open(os.path.join(REPO, 'bin', 'systemd', '_system_services.sh')) as f:
            text = f.read()
        self.assertIn('user default off', text)
        self.assertIn('port 0', text)
        self.assertIn('unixsocketperm 770', text)
        self.assertIn('-@admin -@dangerous -@scripting', text)


@unittest.skipIf(os.geteuid() == 0, 'config.sh refuses to run as root')
class TestAssistantSupport(unittest.TestCase):
    """bin/systemd/config.sh and the files for a Claude Code deployment (README, "Deploy with an AI assistant")"""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ['rm', '-rf', self.home])
        config = os.path.join(self.home, '.config', 'motuz')
        os.makedirs(config)
        with open(os.path.join(config, 'motuz.env'), 'w') as f:
            f.write('MOTUZ_DATABASE_MODE="system"\nMOTUZ_OPERATOR_NAME="Lab"\n')
        with open(os.path.join(config, 'secrets.env'), 'w') as f:
            f.write('MOTUZ_FLASK_SECRET_KEY="topsecretvalue"\nMOTUZ_GDRIVE_CLIENT_SECRET=""\n')
        self.config = config

    def run_config(self, *args):
        return subprocess.run([os.path.join(REPO, 'bin', 'systemd', 'config.sh'), *args], capture_output=True, text=True,
                              env=dict(os.environ, HOME=self.home))

    def test_show_never_prints_secrets(self):
        out = self.run_config('show')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('MOTUZ_OPERATOR_NAME="Lab"', out.stdout)
        self.assertIn('MOTUZ_FLASK_SECRET_KEY: set', out.stdout)
        self.assertIn('MOTUZ_GDRIVE_CLIENT_SECRET: empty', out.stdout)
        self.assertNotIn('topsecretvalue', out.stdout + out.stderr)

    def test_set_writes_settings_only(self):
        out = self.run_config('set', 'MOTUZ_CONTACT_EMAIL', 'admin@example.org')
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(os.path.join(self.config, 'motuz.env')) as f:
            self.assertIn('MOTUZ_CONTACT_EMAIL="admin@example.org"', f.read())
        for key in ('MOTUZ_FLASK_SECRET_KEY', 'MOTUZ_SMTP_PASSWORD', 'MOTUZ_X_TOKEN'):  # secrets
            self.assertNotEqual(self.run_config('set', key, 'x').returncode, 0, key)
        for key in ('MOTUZ_DATABASE_MODE', 'MOTUZ_DATABASE_HOST', 'MOTUZ_AUTH_HELPER'):  # managed by install.sh/deploy.sh
            self.assertNotEqual(self.run_config('set', key, 'x').returncode, 0, key)
        self.assertNotEqual(self.run_config('set', 'PATH', '/x').returncode, 0)
        with open(os.path.join(self.config, 'secrets.env')) as f:
            self.assertNotIn('"x"', f.read())

    def test_ask_secret_needs_a_terminal_and_a_known_name(self):
        self.assertNotEqual(self.run_config('ask-secret', 'MOTUZ_FLASK_SECRET_KEY').returncode, 0)
        out = self.run_config('ask-secret', 'MOTUZ_GDRIVE_CLIENT_SECRET')  # no tty in the test
        self.assertNotEqual(out.returncode, 0)
        with open(os.path.join(self.config, 'secrets.env')) as f:
            self.assertIn('MOTUZ_GDRIVE_CLIENT_SECRET=""', f.read())

    def test_guardrail_settings(self):
        with open(os.path.join(REPO, 'deployment', 'systemd', 'claude', 'settings.json')) as f:
            settings = json.load(f)
        permissions = settings['permissions']
        self.assertEqual(permissions['defaultMode'], 'default')
        self.assertEqual(permissions['disableBypassPermissionsMode'], 'disable')
        for rule in ('Bash(sudo:*)', 'Bash(su:*)', 'Read(~/.config/motuz/secrets.env)', 'Edit(~/.config/motuz/secrets.env)'):
            self.assertIn(rule, permissions['deny'])
        # nothing the playbook needs is denied, and nothing allowed can escalate or read secrets
        for rule in permissions['allow']:
            self.assertNotIn('sudo', rule)
            self.assertNotIn('secrets.env', rule)
        self.assertFalse(set(permissions['allow']) & set(permissions['deny']))

    def test_playbook(self):
        path = os.path.join(REPO, '.claude', 'skills', 'motuz-deploy', 'SKILL.md')
        with open(path) as f:
            text = f.read()
        front = text.split('---')[1]
        self.assertIn('name: motuz-deploy', front)
        self.assertIn('description:', front)
        self.assertIn('disable-model-invocation: true', front)
        # every script it tells the agent to run exists
        for script in ('bin/systemd/deploy.sh', 'bin/systemd/config.sh', 'bin/systemd/smoke_test.py'):
            self.assertIn(script, text)
            self.assertTrue(os.access(os.path.join(REPO, script), os.X_OK), script)


if __name__ == '__main__':
    unittest.main()
