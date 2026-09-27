"""
Read-only viewer for text files: POST /api/system/files/view/ (the first 1 MiB) and
POST /api/system/files/view/chunk/ (the pager: one chunk of lines at a time).

Local files are read by LocalConnection.view / view_chunk, cloud files by
RcloneConnection.view / view_chunk, both as the logged-in user. This module holds the
parts they share: the size caps, parsing a chunk request, aligning chunks to lines,
text detection and decoding, the errors, and running a command with a timeout.
File contents are never logged.
"""
import codecs
import logging
import os
import signal
import subprocess


MAX_VIEW_BYTES = 1024 * 1024 # only the first 1 MiB is shown (/files/view/)
# Pager chunks (/files/view/chunk/): 256 KiB is a few thousand lines of a typical log,
# several screens, so one request per page-down burst; the JSON stays small enough to
# arrive, decode and render in well under a second even through rclone from a cloud,
# and a 16-chunk window in the browser (4 MiB of text) keeps the DOM responsive.
CHUNK_BYTES = 256 * 1024 # default and maximum `length`
MIN_CHUNK_BYTES = 256
# Whether a file is text is decided on its first bytes, also for chunks further in
# (a binary file cannot be viewed by jumping to its end)
HEAD_CHECK_BYTES = 8192
LOCAL_TIMEOUT = 30 # seconds
CLOUD_TIMEOUT = 120 # seconds, per rclone call

# Share of bytes that may be invalid UTF-8 (shown as U+FFFD) in a text file
MAX_INVALID_RATIO = 0.01
# Share of characters that may be control characters other than \t \n \r \f \v \b ESC
MAX_CONTROL_RATIO = 0.05
_ALLOWED_CONTROLS = frozenset('\t\n\r\f\v\b\x1b')
_UTF8_BOM = codecs.BOM_UTF8
_REPLACEMENT_UTF8 = '�'.encode('utf-8')


class ViewError(Exception):
    """A file that cannot be shown; `status` is the HTTP status, the text says why"""
    status = 400


class NotTextError(ViewError):
    status = 415


class NotFoundError(ViewError):
    status = 404


class ForbiddenError(ViewError):
    status = 403


class ViewTimeoutError(ViewError):
    status = 504


def decode_text(data, truncated):
    """
    Decodes the (first MAX_VIEW_BYTES of a) file as UTF-8 text.
    Returns (content, encoding). Raises NotTextError for binary data: NUL bytes, more
    than MAX_INVALID_RATIO invalid UTF-8 or many control characters. Mostly valid UTF-8
    is decoded with U+FFFD for the invalid bytes. A truncated file may end in the
    middle of a character, which is dropped.
    """
    if b'\x00' in data:
        raise NotTextError('not a text file')

    encoding = 'utf-8'
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM):]

    try:
        content = codecs.getincrementaldecoder('utf-8')('strict').decode(data, final=not truncated)
    except UnicodeDecodeError:
        content = codecs.getincrementaldecoder('utf-8')('replace').decode(data, final=not truncated)
        invalid = content.count('�') - data.count(_REPLACEMENT_UTF8)
        if invalid > max(1, len(data) * MAX_INVALID_RATIO):
            raise NotTextError('not a text file')
        encoding = 'utf-8 (invalid bytes replaced)'

    if content:
        controls = sum(1 for c in content if c < ' ' and c not in _ALLOWED_CONTROLS)
        if controls > len(content) * MAX_CONTROL_RATIO:
            raise NotTextError('not a text file')

    return content, encoding


def view_result(path, data, size):
    """The API response for the first bytes `data` (up to MAX_VIEW_BYTES + 1) of a file"""
    truncated = len(data) > MAX_VIEW_BYTES or (size is not None and size > MAX_VIEW_BYTES)
    data = data[:MAX_VIEW_BYTES]
    content, encoding = decode_text(data, truncated)
    return {
        'path': path,
        'content': content,
        'truncated': truncated,
        'size': size if size is not None else len(data),
        'encoding': encoding,
    }


