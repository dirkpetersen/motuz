import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from api.utils import file_view
from api.utils.file_view import (CHUNK_BYTES, HEAD_CHECK_BYTES, MAX_VIEW_BYTES, ChunkRequest, NotTextError,
                                 align_backward, align_complete_lines, align_forward, chunk_result, decode_chunk, decode_text,
                                 parse_chunk_request, view_result)
from api.utils.local_connection import LocalConnection
from api.utils.rclone_connection import RcloneConnection


class TestDecodeText(unittest.TestCase):

    def test_utf8(self):
        self.assertEqual(decode_text('hello\nwörld ✓\n'.encode(), False), ('hello\nwörld ✓\n', 'utf-8'))
        self.assertEqual(decode_text(b'', False), ('', 'utf-8'))
        self.assertEqual(decode_text(b'\xef\xbb\xbfbom', False), ('bom', 'utf-8'))
        self.assertEqual(decode_text(b'tab\tcr\r\nesc\x1b[0m', False)[1], 'utf-8')

    def test_binary(self):
        for data in (b'\x00', b'PK\x03\x04\x00\x00', b'text then \x00 nul', bytes(range(256)) * 4,
                     b'\x89PNG\r\n\x1a\n' + bytes(range(128, 256)) * 10, b'\x01\x02\x03\x04\x05abc'):
            with self.assertRaises(NotTextError, msg=data[:20]):
                decode_text(data, False)

    def test_mostly_valid_utf8_is_shown_with_replacements(self):
        data = ('x' * 500).encode() + b'\xff' + ('y' * 500).encode()
        content, encoding = decode_text(data, False)
        self.assertEqual(content, 'x' * 500 + '�' + 'y' * 500)
        self.assertEqual(encoding, 'utf-8 (invalid bytes replaced)')

    def test_latin1_is_refused(self):
        with self.assertRaises(NotTextError):
            decode_text('Größe über Maß'.encode('latin-1') * 3, False)

    def test_genuine_replacement_characters_are_not_invalid(self):
        data = ('�' * 50).encode()
        self.assertEqual(decode_text(data, False), ('�' * 50, 'utf-8'))

    def test_truncated_in_the_middle_of_a_character(self):
        data = ('a' * 10 + 'é').encode()[:-1] # cut after the first byte of é
        self.assertEqual(decode_text(data, True), ('a' * 10, 'utf-8'))
        # ... but not at the end of a complete file
        self.assertEqual(decode_text(data, False)[1], 'utf-8 (invalid bytes replaced)')


class TestViewResult(unittest.TestCase):

    def test_small(self):
        self.assertEqual(view_result('/a/b.txt', b'hi\n', 3),
                         {'path': '/a/b.txt', 'content': 'hi\n', 'truncated': False, 'size': 3, 'encoding': 'utf-8'})

    def test_truncated(self):
        result = view_result('/big.log', b'x' * (MAX_VIEW_BYTES + 1), 5 * MAX_VIEW_BYTES)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['content']), MAX_VIEW_BYTES)
        self.assertEqual(result['size'], 5 * MAX_VIEW_BYTES)

    def test_exactly_the_cap(self):
        result = view_result('/cap', b'x' * MAX_VIEW_BYTES, MAX_VIEW_BYTES)
        self.assertFalse(result['truncated'])


def simulate(data, request):
    """chunk_result for `request` on a file with contents `data`, as the readers do it"""
    start, count = request.read_range()
    if start < 0:
        start = max(0, len(data) + start)
    return chunk_result('/f', request, len(data), data[:HEAD_CHECK_BYTES], start, data[start:start + count])


def read_forward(data, length):
    chunks, offset = [], 0
    while True:
        chunk = simulate(data, ChunkRequest(offset=offset, length=length))
        chunks.append(chunk)
        if chunk['eof']:
            return chunks
        offset = chunk['end']


def read_backward(data, length):
    chunks = [simulate(data, ChunkRequest(from_end=True, length=length))]
    while not chunks[0]['bof']:
        chunks.insert(0, simulate(data, ChunkRequest(before=chunks[0]['offset'], length=length)))
    return chunks


