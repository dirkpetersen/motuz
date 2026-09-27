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

    def test_never_minus_E(self):
        # sudo-rs (Ubuntu 26.04's sudo) ignores -E; -E would also pass everything
        self.assertNotIn('-E', sudo_as('alice', {'RCLONE_CONFIG_CURRENT_TYPE': 'local'}))


class TestAbsoluteCommands(unittest.TestCase):
    """The sudoers rule allows /usr/local/bin/rclone, /usr/bin/ls, /usr/bin/mkdir, /usr/bin/env"""

    def test_ls_and_mkdir(self):
        with mock.patch.object(local_connection.subprocess, 'check_output', return_value=b'') as run:
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


class TestHomeDirectory(unittest.TestCase):

    def test_from_the_user_database_without_a_shell(self):
        entry = pwd.struct_passwd(('alice', 'x', 1501, 1501, '', '/home/alice', '/bin/bash'))
        with mock.patch.object(local_connection.pwd, 'getpwnam', return_value=entry), \
                mock.patch.object(local_connection.subprocess, 'check_output', side_effect=AssertionError('no subprocess')):
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
        env = {'MOTUZ_REQUIRED_PATHS': tmp + ':/nonexistent/motuz', 'MOTUZ_REQUIRED_MOUNTS': '/:' + tmp + ':relative'}
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
            problems = node_check.required_paths_problems({'MOTUZ_REQUIRED_MOUNTS': '/fh/fast'}, timeout=0.2)
        self.assertEqual(problems, ['/fh/fast does not respond (hung mount?)'])

    def test_script_refuses_to_start(self):
        env = dict(os.environ, MOTUZ_REQUIRED_MOUNTS='/nonexistent/motuz')
        out = subprocess.run([sys.executable, '-I', os.path.join(BACKEND, 'api', 'utils', 'node_check.py'), 'start'],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn('/nonexistent/motuz is not a directory', out.stderr)
        self.assertIn('refusing to start the worker', out.stderr)
        env['MOTUZ_REQUIRED_MOUNTS'] = '/'
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
        with mock.patch.dict(os.environ, {'MOTUZ_REQUIRED_MOUNTS': '/nonexistent/motuz'}):
            with self.assertRaises(RuntimeError) as cm:
                celery_tasks._ensure_node_ready()
        self.assertIn('cannot run jobs: /nonexistent/motuz is not a directory', str(cm.exception))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop('MOTUZ_REQUIRED_MOUNTS', None)
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
    VARIABLES = ('DISTRO_NAME', 'PG_BINDIR', 'REDIS_SERVER', 'REDIS_CLI', 'PAM_TEMPLATE', 'DISTRO_SERVICES')
    FUNCTIONS = ('distro_supported', 'distro_install_packages', 'distro_install_worker_packages', 'distro_firewall_hint')

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


if __name__ == '__main__':
    unittest.main()
