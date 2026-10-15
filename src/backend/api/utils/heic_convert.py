"""
Server-side conversion of HEIC/HEIF images to JPEG for the image viewer (Chrome,
Firefox and Edge cannot show HEIC; Safari can and does not ask for the conversion).

The decoder (pi-heif: libheif + libde265, LGPL-3.0, decode-only; Pillow for the JPEG) is
C code that parses an untrusted file, so it never runs in the server process: heic_child.py
runs in a child process started with `sudo -n -u <the logged-in user> /usr/bin/env
<python> -I -c <heic_child.py>` -- the same sudo rule as every other file operation, so
never as root and never as the service account (convert() refuses both). The file goes in
on stdin, the JPEG comes out on stdout; nothing is written to disk. The child limits
itself (address space, CPU, no files, pixel count; it decodes nothing if a limit cannot
be set); the parent reads at most MAX_OUTPUT_BYTES from it, gives its stderr to nowhere,
and kills its whole process group after a wall-clock timeout. At most
MOTUZ_HEIC_MAX_CONCURRENT conversions run at once (default 2), the others wait a few
seconds and are then answered 503. Nothing of the file is logged, only exit codes.

Settings: MOTUZ_HEIC_PYTHON, an interpreter every user can run (default: the server's
own, which is right for the Docker image), and MOTUZ_HEIC_SITE, a directory with pi-heif
and Pillow for that interpreter (default: none, the interpreter's own packages). The
systemd install sets both: /usr/bin/python3 and /opt/motuz-heic/site
(bin/systemd/install.sh, deploy.sh), because the server's own Python lives in the
service account's home.
"""
import logging
import os
import pwd
import selectors
import subprocess
import sys
import threading
import time

from . import file_view
from . import heic_child
from . import image_view
from .abstract_connection import sudo_as, user_process_env, spawn
from .local_connection import ENV

ENV_PYTHON = 'MOTUZ_HEIC_PYTHON'
ENV_SITE = 'MOTUZ_HEIC_SITE'
TIMEOUT_SECONDS = 60 # wall clock for the whole conversion
BUSY_WAIT_SECONDS = 5 # how long a request waits for a free conversion slot
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
CONVERTIBLE = (image_view.HEIC, image_view.HEIF)
CONVERTED_TYPE = image_view.JPEG

with open(heic_child.__file__.replace('.pyc', '.py'), encoding='utf-8') as _f:
    CHILD_CODE = _f.read()

_SLOTS = threading.BoundedSemaphore(image_view.heic_concurrency())


class ConversionError(image_view.NotAnImageError):
    """The image cannot be converted (415)"""


class ConversionTooLargeError(image_view.TooLargeError):
    """More pixels or memory than the conversion allows (413)"""


class ConversionBusyError(file_view.ViewError):
    """All conversion slots are taken: try again in a moment (503)"""
    status = 503


class ConversionUnavailableError(file_view.ViewError):
    """No converter is installed for the users (503)"""
    status = 503


class ConversionFailedError(file_view.ViewError):
    """The converter could not be started or broke (500): sudo, env, the interpreter"""
    status = 500


class _OutputTooLarge(Exception):
    pass


def python():
    return os.environ.get(ENV_PYTHON) or sys.executable


def site():
    return os.environ.get(ENV_SITE, '')


def refuse_service_user(user):
    """The converter runs as the logged-in user: never as root, never as the account the server runs as"""
    if not isinstance(user, str) or not user:
        raise ValueError('no user to convert as')
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return # sudo refuses an unknown user
    if user == 'root' or uid == 0:
        raise ValueError('the conversion does not run as root')
    if uid == os.geteuid():
        raise ValueError('the conversion does not run as the account the server runs as')


def command(user):
    """The argv of the child: sudo as the user, env, an absolute interpreter, -I, the code"""
    refuse_service_user(user)
    return sudo_as(user) + [ENV, python(), '-I', '-c', CHILD_CODE, site()]


def is_convertible(mime):
    return mime in CONVERTIBLE


def _stop(process):
    """Ends the child and everything it started (its process group): SIGTERM, then SIGKILL"""
    import signal
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    for pipe in (process.stdout, process.stdin):
        try:
            pipe and pipe.close()
        except OSError:
            pass


