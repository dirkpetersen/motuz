"""
Read-only viewer for text files (POST /api/system/files/view/).

Local files are read by LocalConnection.view, cloud files by RcloneConnection.view,
both as the logged-in user. This module holds the parts they share: the size cap,
text detection and decoding, the errors, and running a command with a timeout.
File contents are never logged.
"""
import codecs
import logging
import os
import signal
import subprocess


MAX_VIEW_BYTES = 1024 * 1024 # only the first 1 MiB is shown
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