class ChunkRequest:
    """
    A validated pager request: a forward read of `length` bytes from `offset`, or a
    backward read of the chunk that ends at `before` (the previous chunk), or with
    `from_end` the last chunk of the file (tail).
    `follow` (forward reads only) is the viewer's live follow mode (like `tail -f`):
    it polls with `offset` at the end of what it has. An incomplete last line is
    withheld until its newline arrives, and an offset beyond the end of the file (the
    file was truncated or replaced) returns its new size instead of an error.
    """
    def __init__(self, offset=None, before=None, from_end=False, length=CHUNK_BYTES, follow=False):
        self.offset = offset
        self.before = before
        self.from_end = from_end
        self.length = length
        self.follow = follow

    @property
    def backward(self):
        return self.from_end or self.before is not None

    @property
    def skips_text_check(self):
        """
        A follow read past the start of the file: the viewer opened the file (and the
        text check passed) before it follows it, so a cloud read needs no second
        `rclone cat` of the first bytes. A file that shrank is reported without data,
        and the viewer then reloads its tail with the check.
        """
        return self.follow and not self.backward and self.offset > 0

    def read_range(self):
        """
        (start, count) of the bytes to read. For a backward read the byte before the
        chunk is read too, so that a chunk that starts right after a newline keeps its
        first line. A tail read has a negative start: that many bytes from the end.
        """
        if self.from_end:
            return -(self.length + 1), self.length + 1
        if self.before is not None:
            start = max(0, max(0, self.before - self.length) - 1)
            return start, self.before - start
        return self.offset, self.length


def _int_param(data, name, default=None, minimum=0, maximum=None):
    value = data.get(name, default)
    if value is None:
        return default
    # bool is an int in Python, but true/false is no offset
    if isinstance(value, bool) or not isinstance(value, int):
        raise ViewError("'{}' must be an integer".format(name))
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ViewError("'{}' must be at least {}".format(name, minimum))
        raise ViewError("'{}' must be between {} and {}".format(name, minimum, maximum))
    return value


def parse_chunk_request(data):
    """ChunkRequest from the request JSON; ViewError (400) for invalid parameters"""
    if not isinstance(data, dict):
        raise ViewError('Invalid request')
    from_end = data.get('from_end', False)
    if from_end is None:
        from_end = False
    if not isinstance(from_end, bool):
        raise ViewError("'from_end' must be true or false")
    offset = _int_param(data, 'offset')
    before = _int_param(data, 'before')
    length = _int_param(data, 'length', CHUNK_BYTES, MIN_CHUNK_BYTES, CHUNK_BYTES)
    follow = data.get('follow', False)
    if follow is None:
        follow = False
    if not isinstance(follow, bool):
        raise ViewError("'follow' must be true or false")
    if sum((offset is not None, before is not None, from_end)) > 1:
        raise ViewError("Use only one of 'offset', 'before' and 'from_end'")
    if follow and (before is not None or from_end):
        raise ViewError("'follow' reads forward from 'offset'")
    if offset is None and before is None and not from_end:
        offset = 0
    return ChunkRequest(offset=offset, before=before, from_end=from_end, length=length, follow=follow)


def _utf8_incomplete_tail(data):
    """Number of bytes at the end of `data` that are an incomplete UTF-8 sequence"""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte < 0x80:
            return 0 # ASCII: complete
        if byte >= 0xC0: # lead byte of a sequence of `need` bytes
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return back if need > back else 0
        # continuation byte: look further back
    return 0


def _utf8_continuation_head(data):
    """Number of UTF-8 continuation bytes (at most 3) at the start of `data`"""
    count = 0
    while count < min(3, len(data)) and 0x80 <= data[count] < 0xC0:
        count += 1
    return count


def align_forward(data, at_eof):
    """
    How many bytes of `data` (read forward) the chunk keeps: up to and including the
    last newline, all of it at the end of the file. A line longer than the chunk is
    split, but never inside a UTF-8 character. Never 0 for non-empty data.
    """
    if at_eof or not data:
        return len(data)
    newline = data.rfind(b'\n')
    if newline >= 0:
        return newline + 1
    keep = len(data) - _utf8_incomplete_tail(data)
    return keep if keep > 0 else len(data)


def align_complete_lines(data, length):
    """
    How many bytes of `data` (read forward up to the end of the file) a follow read
    keeps: up to and including the last newline, so an incomplete last line is
    withheld until its newline arrives. Only a line longer than a whole chunk
    (`length`) is split, like align_forward does.
    """
    newline = data.rfind(b'\n')
    if newline >= 0:
        return newline + 1
    if len(data) >= length:
        return align_forward(data, False)
    return 0


