import json
import logging
import os
import re
import subprocess

from ..exceptions import *
from .abstract_connection import AbstractConnection, RcloneException
from .file_times import epoch_to_iso_utc
from . import file_view


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

        header, content = _read_with_impersonation(path, user, file_view.MAX_VIEW_BYTES + 1)
        try:
            return file_view.view_result(path, content, header.get('size'))
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
    command = [
        'sudo',
        '-n',
        '-u', user,
        '-i', 'eval',
        'echo $HOME'
    ]

    byteOutput = subprocess.check_output(command)
    output = byteOutput.decode('UTF-8').rstrip()
    return output


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
        'ls',
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
        byteOutput = subprocess.check_output(command)
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
        'mkdir',
        '-p',
        '--', # Never interpret the path as an option
        path,
    ]

    byteOutput = subprocess.check_output(command)
    output = byteOutput.decode('UTF-8').rstrip()
    return output


# Runs as the user: opens the file non-blocking (a FIFO cannot hang it), accepts only a
# regular file (checked before opening, so devices are never opened, and again on the
# open file) and prints a JSON header line ({"size"}) followed by at most `cap` bytes.
# Errors: exit status 3 and {"error": kind} on stdout.
_VIEW_READER = r'''
import json, os, stat, sys
cap, path = int(sys.argv[1]), sys.argv[2]
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
chunks, size = [], 0
try:
    while size < cap:
        chunk = os.read(fd, min(cap - size, 65536))
        if not chunk:
            break
        chunks.append(chunk); size += len(chunk)
except OSError as e:
    fail(kind_of(e))
out = sys.stdout.buffer
out.write(json.dumps({"size": st.st_size}).encode() + b"\n")
out.write(b"".join(chunks))
out.flush()
'''

_VIEW_ERRORS = {
    'missing': (file_view.NotFoundError, "'{path}' does not exist"),
    'denied': (file_view.ForbiddenError, "User {user} cannot read '{path}'"),
    'dir': (file_view.ViewError, "'{path}' is a folder"),
    'special': (file_view.ViewError, "'{path}' is not a regular file"),
    'error': (file_view.ViewError, "'{path}' could not be read"),
}


def _read_with_impersonation(path, user, cap):
    """
    Reads up to `cap` bytes of `path` as `user`, never as root.
    Returns ({'size': file size}, bytes). Raises file_view errors.
    """
    from .local_credentials import _python
    command = ['sudo', '-n', '-u', user, '--', 'env']
    # sudo drops LD_LIBRARY_PATH, which the image's python needs when /etc is the host's
    if os.environ.get('LD_LIBRARY_PATH'):
        command.append('LD_LIBRARY_PATH={}'.format(os.environ['LD_LIBRARY_PATH']))
    # The path is an argument of the script, never an option of env or python
    command += [_python(), '-I', '-S', '-c', _VIEW_READER, str(cap), path]

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
    except ValueError:
        raise file_view.ViewError("'{}' could not be read".format(path))
    return header, content
