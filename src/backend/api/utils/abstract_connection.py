import subprocess
import os


# Only these variables of the server's environment reach rclone and other commands run
# as a user. The server's environment holds its secrets (MOTUZ_FLASK_SECRET_KEY signs
# the login tokens, database password, ...), and a process running as a user can be
# inspected by that user, e.g. /proc/<pid>/environ on the host.
_INHERITED_VARIABLES = ('PATH', 'LANG', 'LC_ALL', 'TZ')


def user_process_env(extra=None):
    """Minimal environment for a command run as a user, plus `extra` (rclone config)"""
    env = {key: os.environ[key] for key in _INHERITED_VARIABLES if key in os.environ}
    env.setdefault('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    env.update(extra or {})
    return env


class AbstractConnection:
    """
    A symmetric API for rclone_connection to be used locally
    """

    def _execute(self, command, env={}):
        full_env = user_process_env(env)
        try:
            byteOutput = subprocess.check_output(
                command,
                stderr=subprocess.PIPE,
                env=full_env
            )
            output = byteOutput.decode('UTF-8').rstrip()
            return output
        except subprocess.CalledProcessError as err:
            if (err.stderr is None):
                raise
            stderr = err.stderr.decode('UTF-8').strip()
            if len(stderr) == 0:
                raise
            raise RcloneException(stderr)


class RcloneException(Exception):
    pass

