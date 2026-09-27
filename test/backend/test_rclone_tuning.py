"""
rclone performance settings (utils/rclone_tuning.py): installation defaults from
MOTUZ_RCLONE_*, per-job overrides, caps, the memory budget, presets and the argv they
become, including attempts to smuggle other flags into rclone's command line.
"""
import logging
import random
import types
import unittest
from unittest import mock

from api.utils import rclone_tuning as tuning
from api.utils.rclone_tuning import MiB, GiB, TuningError, TuningConfigError
from api.utils.rclone_connection import RcloneConnection
from api.utils.abstract_connection import sudo_as


def settings(**env):
    return tuning.load_settings({'MOTUZ_RCLONE_' + k.upper(): v for k, v in env.items()})


DEFAULT = settings()

# Every flag rclone_tuning may put on a command line
ALLOWED_FLAGS = {p.flag for p in tuning.PARAMS} | set(tuning.EXTRA_FLAGS)

INJECTIONS = [
    '4 --config=/etc/shadow',
    '--config=/etc/shadow',
    '--foo',
    '4\n--config=/etc/shadow',
    '4\n',
    ' 4',
    '4;rm -rf /',
    '$(id)',
    '`id`',
    '4\x00',
    '64M --rc',
    '64M\n--rc',
    '64M=--rc',
    '-1',
    '4.5',
    '0x10',
    '1e3',
    '１',  # a non-ASCII digit
    [4],
    {'value': 4},
    True,
    4.0,
]


