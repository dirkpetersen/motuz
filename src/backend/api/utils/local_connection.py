import json
import logging
import os
import pwd
import re
import subprocess

from ..exceptions import *
from .abstract_connection import AbstractConnection, RcloneException, check_output
from .file_times import epoch_to_iso_utc
from . import file_view


# Absolute paths: the sudoers rule of a non-root install (bin/systemd/install.sh) allows
# exactly these commands
LS = '/usr/bin/ls'
MKDIR = '/usr/bin/mkdir'
ENV = '/usr/bin/env'

class LocalConnection(AbstractConnection):
    """
    A symmetric API for RcloneConnection to be used locally
    """

    def ls(self, data, path):
        user = data.owner

        try:
            output = _ls_with_impersonation(path, user)
        except subprocess.CalledProcessError as err:
            logging.exception(err)
            raise HTTP_403_FORBIDDEN("User {user} does not have privilege for path '{path}'".format(
                user=user,
                path=path,
            ))
        except Exception as err:
            logging.exception(err)
            raise HTTP_403_FORBIDDEN(str(err))

        files = _parse_ls(output)
        return {
            'files': files,
            'path': path,
        }


    def lshome(self, data):
        user = data.owner

        try:
            homePath = _homepath_with_impersonation(user)
            return self.ls(data, homePath)
        except Exception as e:
            logging.error("User does not have a home", exc_info=True)
            return self.ls(data, '/')


    def view(self, data, path):
        """
        The first MAX_VIEW_BYTES of a text file, read as the user (see file_view)
        """
        user = data.owner
        if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
            raise file_view.ViewError("Local path must be absolute: '{}'".format(path))

        header, _, content = _read_with_impersonation(path, user, 0, file_view.MAX_VIEW_BYTES + 1)
        try:
            return file_view.view_result(path, content, header.get('size'))
        except file_view.NotTextError:
            raise file_view.NotTextError("'{}' is not a text file".format(os.path.basename(path)))


    def view_chunk(self, data, path, request):
        """
        One chunk of a text file for the pager (file_view.ChunkRequest), read as the
        user by a single reader process, which also returns the size and the first
        bytes (text check), so a tail read needs no second process.
        """
        user = data.owner
        if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
            raise file_view.ViewError("Local path must be absolute: '{}'".format(path))

        start, count = request.read_range()
        header, head, content = _read_with_impersonation(path, user, start, count, file_view.HEAD_CHECK_BYTES)
        size, data_start = header.get('size'), header.get('start')
        if not isinstance(size, int) or not isinstance(data_start, int):
            raise file_view.ViewError("'{}' could not be read".format(path))
        try:
            return file_view.chunk_result(path, request, size, head, data_start, content)
        except file_view.NotTextError:
            raise file_view.NotTextError("'{}' is not a text file".format(os.path.basename(path)))


    def mkdir(self, data, path):
        user = data.owner

        try:
            output = _mkdir_with_impersonation(path, user)
        except subprocess.CalledProcessError as err:
            raise HTTP_403_FORBIDDEN("User {user} does not have privilege for path '{path}'".format(
                user=user,
                path=path,
            ))
        except Exception as err:
            raise HTTP_403_FORBIDDEN(str(err))

        return {
            'message': 'success',
        }



def _homepath_with_impersonation(user):
    """
    The user's home directory from the user database (NSS: /etc/passwd, SSSD, ...).
    Not through a login shell as the user (`sudo -i`), which the sudoers rule of a
    non-root install does not allow and which would run the user's shell profile.
    Raises KeyError for an unknown user.
    """
    return pwd.getpwnam(user).pw_dir


def _parse_ls(output):
    """
    Parses `ls -a -l -L -g -o --time-style=+%s`. Each line looks like one of

    drwxr-xr-x 12  384 1720280520 dirname
    -rw-r--r--  1 4096 1720280520  name with a leading space
    crw-rw-rw-  1 1, 3 1720280520 null            (device: major, minor)
    l?????????  ?    ? ? broken-symlink           (-L could not dereference it)
    permissions | links | size | mtime (epoch seconds) | filename

    The mtime is epoch seconds, i.e. UTC whatever TZ is; it becomes `modified` in
    ISO 8601 UTC (file_times). ls separates it from the name by exactly one space, so
    names keep leading spaces.

    Returns [{name, type, size, modified}], size an int or None, modified a string or None.
    """
    regex = re.compile(r'^(\S+)\s+(\S+)\s+(\d+,\s*\d+|\S+)\s+(-?\d+|\?) (.+)$')

    result = []
    for line in output.split('\n'):
        if line.startswith("total") or line == '':
            continue

        match = regex.match(line)
        if match is None:
            logging.error("Could not parse line `{}`".format(line))
            continue

        permissions, _links, size, mtime, filename = match.groups()

        if filename == '.' or filename == '..':
            continue

        if permissions[0] == 'l':
            type = "symlink"
        elif permissions[0] == 'd':
            type = "dir"
        elif permissions[0] == '-':
            type = "file"
        else:
            type = "unknown"

        result.append({
            "name": filename,
            "type": type,
            "size": int(size) if size.isdigit() else None,
            "modified": epoch_to_iso_utc(mtime) if mtime != '?' else None,
        })

    return result



