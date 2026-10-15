"""
HEIC/HEIF -> JPEG conversion of the image viewer (utils/heic_convert.py, heic_child.py):
the converter itself (upright, no metadata, limits), the child process it runs in, the
command that starts it (sudo as the logged-in user, never root) and the endpoint.
Needs pi-heif and Pillow (requirements.in); the C decoder only ever runs in a child.
"""
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from api.utils import file_view, heic_child, heic_convert, image_view

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'e2e'))
import viewer_fixtures # noqa: E402 (the same tiny HEIC files as the e2e tests)

HEIC = viewer_fixtures.HEIC # 48 x 64 portrait, a red bar along the top edge
ROTATED = viewer_fixtures.ROTATED_HEIC # the same with irot: displayed 64 x 48, the bar on the left
WITH_METADATA = viewer_fixtures.METADATA_HEIC # 32 x 24 with EXIF (Make, Model, GPS) and XMP
SMALL_CAP = heic_convert.CHILD_CODE.replace('MAX_PIXELS = 100 * 1000 * 1000', 'MAX_PIXELS = 1000')


def jpeg_image(data):
    from PIL import Image
    return Image.open(io.BytesIO(data))


def direct_command(user):
    """The child's command without sudo (a test is not the logged-in user's sudo)"""
    return [sys.executable, '-I', '-c', heic_convert.CHILD_CODE, heic_convert.site()]


class TestConverter(unittest.TestCase):

    def test_portrait_stays_portrait_with_the_bar_on_top(self):
        image = jpeg_image(heic_child.convert(HEIC))
        self.assertEqual(image.format, 'JPEG')
        self.assertEqual(image.size, (48, 64))
        pixel = image.convert('RGB').getpixel((24, 3))
        self.assertGreater(pixel[0], 200) # the red bar
        self.assertLess(pixel[1], 80)

    def test_irot_is_applied(self):
        image = jpeg_image(heic_child.convert(ROTATED)).convert('RGB')
        self.assertEqual(image.size, (64, 48)) # upright: rotated like a viewer shows it
        left, top_middle = image.getpixel((3, 24)), image.getpixel((32, 3))
        self.assertGreater(left[0], 200) # the bar is now on the left edge
        self.assertLess(left[1], 80)
        self.assertLess(top_middle[0], 200) # and no longer along the top

    def test_no_metadata_in_the_output(self):
        jpeg = heic_child.convert(WITH_METADATA)
        image = jpeg_image(jpeg)
        self.assertEqual(image.size, (32, 24))
        self.assertEqual(len(image.getexif()), 0)
        self.assertNotIn('exif', image.info)
        self.assertNotIn('xmp', image.info)
        for secret in (b'SecretMaker', b'SecretCamera', b'SecretXmpAuthor', b'Exif\x00\x00', b'ns.adobe.com/xap', b'GPS'):
            self.assertNotIn(secret, jpeg)

    def test_not_a_heic(self):
        for data in (b'', b'plain text', HEIC[:100], HEIC[:300] + b'\x00' * 300, b'\x89PNG\r\n\x1a\n' + b'\x00' * 64):
            with self.assertRaises(Exception, msg=data[:20]):
                heic_child.convert(data)

    def test_pixel_cap(self):
        with self.assertRaisesRegex(ValueError, 'too large'):
            heic_child.convert(HEIC, max_pixels=48 * 64 - 1)
        heic_child.convert(HEIC, max_pixels=48 * 64)

    def test_long_side_is_capped(self):
        image = jpeg_image(heic_child.convert(HEIC, max_side=32))
        self.assertEqual(image.size, (24, 32))


class TestChild(unittest.TestCase):
    """heic_child.py as the process heic_convert starts (python -I -c <code> <site>)"""

    def run_child(self, data, code=None):
        process = subprocess.run([sys.executable, '-I', '-c', code or heic_convert.CHILD_CODE, ''],
                                 input=data, capture_output=True, timeout=60)
        return process.returncode, process.stdout

    def test_exit_codes(self):
        code, out = self.run_child(HEIC)
        self.assertEqual(code, heic_child.EXIT_OK)
        self.assertTrue(out.startswith(b'\xff\xd8\xff'))
        self.assertEqual(self.run_child(b'not an image')[0], heic_child.EXIT_NOT_DECODABLE)
        self.assertEqual(self.run_child(b'')[0], heic_child.EXIT_NOT_DECODABLE)
        self.assertNotEqual(SMALL_CAP, heic_convert.CHILD_CODE)
        self.assertEqual(self.run_child(HEIC, SMALL_CAP)[0], heic_child.EXIT_TOO_LARGE)

    def test_resource_limits_are_set(self):
        probe = heic_convert.CHILD_CODE.replace("if __name__ == '__main__':\n    sys.exit(main())", """
set_limits()
import resource
for name in ('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE', 'RLIMIT_CORE'):
    print(name, resource.getrlimit(getattr(resource, name)))
""")
        self.assertNotEqual(probe, heic_convert.CHILD_CODE)
        process = subprocess.run([sys.executable, '-I', '-c', probe, ''], capture_output=True, timeout=60)
        out = process.stdout.decode()
        for name, value in heic_child.LIMITS.items():
            self.assertIn('{} ({}, {})'.format(name, value, value), out)

    def test_a_file_cannot_be_written(self):
        probe = heic_convert.CHILD_CODE.replace("if __name__ == '__main__':\n    sys.exit(main())", """
set_limits()
import signal
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
try:
    with open('/tmp/heic-limit-probe.txt', 'w') as f:
        f.write('x' * 10)
        f.flush()
    print('written')
except OSError:
    print('refused')
""")
        process = subprocess.run([sys.executable, '-I', '-c', probe, ''], capture_output=True, timeout=60)
        self.assertIn(b'refused', process.stdout) # RLIMIT_FSIZE 0
        self.assertFalse(os.path.exists('/tmp/heic-limit-probe.txt') and os.path.getsize('/tmp/heic-limit-probe.txt'))