def _communicate(process, data, timeout, max_output):
    """
    Feeds `data` to the child's stdin and reads its stdout without ever holding more than
    max_output bytes of it: _OutputTooLarge past that, subprocess.TimeoutExpired past
    the deadline (the caller kills the process group). Returns the stdout.
    """
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    out = bytearray()
    stdout, stdin = process.stdout, process.stdin
    os.set_blocking(stdout.fileno(), False)
    selector.register(stdout, selectors.EVENT_READ)
    view, position = memoryview(data), 0
    if data:
        os.set_blocking(stdin.fileno(), False)
        selector.register(stdin, selectors.EVENT_WRITE)
    else:
        stdin.close()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in selector.select(remaining):
                if key.fileobj is stdout:
                    chunk = os.read(stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stdout)
                        continue
                    out += chunk
                    if len(out) > max_output:
                        raise _OutputTooLarge()
                else:
                    try:
                        position += os.write(stdin.fileno(), view[position:position + 65536])
                    except (BrokenPipeError, OSError): # the child ended without reading it all
                        position = len(data)
                    if position >= len(data):
                        selector.unregister(stdin)
                        stdin.close()
    finally:
        selector.close()
        view.release()
        try:
            os.set_blocking(stdout.fileno(), True) # for the caller's cleanup (communicate)
        except OSError:
            pass
        if not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
    process.wait(timeout=max(0.1, deadline - time.monotonic()))
    stdout.close()
    return bytes(out)


def convert(data, user, timeout=TIMEOUT_SECONDS):
    """
    JPEG bytes of the HEIC/HEIF image `data`, converted by a child process run as `user`.
    Raises ConversionBusyError (503: all slots taken), ConversionError (415: not
    decodable), ConversionTooLargeError (413), ConversionUnavailableError (503: not
    installed), ConversionFailedError (500) and file_view.ViewTimeoutError (504).
    """
    if not _SLOTS.acquire(timeout=BUSY_WAIT_SECONDS):
        raise ConversionBusyError('The server is converting other images; try again in a moment')
    try:
        return _convert(data, user, timeout)
    finally:
        _SLOTS.release()


def _convert(data, user, timeout):
    process = spawn(command(user), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=user_process_env(), start_new_session=True)
    try:
        stdout = _communicate(process, data, timeout, MAX_OUTPUT_BYTES)
    except subprocess.TimeoutExpired:
        _stop(process)
        raise file_view.ViewTimeoutError('Converting the image took longer than {} seconds'.format(timeout))
    except _OutputTooLarge:
        _stop(process)
        raise ConversionError('The converted image is too large')
    except BaseException:
        _stop(process)
        raise
    code = process.returncode
    if code == heic_child.EXIT_OK:
        if not stdout.startswith(b'\xff\xd8\xff'):
            logging.warning('HEIC conversion for %s ended without a JPEG', user)
            raise ConversionFailedError('The image could not be converted')
        return stdout
    if code == heic_child.EXIT_TOO_LARGE:
        raise ConversionTooLargeError('The image has more than {} megapixels; too large to convert for the viewer'.format(
            heic_child.MAX_PIXELS // 1000000))
    if code == heic_child.EXIT_MEMORY:
        raise ConversionTooLargeError('The image needs more memory to convert than the viewer allows')
    if code == heic_child.EXIT_NOT_DECODABLE:
        raise ConversionError('The image is not a HEIC/HEIF file that can be decoded')
    logging.warning('HEIC conversion for %s failed with exit code %s', user, code)
    if code == heic_child.EXIT_UNAVAILABLE:
        raise ConversionUnavailableError('HEIC/HEIF conversion is not installed on this server')
    if code in (-9, 128 + 9): # SIGKILL, and not by our timeout: most likely the kernel's out-of-memory killer
        raise ConversionTooLargeError('The image needs more memory to convert than the server has')
    if code in (-24, 128 + 24): # SIGXCPU: the CPU time limit
        raise file_view.ViewTimeoutError('Converting the image took too long')
    # 1, 126, 127: sudo refused or env could not run the interpreter; EXIT_LIMITS; a crash
    raise ConversionFailedError('The HEIC/HEIF converter could not run on this server (exit code {})'.format(code))
