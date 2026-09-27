import logging
import os
import re
import subprocess

from ..exceptions import *
from .abstract_connection import AbstractConnection, RcloneException
from .file_times import epoch_to_iso_utc


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