class TestParent(unittest.TestCase):

    def test_convert(self):
        with mock.patch.object(heic_convert, 'command', direct_command):
            jpeg = heic_convert.convert(HEIC, 'alice')
        self.assertEqual(jpeg_image(jpeg).size, (48, 64))

    def test_not_decodable_is_415(self):
        with mock.patch.object(heic_convert, 'command', direct_command):
            with self.assertRaises(heic_convert.ConversionError) as caught:
                heic_convert.convert(b'plain text', 'alice')
        self.assertEqual(caught.exception.status, 415)

    def test_too_many_pixels_is_413(self):
        with mock.patch.object(heic_convert, 'command', lambda user: [sys.executable, '-I', '-c', SMALL_CAP, '']):
            with self.assertRaises(heic_convert.ConversionTooLargeError) as caught:
                heic_convert.convert(HEIC, 'alice')
        self.assertEqual(caught.exception.status, 413)

    def test_timeout_is_504_and_kills_the_process_group(self):
        marker = os.path.join(os.path.dirname(__file__), '.heic-timeout-marker')
        sleeper = ['/bin/sh', '-c', 'sleep 30 & echo $! > ' + marker + '; wait']
        with mock.patch.object(heic_convert, 'command', lambda user: sleeper):
            with self.assertRaises(file_view.ViewTimeoutError) as caught:
                heic_convert.convert(HEIC, 'alice', timeout=1)
        self.assertEqual(caught.exception.status, 504)
        with open(marker) as f:
            pid = int(f.read())
        os.remove(marker)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0) # the grandchild went with the group

    def test_missing_library(self):
        broken = ['/bin/sh', '-c', 'exit %d' % heic_child.EXIT_UNAVAILABLE]
        with mock.patch.object(heic_convert, 'command', lambda user: broken):
            with self.assertRaisesRegex(heic_convert.ConversionError, 'not installed'):
                heic_convert.convert(HEIC, 'alice')

    def test_the_command_is_sudo_as_the_user_through_env(self):
        with mock.patch.dict(os.environ, {'MOTUZ_HEIC_PYTHON': '/usr/bin/python3', 'MOTUZ_HEIC_SITE': '/opt/motuz-heic/site'}):
            command = heic_convert.command('alice')
        self.assertEqual(command[:2], ['sudo', '-n'])
        index = command.index('-u')
        self.assertEqual(command[index + 1], 'alice')
        self.assertEqual(command[index + 2:index + 6], ['/usr/bin/env', '/usr/bin/python3', '-I', '-c'])
        self.assertEqual(command[-1], '/opt/motuz-heic/site')
        self.assertNotIn('-E', command[:index])

    def test_never_root_never_the_service_account(self):
        for user in ('root', '', None):
            with self.assertRaises(ValueError, msg=user):
                heic_convert.command(user)
        import pwd
        with self.assertRaises(ValueError):
            heic_convert.command(pwd.getpwuid(os.geteuid()).pw_name)


