"""
The running `rclone rcat` of each file of an upload from the browser
(managers/upload_manager.py). rcat runs as the user and writes the destination file; the
file's chunks arrive in separate requests, and each is written to rcat's stdin, so the
data is never stored on the server.

A pipe exists in this process only. uWSGI runs one process with threads (wsgi.ini), so
every chunk of a file reaches the same process. A chunk that finds no pipe (the server
restarted, or the pipe was idle too long) is refused, and the browser sends the file
again from the start.

A file is aborted (Stop, idle) in one of two ways, chosen when it starts:
- `by_signal=False`: rcat gets end of file before all bytes, fails because the size
  differs and removes what it wrote. Only for a temporary name: rcat writes a local file
  in place, so this would also remove a file it replaces (upload_manager renames the
  temporary file after rcat succeeds).
- `by_signal=True`: SIGTERM, on which rclone aborts a multipart upload (atexit), so an
  object it would replace is kept; a single PUT never completes.
Only file names and rclone's error text are logged, never file contents.
"""
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time

from .abstract_connection import spawn


READ_BYTES = 1 << 20      # bytes read from the request and written to rcat at once
IDLE_SECONDS = 300        # a pipe that gets no chunk for this long is aborted
MAX_PIPES = 64            # files uploaded at the same time, all users
FINISH_TIMEOUT = 900      # rcat's last upload request (e.g. CompleteMultipartUpload)
_ERROR_CHARS = 2000


class PipeError(Exception):
    """A chunk that cannot be written; `written` is the pipe's position, None if no pipe"""
    def __init__(self, message, written=None):
        super().__init__(message)
        self.written = written


class RcloneExited(PipeError):
    """rcat exited before the end of the file: finish(key, pipe) returns its error text"""
    def __init__(self, message, pipe):
        super().__init__(message, pipe.written)
        self.pipe = pipe


class PipeBusy(Exception):
    """MAX_PIPES files are being uploaded"""


class _Pipe:
    def __init__(self, process, stderr, size, by_signal):
        self.process = process
        self.stderr = stderr
        self.size = size
        self.by_signal = by_signal
        self.written = 0
        self.used = time.monotonic()
        self.lock = threading.Lock()  # one chunk at a time


_pipes = {}  # (job id, file index) -> _Pipe
_pipes_lock = threading.Lock()
_reaper = None


def start(key, command, env, size, by_signal):
    """
    Starts `command` (rclone rcat as the user) for the file `key` of `size` bytes;
    `by_signal`: how it is aborted (see above)
    """
    abort(key)
    with _pipes_lock:
        if len(_pipes) >= MAX_PIPES:
            raise PipeBusy('The server is busy with other uploads')
        stderr = tempfile.TemporaryFile()
        process = spawn(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=stderr, env=env, start_new_session=True)
        _pipes[key] = _Pipe(process, stderr, size, by_signal)
        _start_reaper()


def written(key):
    """The bytes written to the pipe of `key`, None if it has no pipe"""
    with _pipes_lock:
        pipe = _pipes.get(key)
    return None if pipe is None else pipe.written


def write(key, offset, stream, length):
    """
    Copies `length` bytes of `stream` (the request body) to the pipe of `key`, at
    `offset` of the file. Returns the pipe (its `written`: the bytes so far, for
    finish()). Raises PipeError if there is
    no pipe, the offset is not where the pipe is, the chunk is too long, the body ends
    early, or RcloneExited if rcat has exited.
    """
    with _pipes_lock:
        pipe = _pipes.get(key)
    if pipe is None:
        raise PipeError('The upload of this file must start again')
    with pipe.lock:
        if offset != pipe.written:
            raise PipeError('The chunk does not start where the file is', pipe.written)
        if offset + length > pipe.size:
            raise PipeError('The chunk goes past the end of the file', pipe.written)
        remaining = length
        while remaining:
            data = stream.read(min(READ_BYTES, remaining))
            if not data:
                raise PipeError('The chunk ended early', pipe.written)
            try:
                pipe.process.stdin.write(data)
            except (BrokenPipeError, ValueError):
                raise RcloneExited('rclone stopped', pipe)
            pipe.written += len(data)
            remaining -= len(data)
        pipe.used = time.monotonic()
        return pipe


def finish(key, pipe):
    """
    Closes `pipe` (of `key`, from write()) after its last byte and waits for rcat.
    Returns (exit status, rclone's error text); (None, text) if it is no longer the
    pipe of `key` (aborted, or replaced by a new start of the file).
    """
    with _pipes_lock:
        if _pipes.get(key) is not pipe:
            return None, 'The upload of this file must start again'
        del _pipes[key]
    with pipe.lock:
        _close(pipe)
        try:
            returncode = pipe.process.wait(timeout=FINISH_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(pipe)
            returncode = pipe.process.wait()
        return returncode, _error_text(pipe)


def abort(key):
    """Aborts the file `key`: rcat removes what it wrote"""
    with _pipes_lock:
        pipe = _pipes.pop(key, None)
    if pipe is not None:
        _abort(pipe)


def abort_job(job_id):
    """Aborts every file of the job `job_id` that is being uploaded"""
    with _pipes_lock:
        pipes = [_pipes.pop(key) for key in list(_pipes) if key[0] == job_id]
    for pipe in pipes:
        _abort(pipe)


def reap(now=None):
    """Aborts the pipes that got no chunk for IDLE_SECONDS"""
    now = time.monotonic() if now is None else now
    with _pipes_lock:
        idle = [key for key, pipe in _pipes.items() if now - pipe.used > IDLE_SECONDS]
        pipes = [_pipes.pop(key) for key in idle]
    for key, pipe in zip(idle, pipes):
        logging.info('Upload of file %s of copy job %s: no data for %s s, aborted', key[1], key[0], IDLE_SECONDS)
        _abort(pipe)


def _abort(pipe):
    # A chunk being written holds the lock; its write fails when stdin is closed or rcat
    # exits. With a signal, stdin stays open until rcat has exited: end of file first
    # would let rcat complete the upload and then remove it, with the file it replaced.
    if pipe.by_signal:
        # To sudo only, which relays it to rclone once: rclone stops handling signals
        # after the first, so a second one (e.g. to the whole group) would kill it in
        # the middle of its cleanup (azureblob: after committing an empty blob, before
        # removing it)
        try:
            os.kill(pipe.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        _close(pipe)
    try:
        pipe.process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _kill(pipe)
        pipe.process.wait()
    _close(pipe)
    pipe.stderr.close()


def _close(pipe):
    try:
        pipe.process.stdin.close()
    except (BrokenPipeError, OSError):
        pass


def _kill(pipe, sig=signal.SIGKILL):
    try:
        os.killpg(pipe.process.pid, sig)
    except ProcessLookupError:
        pass


def _error_text(pipe):
    pipe.stderr.seek(0)
    text = pipe.stderr.read().decode('utf-8', 'replace').strip()
    pipe.stderr.close()
    return text[-_ERROR_CHARS:]


def _start_reaper():
    """Starts the thread that aborts idle pipes, once per process (needs _pipes_lock)"""
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return

    def loop():
        while True:
            time.sleep(60)
            try:
                reap()
            except Exception:
                logging.exception('Could not abort idle uploads')

    _reaper = threading.Thread(target=loop, name='upload-reaper', daemon=True)
    _reaper.start()