class TestChunkAlignment(unittest.TestCase):

    def assert_covers(self, data, chunks, length=300):
        """The chunks are contiguous, at most `length` bytes each, and their contents are the file"""
        self.assertEqual(chunks[0]['offset'], 0)
        self.assertEqual(chunks[-1]['end'], len(data))
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a['end'], b['offset'])
        self.assertEqual(''.join(c['content'] for c in chunks), data.decode('utf-8'))
        for c in chunks:
            self.assertEqual(c['content'].encode('utf-8'), data[c['offset']:c['end']])
            self.assertLessEqual(c['end'] - c['offset'], length)

    def test_forward_chunks_end_after_a_newline(self):
        data = b''.join(b'line %06d of the file\n' % i for i in range(100))
        chunks = read_forward(data, 300)
        self.assert_covers(data, chunks)
        self.assertTrue(all(c['content'].endswith('\n') for c in chunks))
        self.assertTrue(chunks[0]['bof'] and not chunks[0]['eof'] and chunks[-1]['eof'] and not chunks[-1]['bof'])
        self.assertTrue(all(len(c['content']) > 250 for c in chunks[:-1]))

    def test_backward_chunks_start_after_a_newline(self):
        data = b''.join(b'line %06d of the file\n' % i for i in range(100))
        chunks = read_backward(data, 300)
        self.assert_covers(data, chunks)
        self.assertTrue(all(c['content'].startswith('line ') for c in chunks))
        tail = chunks[-1]
        self.assertTrue(tail['eof'] and not tail['bof'] and tail['content'].endswith('line 000099 of the file\n'))

    def test_a_chunk_that_starts_right_after_a_newline_keeps_its_first_line(self):
        data = b'a' * 99 + b'\n' + b'b' * 199 + b'\n' # exactly 300 bytes
        tail = simulate(data, ChunkRequest(from_end=True, length=300))
        self.assertEqual(tail['offset'], 0)
        data = b'x\n' + data # the tail's 300 bytes start right after a newline
        tail = simulate(data, ChunkRequest(from_end=True, length=300))
        self.assertEqual((tail['offset'], tail['content'][:3]), (2, 'aaa'))

    def test_line_longer_than_the_chunk_is_split(self):
        data = b'short\n' + b'L' * 1000 + b'\nend\n'
        chunks = read_forward(data, 300)
        self.assert_covers(data, chunks)
        self.assertEqual(chunks[0]['content'], 'short\n')
        self.assertEqual(len(chunks[1]['content']), 300)
        chunks = read_backward(data, 300)
        self.assert_covers(data, chunks)

    def test_multibyte_character_at_a_chunk_boundary(self):
        for prefix in range(4): # move the characters across the boundary
            data = b'x' * prefix + 'ü€😀'.encode('utf-8') * 200 # one long line
            for chunks in (read_forward(data, 256), read_backward(data, 256)):
                self.assert_covers(data, chunks, 256)
                self.assertFalse(any('�' in c['content'] for c in chunks))

    def test_tail_read_at_the_beginning_of_the_file(self):
        data = b'one\ntwo\n'
        tail = simulate(data, ChunkRequest(from_end=True, length=CHUNK_BYTES))
        self.assertEqual(tail, {'path': '/f', 'content': 'one\ntwo\n', 'offset': 0, 'end': 8, 'size': 8,
                                'bof': True, 'eof': True, 'encoding': 'utf-8'})

    def test_empty_file(self):
        for request in (ChunkRequest(offset=0), ChunkRequest(from_end=True), ChunkRequest(before=0)):
            self.assertEqual(simulate(b'', request), {'path': '/f', 'content': '', 'offset': 0, 'end': 0, 'size': 0,
                                                      'bof': True, 'eof': True, 'encoding': 'utf-8'})

    def test_no_trailing_newline(self):
        data = b''.join(b'row %04d\n' % i for i in range(100)) + b'last line without newline'
        chunks = read_forward(data, 256)
        self.assert_covers(data, chunks, 256)
        self.assertTrue(chunks[-1]['content'].endswith('\nlast line without newline'))
        chunks = read_backward(data, 256)
        self.assert_covers(data, chunks, 256)
        self.assertTrue(chunks[-1]['content'].endswith('\nlast line without newline'))

    def test_offset_at_the_end_and_beyond(self):
        chunk = simulate(b'abc\n', ChunkRequest(offset=4))
        self.assertEqual((chunk['content'], chunk['offset'], chunk['end'], chunk['eof'], chunk['bof']), ('', 4, 4, True, False))
        for request in (ChunkRequest(offset=5), ChunkRequest(before=5)):
            with self.assertRaisesRegex(file_view.ViewError, 'beyond the end'):
                simulate(b'abc\n', request)

    def test_follow_returns_complete_lines_only(self):
        log = b'one\ntwo\n'
        first = simulate(log, ChunkRequest(offset=0, follow=True))
        self.assertEqual((first['content'], first['end'], first['eof']), ('one\ntwo\n', 8, True))
        # nothing new: empty, eof, at the same offset
        same = simulate(log, ChunkRequest(offset=8, follow=True))
        self.assertEqual((same['content'], same['offset'], same['end'], same['size'], same['eof']), ('', 8, 8, 8, True))
        # an incomplete line is withheld (end < size, still eof) until its newline arrives
        log += b'three\nfou'
        grown = simulate(log, ChunkRequest(offset=8, follow=True))
        self.assertEqual((grown['content'], grown['offset'], grown['end'], grown['size'], grown['eof']),
                         ('three\n', 8, 14, 17, True))
        partial = simulate(log, ChunkRequest(offset=14, follow=True))
        self.assertEqual((partial['content'], partial['end'], partial['eof']), ('', 14, True))
        log += b'r\n'
        done = simulate(log, ChunkRequest(offset=14, follow=True))
        self.assertEqual((done['content'], done['offset'], done['end'], done['eof']), ('four\n', 14, 19, True))
        # without follow, the end of the file is returned as it is
        self.assertEqual(simulate(b'one\nfou', ChunkRequest(offset=0))['content'], 'one\nfou')

    def test_follow_more_than_a_chunk_behind(self):
        data = b''.join(b'line %04d\n' % i for i in range(100)) # 1000 bytes
        chunk = simulate(data, ChunkRequest(offset=0, length=300, follow=True))
        self.assertEqual((chunk['end'], chunk['eof']), (300, False)) # the poll continues at once
        chunks = [chunk]
        while not chunks[-1]['eof']:
            chunks.append(simulate(data, ChunkRequest(offset=chunks[-1]['end'], length=300, follow=True)))
        self.assertEqual(''.join(c['content'] for c in chunks).encode(), data)
        # a line longer than a chunk is still split
        long_line = simulate(b'x' * 400, ChunkRequest(offset=0, length=300, follow=True))
        self.assertEqual((long_line['end'], long_line['eof']), (300, False))
        self.assertEqual(simulate(b'x' * 299, ChunkRequest(offset=0, length=300, follow=True))['end'], 0)

    def test_follow_beyond_the_end_reports_the_new_size(self):
        chunk = simulate(b'new\n', ChunkRequest(offset=5000, follow=True))
        self.assertEqual((chunk['content'], chunk['offset'], chunk['end'], chunk['size'], chunk['eof'], chunk['bof']),
                         ('', 4, 4, 4, True, False))
        chunk = simulate(b'', ChunkRequest(offset=10, follow=True))
        self.assertEqual((chunk['size'], chunk['bof'], chunk['eof']), (0, True, True))

    def test_nothing_is_checked_or_read_at_the_end(self):
        # A forward read at the end needs neither the first bytes nor data
        data = b'\x89PNG\x00\x00' + b'text\n' * 100
        chunk = chunk_result('/f', ChunkRequest(offset=len(data)), len(data), b'', len(data), b'')
        self.assertEqual((chunk['content'], chunk['eof'], chunk['size']), ('', True, len(data)))

    def test_align_complete_lines(self):
        self.assertEqual(align_complete_lines(b'ab\ncd', 300), 3)
        self.assertEqual(align_complete_lines(b'abcd', 300), 0)
        self.assertEqual(align_complete_lines(b'', 300), 0)
        self.assertEqual(align_complete_lines(b'x' * 300, 300), 300)
        self.assertEqual(align_complete_lines(('x\u20ac' * 75).encode(), 300), 300)
        self.assertEqual(align_complete_lines(('x\u20ac' * 75).encode()[:299], 299), 297) # never inside a character

    def test_binary_is_refused_also_from_the_end(self):
        data = b'\x89PNG\r\n\x1a\n\x00\x00' + b'text\n' * 100000
        for request in (ChunkRequest(from_end=True), ChunkRequest(offset=len(data) // 2), ChunkRequest(before=len(data))):
            with self.assertRaises(NotTextError):
                simulate(data, request)

    def test_later_chunks_replace_invalid_bytes(self):
        data = b'text\n' * 10000 + b'bad \xff\xfe byte\n'
        tail = simulate(data, ChunkRequest(from_end=True, length=256))
        self.assertTrue(tail['content'].endswith('bad �� byte\n'))
        self.assertEqual(tail['encoding'], 'utf-8 (invalid bytes replaced)')

    def test_bom_is_dropped_at_the_start_only(self):
        chunk = simulate(b'\xef\xbb\xbfhello\n', ChunkRequest(offset=0))
        self.assertEqual((chunk['content'], chunk['offset'], chunk['end']), ('hello\n', 0, 9))
        self.assertEqual(decode_chunk(b'\xef\xbb\xbfx', False), ('﻿x', 'utf-8'))

    def test_unknown_size(self):
        request = ChunkRequest(offset=0, length=256)
        chunk = chunk_result('/f', request, None, b'abc\n', 0, b'abc\n')
        self.assertEqual((chunk['size'], chunk['eof'], chunk['end']), (4, True, 4))
        chunk = chunk_result('/f', request, None, b'a\n' * 128, 0, b'a\n' * 128)
        self.assertEqual((chunk['size'], chunk['eof']), (None, False))

    def test_align_helpers(self):
        self.assertEqual(align_forward(b'ab\ncd', False), 3)
        self.assertEqual(align_forward(b'ab\ncd', True), 5)
        self.assertEqual(align_forward('abc€'.encode()[:-1], False), 3)
        self.assertEqual(align_forward(b'\xe2\x82', False), 2) # never empty
        self.assertEqual(align_backward(b'\nabc', 9, 10), 10)
        self.assertEqual(align_backward(b'xab\ncd', 9, 10), 13)
        self.assertEqual(align_backward('€cd'.encode(), 9, 10), 12) # lo is inside €: skips the rest of it
        self.assertEqual(align_backward('x€cd'.encode(), 9, 10), 10) # lo is at €
        self.assertEqual(align_backward(b'abc\n', 9, 10), 10) # the only newline ends the chunk: split
        self.assertEqual(align_backward(b'abc', 0, 0), 0)


class TestParseChunkRequest(unittest.TestCase):

    def test_defaults(self):
        r = parse_chunk_request({'path': '/x', 'connection_id': 0})
        self.assertEqual((r.offset, r.before, r.from_end, r.length), (0, None, False, CHUNK_BYTES))
        r = parse_chunk_request({'from_end': True, 'length': 1000})
        self.assertEqual((r.offset, r.from_end, r.length, r.read_range()), (None, True, 1000, (-1001, 1001)))
        self.assertEqual(parse_chunk_request({'before': 5000, 'length': 1000}).read_range(), (3999, 1001))
        self.assertEqual(parse_chunk_request({'before': 500, 'length': 1000}).read_range(), (0, 500))
        self.assertEqual(parse_chunk_request({'offset': 7, 'length': 300}).read_range(), (7, 300))

    def test_follow(self):
        r = parse_chunk_request({'offset': 100, 'follow': True})
        self.assertEqual((r.offset, r.follow, r.skips_text_check), (100, True, True))
        self.assertFalse(parse_chunk_request({'offset': 0, 'follow': True}).skips_text_check)
        self.assertFalse(parse_chunk_request({'offset': 100}).skips_text_check)
        self.assertFalse(parse_chunk_request({'offset': 100, 'follow': None}).follow)

    def test_invalid(self):
        for data in ({'follow': 'yes'}, {'follow': 1}, {'from_end': True, 'follow': True},
                     {'before': 100, 'follow': True}, {'offset': -1}, {'offset': '5'}, {'offset': 1.5}, {'offset': True}, {'length': 0},
                     {'length': CHUNK_BYTES + 1}, {'length': 10}, {'before': -3}, {'from_end': 'yes'},
                     {'offset': 5, 'from_end': True}, {'offset': 5, 'before': 9}, {'before': 5, 'from_end': True}):
            with self.assertRaises(file_view.ViewError, msg=data):
                parse_chunk_request(data)


class TestRunLimited(unittest.TestCase):

    def test_output_and_status(self):
        self.assertEqual(file_view.run_limited([sys.executable, '-c', 'import sys; sys.stdout.write("x"); sys.exit(3)'], 10),
                         (3, b'x', b''))

    def test_timeout_kills_the_process_group(self):
        with self.assertRaises(file_view.ViewTimeoutError):
            file_view.run_limited(['sh', '-c', 'sleep 30 & sleep 30'], 0.5)


def _without_sudo(test):
    """Runs the reader as the current user: drops `sudo -n -u <user> --`"""
    real = file_view.run_limited

    def run(command, timeout, env=None):
        test.assertEqual(command[:5], ['sudo', '-n', '-u', 'alice', '--'])
        test.assertIn('-I', command) # isolated python: no user site-packages, no PYTHON* variables
        return real(command[5:], timeout, env)
    return mock.patch('api.utils.local_connection.file_view.run_limited', side_effect=run)


class Owner:
    owner = 'alice'


class TestLocalView(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def view(self, path):
        with _without_sudo(self):
            return LocalConnection().view(Owner(), path)

    def test_text(self):
        path = self.write('notes.txt', 'line 1\nlïne 2\n'.encode())
        self.assertEqual(self.view(path), {'path': path, 'content': 'line 1\nlïne 2\n', 'truncated': False,
                                           'size': 15, 'encoding': 'utf-8'})

    def test_large_file_is_truncated(self):
        path = self.write('big.log', b'0123456789\n' * 200000)
        result = self.view(path)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['size'], 2200000)
        self.assertEqual(len(result['content']), MAX_VIEW_BYTES)

    def test_binary(self):
        path = self.write('image.png', b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')
        with self.assertRaisesRegex(file_view.NotTextError, "'image.png' is not a text file"):
            self.view(path)

    def test_errors(self):
        with self.assertRaises(file_view.NotFoundError):
            self.view(os.path.join(self.dir, 'missing'))
        with self.assertRaisesRegex(file_view.ViewError, 'is a folder'):
            self.view(self.dir)
        os.mkfifo(os.path.join(self.dir, 'fifo'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view(os.path.join(self.dir, 'fifo'))
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.view('/dev/zero')
        for relative in ('notes.txt', '-la', '', '--help'):
            with self.assertRaisesRegex(file_view.ViewError, 'must be absolute'):
                self.view(relative)

    @unittest.skipIf(os.geteuid() == 0, 'root can read everything')
    def test_permission_denied(self):
        path = self.write('secret', b'secret')
        os.chmod(path, 0)
        with self.assertRaises(file_view.ForbiddenError):
            self.view(path)

    def test_symlink_is_followed_as_the_user(self):
        target = self.write('target.txt', b'target')
        os.symlink(target, os.path.join(self.dir, 'link'))
        self.assertEqual(self.view(os.path.join(self.dir, 'link'))['content'], 'target')

    def test_sudo_failure(self):
        with mock.patch('api.utils.local_connection.file_view.run_limited', return_value=(1, b'', b'sudo: a password is required')):
            with self.assertRaises(file_view.ForbiddenError):
                LocalConnection().view(Owner(), '/etc/hostname')
            with self.assertRaises(file_view.ForbiddenError):
                LocalConnection().view_chunk(Owner(), '/etc/hostname', ChunkRequest(offset=0))

    def chunk(self, path, **kwargs):
        with _without_sudo(self):
            return LocalConnection().view_chunk(Owner(), path, ChunkRequest(**kwargs))

    def test_chunks(self):
        data = b''.join(b'line %06d\n' % i for i in range(100000)) # 1.2 MB
        path = self.write('big.log', data)
        first = self.chunk(path, offset=0)
        self.assertEqual((first['offset'], first['bof'], first['eof'], first['size']), (0, True, False, len(data)))
        self.assertEqual(first['end'], CHUNK_BYTES - CHUNK_BYTES % 12)
        self.assertTrue(first['content'].startswith('line 000000\n') and first['content'].endswith('\n'))
        second = self.chunk(path, offset=first['end'])
        self.assertEqual(second['offset'], first['end'])
        self.assertTrue(second['content'].startswith('line '))
        tail = self.chunk(path, from_end=True)
        self.assertEqual((tail['end'], tail['eof'], tail['bof']), (len(data), True, False))
        self.assertTrue(tail['content'].startswith('line ') and tail['content'].endswith('line 099999\n'))
        before = self.chunk(path, before=tail['offset'], length=1000)
        self.assertEqual(before['end'], tail['offset'])
        self.assertEqual(before['content'].encode(), data[before['offset']:before['end']])

    def test_follow(self):
        path = self.write('app.log', b'one\ntwo\n')
        with _without_sudo(self), mock.patch('api.utils.local_connection.file_view.chunk_result',
                                             wraps=file_view.chunk_result) as result:
            same = LocalConnection().view_chunk(Owner(), path, ChunkRequest(offset=8, follow=True))
        self.assertEqual((same['content'], same['end'], same['size'], same['eof']), ('', 8, 8, True))
        self.assertEqual((result.call_args[0][3], result.call_args[0][5]), (b'', b'')) # nothing read at the end
        with open(path, 'ab') as f:
            f.write(b'three\nfou')
        grown = self.chunk(path, offset=8, follow=True)
        self.assertEqual((grown['content'], grown['end'], grown['size']), ('three\n', 14, 17))
        with open(path, 'ab') as f:
            f.write(b'r\n')
        self.assertEqual(self.chunk(path, offset=14, follow=True)['content'], 'four\n')
        with open(path, 'wb') as f:
            f.write(b'new\n')
        shrunk = self.chunk(path, offset=19, follow=True)
        self.assertEqual((shrunk['content'], shrunk['size'], shrunk['end']), ('', 4, 4))
        with self.assertRaisesRegex(file_view.ViewError, 'beyond the end'):
            self.chunk(path, offset=19)

    def test_chunk_errors(self):
        text = self.write('t.txt', b'abc\n')
        with self.assertRaisesRegex(file_view.ViewError, 'beyond the end'):
            self.chunk(text, offset=5)
        binary = self.write('image.png', b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'text\n' * 100000)
        with self.assertRaisesRegex(file_view.NotTextError, "'image.png' is not a text file"):
            self.chunk(binary, from_end=True)
        with self.assertRaises(file_view.NotFoundError):
            self.chunk(os.path.join(self.dir, 'missing'), offset=0)
        with self.assertRaisesRegex(file_view.ViewError, 'is a folder'):
            self.chunk(self.dir, from_end=True)
        with self.assertRaisesRegex(file_view.ViewError, 'not a regular file'):
            self.chunk('/dev/zero', from_end=True)
        for relative in ('t.txt', '-la', ''):
            with self.assertRaisesRegex(file_view.ViewError, 'must be absolute'):
                self.chunk(relative, offset=0)


class TestCloudView(unittest.TestCase):

    def data(self):
        conn = mock.Mock()
        conn.owner = 'alice'
        conn.type = 'webdav'
        conn.webdav_url = 'http://127.0.0.1:9/'
        conn.webdav_user = None
        conn.webdav_pass = None
        return conn

    def run_view(self, outputs):
        calls = []

        def run(command, timeout, env=None):
            calls.append((command, env))
            return outputs.pop(0)

        with mock.patch('api.utils.rclone_connection.file_view.run_limited', side_effect=run):
            try:
                return RcloneConnection().view(self.data(), '/dir/notes.txt'), calls
            except Exception as e:
                return e, calls

    def test_text(self):
        result, calls = self.run_view([(0, b'{"Path":"notes.txt","Size":6,"IsDir":false}', b''), (0, b'hello\n', b'')])
        self.assertEqual(result, {'path': '/dir/notes.txt', 'content': 'hello\n', 'truncated': False, 'size': 6, 'encoding': 'utf-8'})
        stat, cat = calls[0][0], calls[1][0]
        self.assertSudoAsAlice(stat, calls[0][1])
        self.assertEqual(stat[-3:], ['lsjson', '--stat', 'current:/dir/notes.txt'])
        self.assertEqual(cat[-4:], ['cat', '--count', str(MAX_VIEW_BYTES + 1), 'current:/dir/notes.txt'])
        env = calls[1][1]
        self.assertEqual(env['RCLONE_CONFIG_CURRENT_TYPE'], 'webdav')
        self.assertFalse(any(key.startswith('MOTUZ_') for key in env))

    def assertSudoAsAlice(self, command, env):
        # sudo -n --preserve-env=<exactly the variables Motuz sets, never PATH> -u alice rclone
        self.assertEqual(command[:2], ['sudo', '-n'])
        self.assertTrue(command[2].startswith('--preserve-env='), command[2])
        names = command[2][len('--preserve-env='):].split(',')
        self.assertEqual(sorted(names), sorted(key for key in env if key != 'PATH'))
        self.assertIn('RCLONE_CONFIG_CURRENT_TYPE', names)
        self.assertEqual(command[3:6], ['-u', 'alice', '/usr/local/bin/rclone'])

    def test_folder_is_never_catted(self):
        result, calls = self.run_view([(0, b'{"Path":"dir","IsDir":true}', b'')])
        self.assertIsInstance(result, file_view.ViewError)
        self.assertIn('folder or does not exist', str(result))
        self.assertEqual(len(calls), 1)
        result, calls = self.run_view([(0, b'null', b'')])
        self.assertIsInstance(result, file_view.NotFoundError)

    def test_binary_and_truncated(self):
        result, _ = self.run_view([(0, b'{"Size":9,"IsDir":false}', b''), (0, b'\x00\x01bin', b'')])
        self.assertIsInstance(result, file_view.NotTextError)
        result, _ = self.run_view([(0, b'{"Size":3000000,"IsDir":false}', b''), (0, b'a' * (MAX_VIEW_BYTES + 1), b'')])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['size'], 3000000)

    def test_not_found(self):
        result, _ = self.run_view([(3, b'', b'ERROR : error listing: object not found')])
        self.assertIsInstance(result, file_view.NotFoundError)

    def run_chunk(self, outputs, **kwargs):
        calls = []

        def run(command, timeout, env=None):
            calls.append(command)
            return outputs.pop(0)

        with mock.patch('api.utils.rclone_connection.file_view.run_limited', side_effect=run):
            try:
                return RcloneConnection().view_chunk(self.data(), '/dir/log.txt', ChunkRequest(**kwargs)), calls
            except Exception as e:
                return e, calls

    def test_chunk_from_the_start(self):
        data = b'line\n' * 100
        result, calls = self.run_chunk([(0, b'{"Size":500,"IsDir":false}', b''), (0, data, b'')], offset=0)
        self.assertEqual((result['offset'], result['end'], result['size'], result['bof'], result['eof']), (0, 500, 500, True, True))
        self.assertEqual(len(calls), 2) # the range starts at 0 and holds the first bytes: no separate text check
        self.assertEqual(calls[0][-3:], ['lsjson', '--stat', 'current:/dir/log.txt'])
        self.assertEqual(calls[1][-6:], ['cat', '--offset', '0', '--count', '500', 'current:/dir/log.txt'])
        self.assertEqual(calls[1][:2], ['sudo', '-n'])
        self.assertEqual(calls[1][3:6], ['-u', 'alice', '/usr/local/bin/rclone'])
        result, calls = self.run_chunk([(0, b'{"Size":500,"IsDir":false}', b''), (0, data[:300], b''), (0, data, b'')],
                                       offset=0, length=300)
        self.assertEqual((result['offset'], result['end'], result['bof'], result['eof']), (0, 300, True, False))
        self.assertEqual(calls[1][-6:], ['cat', '--offset', '0', '--count', '300', 'current:/dir/log.txt'])
        self.assertEqual(calls[2][-4:], ['cat', '--count', str(HEAD_CHECK_BYTES), 'current:/dir/log.txt'])

    def test_tail_chunk(self):
        data = b''.join(b'line %04d\n' % i for i in range(1000)) # 10000 bytes
        result, calls = self.run_chunk([(0, b'{"Size":10000,"IsDir":false}', b''), (0, data[10000 - 301:], b''),
                                        (0, data[:HEAD_CHECK_BYTES], b'')], from_end=True, length=300)
        self.assertEqual(calls[1][-6:], ['cat', '--offset', '9699', '--count', '301', 'current:/dir/log.txt'])
        self.assertEqual(calls[2][-4:], ['cat', '--count', str(HEAD_CHECK_BYTES), 'current:/dir/log.txt'])
        self.assertEqual((result['offset'], result['end'], result['eof'], result['bof']), (9700, 10000, True, False))
        self.assertTrue(result['content'].startswith('line 0970\n') and result['content'].endswith('line 0999\n'))
        # the previous chunk
        result, calls = self.run_chunk([(0, b'{"Size":10000,"IsDir":false}', b''), (0, data[9399:9700], b''),
                                        (0, data[:HEAD_CHECK_BYTES], b'')], before=9700, length=300)
        self.assertEqual(calls[1][-6:], ['cat', '--offset', '9399', '--count', '301', 'current:/dir/log.txt'])
        self.assertEqual((result['offset'], result['end']), (9400, 9700))

    def test_chunk_errors(self):
        result, calls = self.run_chunk([(0, b'{"Size":10,"IsDir":false}', b'')], offset=11)
        self.assertIn('beyond the end', str(result))
        self.assertEqual(len(calls), 1)
        result, calls = self.run_chunk([(0, b'{"Path":"dir","IsDir":true}', b'')], from_end=True)
        self.assertIn('folder or does not exist', str(result))
        self.assertEqual(len(calls), 1) # never catted
        result, calls = self.run_chunk([(0, b'{"Size":-1,"IsDir":false}', b'')], from_end=True)
        self.assertIn('size', str(result))
        result, _ = self.run_chunk([(0, b'{"Size":100000,"IsDir":false}', b''), (0, b'text\n' * 50, b''),
                                    (0, b'\x00\x01binary', b'')], from_end=True, length=256)
        self.assertIsInstance(result, file_view.NotTextError)

    def test_forward_read_at_the_end_is_only_a_stat(self):
        result, calls = self.run_chunk([(0, b'{"Size":500,"IsDir":false}', b'')], offset=500, follow=True)
        self.assertEqual((result['content'], result['offset'], result['end'], result['size'], result['eof']),
                         ('', 500, 500, 500, True))
        self.assertEqual(len(calls), 1) # lsjson --stat, no cat
        result, calls = self.run_chunk([(0, b'{"Size":500,"IsDir":false}', b'')], offset=500)
        self.assertEqual((result['content'], result['eof'], len(calls)), ('', True, 1))

    def test_follow_skips_the_second_text_check(self):
        result, calls = self.run_chunk([(0, b'{"Size":519,"IsDir":false}', b''), (0, b'new line\npart', b'')],
                                       offset=506, follow=True)
        self.assertEqual((result['content'], result['offset'], result['end'], result['size'], result['eof']),
                         ('new line\n', 506, 515, 519, True))
        self.assertEqual(len(calls), 2) # lsjson --stat and the range, no cat --count 8192
        self.assertEqual(calls[1][-6:], ['cat', '--offset', '506', '--count', '13', 'current:/dir/log.txt'])

    def test_follow_after_the_file_shrank(self):
        result, calls = self.run_chunk([(0, b'{"Size":40,"IsDir":false}', b'')], offset=500, follow=True)
        self.assertEqual((result['content'], result['end'], result['size'], result['eof']), ('', 40, 40, True))
        self.assertEqual(len(calls), 1)
        result, calls = self.run_chunk([(0, b'{"Size":40,"IsDir":false}', b'')], offset=500)
        self.assertIn('beyond the end', str(result))