class TestInstalls(unittest.TestCase):
    """The Docker image and the systemd install put the converter's packages where every user can read them"""

    ROOT = os.path.join(os.path.dirname(__file__), '..', '..')

    def read(self, *path):
        with open(os.path.join(self.ROOT, *path)) as f:
            return f.read()

    def test_pinned(self):
        for name in ('requirements.in', 'requirements.txt', 'requirements-dev.txt'):
            text = self.read(name).lower()
            self.assertIn('pi-heif', text, name)
            self.assertIn('pillow', text, name)
        for line in self.read('requirements.txt').lower().splitlines():
            if line.startswith(('pi-heif', 'pillow')):
                self.assertRegex(line, r'^[a-z-]+==\d')
        self.assertNotIn('pillow-heif', self.read('requirements.txt').lower()) # the decode-only build

    def test_docker_image(self):
        dockerfile = self.read('deployment', 'docker', 'app', 'Dockerfile')
        self.assertIn('--target /opt/motuz-heic/site', dockerfile)
        self.assertIn('chmod -R a+rX /opt/motuz-heic', dockerfile)
        self.assertIn('ENV MOTUZ_HEIC_SITE=/opt/motuz-heic/site', dockerfile)
        self.assertIn('ENV MOTUZ_HEIC_PYTHON=/usr/local/bin/python3', dockerfile)

    def test_systemd_install(self):
        install = self.read('bin', 'systemd', 'install.sh')
        self.assertIn('--target /opt/motuz-heic.new/site', install)
        self.assertIn('/usr/bin/python3', install)
        deploy = self.read('bin', 'systemd', 'deploy.sh')
        self.assertIn('MOTUZ_HEIC_SITE /opt/motuz-heic/site', deploy)
        self.assertIn('MOTUZ_HEIC_PYTHON /usr/bin/python3', deploy)
        self.assertIn('MOTUZ_HEIC_SITE', self.read('deployment', 'systemd', 'motuz.env.example'))
        # the only privilege is still the one sudoers rule: env runs the interpreter
        self.assertIn('/usr/bin/env', install)


class TestManagerAndEndpoint(unittest.TestCase):

    def setUp(self):
        from api import create_app
        from api.managers import system_manager
        self.system_manager = system_manager
        self.app = create_app('test')
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.user = mock.patch.object(system_manager, 'get_logged_in_user', return_value='alice')
        self.user.start()
        self.view_image = system_manager.view_image.__wrapped__
        self.client = self.app.test_client()

    def tearDown(self):
        self.user.stop()
        self.ctx.pop()

    def test_convert_jpeg_runs_as_the_logged_in_user(self):
        from api.utils.local_connection import LocalConnection
        with mock.patch.object(LocalConnection, 'view_image', return_value=(HEIC, image_view.HEIC)), \
                mock.patch.object(heic_convert, 'convert', return_value=b'\xff\xd8\xffJPEG') as convert:
            result = self.view_image({'connection_id': 0, 'path': '/home/alice/a.heic', 'convert': 'jpeg'})
        self.assertEqual(result, (b'\xff\xd8\xffJPEG', image_view.JPEG, '/home/alice/a.heic', image_view.HEIC))
        self.assertEqual(convert.call_args.args, (HEIC, 'alice'))

    def test_without_convert_or_for_other_types_nothing_is_converted(self):
        from api.utils.local_connection import LocalConnection
        with mock.patch.object(heic_convert, 'convert') as convert:
            with mock.patch.object(LocalConnection, 'view_image', return_value=(HEIC, image_view.HEIC)):
                self.assertEqual(self.view_image({'connection_id': 0, 'path': '/a.heic'})[0], HEIC)
            with mock.patch.object(LocalConnection, 'view_image', return_value=(b'GIF89a', image_view.GIF)):
                result = self.view_image({'connection_id': 0, 'path': '/a.heic', 'convert': 'jpeg'})
                self.assertEqual(result, (b'GIF89a', image_view.GIF, '/a.heic', None))
        convert.assert_not_called()

    def test_errors(self):
        from api.exceptions import (HTTP_400_BAD_REQUEST, HTTP_413_PAYLOAD_TOO_LARGE,
                                    HTTP_415_UNSUPPORTED_MEDIA_TYPE, HTTP_504_GATEWAY_TIMEOUT)
        from api.utils.local_connection import LocalConnection
        for error, expected in ((heic_convert.ConversionError('x'), HTTP_415_UNSUPPORTED_MEDIA_TYPE),
                                (heic_convert.ConversionTooLargeError('x'), HTTP_413_PAYLOAD_TOO_LARGE),
                                (file_view.ViewTimeoutError('x'), HTTP_504_GATEWAY_TIMEOUT)):
            with mock.patch.object(LocalConnection, 'view_image', return_value=(HEIC, image_view.HEIC)), \
                    mock.patch.object(heic_convert, 'convert', side_effect=error):
                with self.assertRaises(expected):
                    self.view_image({'connection_id': 0, 'path': '/a.heic', 'convert': 'jpeg'})
        with self.assertRaises(HTTP_400_BAD_REQUEST):
            self.view_image({'connection_id': 0, 'path': '/a.heic', 'convert': 'png'})

    def test_headers_of_a_converted_image(self):
        converted = (b'\xff\xd8\xffJPEG', image_view.JPEG, '/home/alice/IMG_1.heic', image_view.HEIC)
        with mock.patch.object(self.system_manager, 'view_image', return_value=converted):
            response = self.client.post('/api/system/files/view/image/',
                                        json={'connection_id': 0, 'path': '/home/alice/IMG_1.heic', 'convert': 'jpeg'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
        self.assertEqual(response.headers['X-Motuz-Converted-From'], 'image/heic')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Content-Security-Policy'], "default-src 'none'; sandbox")
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_only_jpeg_is_a_valid_convert(self):
        response = self.client.post('/api/system/files/view/image/',
                                    json={'connection_id': 0, 'path': '/a.heic', 'convert': 'png'})
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
