import contextlib
import os
import signal
import subprocess


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


@contextlib.contextmanager
def signals_unblocked():
    """
    Commands started inside this block begin with no blocked signals. A child inherits
    the signal mask of the thread that forks it, and uWSGI blocks almost every signal in
    its worker threads: classic sudo resets its mask, but sudo-rs (Ubuntu 26.04) keeps it,
    never sees its command exit (SIGCHLD) and hangs, and the command (rclone) would ignore
    SIGTERM from Stop. The mask is per thread and restored at once.
    """
    try:
        previous = signal.pthread_sigmask(signal.SIG_SETMASK, [])
    except (AttributeError, OSError, ValueError):
        yield
        return
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def spawn(command, **kwargs):
    """subprocess.Popen, the child starting with no blocked signals (signals_unblocked);
    only the fork runs with the calling thread's signals unblocked"""
    with signals_unblocked():
        return subprocess.Popen(command, **kwargs)


def check_output(command, stderr=None, env=None, timeout=None):
    """subprocess.check_output through spawn(); returns stdout bytes"""
    process = spawn(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, env=env)
    try:
        stdout, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, err)
    return stdout


def run(command, timeout):
    """subprocess.run(capture_output=True, stdin=DEVNULL) through spawn(); raises
    subprocess.TimeoutExpired after killing the command"""
    process = spawn(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def sudo_as(user, extra=None):
    """
    The `sudo` prefix of a command run as `user` with the environment
    user_process_env(extra). Never asks for a password (-n). Only the named variables
    are kept (`--preserve-env=<names>`, which needs the SETENV tag in sudoers when
    Motuz does not run as root): `-E` would keep them all, and sudo-rs, the default
    sudo of Ubuntu 26.04, ignores -E. sudo sets PATH (secure_path), HOME and USER of the
    target user itself; a HOME in `extra` wins.
    """
    names = sorted(name for name in user_process_env(extra) if name != 'PATH')
    command = ['sudo', '-n']
    if names:
        command.append('--preserve-env={}'.format(','.join(names)))
    return command + ['-u', user]


class AbstractConnection:
    """
    A symmetric API for rclone_connection to be used locally
    """

    def _execute(self, command, env={}):
        full_env = user_process_env(env)
        try:
            byteOutput = check_output(command, stderr=subprocess.PIPE, env=full_env)
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