class TestSizes(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(tuning.parse_size('64M'), 64 * MiB)
        self.assertEqual(tuning.parse_size('64Mi'), 64 * MiB)
        self.assertEqual(tuning.parse_size('64MiB'), 64 * MiB)
        self.assertEqual(tuning.parse_size('64m'), 64 * MiB)
        self.assertEqual(tuning.parse_size('1G'), GiB)
        self.assertEqual(tuning.parse_size('1.5G'), 1536 * MiB)
        self.assertEqual(tuning.parse_size('512K'), 512 * 1024)
        self.assertEqual(tuning.parse_size('100B'), 100)

    def test_parse_rejects(self):
        for value in ('64', '', 'M', '64 M', ' 64M', '64M ', '64M\n', '-64M', '64X', '1e3M', '64M--rc', None, 64):
            self.assertIsNone(tuning.parse_size(value), repr(value))

    def test_format(self):
        self.assertEqual(tuning.format_size(64 * MiB), '64Mi')
        self.assertEqual(tuning.format_size(GiB), '1Gi')
        self.assertEqual(tuning.format_size(1536 * MiB), '1536Mi')
        self.assertEqual(tuning.format_size(100), '100B')

    def test_size_in_bytes(self):
        # the API may give sizes as a number of bytes
        self.assertEqual(tuning.validate_overrides({'s3_chunk_size': 64 * MiB}, 's3', DEFAULT), {'s3_chunk_size': 64 * MiB})
        with self.assertRaises(TuningError):
            tuning.validate_overrides({'s3_chunk_size': 64}, 's3', DEFAULT) # 64 bytes

    def test_parse_int(self):
        self.assertEqual(tuning.parse_int(4), 4)
        self.assertEqual(tuning.parse_int('32'), 32)
        for value in ('4 ', '4\n', '-1', '4.0', '', True, 4.0, None, '１'):
            self.assertIsNone(tuning.parse_int(value), repr(value))


class TestInstallationSettings(unittest.TestCase):

    def test_unset_means_rclone_defaults(self):
        self.assertEqual(DEFAULT.defaults, {})
        self.assertEqual(DEFAULT.extra_flags, ())
        self.assertEqual(DEFAULT.memory_budget, 8 * GiB)
        self.assertEqual(DEFAULT.caps, {'transfers': 64, 'checkers': 128, 'multi_thread_streams': 32,
                                        'upload_concurrency': 64})
        self.assertEqual(tuning.copy_flags(None, 's3', DEFAULT), [])
        self.assertEqual(tuning.copy_flags({}, None, DEFAULT), [])
        self.assertEqual(tuning.hashsum_flags(None, DEFAULT), [])

    def test_empty_values_are_unset(self):
        # docker-compose passes unset .env variables as empty strings
        self.assertEqual(settings(transfers='', s3_chunk_size='  ', memory_budget='').defaults, {})

    def test_values(self):
        s = settings(transfers='16', checkers='32', multi_thread_streams='8', multi_thread_cutoff='128M',
                     buffer_size='32M', s3_upload_concurrency='8', s3_chunk_size='64M',
                     azureblob_upload_concurrency='16', azureblob_chunk_size='32M', memory_budget='64G')
        self.assertEqual(s.defaults['transfers'], 16)
        self.assertEqual(s.defaults['s3_chunk_size'], 64 * MiB)
        self.assertEqual(s.memory_budget, 64 * GiB)
        self.assertEqual(tuning.copy_flags(None, 's3', s), [
            '--transfers=16', '--checkers=32', '--multi-thread-streams=8', '--multi-thread-cutoff=128Mi',
            '--buffer-size=32Mi', '--s3-upload-concurrency=8', '--s3-chunk-size=64Mi'])
        self.assertEqual(tuning.copy_flags(None, 'azureblob', s), [
            '--transfers=16', '--checkers=32', '--multi-thread-streams=8', '--multi-thread-cutoff=128Mi',
            '--buffer-size=32Mi', '--azureblob-upload-concurrency=16', '--azureblob-chunk-size=32Mi'])
        # No backend flags for other destinations
        self.assertEqual(tuning.copy_flags(None, None, s), [
            '--transfers=16', '--checkers=32', '--multi-thread-streams=8', '--multi-thread-cutoff=128Mi',
            '--buffer-size=32Mi'])
        self.assertEqual(tuning.hashsum_flags(None, s), ['--checkers=32'])

    def test_invalid_values_name_the_variable(self):
        cases = [
            ({'transfers': 'abc'}, 'MOTUZ_RCLONE_TRANSFERS'),
            ({'transfers': '0'}, 'MOTUZ_RCLONE_TRANSFERS'),
            ({'transfers': '4 --config=/etc/shadow'}, 'MOTUZ_RCLONE_TRANSFERS'),
            ({'checkers': '99999'}, 'MOTUZ_RCLONE_CHECKERS'),
            ({'buffer_size': '64'}, 'MOTUZ_RCLONE_BUFFER_SIZE'), # no unit
            ({'buffer_size': '2G'}, 'MOTUZ_RCLONE_BUFFER_SIZE'),
            ({'s3_chunk_size': '1M'}, 'MOTUZ_RCLONE_S3_CHUNK_SIZE'), # below S3's 5 MiB parts
            ({'s3_chunk_size': '64M --rc'}, 'MOTUZ_RCLONE_S3_CHUNK_SIZE'),
            ({'max_transfers': '0'}, 'MOTUZ_RCLONE_MAX_TRANSFERS'),
            ({'max_upload_concurrency': 'x'}, 'MOTUZ_RCLONE_MAX_UPLOAD_CONCURRENCY'),
            ({'memory_budget': '8'}, 'MOTUZ_RCLONE_MEMORY_BUDGET'),
            ({'memory_budget': '1M'}, 'MOTUZ_RCLONE_MEMORY_BUDGET'),
        ]
        for env, name in cases:
            with self.assertRaises(TuningConfigError, msg=env) as raised:
                settings(**env)
            self.assertIn(name, str(raised.exception))

    def test_default_above_cap(self):
        with self.assertRaisesRegex(TuningConfigError, 'MOTUZ_RCLONE_TRANSFERS=100 is above MOTUZ_RCLONE_MAX_TRANSFERS=64'):
            settings(transfers='100')
        self.assertEqual(settings(transfers='100', max_transfers='128', memory_budget='16G').defaults['transfers'], 100)
        with self.assertRaisesRegex(TuningConfigError, 'MOTUZ_RCLONE_MAX_UPLOAD_CONCURRENCY'):
            settings(azureblob_upload_concurrency='128')

    def test_defaults_above_memory_budget(self):
        # 32 transfers * (4 streams * 64 MiB + 16 * 64 MiB) = 40 GiB for S3 destinations
        with self.assertRaisesRegex(TuningConfigError, 'MOTUZ_RCLONE_MEMORY_BUDGET=8Gi'):
            settings(transfers='32', buffer_size='64M', s3_upload_concurrency='16', s3_chunk_size='64M')
        s = settings(transfers='32', buffer_size='64M', s3_upload_concurrency='16', s3_chunk_size='64M',
                     memory_budget='64G')
        self.assertEqual(tuning.estimate_memory(s.defaults, 's3'), 40 * GiB)

    def test_extra_flags(self):
        s = settings(extra_flags='--fast-list --max-buffer-memory=16G --use-mmap=false --low-level-retries=20')
        self.assertEqual(s.extra_flags, ('--fast-list', '--max-buffer-memory=16Gi', '--use-mmap=false',
                                         '--low-level-retries=20'))
        self.assertEqual(tuning.copy_flags(None, None, s)[-4:], list(s.extra_flags))
        self.assertEqual(tuning.hashsum_flags(None, s), list(s.extra_flags))

    def test_extra_flags_allowlist(self):
        for text in ('--rc', '--rc-addr=0.0.0.0:5572', '--config=/etc/shadow', '--password-command=id',
                     '--metadata-mapper=/bin/sh', '--log-file=/etc/passwd', '--dump=headers', '--transfers=64',
                     '--ignore-checksum', 'fast-list', '-v', '--fast-list;id', '--fast-list=yes',
                     '--max-buffer-memory', '--max-buffer-memory=lots', '--max-buffer-memory=16G;id',
                     '--low-level-retries=0', '--fast-list --fast-list', '--FAST-LIST'):
            with self.assertRaises(TuningConfigError, msg=text) as raised:
                settings(extra_flags=text)
            self.assertIn('MOTUZ_RCLONE_EXTRA_FLAGS', str(raised.exception))


class TestOverrides(unittest.TestCase):

    def test_custom_values(self):
        values = tuning.validate_overrides({'transfers': 16, 'checkers': '32', 'multi_thread_streams': 8,
                                            'multi_thread_cutoff': '128M', 's3_upload_concurrency': 8,
                                            's3_chunk_size': '32M'}, 's3', DEFAULT)
        self.assertEqual(values, {'transfers': 16, 'checkers': 32, 'multi_thread_streams': 8,
                                  'multi_thread_cutoff': 128 * MiB, 's3_upload_concurrency': 8,
                                  's3_chunk_size': 32 * MiB})
        self.assertEqual(tuning.copy_flags(values, 's3', DEFAULT), [
            '--transfers=16', '--checkers=32', '--multi-thread-streams=8', '--multi-thread-cutoff=128Mi',
            '--s3-upload-concurrency=8', '--s3-chunk-size=32Mi'])
        # What a job stores: ints and rclone sizes, valid input again
        stored = tuning.to_api(values)
        self.assertEqual(stored['s3_chunk_size'], '32Mi')
        self.assertEqual(tuning.validate_overrides(stored, 's3', DEFAULT), values)

    def test_override_installation_default(self):
        s = settings(transfers='8', checkers='16')
        self.assertEqual(tuning.copy_flags({'transfers': 32}, None, s), ['--transfers=32', '--checkers=16'])

    def test_empty_values_inherit(self):
        self.assertEqual(tuning.validate_overrides({'transfers': None, 'checkers': ''}, None, DEFAULT), {})
        self.assertEqual(tuning.validate_overrides(None, None, DEFAULT), {})

    def test_other_backends_are_dropped(self):
        raw = {'transfers': 4, 's3_chunk_size': '64M', 'azureblob_chunk_size': '64M'}
        self.assertEqual(tuning.validate_overrides(raw, None, DEFAULT), {'transfers': 4})
        self.assertEqual(tuning.validate_overrides(raw, 'azureblob', DEFAULT),
                         {'transfers': 4, 'azureblob_chunk_size': 64 * MiB})
        self.assertEqual(tuning.copy_flags(raw, 'azureblob', DEFAULT), ['--transfers=4', '--azureblob-chunk-size=64Mi'])
        # but still validated
        with self.assertRaises(TuningError):
            tuning.validate_overrides({'s3_chunk_size': '64M --rc'}, None, DEFAULT)

    def test_installation_only_and_unknown_names(self):
        for raw in ({'buffer_size': '1G'}, {'config': '/etc/shadow'}, {'--config': '/etc/shadow'},
                    {'transfers --config': 4}, {'max_transfers': 1000}, {'extra_flags': '--rc'}):
            with self.assertRaisesRegex(TuningError, 'Unknown performance setting', msg=raw):
                tuning.validate_overrides(raw, 's3', DEFAULT)
        for raw in ('--transfers=4', ['transfers', 4], 4):
            with self.assertRaisesRegex(TuningError, 'must be an object'):
                tuning.validate_overrides(raw, 's3', DEFAULT)

    def test_injection_attempts(self):
        for name in tuning.PER_JOB:
            for value in INJECTIONS:
                with self.assertRaises(TuningError, msg=(name, value)):
                    tuning.validate_overrides({name: value}, 's3' if 's3' in name else 'azureblob', DEFAULT)
                with self.assertRaises(TuningError, msg=(name, value)):
                    tuning.copy_flags({name: value}, 's3' if 's3' in name else 'azureblob', DEFAULT)

    def test_argv_only_allowlisted_flags(self):
        """Whatever valid values: one argv item per parameter, an allowlisted --flag=value"""
        rng = random.Random(1)
        s = tuning.Settings({}, {'transfers': 1024, 'checkers': 1024, 'multi_thread_streams': 256,
                                 'upload_concurrency': 256}, 2 ** 80, ())
        for _ in range(500):
            dst = rng.choice([None, 's3', 'azureblob', 'onedrive'])
            raw = {}
            for name in tuning.PER_JOB:
                if rng.random() < 0.6:
                    param = tuning.PARAMS_BY_NAME[name]
                    if param.kind == 'int':
                        raw[name] = rng.choice([rng.randint(param.lo, param.hi), str(rng.randint(param.lo, param.hi))])
                    else:
                        raw[name] = '{}{}'.format(rng.randint(5, 999), rng.choice(['M', 'Mi', 'MiB', 'm']))
            flags = tuning.copy_flags(raw, dst, s)
            applicable = [n for n in raw if tuning.PARAMS_BY_NAME[n].backend in (None, dst)]
            self.assertEqual(len(flags), len(applicable), (raw, flags))
            for flag in flags:
                name, _, value = flag.partition('=')
                self.assertIn(name, ALLOWED_FLAGS)
                self.assertRegex(value, r'\A[0-9]+(Ki|Mi|Gi|Ti|B)?\Z')

    def test_caps(self):
        with self.assertRaisesRegex(TuningError, 'Parallel transfers must be at most 64 on this server'):
            tuning.validate_overrides({'transfers': 65}, None, DEFAULT)
        with self.assertRaisesRegex(TuningError, 'at most 128'):
            tuning.validate_overrides({'checkers': 129}, None, DEFAULT)
        with self.assertRaisesRegex(TuningError, 'at most 32'):
            tuning.validate_overrides({'multi_thread_streams': 33}, None, DEFAULT)
        with self.assertRaisesRegex(TuningError, 'S3 upload concurrency must be at most 64'):
            tuning.validate_overrides({'s3_upload_concurrency': 65}, 's3', DEFAULT)
        with self.assertRaisesRegex(TuningError, 'between 1 and 1024'):
            tuning.validate_overrides({'transfers': 0}, None, DEFAULT)
        with self.assertRaisesRegex(TuningError, 'between 5Mi and 5Gi'):
            tuning.validate_overrides({'s3_chunk_size': '1M'}, 's3', DEFAULT)
        # caps also apply to settings for another destination
        with self.assertRaises(TuningError):
            tuning.validate_overrides({'azureblob_upload_concurrency': 65}, 's3', DEFAULT)
        s = settings(max_transfers='8')
        with self.assertRaisesRegex(TuningError, 'at most 8'):
            tuning.validate_overrides({'transfers': 9}, None, s)

    def test_stored_job_revalidated_against_current_caps(self):
        stored = {'transfers': 64}
        self.assertEqual(tuning.copy_flags(stored, None, DEFAULT), ['--transfers=64'])
        with self.assertRaisesRegex(TuningError, 'at most 16'):
            tuning.copy_flags(stored, None, settings(max_transfers='16'))

    def test_hashsum_only_checkers(self):
        self.assertEqual(tuning.hashsum_flags({'checkers': 64}, DEFAULT), ['--checkers=64'])
        self.assertEqual(tuning.hashsum_flags({'checkers': 64}, settings(checkers='16')), ['--checkers=64'])
        with self.assertRaisesRegex(TuningError, 'Unknown performance setting'):
            tuning.hashsum_flags({'transfers': 4}, DEFAULT)
        for value in INJECTIONS:
            with self.assertRaises(TuningError, msg=value):
                tuning.hashsum_flags({'checkers': value}, DEFAULT)


class TestMemory(unittest.TestCase):

    def test_estimate(self):
        # rclone's defaults: 4 transfers * (4 streams * 16 MiB + 4 * 5 MiB) for S3
        self.assertEqual(tuning.estimate_memory({}, 's3'), 4 * (4 * 16 + 4 * 5) * MiB)
        self.assertEqual(tuning.estimate_memory({}, 'azureblob'), 4 * (4 * 16 + 16 * 4) * MiB)
        self.assertEqual(tuning.estimate_memory({}, None), 4 * 4 * 16 * MiB)
        # streams above the upload concurrency drive the chunk term
        self.assertEqual(tuning.estimate_memory({'transfers': 2, 'multi_thread_streams': 8,
                                                 's3_upload_concurrency': 4, 's3_chunk_size': 64 * MiB}, 's3'),
                         2 * (8 * 16 + 8 * 64) * MiB)
        # no multi-thread: one buffer per transfer
        self.assertEqual(tuning.estimate_memory({'multi_thread_streams': 0}, None), 4 * 16 * MiB)

    def test_budget(self):
        # 16 * (16 * 16 MiB + 16 * 64 MiB) = 20 GiB
        raw = {'transfers': 16, 'multi_thread_streams': 16, 's3_upload_concurrency': 16, 's3_chunk_size': '64M'}
        with self.assertRaisesRegex(TuningError, r'about 20 GiB of memory, more than the 8 GiB'):
            tuning.validate_overrides(raw, 's3', DEFAULT)
        # the same for a local destination: 16 * 16 * 16 MiB = 4 GiB
        self.assertEqual(tuning.validate_overrides(raw, None, DEFAULT), {'transfers': 16, 'multi_thread_streams': 16})
        self.assertTrue(tuning.validate_overrides(raw, 's3', settings(memory_budget='32G')))

    def test_budget_counts_installation_defaults(self):
        s = settings(buffer_size='256M', memory_budget='8G') # 4 * 4 * 256 MiB = 4 GiB
        tuning.validate_overrides({'transfers': 8}, None, s) # 8 GiB
        with self.assertRaises(TuningError):
            tuning.validate_overrides({'transfers': 9}, None, s)


class TestPresets(unittest.TestCase):

    def values(self, dst, s=DEFAULT):
        return {p['id']: p for p in tuning.presets(dst, s)}

    def test_default_budget(self):
        local = self.values(None)
        self.assertEqual(local['default']['values'], {})
        self.assertEqual(local['small_files']['values'], {'transfers': 32, 'checkers': 64})
        self.assertEqual(local['large_files']['values'], {'transfers': 4, 'multi_thread_streams': 16,
                                                          'multi_thread_cutoff': '64Mi'})
        self.assertEqual(local['maximum']['values'], {'transfers': 32, 'checkers': 128, 'multi_thread_streams': 16,
                                                      'multi_thread_cutoff': '64Mi'})
        s3 = self.values('s3')
        self.assertEqual(s3['large_files']['values'], {'transfers': 4, 'multi_thread_streams': 16,
                                                       'multi_thread_cutoff': '64Mi', 's3_upload_concurrency': 16,
                                                       's3_chunk_size': '64Mi'})
        self.assertFalse(s3['large_files']['reduced'])
        self.assertEqual(s3['maximum']['values'], {'transfers': 10, 'checkers': 128, 'multi_thread_streams': 16,
                                                   'multi_thread_cutoff': '64Mi', 's3_upload_concurrency': 16,
                                                   's3_chunk_size': '32Mi'})
        self.assertTrue(s3['maximum']['reduced'])
        az = self.values('azureblob')
        self.assertEqual(az['maximum']['values']['azureblob_chunk_size'], '32Mi')

    def test_large_budget(self):
        s3 = self.values('s3', settings(memory_budget='128G'))
        # 64 * (16 * 16 MiB + 16 * 64 MiB) = 80 GiB
        self.assertEqual(s3['maximum']['values'], {'transfers': 64, 'checkers': 128, 'multi_thread_streams': 16,
                                                   'multi_thread_cutoff': '64Mi', 's3_upload_concurrency': 16,
                                                   's3_chunk_size': '64Mi'})
        self.assertFalse(s3['maximum']['reduced'])
        self.assertEqual(s3['maximum']['memory'], 80 * GiB)

    def test_caps_clamp(self):
        s = settings(max_transfers='16', max_checkers='32', max_multi_thread_streams='8', max_upload_concurrency='8')
        s3 = self.values('s3', s)
        self.assertEqual(s3['small_files']['values'], {'transfers': 16, 'checkers': 32})
        self.assertTrue(s3['small_files']['reduced'])
        self.assertEqual(s3['large_files']['values']['multi_thread_streams'], 8)

    def test_presets_are_valid_overrides(self):
        for budget in ('256M', '1G', '8G', '64G'):
            for extra in ({}, {'buffer_size': '64M'}):
                try:
                    s = settings(memory_budget=budget, **extra)
                except TuningConfigError:
                    continue
                for dst in (None, 's3', 'azureblob'):
                    for preset in tuning.presets(dst, s):
                        if preset['available']:
                            tuning.validate_overrides(preset['values'], dst, s) # no TuningError
                            self.assertLessEqual(preset['memory'], s.memory_budget)

    def test_unavailable(self):
        # A budget one transfer of the preset cannot fit (built directly, load_settings refuses it)
        s = tuning.Settings({'buffer_size': 1 * GiB}, dict(tuning.CAPS), 2 * GiB, ())
        maximum = next(target for preset_id, _, _, target in tuning.PRESETS if preset_id == 'maximum')
        values, reduced = tuning.fit_preset(maximum, None, s)
        self.assertIsNone(values)
        self.assertTrue(reduced)

    def test_describe(self):
        d = tuning.describe('s3', settings(transfers='8'))
        names = [f['name'] for f in d['fields']]
        self.assertEqual(names, ['transfers', 'checkers', 'multi_thread_streams', 'multi_thread_cutoff',
                                 's3_upload_concurrency', 's3_chunk_size'])
        transfers = d['fields'][0]
        self.assertEqual((transfers['min'], transfers['max'], transfers['default']), (1, 64, 8))
        self.assertEqual(d['fields'][-1]['default'], '5Mi')
        self.assertEqual(d['memory_budget'], 8 * GiB)
        self.assertEqual([p['id'] for p in d['presets']], ['default', 'small_files', 'large_files', 'maximum'])
        self.assertNotIn('s3_chunk_size', [f['name'] for f in tuning.describe(None, DEFAULT)['fields']])


def _s3_connection():
    return types.SimpleNamespace(
        type='s3', subtype=None, owner='alice', s3_access_key_id='AKIAEXAMPLEKEY1234',
        s3_secret_access_key='SECRET/VALUE', s3_region='us-west-2', s3_endpoint=None, s3_v2_auth=None,
        kms_encryption_key_arn=None,
    )


class TestRcloneCommand(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.INFO)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def command(self, s, performance=None, dst=None, method='copy'):
        connection = RcloneConnection()
        pushed = {}
        queue = connection._copy_job_queue if method == 'copy' else connection._hashsum_job_queue
        with mock.patch.object(tuning, '_settings', s), \
                mock.patch.object(queue, 'push', side_effect=lambda command, env, job_id: pushed.update(command=command, env=env)):
            if method == 'copy':
                connection.copy(None, '/tmp', dst, '/bucket/x', 'alice', False, 1, performance=performance)
            else:
                connection.md5sum(dst, '/bucket/x', 'alice', '1_src', performance=performance)
        return pushed['command'], pushed['env']

    def test_defaults_unset_command_unchanged(self):
        command, env = self.command(DEFAULT, dst=_s3_connection())
        self.assertEqual(command, [
            *sudo_as('alice', env), '/usr/local/bin/rclone', '--config=/dev/null',
            '--s3-disable-checksum', '--s3-no-check-bucket', '--s3-acl', 'bucket-owner-full-control',
            '--exclude=\\.snapshot/', '--contimeout=5m', 'copyto', '/tmp', 'dst:/bucket/x', '--progress', '--stats', '2s'])
        command, env = self.command(DEFAULT, dst=_s3_connection(), method='md5sum')
        self.assertEqual(command, [*sudo_as('alice', env), '/usr/local/bin/rclone', '--config=/dev/null',
                                   'md5sum', 'src:/bucket/x'])

    def test_preset_flags_in_command(self):
        preset = {p['id']: p for p in tuning.presets('s3', DEFAULT)}['maximum']['values']
        command, _ = self.command(DEFAULT, performance=preset, dst=_s3_connection())
        flags = command[command.index('--contimeout=5m') + 1:command.index('copyto')]
        self.assertEqual(flags, ['--transfers=10', '--checkers=128', '--multi-thread-streams=16',
                                 '--multi-thread-cutoff=64Mi', '--s3-upload-concurrency=16', '--s3-chunk-size=32Mi'])
        self.assertEqual(command[-6:], ['copyto', '/tmp', 'dst:/bucket/x', '--progress', '--stats', '2s'])

    def test_installation_defaults_and_hashsum_checkers(self):
        s = settings(checkers='64', extra_flags='--fast-list')
        command, _ = self.command(s, dst=None)
        self.assertIn('--checkers=64', command)
        self.assertIn('--fast-list', command)
        command, _ = self.command(s, performance={'checkers': 16}, dst=_s3_connection(), method='md5sum')
        self.assertEqual(command[command.index('--config=/dev/null') + 1:][:3], ['--checkers=16', '--fast-list', 'md5sum'])

    def test_injection_never_reaches_command(self):
        for value in INJECTIONS:
            with self.assertRaises(TuningError, msg=value):
                self.command(DEFAULT, performance={'transfers': value}, dst=_s3_connection())

    def test_log_masks_secrets_and_shows_flags(self):
        command, env = self.command(DEFAULT, performance={'transfers': 16}, dst=_s3_connection())
        logged = RcloneConnection()._log_command(command, env)
        self.assertIn('--transfers=16', logged)
        self.assertNotIn('SECRET/VALUE', logged)
        self.assertNotIn('AKIAEXAMPLEKEY1234', logged)
        self.assertIn("RCLONE_CONFIG_DST_ACCESS_KEY_ID='***1234'", logged)


class TestApi(unittest.TestCase):
    """The managers turn invalid settings into 400; the DTO takes an object or null"""

    def setUp(self):
        patcher = mock.patch.object(tuning, '_settings', DEFAULT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_copy_job_manager(self):
        from api.exceptions import HTTP_400_BAD_REQUEST
        from api.managers import copy_job_manager
        s3 = types.SimpleNamespace(type='s3')
        self.assertIsNone(copy_job_manager.validate_performance(None, s3))
        self.assertIsNone(copy_job_manager.validate_performance({}, None))
        self.assertEqual(copy_job_manager.validate_performance({'transfers': '16', 's3_chunk_size': '64M'}, s3),
                         {'transfers': 16, 's3_chunk_size': '64Mi'})
        for raw in ({'transfers': '4 --config=/etc/shadow'}, {'transfers': 65}, {'buffer_size': '1G'},
                    {'transfers': 16, 'multi_thread_streams': 16, 's3_upload_concurrency': 16, 's3_chunk_size': '64M'}):
            with self.assertRaises(HTTP_400_BAD_REQUEST, msg=raw):
                copy_job_manager.validate_performance(raw, s3)

    def test_hashsum_job_manager(self):
        from api.exceptions import HTTP_400_BAD_REQUEST
        from api.managers import hashsum_job_manager
        self.assertEqual(hashsum_job_manager._validate_performance({'checkers': '64'}), {'checkers': 64})
        self.assertIsNone(hashsum_job_manager._validate_performance(None))
        for raw in ({'transfers': 4}, {'checkers': '--rc'}, {'checkers': 1000}):
            with self.assertRaises(HTTP_400_BAD_REQUEST, msg=raw):
                hashsum_job_manager._validate_performance(raw)

    def test_dto(self):
        from api import create_app
        from api.managers import copy_job_manager
        client = create_app('test').test_client()
        job = {'description': 'x', 'src_resource_path': '/a', 'dst_resource_path': '/b', 'copy_links': False}
        with mock.patch.object(copy_job_manager, 'create', return_value=dict(job, id=1, performance={'transfers': 8})) as create:
            for performance in ('--transfers=64', 4, ['--rc']):
                response = client.post('/api/copy-jobs/', json=dict(job, performance=performance))
                self.assertEqual(response.status_code, 400, performance)
            create.assert_not_called()
            for performance in (None, {}, {'transfers': 8}):
                response = client.post('/api/copy-jobs/', json=dict(job, performance=performance))
                self.assertEqual(response.status_code, 201, performance)
                self.assertEqual(response.get_json()['performance'], {'transfers': 8})
            self.assertEqual(create.call_args[0][0]['performance'], {'transfers': 8})

    def test_performance_endpoint(self):
        from api import create_app
        from api.managers import copy_job_manager
        client = create_app('test').test_client()
        self.assertEqual(client.get('/api/copy-jobs/performance/?dst_cloud_id=x').status_code, 400)
        self.assertEqual(client.get('/api/copy-jobs/performance/').status_code, 401) # login required
        with mock.patch.object(copy_job_manager, 'performance', side_effect=lambda cid: tuning.describe(None)) as m:
            body = client.get('/api/copy-jobs/performance/?dst_cloud_id=0').get_json()
        m.assert_called_once_with(0)
        self.assertEqual(body['presets'][0]['id'], 'default')


if __name__ == '__main__':
    unittest.main()
