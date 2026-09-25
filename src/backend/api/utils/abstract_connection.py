import subprocess
import os


# Server secrets (docker secrets, exported by load-secrets.sh). Commands run as the
# logged-in user with `sudo -E`, so without this they would inherit them, and the user
# could read them from /proc/<pid>/environ of their own rclone process.
SERVER_SECRET_ENV = (
    'MOTUZ_FLASK_SECRET_KEY',
    'MOTUZ_DATABASE_PASSWORD',
    'MOTUZ_SMTP_PASSWORD',
    'MOTUZ_ONEDRIVE_CLIENT_SECRET',
)


def subprocess_env(env=None):
    """Environment for a child process: the server's, without its secrets, plus `env`"""
    full_env = {key: value for key, value in os.environ.items() if key not in SERVER_SECRET_ENV}
    full_env.update(env or {})
    return full_env


class AbstractConnection:
    """
    A symmetric API for rclone_connection to be used locally
    """

    def _execute(self, command, env={}):
        full_env = subprocess_env(env)
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

