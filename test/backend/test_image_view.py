"""
Image viewer (utils/image_view.py, LocalConnection.view_image, RcloneConnection.view_image,
POST /api/system/files/view/image/): type detection by magic number only, SVG refused,
the size cap before and while reading, headers, paths, other users' connections.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from api.utils import file_view, image_view
from api.utils.image_view import GIF, JPEG, PNG, WEBP, detect_type, image_result, parse_max_bytes
from api.utils.local_connection import LocalConnection
from api.utils.rclone_connection import RcloneConnection


PNG_1x1 = bytes.fromhex(
    '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
    '1f15c4890000000d49444154789c6360f80f0000010101005a4d6b0e0000000049454e44ae426082')
JPEG_HEAD = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
GIF_HEAD = b'GIF89a\x01\x00\x01\x00\x80\x00\x00'
WEBP_HEAD = b'RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00'
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

CAP = 4096


class TestDetectType(unittest.TestCase):

    def test_magic_numbers(self):
        self.assertEqual(detect_type(PNG_1x1), PNG)
        self.assertEqual(detect_type(JPEG_HEAD), JPEG)
        self.assertEqual(detect_type(b'\xff\xd8\xff\xdb'), JPEG)
        self.assertEqual(detect_type(GIF_HEAD), GIF)
        self.assertEqual(detect_type(b'GIF87a...'), GIF)
        self.assertEqual(detect_type(WEBP_HEAD), WEBP)
        self.assertEqual(detect_type(b'RIFF\x00\x00\x00\x00WEBPVP8L'), WEBP)
        self.assertEqual(detect_type(b'RIFF\x00\x00\x00\x00WEBPVP8X'), WEBP)

    def test_everything_else(self):
        for data in (b'', b'\x89PNG', b'\x89PNG\r\n\x1a', b'hello', SVG, b'<svg>', b'%PDF-1.7',
                     b'RIFF\x00\x00\x00\x00WAVEfmt ', b'RIFF\x00\x00\x00\x00WEBPXXXX', b'GIF90a',
                     b'\xff\xd8', b'BM\x00\x00', b'\x00\x00\x01\x00', b'II*\x00', b'<html><img src=x onerror=alert(1)>',
                     b' \x89PNG\r\n\x1a\n'):
            self.assertIsNone(detect_type(data), data)

    def test_svg_is_refused_with_its_own_message(self):
        for data in (SVG, b'<svg xmlns="http://www.w3.org/2000/svg"/>', b'\xef\xbb\xbf <svg/>',
                     b'<!-- comment -->\n<svg/>', b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"><svg/>'):
            with self.assertRaisesRegex(image_view.NotAnImageError, 'SVG', msg=data):
                image_result('x.svg', data, len(data), CAP)
        with self.assertRaisesRegex(image_view.NotAnImageError, "'fake.png' is not a PNG, JPEG, GIF or WebP image"):
            image_result('fake.png', b'just text\n', 10, CAP)
        self.assertEqual(image_view.NotAnImageError.status, 415)

    def test_image_result(self):
        self.assertEqual(image_result('a.png', PNG_1x1, len(PNG_1x1), CAP), (PNG_1x1, PNG))
        self.assertEqual(image_result('a.jpg', JPEG_HEAD, None, CAP), (JPEG_HEAD, JPEG))
        data = b'\x89PNG\r\n\x1a\n' + b'\x00' * (CAP - 8)
        self.assertEqual(image_result('cap.png', data, CAP, CAP)[1], PNG) # exactly the cap
        with self.assertRaises(image_view.TooLargeError) as e:
            image_result('grew.png', data + b'\x00', CAP, CAP) # grew after the stat
        self.assertEqual(e.exception.status, 413)
        self.assertIn('the viewer shows images up to 4 KiB', str(e.exception))


class TestMaxBytes(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(parse_max_bytes(None), 25 * 1024 * 1024)
        self.assertEqual(parse_max_bytes(''), 25 * 1024 * 1024)
        self.assertEqual(parse_max_bytes('  '), 25 * 1024 * 1024)
        self.assertEqual(parse_max_bytes('26214400'), 26214400)
        self.assertEqual(parse_max_bytes('25M'), 25 * 1024 * 1024)
        self.assertEqual(parse_max_bytes('25MiB'), 25 * 1024 * 1024)
        self.assertEqual(parse_max_bytes('512k'), 512 * 1024)
        self.assertEqual(parse_max_bytes('256M'), 256 * 1024 * 1024)
        for bad in ('-1', 'abc', '25 MB?', '1.5M', '0', '1023', '257M', '1G', '1T'):
            with self.assertRaises(image_view.ImageConfigError, msg=bad):
                parse_max_bytes(bad)

    def test_environment(self):
        self.assertEqual(image_view.max_bytes({}), image_view.DEFAULT_MAX_BYTES)
        self.assertEqual(image_view.max_bytes({'MOTUZ_VIEW_IMAGE_MAX_BYTES': '4M'}), 4 * 1024 * 1024)
        with self.assertRaisesRegex(image_view.ImageConfigError, 'MOTUZ_VIEW_IMAGE_MAX_BYTES'):
            image_view.max_bytes({'MOTUZ_VIEW_IMAGE_MAX_BYTES': 'lots'})


class TestHeaders(unittest.TestCase):

    def test_headers(self):
        headers = image_view.response_headers('/home/alice/photo.png', PNG)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Content-Security-Policy'], "default-src 'none'; sandbox")
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Content-Disposition'], 'inline; filename="photo.png"; filename*=UTF-8\'\'photo.png')
        with self.assertRaises(ValueError):
            image_view.response_headers('/x.svg', 'image/svg+xml')
        with self.assertRaises(ValueError):
            image_view.response_headers('/x.html', 'text/html')

    def test_filename_is_sanitized(self):
        for path, ascii_name in (('/a/b"; filename=evil.html', 'b__ filename=evil.html'),
                                 ('/a/line\r\nX-Injected: 1.png', 'line__X-Injected_ 1.png'),
                                 ('/a/Grüße ✓.jpg', 'Gru_e _.jpg'),
                                 ('bucket/dir/', 'dir'), ('/', 'image'), ('/a/\\x.png', '_x.png')):
            disposition = image_view.response_headers(path, JPEG)['Content-Disposition']
            self.assertTrue(disposition.startswith('inline; filename="{}"; '.format(ascii_name)), (path, disposition))
            self.assertNotIn('\r', disposition)
            self.assertNotIn('\n', disposition)
            self.assertEqual(disposition.count('"'), 2)
        self.assertIn("filename*=UTF-8''Gr%C3%BC%C3%9Fe%20%E2%9C%93.jpg",
                      image_view.response_headers('/a/Grüße ✓.jpg', JPEG)['Content-Disposition'])


def _without_sudo(test):
    """Runs the reader as the current user: drops `sudo -n -u <user> --`"""
    real = file_view.run_limited

    def run(command, timeout, env=None):
        test.assertEqual(command[:5], ['sudo', '-n', '-u', 'alice', '--'])
        test.assertIn('-I', command)
        return real(command[5:], timeout, env)
    return mock.patch('api.utils.local_connection.file_view.run_limited', side_effect=run)


class Owner:
    owner = 'alice'


class TestLocalImage(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def view(self, path, max_bytes=CAP):
        with _without_sudo(self):
            return LocalConnection().view_image(Owner(), path, max_bytes)

    def test_types(self):
        self.assertEqual(self.view(self.write('a.png', PNG_1x1)), (PNG_1x1, PNG))
        self.assertEqual(self.view(self.write('a.JPG', JPEG_HEAD)), (JPEG_HEAD, JPEG))
        self.assertEqual(self.view(self.write('noext', GIF_HEAD)), (GIF_HEAD, GIF)) # by content, not name
        self.assertEqual(self.view(self.write('a.webp', WEBP_HEAD)), (WEBP_HEAD, WEBP))

    def test_not_an_image(self):
        with self.assertRaisesRegex(image_view.NotAnImageError, "'fake.png' is not a PNG"):
            self.view(self.write('fake.png', b'hello\n'))
        with self.assertRaisesRegex(image_view.NotAnImageError, 'SVG'):
            self.view(self.write('logo.svg', SVG))
        with self.assertRaisesRegex(image_view.NotAnImageError, 'SVG'):
            self.view(self.write('logo.png', SVG)) # an SVG named .png

    def test_too_large_is_refused_before_reading(self):
        path = self.write('big.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * CAP)
        calls = []
        real = file_view.run_limited

        def run(command, timeout, env=None):
            calls.append(command)
            code, stdout, stderr = real(command[5:], timeout, env)
            calls.append(stdout)
            return code, stdout, stderr
        with mock.patch('api.utils.local_connection.file_view.run_limited', side_effect=run):
            with self.assertRaisesRegex(image_view.TooLargeError, "'big.png' is 4 KiB; the viewer shows images up to 4 KiB"):
                LocalConnection().view_image(Owner(), path, CAP)
        self.assertEqual(calls[1], b'{"size": 4104, "error": "large"}') # nothing of the file was read
        self.assertEqual(calls[0][-2:], [str(CAP), path])
        exact = self.write('exact.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * (CAP - 8))
        self.assertEqual(self.view(exact)[1], PNG)

    def test_paths_and_files(self):
        for relative in ('a.png', '-la', '', '--help', '../a.png'):
            with self.assertRaisesRegex(file_view.ViewError, 'must be absolute'):
                self.view(relative)
        with self.assertRaises(file_view.NotFoundError):
            self.view(os.path.join(self.dir, 'missing.png'))
        with self.assertRaisesRegex(file_view.ViewError, 'is a folder'):
            self.view(self.dir)
        os.mkfifo(os.path.join(self.dir, 'fifo.png'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view(os.path.join(self.dir, 'fifo.png'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view('/dev/zero')

    @unittest.skipIf(os.geteuid() == 0, 'root can read everything')
    def test_permission_denied(self):
        path = self.write('secret.png', PNG_1x1)
        os.chmod(path, 0)
        with self.assertRaises(file_view.ForbiddenError):
            self.view(path)

    def test_timeout(self):
        with mock.patch('api.utils.local_connection.file_view.run_limited',
                        side_effect=file_view.ViewTimeoutError('timed out')):
            with self.assertRaises(file_view.ViewTimeoutError):
                LocalConnection().view_image(Owner(), '/x.png', CAP)


class TestCloudImage(unittest.TestCase):

    def data(self):
        conn = mock.Mock()
        conn.owner = 'alice'
        conn.type = 'webdav'
        conn.webdav_url = 'http://127.0.0.1:9/'
        conn.webdav_user = None
        conn.webdav_pass = None
        return conn

    def run_image(self, outputs, path='/dir/photo.png'):
        calls = []

        def run(command, timeout, env=None):
            calls.append((command, env))
            return outputs.pop(0)

        with mock.patch('api.utils.rclone_connection.file_view.run_limited', side_effect=run):
            try:
                return RcloneConnection().view_image(self.data(), path, CAP), calls
            except Exception as e:
                return e, calls

    def test_png(self):
        result, calls = self.run_image([(0, b'{"Size":%d,"IsDir":false,"MimeType":"text/html"}' % len(PNG_1x1), b''),
                                        (0, PNG_1x1, b'')])
        self.assertEqual(result, (PNG_1x1, PNG)) # the detected type, not rclone's MimeType
        stat, cat = calls[0][0], calls[1][0]
        self.assertEqual(stat[:4], ['sudo', '-E', '-u', 'alice'])
        self.assertEqual(stat[-3:], ['lsjson', '--stat', 'current:/dir/photo.png'])
        self.assertEqual(cat[-4:], ['cat', '--count', str(CAP + 1), 'current:/dir/photo.png'])
        self.assertFalse(any(key.startswith('MOTUZ_') for key in calls[1][1]))

    def test_too_large_is_never_catted(self):
        result, calls = self.run_image([(0, b'{"Size":5000000,"IsDir":false}', b'')])
        self.assertIsInstance(result, image_view.TooLargeError)
        self.assertIn('4.8 MiB', str(result))
        self.assertEqual(len(calls), 1)
        # Grew after the stat: more than the cap arrives
        result, calls = self.run_image([(0, b'{"Size":100,"IsDir":false}', b''), (0, b'\x89PNG\r\n\x1a\n' + b'\x00' * CAP, b'')])
        self.assertIsInstance(result, image_view.TooLargeError)

    def test_refused(self):
        result, calls = self.run_image([(0, b'{"Size":10,"IsDir":true}', b'')])
        self.assertRegex(str(result), 'is a folder or does not exist')
        self.assertEqual(len(calls), 1)
        result, _ = self.run_image([(0, b'{"Size":%d,"IsDir":false,"MimeType":"image/png"}' % len(SVG), b''), (0, SVG, b'')])
        self.assertIsInstance(result, image_view.NotAnImageError)
        self.assertIn('SVG', str(result))
        result, _ = self.run_image([(0, b'{"Size":6,"IsDir":false,"MimeType":"image/png"}', b''), (0, b'hello\n', b'')])
        self.assertIsInstance(result, image_view.NotAnImageError)
        result, _ = self.run_image([(3, b'', b'ERROR : directory not found')])
        self.assertIsInstance(result, file_view.NotFoundError)


class TestEndpoint(unittest.TestCase):

    def setUp(self):
        from api import create_app
        self.app = create_app('test')
        self.client = self.app.test_client()

    def test_login_required(self):
        response = self.client.post('/api/system/files/view/image/', json={'connection_id': 0, 'path': '/a.png'})
        self.assertEqual(response.status_code, 401)

    def test_headers(self):
        from api.managers import system_manager
        with mock.patch.object(system_manager, 'view_image', return_value=(PNG_1x1, PNG, '/home/alice/a b.png')):
            response = self.client.post('/api/system/files/view/image/', json={'connection_id': 0, 'path': '/home/alice/a b.png'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG_1x1)
        self.assertEqual(response.headers['Content-Type'], 'image/png')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Content-Security-Policy'], "default-src 'none'; sandbox")
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Content-Disposition'], 'inline; filename="a b.png"; filename*=UTF-8\'\'a%20b.png')
        self.assertEqual(response.headers['Content-Length'], str(len(PNG_1x1)))

    def test_errors_are_json(self):
        from api.exceptions import HTTP_413_PAYLOAD_TOO_LARGE, HTTP_415_UNSUPPORTED_MEDIA_TYPE
        from api.managers import system_manager
        for error, status in ((HTTP_415_UNSUPPORTED_MEDIA_TYPE("'x.svg' is an SVG image"), 415),
                              (HTTP_413_PAYLOAD_TOO_LARGE("'x.png' is 30.0 MiB"), 413)):
            with mock.patch.object(system_manager, 'view_image', side_effect=error):
                response = self.client.post('/api/system/files/view/image/', json={'connection_id': 0, 'path': '/x'})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers['Content-Type'], 'application/json')
            self.assertIn('x.', response.get_json()['message'])

    def test_validation(self):
        for body in ({'path': '/a.png'}, {'connection_id': 0}, {'connection_id': 'x', 'path': '/a.png'},
                     {'connection_id': 0, 'path': 5}):
            self.assertEqual(self.client.post('/api/system/files/view/image/', json=body).status_code, 400, body)


class TestManager(unittest.TestCase):
    """view_image as alice: the error mapping, the cap from the config and other users' connections"""

    def setUp(self):
        from api import create_app
        from api.managers import system_manager
        self.system_manager = system_manager
        self.app = create_app('test')
        self.app.config['VIEW_IMAGE_MAX_BYTES'] = 12345
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.user = mock.patch.object(system_manager, 'get_logged_in_user', return_value='alice')
        self.user.start()
        # token_required checks the token first; call the function it wraps
        self.view_image = system_manager.view_image.__wrapped__

    def tearDown(self):
        self.user.stop()
        self.ctx.pop()

    def test_local(self):
        with mock.patch.object(LocalConnection, 'view_image', return_value=(PNG_1x1, PNG)) as view:
            self.assertEqual(self.view_image({'connection_id': 0, 'path': '/home/alice/a.png'}),
                             (PNG_1x1, PNG, '/home/alice/a.png'))
        self.assertEqual(view.call_args.kwargs['max_bytes'], 12345)
        self.assertEqual(view.call_args.kwargs['data'].owner, 'alice')

    def test_errors(self):
        from api.exceptions import (HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND,
                                    HTTP_413_PAYLOAD_TOO_LARGE, HTTP_415_UNSUPPORTED_MEDIA_TYPE, HTTP_504_GATEWAY_TIMEOUT)
        for error, http in ((image_view.TooLargeError('big'), HTTP_413_PAYLOAD_TOO_LARGE),
                            (image_view.NotAnImageError('svg'), HTTP_415_UNSUPPORTED_MEDIA_TYPE),
                            (file_view.ForbiddenError('no'), HTTP_403_FORBIDDEN),
                            (file_view.NotFoundError('gone'), HTTP_404_NOT_FOUND),
                            (file_view.ViewError('folder'), HTTP_400_BAD_REQUEST),
                            (file_view.ViewTimeoutError('slow'), HTTP_504_GATEWAY_TIMEOUT)):
            with mock.patch.object(LocalConnection, 'view_image', side_effect=error):
                with self.assertRaises(http):
                    self.view_image({'connection_id': 0, 'path': '/a.png'})
        with self.assertRaises(HTTP_400_BAD_REQUEST):
            self.view_image({'connection_id': 0, 'path': ''})

    def test_other_users_connection_is_404(self):
        from api.exceptions import HTTP_404_NOT_FOUND
        from api.managers import cloud_connection_manager
        with mock.patch.object(cloud_connection_manager, 'retrieve',
                               side_effect=HTTP_404_NOT_FOUND('Cloud Connection with id 7 not found')), \
                mock.patch.object(RcloneConnection, 'view_image') as view:
            with self.assertRaises(HTTP_404_NOT_FOUND):
                self.view_image({'connection_id': 7, 'path': '/bucket/a.png'})
        view.assert_not_called()
        bobs = mock.Mock(owner='bob')
        with mock.patch.object(cloud_connection_manager, 'retrieve', return_value=bobs), \
                mock.patch.object(RcloneConnection, 'view_image') as view:
            with self.assertRaises(HTTP_404_NOT_FOUND):
                self.view_image({'connection_id': 7, 'path': '/bucket/a.png'})
        view.assert_not_called()
        alices = mock.Mock(owner='alice')
        with mock.patch.object(cloud_connection_manager, 'retrieve', return_value=alices), \
                mock.patch.object(RcloneConnection, 'view_image', return_value=(GIF_HEAD, GIF)) as view:
            self.assertEqual(self.view_image({'connection_id': 7, 'path': '/bucket/a.gif'})[1], GIF)
        self.assertIs(view.call_args.kwargs['data'], alices)


if __name__ == '__main__':
    unittest.main()
