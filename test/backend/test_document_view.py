"""
Document viewer (utils/document_view.py, LocalConnection.view_document,
RcloneConnection.view_document, POST /api/system/files/view/document/): container
detection by magic number, the whole-file cap before and while reading, PDF ranges
(type check on the first bytes for every range), headers, paths, other users'
connections.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from api.utils import document_view, file_view, image_view
from api.utils.document_view import (CFB, PDF, ZIP, DocumentRequest, detect_container, document_result,
                                     parse_document_request, parse_max_bytes, range_result)
from api.utils.local_connection import LocalConnection
from api.utils.rclone_connection import RcloneConnection


PDF_DATA = b'%PDF-1.7\n' + b'1 0 obj\n<< /Type /Catalog >>\nendobj\n' * 200 + b'%%EOF\n'
ZIP_DATA = b'PK\x03\x04' + b'\x00' * 60
CFB_DATA = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 60
CAP = 4096


class TestDetect(unittest.TestCase):

    def test_magic_numbers(self):
        self.assertEqual(detect_container(PDF_DATA), PDF)
        self.assertEqual(detect_container(b'\xef\xbb\xbfjunk before the header %PDF-1.4'), PDF) # within 1 KiB
        self.assertEqual(detect_container(ZIP_DATA), ZIP)
        self.assertEqual(detect_container(CFB_DATA), CFB)

    def test_everything_else(self):
        for data in (b'', b'hello\n', b'x' * 1024 + b'%PDF-1.4', b'PK\x05\x06' + b'\x00' * 18, b'\x89PNG\r\n\x1a\n',
                     b'<html><script>alert(1)</script></html>', b'{\\rtf1'):
            self.assertIsNone(detect_container(data), data[:20])

    def test_document_result(self):
        self.assertEqual(document_result('a.docx', ZIP_DATA, len(ZIP_DATA), CAP), (ZIP_DATA, ZIP))
        self.assertEqual(document_result('a.xls', CFB_DATA, None, CAP), (CFB_DATA, CFB))
        with self.assertRaisesRegex(document_view.NotADocumentError, "'a.docx' is not a PDF, Office"):
            document_result('a.docx', b'hello', 5, CAP)
        with self.assertRaisesRegex(image_view.TooLargeError, 'the viewer shows documents up to 4 KiB'):
            document_result('a.docx', ZIP_DATA + b'\x00' * CAP, 100, CAP) # grew after the stat

    def test_range_result(self):
        self.assertEqual(range_result('a.pdf', PDF_DATA[:1024], b'abc'), (b'abc', PDF))
        with self.assertRaisesRegex(document_view.NotADocumentError, 'only PDFs are read in ranges'):
            range_result('a.docx', ZIP_DATA, b'abc')
        with self.assertRaises(document_view.NotADocumentError):
            range_result('a.pdf', b'hello', b'abc')


class TestRequest(unittest.TestCase):

    def test_whole_file_and_range(self):
        whole = parse_document_request({'connection_id': 0, 'path': '/a'})
        self.assertFalse(whole.is_range)
        ranged = parse_document_request({'offset': 0, 'length': 65536})
        self.assertTrue(ranged.is_range)
        self.assertEqual((ranged.offset, ranged.length), (0, 65536))
        self.assertEqual(parse_document_request({'offset': 10 ** 12, 'length': 4 * 1024 * 1024}).offset, 10 ** 12)

    def test_invalid(self):
        for data in ({'offset': 0}, {'length': 10}, {'offset': -1, 'length': 10}, {'offset': 0, 'length': 0},
                     {'offset': 0, 'length': 4 * 1024 * 1024 + 1}, {'offset': True, 'length': 10},
                     {'offset': 0, 'length': '10'}, {'offset': 1.5, 'length': 10}, [], None):
            with self.assertRaises(file_view.ViewError, msg=data):
                parse_document_request(data)


class TestMaxBytes(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(parse_max_bytes(None), 50 * 1024 * 1024)
        self.assertEqual(parse_max_bytes(''), 50 * 1024 * 1024)
        self.assertEqual(parse_max_bytes('8M'), 8 * 1024 * 1024)
        self.assertEqual(parse_max_bytes('512MiB'), 512 * 1024 * 1024)
        for bad in ('1G', '1000', 'lots', '-5M', '5 T'):
            with self.assertRaisesRegex(document_view.DocumentConfigError, 'MOTUZ_VIEW_DOCUMENT_MAX_BYTES'):
                parse_max_bytes(bad)

    def test_environment(self):
        self.assertEqual(document_view.max_bytes({'MOTUZ_VIEW_DOCUMENT_MAX_BYTES': '2M'}), 2 * 1024 * 1024)
        self.assertEqual(document_view.max_bytes({}), document_view.DEFAULT_MAX_BYTES)

    def test_image_messages_unchanged(self):
        with self.assertRaisesRegex(image_view.ImageConfigError, r'e\.g\. 26214400 or 25M'):
            image_view.parse_max_bytes('lots')
        with self.assertRaisesRegex(image_view.ImageConfigError, r'\(256M\)'):
            image_view.parse_max_bytes('1G')


class TestHeaders(unittest.TestCase):

    def test_headers(self):
        headers = document_view.response_headers('/home/alice/Report "Q3".pdf', PDF, 123456, 65536)
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Content-Security-Policy'], "default-src 'none'; sandbox")
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertTrue(headers['Content-Disposition'].startswith('attachment; filename="Report _Q3_.pdf"; '))
        self.assertEqual(headers['X-Motuz-Document-Type'], 'pdf')
        self.assertEqual(headers['X-Motuz-File-Size'], '123456')
        self.assertEqual(headers['X-Motuz-Range-Start'], '65536')
        self.assertEqual(document_view.response_headers('/a.docx', ZIP, 1, 0)['Content-Type'], 'application/zip')
        self.assertEqual(document_view.response_headers('/a.xls', CFB, 1, 0)['Content-Type'], 'application/x-cfb')
        with self.assertRaises(ValueError):
            document_view.response_headers('/a.html', 'html', 1, 0)


def _without_sudo(test, calls=None):
    """Runs the reader as the current user: drops `sudo -n -u <user> --`"""
    real = file_view.run_limited

    def run(command, timeout, env=None):
        test.assertEqual(command[:5], ['sudo', '-n', '-u', 'alice', '--'])
        code, stdout, stderr = real(command[5:], timeout, env)
        if calls is not None:
            calls.append((command, stdout))
        return code, stdout, stderr
    return mock.patch('api.utils.local_connection.file_view.run_limited', side_effect=run)


class Owner:
    owner = 'alice'


class TestLocalDocument(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def view(self, path, request=None, max_bytes=CAP, calls=None):
        with _without_sudo(self, calls):
            return LocalConnection().view_document(Owner(), path, max_bytes, request or DocumentRequest())

    def test_whole_files(self):
        self.assertEqual(self.view(self.write('a.docx', ZIP_DATA)), (ZIP_DATA, ZIP, len(ZIP_DATA), 0))
        self.assertEqual(self.view(self.write('old.xls', CFB_DATA)), (CFB_DATA, CFB, len(CFB_DATA), 0))
        self.assertEqual(self.view(self.write('noext', PDF_DATA[:2000])), (PDF_DATA[:2000], PDF, 2000, 0))
        with self.assertRaisesRegex(document_view.NotADocumentError, "'fake.docx' is not a PDF"):
            self.view(self.write('fake.docx', b'<html><script>alert(1)</script></html>'))

    def test_too_large_is_refused_before_reading(self):
        path = self.write('big.xlsx', ZIP_DATA + b'\x00' * CAP)
        calls = []
        with self.assertRaisesRegex(image_view.TooLargeError, "'big.xlsx' is 4 KiB; the viewer shows documents up to 4 KiB"):
            self.view(path, calls=calls)
        self.assertEqual(calls[0][1], b'{"size": 4160, "error": "large"}') # nothing of the file was read

    def test_ranges(self):
        path = self.write('doc.pdf', PDF_DATA)
        calls = []
        content, container, size, start = self.view(path, DocumentRequest(100, 50), calls=calls)
        self.assertEqual((content, container, size, start), (PDF_DATA[100:150], PDF, len(PDF_DATA), 100))
        self.assertEqual(len(calls), 1) # the type check and the range in one process
        self.assertEqual(calls[0][0][-5:], ['1024', '100', '50', '-1', path]) # no size limit for ranges
        # The end of the file: shorter than asked for
        tail = self.view(path, DocumentRequest(len(PDF_DATA) - 10, 4096))
        self.assertEqual(tail[0], PDF_DATA[-10:])
        with self.assertRaisesRegex(file_view.ViewError, 'beyond the end'):
            self.view(path, DocumentRequest(len(PDF_DATA), 10))
        # A PDF larger than the whole-file cap is still read in ranges
        self.assertGreater(len(PDF_DATA), CAP)

    def test_ranges_only_for_pdfs(self):
        with self.assertRaisesRegex(document_view.NotADocumentError, 'only PDFs'):
            self.view(self.write('a.docx', ZIP_DATA), DocumentRequest(10, 10))
        with self.assertRaises(document_view.NotADocumentError):
            self.view(self.write('a.txt', b'hello world\n' * 100), DocumentRequest(10, 10))

    def test_paths_and_files(self):
        for relative in ('a.pdf', '-la', '', '--help', '../a.pdf'):
            with self.assertRaisesRegex(file_view.ViewError, 'must be absolute'):
                self.view(relative)
        with self.assertRaises(file_view.NotFoundError):
            self.view(os.path.join(self.dir, 'missing.pdf'), DocumentRequest(0, 10))
        with self.assertRaisesRegex(file_view.ViewError, 'is a folder'):
            self.view(self.dir)
        os.mkfifo(os.path.join(self.dir, 'fifo.pdf'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view(os.path.join(self.dir, 'fifo.pdf'), DocumentRequest(0, 10))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view('/dev/zero')

    @unittest.skipIf(os.geteuid() == 0, 'root can read everything')
    def test_permission_denied(self):
        path = self.write('secret.pdf', PDF_DATA)
        os.chmod(path, 0)
        with self.assertRaises(file_view.ForbiddenError):
            self.view(path, DocumentRequest(0, 10))


class TestCloudDocument(unittest.TestCase):

    def data(self):
        conn = mock.Mock()
        conn.owner = 'alice'
        conn.type = 'webdav'
        conn.webdav_url = 'http://127.0.0.1:9/'
        conn.webdav_user = None
        conn.webdav_pass = None
        return conn

    def run_view(self, outputs, request=None, path='/dir/report.pdf'):
        calls = []

        def run(command, timeout, env=None):
            calls.append((command, env))
            # the range and the type check may run at the same time: answer by command
            if command[-4:-1] == ['cat', '--count', '1024'] and 'head' in outputs:
                return outputs['head']
            return outputs['queue'].pop(0)

        with mock.patch('api.utils.rclone_connection.file_view.run_limited', side_effect=run):
            try:
                return RcloneConnection().view_document(self.data(), path, CAP, request or DocumentRequest()), calls
            except Exception as e:
                return e, calls

    def test_whole_file(self):
        result, calls = self.run_view({'queue': [(0, b'{"Size":%d,"IsDir":false}' % len(ZIP_DATA), b''), (0, ZIP_DATA, b'')]},
                                      path='/dir/a.docx')
        self.assertEqual(result, (ZIP_DATA, ZIP, len(ZIP_DATA), 0))
        stat, cat = calls[0][0], calls[1][0]
        self.assertEqual(stat[:4], ['sudo', '-E', '-u', 'alice'])
        self.assertEqual(stat[-3:], ['lsjson', '--stat', 'current:/dir/a.docx'])
        self.assertEqual(cat[-4:], ['cat', '--count', str(CAP + 1), 'current:/dir/a.docx'])
        self.assertFalse(any(key.startswith('MOTUZ_') for key in calls[1][1]))

    def test_too_large_is_never_catted(self):
        result, calls = self.run_view({'queue': [(0, b'{"Size":5000000,"IsDir":false}', b'')]}, path='/a.xlsx')
        self.assertIsInstance(result, image_view.TooLargeError)
        self.assertIn('documents up to', str(result))
        self.assertEqual(len(calls), 1)

    def test_range_from_the_start(self):
        size = len(PDF_DATA)
        result, calls = self.run_view({'queue': [(0, b'{"Size":%d,"IsDir":false}' % size, b''), (0, PDF_DATA[:2048], b'')]},
                                      DocumentRequest(0, 2048))
        self.assertEqual(result, (PDF_DATA[:2048], PDF, size, 0))
        self.assertEqual(len(calls), 2) # the range holds the first bytes: no second cat
        self.assertEqual(calls[1][0][-6:], ['cat', '--offset', '0', '--count', '2048', 'current:/dir/report.pdf'])

    def test_range_further_in(self):
        size = len(PDF_DATA)
        result, calls = self.run_view({'queue': [(0, b'{"Size":%d,"IsDir":false}' % size, b''), (0, PDF_DATA[3000:3100], b'')],
                                       'head': (0, PDF_DATA[:1024], b'')},
                                      DocumentRequest(3000, 100))
        self.assertEqual(result, (PDF_DATA[3000:3100], PDF, size, 3000))
        commands = sorted(call[0][-6:] for call in calls[1:])
        self.assertIn(['cat', '--offset', '3000', '--count', '100', 'current:/dir/report.pdf'], commands)
        self.assertIn(['--config=/dev/null', 'cat', '--count', '1024', 'current:/dir/report.pdf'][-4:],
                      [c[-4:] for c in commands])
        # The count is clamped to the end of the file
        result, calls = self.run_view({'queue': [(0, b'{"Size":%d,"IsDir":false}' % size, b''), (0, PDF_DATA[-5:], b'')],
                                       'head': (0, PDF_DATA[:1024], b'')},
                                      DocumentRequest(size - 5, 4096))
        self.assertEqual(result[0], PDF_DATA[-5:])
        self.assertTrue(any(c[0][-6:-1] == ['cat', '--offset', str(size - 5), '--count', '5'] for c in calls))

    def test_range_refused(self):
        size = len(PDF_DATA)
        # Not a PDF: a ZIP read in ranges
        result, _ = self.run_view({'queue': [(0, b'{"Size":%d,"IsDir":false}' % size, b''), (0, b'abc', b'')],
                                   'head': (0, ZIP_DATA, b'')}, DocumentRequest(100, 3))
        self.assertIsInstance(result, document_view.NotADocumentError)
        # Past the end: only the stat
        result, calls = self.run_view({'queue': [(0, b'{"Size":10,"IsDir":false}', b'')]}, DocumentRequest(10, 3))
        self.assertRegex(str(result), 'beyond the end')
        self.assertEqual(len(calls), 1)
        # A folder, or a missing object in bucket storage
        result, calls = self.run_view({'queue': [(0, b'{"Size":0,"IsDir":true}', b'')]}, DocumentRequest(0, 3))
        self.assertRegex(str(result), 'is a folder or does not exist')
        self.assertEqual(len(calls), 1)
        # Unknown size: no ranges
        result, _ = self.run_view({'queue': [(0, b'{"Size":-1,"IsDir":false}', b'')]}, DocumentRequest(0, 3))
        self.assertRegex(str(result), 'size .* is unknown')
        result, _ = self.run_view({'queue': [(3, b'', b'ERROR : object not found')]})
        self.assertIsInstance(result, file_view.NotFoundError)


class TestEndpoint(unittest.TestCase):

    def setUp(self):
        from api import create_app
        self.app = create_app('test')
        self.client = self.app.test_client()

    def test_login_required(self):
        response = self.client.post('/api/system/files/view/document/', json={'connection_id': 0, 'path': '/a.pdf'})
        self.assertEqual(response.status_code, 401)

    def test_headers(self):
        from api.managers import system_manager
        with mock.patch.object(system_manager, 'view_document', return_value=(b'%PDF-x', PDF, 999, 0, '/home/alice/a.pdf')):
            response = self.client.post('/api/system/files/view/document/',
                                        json={'connection_id': 0, 'path': '/home/alice/a.pdf', 'offset': 0, 'length': 6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'%PDF-x')
        self.assertEqual(response.headers['Content-Type'], 'application/pdf')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Content-Security-Policy'], "default-src 'none'; sandbox")
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Motuz-File-Size'], '999')
        self.assertEqual(response.headers['X-Motuz-Range-Start'], '0')
        self.assertEqual(response.headers['X-Motuz-Document-Type'], 'pdf')

    def test_errors_are_json(self):
        from api.exceptions import HTTP_413_PAYLOAD_TOO_LARGE, HTTP_415_UNSUPPORTED_MEDIA_TYPE
        from api.managers import system_manager
        for error, status in ((HTTP_415_UNSUPPORTED_MEDIA_TYPE("'x.docx' is not a PDF, Office"), 415),
                              (HTTP_413_PAYLOAD_TOO_LARGE("'x.xlsx' is 60.0 MiB"), 413)):
            with mock.patch.object(system_manager, 'view_document', side_effect=error):
                response = self.client.post('/api/system/files/view/document/', json={'connection_id': 0, 'path': '/x'})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers['Content-Type'], 'application/json')
            self.assertIn('x.', response.get_json()['message'])

    def test_validation(self):
        for body in ({'path': '/a.pdf'}, {'connection_id': 0}, {'connection_id': 'x', 'path': '/a.pdf'},
                     {'connection_id': 0, 'path': 5}, {'connection_id': 0, 'path': '/a.pdf', 'offset': 'x', 'length': 1}):
            self.assertEqual(self.client.post('/api/system/files/view/document/', json=body).status_code, 400, body)


class TestManager(unittest.TestCase):
    """view_document as alice: validation, the cap from the config, error mapping, other users' connections"""

    def setUp(self):
        from api import create_app
        from api.managers import system_manager
        self.app = create_app('test')
        self.app.config['VIEW_DOCUMENT_MAX_BYTES'] = 54321
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.user = mock.patch.object(system_manager, 'get_logged_in_user', return_value='alice')
        self.user.start()
        self.view_document = system_manager.view_document.__wrapped__

    def tearDown(self):
        self.user.stop()
        self.ctx.pop()

    def test_local(self):
        with mock.patch.object(LocalConnection, 'view_document', return_value=(ZIP_DATA, ZIP, 64, 0)) as view:
            self.assertEqual(self.view_document({'connection_id': 0, 'path': '/home/alice/a.docx'}),
                             (ZIP_DATA, ZIP, 64, 0, '/home/alice/a.docx'))
        self.assertEqual(view.call_args.kwargs['max_bytes'], 54321)
        self.assertEqual(view.call_args.kwargs['data'].owner, 'alice')
        self.assertFalse(view.call_args.kwargs['request'].is_range)
        with mock.patch.object(LocalConnection, 'view_document', return_value=(b'x', PDF, 64, 5)) as view:
            self.view_document({'connection_id': 0, 'path': '/home/alice/a.pdf', 'offset': 5, 'length': 1})
        self.assertEqual((view.call_args.kwargs['request'].offset, view.call_args.kwargs['request'].length), (5, 1))

    def test_errors(self):
        from api.exceptions import (HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND,
                                    HTTP_413_PAYLOAD_TOO_LARGE, HTTP_415_UNSUPPORTED_MEDIA_TYPE, HTTP_504_GATEWAY_TIMEOUT)
        for error, http in ((image_view.TooLargeError('big'), HTTP_413_PAYLOAD_TOO_LARGE),
                            (document_view.NotADocumentError('html'), HTTP_415_UNSUPPORTED_MEDIA_TYPE),
                            (file_view.ForbiddenError('no'), HTTP_403_FORBIDDEN),
                            (file_view.NotFoundError('gone'), HTTP_404_NOT_FOUND),
                            (file_view.ViewError('folder'), HTTP_400_BAD_REQUEST),
                            (file_view.ViewTimeoutError('slow'), HTTP_504_GATEWAY_TIMEOUT)):
            with mock.patch.object(LocalConnection, 'view_document', side_effect=error):
                with self.assertRaises(http):
                    self.view_document({'connection_id': 0, 'path': '/a.pdf'})
        with mock.patch.object(LocalConnection, 'view_document') as view:
            for body in ({'connection_id': 0, 'path': ''}, {'connection_id': 0, 'path': '/a.pdf', 'offset': 1},
                         {'connection_id': 0, 'path': '/a.pdf', 'offset': 0, 'length': 5 * 1024 * 1024}):
                with self.assertRaises(HTTP_400_BAD_REQUEST):
                    self.view_document(body)
        view.assert_not_called()

    def test_other_users_connection_is_404(self):
        from api.exceptions import HTTP_404_NOT_FOUND
        from api.managers import cloud_connection_manager
        with mock.patch.object(cloud_connection_manager, 'retrieve',
                               side_effect=HTTP_404_NOT_FOUND('Cloud Connection with id 7 not found')), \
                mock.patch.object(RcloneConnection, 'view_document') as view:
            with self.assertRaises(HTTP_404_NOT_FOUND):
                self.view_document({'connection_id': 7, 'path': '/bucket/a.pdf', 'offset': 0, 'length': 10})
        view.assert_not_called()
        bobs = mock.Mock(owner='bob')
        with mock.patch.object(cloud_connection_manager, 'retrieve', return_value=bobs), \
                mock.patch.object(RcloneConnection, 'view_document') as view:
            with self.assertRaises(HTTP_404_NOT_FOUND):
                self.view_document({'connection_id': 7, 'path': '/bucket/a.docx'})
        view.assert_not_called()
        alices = mock.Mock(owner='alice')
        with mock.patch.object(cloud_connection_manager, 'retrieve', return_value=alices), \
                mock.patch.object(RcloneConnection, 'view_document', return_value=(ZIP_DATA, ZIP, 64, 0)) as view:
            self.assertEqual(self.view_document({'connection_id': 7, 'path': '/bucket/a.docx'})[1], ZIP)
        self.assertIs(view.call_args.kwargs['data'], alices)


if __name__ == '__main__':
    unittest.main()
