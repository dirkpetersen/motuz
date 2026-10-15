"""
Server-side conversion of HEIC/HEIF images to JPEG for the image viewer (Chrome,
Firefox and Edge cannot show HEIC; Safari can and does not ask for the conversion).

The decoder (pi-heif: libheif + libde265, LGPL-3.0, decode-only; Pillow for the JPEG) is
C code that parses an untrusted file, so it never runs in the server process: heic_child.py
runs in a child process started with `sudo -n -u <the logged-in user> /usr/bin/env
<python> -I -c <heic_child.py>` -- the same sudo rule as every other file operation, so
never as root and never as the service account (convert() refuses both). The file goes in
on stdin, the JPEG comes out on stdout; nothing is written to disk. The child limits
itself (address space, CPU, no files, pixel count, output size) and the parent kills its
whole process group after a wall-clock timeout. Nothing of the file is logged.

Settings: MOTUZ_HEIC_PYTHON, an interpreter every user can run (default: the server's
own, which is right for the Docker image), and MOTUZ_HEIC_SITE, a directory with pi-heif
and Pillow for that interpreter (default: none, the interpreter's own packages). The
systemd install sets both: /usr/bin/python3 and /opt/motuz-heic/site
(bin/systemd/install.sh), because the server's own Python lives in the service
account's home.
"""
import os
import pwd
import subprocess
import sys

from . import file_view
from . import heic_child
from . import image_view
from .abstract_connection import sudo_as, user_process_env, spawn
from .local_connection import ENV

ENV_PYTHON = 'MOTUZ_HEIC_PYTHON'
ENV_SITE = 'MOTUZ_HEIC_SITE'
TIMEOUT_SECONDS = 60 # wall clock for the whole conversion
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
CONVERTIBLE = (image_view.HEIC, image_view.HEIF)
CONVERTED_TYPE = image_view.JPEG

with open(heic_child.__file__.replace('.pyc', '.py'), encoding='utf-8') as _f:
    CHILD_CODE = _f.read()


class ConversionError(image_view.NotAnImageError):
    """The image cannot be converted (415)"""


class ConversionTooLargeError(image_view.TooLargeError):
    """More pixels or memory than the conversion allows (413)"""


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


def convert(data, user, timeout=TIMEOUT_SECONDS):
    """
    JPEG bytes of the HEIC/HEIF image `data`, converted by a child process run as `user`.
    Raises ConversionError (415: not decodable, or no converter installed),
    ConversionTooLargeError (413) and file_view.ViewTimeoutError (504).
    """
    process = spawn(command(user), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=user_process_env(), start_new_session=True)
    try:
        stdout, _ = process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        file_view._kill_group(process)
        raise file_view.ViewTimeoutError('Converting the image took longer than {} seconds'.format(timeout))
    except BrokenPipeError:
        stdout = b'' # the child ended without reading its input; its exit code says why
        process.wait()
    code = process.returncode
    if code == heic_child.EXIT_OK:
        if not stdout.startswith(b'\xff\xd8\xff') or len(stdout) > MAX_OUTPUT_BYTES:
            raise ConversionError('The image could not be converted')
        return stdout
    if code == heic_child.EXIT_TOO_LARGE:
        raise ConversionTooLargeError('The image has more than {} megapixels; too large to convert for the viewer'.format(
            heic_child.MAX_PIXELS // 1000000))
    if code == heic_child.EXIT_MEMORY:
        raise ConversionTooLargeError('The image needs more memory to convert than the viewer allows')
    if code == heic_child.EXIT_UNAVAILABLE:
        raise ConversionError('HEIC/HEIF conversion is not installed on this server')
    if code in (-24, 128 + 24, -9, 128 + 9): # SIGXCPU (CPU limit) / SIGKILL (a kill by the OS)
        raise file_view.ViewTimeoutError('Converting the image took too long')
    raise ConversionError('The image is not a HEIC/HEIF file that can be decoded')