def align_backward(data, data_start, lo):
    """
    Where a chunk read backward starts (an absolute offset). `data` holds the bytes
    from `data_start` to the end of the chunk; `lo` is the lowest offset the chunk may
    start at (`data_start` is `lo` or, to see the newline before it, `lo - 1`).
    The chunk starts at the beginning of the file, else after the first newline. A
    line longer than the chunk is split, but never inside a UTF-8 character.
    """
    if lo <= 0:
        return 0
    newline = data.find(b'\n')
    if 0 <= newline < len(data) - 1:
        return data_start + newline + 1
    skip = lo - data_start
    start = skip + _utf8_continuation_head(data[skip:])
    if start >= len(data):
        start = skip
    return data_start + start


def decode_chunk(data, at_bof):
    """
    Text of a chunk and its encoding. The file was already found to be text by its
    first bytes; invalid bytes become U+FFFD. A BOM at the start of the file is dropped.
    """
    if at_bof and data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM):]
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        return data.decode('utf-8', 'replace'), 'utf-8 (invalid bytes replaced)'


def check_text(head, size):
    """Raises NotTextError unless the first bytes of the file (`head`) are text"""
    head = head[:HEAD_CHECK_BYTES]
    decode_text(head, truncated=size is None or size > len(head))


def chunk_result(path, request, size, head, data_start, data):
    """
    The API response for a pager request: {path, content, offset, end, size, bof, eof,
    encoding}, where offset/end is the byte range the content covers.
    `head` is the first bytes of the file (text check), `data` the bytes read from
    `data_start` (request.read_range(), with a tail read's start resolved). `size` may
    be None when a cloud backend does not know it (forward reads only).
    A forward read at the end of the file returns no content and eof without a text
    check (the readers read nothing then): the viewer's cheap "nothing new" poll.
    """
    requested = request.before if request.before is not None else request.offset
    if size is not None and requested is not None and requested > size:
        if not request.follow:
            raise ViewError('Offset {} is beyond the end of the file ({} bytes)'.format(requested, size))
        # Following a file that shrank (truncated, or replaced by a shorter one): its
        # new size and no content; the viewer reloads the tail
        return _empty_result(path, size, size)
    if not request.backward and size is not None and request.offset == size and not data:
        return _empty_result(path, size, size)
    if size is not None and data:
        # A file that grew while it was read
        size = max(size, data_start + len(data))
    check_text(head, size)

    if request.backward:
        before = data_start + len(data)
        lo = max(0, before - request.length)
        start = align_backward(data, data_start, lo)
        chunk = data[start - data_start:]
        end = before
    else:
        start = data_start
        if size is not None:
            at_eof = data_start + len(data) >= size
        else:
            at_eof = len(data) < request.length # fewer bytes than asked for
        if request.follow and at_eof:
            chunk = data[:align_complete_lines(data, request.length)]
        else:
            chunk = data[:align_forward(data, at_eof)]
        end = start + len(chunk)
        if size is None and at_eof:
            size = data_start + len(data)
        if request.follow:
            # All complete lines up to the end of the file: an incomplete last line
            # (end < size) follows once it is complete
            content, encoding = decode_chunk(chunk, start == 0)
            return _result(path, content, start, end, size, at_eof, encoding)

    content, encoding = decode_chunk(chunk, start == 0)
    return _result(path, content, start, end, size, size is not None and end >= size, encoding)


def _result(path, content, start, end, size, eof, encoding):
    return {
        'path': path,
        'content': content,
        'offset': start,
        'end': end,
        'size': size,
        'bof': start == 0,
        'eof': eof,
        'encoding': encoding,
    }


def _empty_result(path, offset, size):
    """No content at `offset`, the end of the file (a forward read with nothing new)"""
    return _result(path, '', offset, offset, size, True, 'utf-8')


def run_limited(command, timeout, env=None):
    """
    Runs `command` (sudo ... as the user) in its own process group and returns
    (returncode, stdout bytes, stderr bytes). On timeout the group is terminated and
    ViewTimeoutError raised.
    """
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise ViewTimeoutError('Reading the file timed out after {} seconds'.format(timeout))
    return process.returncode, stdout, stderr


def _kill_group(process):
    # sudo relays SIGTERM to the command it runs; SIGKILL if that does not end it
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.communicate(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
    logging.error("Could not stop the file viewer process %s", process.pid)