def _ls_with_impersonation(path, user):
    command = [
        'sudo',
        '-n',
        '-u', user,
        LS,
        '-a', # Hidden files
        '-l', # List format
        '-L', # Dereference symlinks
        '-g', # Exclude owner user info (if needed, consider -n)
        '-o', # Exclude group user info (if needed consider -n)
        '--time-style=+%s', # Modification times as epoch seconds (UTC, whatever TZ is)
        '--', # Never interpret the path as an option
        path,
    ]

    try:
        byteOutput = check_output(command)
        output = byteOutput.decode('UTF-8').rstrip('\n') # names may end in spaces
        return output
    except subprocess.CalledProcessError as err:
        # Sometimes `ls -alLgo` errors out when it cannot dereference symlinks, but it
        # still returns some results on stdout. We should display those cases
        try:
            output = err.stdout.decode('UTF-8').rstrip('\n')
            if len(output) == 0:
                raise
            return output
        except:
            raise


def _mkdir_with_impersonation(path, user):
    command = [
        'sudo',
        '-n',
        '-u', user,
        MKDIR,
        '-p',
        '--', # Never interpret the path as an option
        path,
    ]

    byteOutput = check_output(command)
    output = byteOutput.decode('UTF-8').rstrip()
    return output


# Runs as the user: opens the file non-blocking (a FIFO cannot hang it), accepts only a
# regular file (checked before opening, so devices are never opened, and again on the
# open file) and prints a JSON header line ({"size", "start", "head"}) followed by the
# first `head` bytes of the file (at most, for the text check) and at most `count` bytes
# from `start` (a negative start counts from the end of the file, for a tail read).
# Nothing is read when `start` is at (or past) the end of the file.
# Errors: exit status 3 and {"error": kind} on stdout.
_VIEW_READER = r'''
import json, os, stat, sys
head_cap, start, count, path = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
def fail(kind):
    sys.stdout.write(json.dumps({"error": kind})); sys.stdout.flush(); os._exit(3)
def kind_of(e):
    if isinstance(e, (FileNotFoundError, NotADirectoryError)): return "missing"
    if isinstance(e, PermissionError): return "denied"
    return "error"
try:
    st = os.stat(path)
except OSError as e:
    fail(kind_of(e))
if stat.S_ISDIR(st.st_mode): fail("dir")
if not stat.S_ISREG(st.st_mode): fail("special")
try:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
except OSError as e:
    fail(kind_of(e))
st = os.fstat(fd)
if not stat.S_ISREG(st.st_mode): fail("special")
if start < 0:
    start = max(0, st.st_size + start)
if start >= st.st_size:
    head_cap = count = 0 # at the end: nothing to read (a follow poll without news)
def read_at(pos, n):
    chunks, size = [], 0
    while size < n:
        chunk = os.pread(fd, min(n - size, 65536), pos + size)
        if not chunk:
            break
        chunks.append(chunk); size += len(chunk)
    return b"".join(chunks)
try:
    head = read_at(0, head_cap) if head_cap > 0 else b""
    data = read_at(start, count) if count > 0 else b""
except OSError as e:
    fail(kind_of(e))
out = sys.stdout.buffer
out.write(json.dumps({"size": st.st_size, "start": start, "head": len(head)}).encode() + b"\n")
out.write(head)
out.write(data)
out.flush()
'''

_VIEW_ERRORS = {
    'missing': (file_view.NotFoundError, "'{path}' does not exist"),
    'denied': (file_view.ForbiddenError, "User {user} cannot read '{path}'"),
    'dir': (file_view.ViewError, "'{path}' is a folder"),
    'special': (file_view.ViewError, "'{path}' is not a regular file"),
    'error': (file_view.ViewError, "'{path}' could not be read"),
}


def _read_with_impersonation(path, user, start, count, head=0):
    """
    Reads up to `count` bytes of `path` from `start` (negative: from the end), and the
    first `head` bytes, as `user`, never as root.
    Returns ({'size': file size, 'start': resolved start}, head bytes, bytes).
    Raises file_view errors.
    """
    from .local_credentials import _python
    command = ['sudo', '-n', '-u', user, '--', ENV]
    # sudo drops LD_LIBRARY_PATH, which the image's python needs when /etc is the host's
    if os.environ.get('LD_LIBRARY_PATH'):
        command.append('LD_LIBRARY_PATH={}'.format(os.environ['LD_LIBRARY_PATH']))
    # The path is an argument of the script, never an option of env or python
    command += [_python(), '-I', '-S', '-c', _VIEW_READER, str(int(head)), str(int(start)), str(int(count)), path]

    returncode, stdout, stderr = file_view.run_limited(command, file_view.LOCAL_TIMEOUT)

    if returncode == 3:
        try:
            kind = json.loads(stdout.decode('utf-8'))['error']
        except (ValueError, KeyError, TypeError):
            kind = 'error'
        error, message = _VIEW_ERRORS.get(kind, _VIEW_ERRORS['error'])
        raise error(message.format(path=path, user=user))
    header, newline, content = stdout.partition(b'\n')
    if returncode != 0 or not newline:
        # stderr of sudo / the reader, never file contents
        logging.error("Viewing '%s' as %s failed (%s): %s", path, user, returncode,
                      stderr.decode('utf-8', 'replace')[-500:])
        raise file_view.ForbiddenError("User {} cannot read '{}'".format(user, path))
    try:
        header = json.loads(header.decode('utf-8'))
        head_length = int(header.get('head', 0))
    except (ValueError, TypeError, AttributeError):
        raise file_view.ViewError("'{}' could not be read".format(path))
    if not 0 <= head_length <= len(content):
        raise file_view.ViewError("'{}' could not be read".format(path))
    return header, content[:head_length], content[head_length:]
