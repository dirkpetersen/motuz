import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import defaultdict

from .abstract_connection import signals_unblocked, RcloneException, user_process_env


# Width of each hash in rclone's output (an empty hash is as many spaces)
HASH_WIDTHS = {'md5': 32, 'sha256': 64, 'crc32c': 8}
_HEX = re.compile(r'^[0-9a-f]+$')


def parse_line(line, hash_type='md5'):
    """
    {'Name', 'md5chksum'} of a line of `rclone md5sum` / `hashsum <type>` (the hash, 2
    spaces, the file name), None if it is not one. md5chksum is None if the backend has
    no hash for the file (spaces); for SHA-256 and CRC32C also if it is not a hash of
    that width (rclone prints e.g. UNSUPPORTED or ERROR instead).
    """
    line = line.rstrip('\r\n')
    width = HASH_WIDTHS[hash_type]
    if line.startswith(' ' * (width + 2)):
        return {'Name': line[width + 2:], 'md5chksum': None}
    if hash_type == 'md5':
        groups = re.search(r'^(.{32})\s\s(.*)', line) # as ever: any 32 characters
        if groups is None:
            return None
        return {'Name': groups[2], 'md5chksum': groups[1].strip() or None}
    groups = re.search(r'^(\S+)  (.*)', line)
    if groups is None:
        return None
    value = groups[1]
    return {'Name': groups[2], 'md5chksum': value if len(value) == width and _HEX.match(value) else None}


class HashsumJobQueue:
    def __init__(self):
        self._job_status = defaultdict(list) # Mapping from id to list of dict

        self._job_text = {}
        self._job_error_text = defaultdict(str)
        self._job_percent = defaultdict(int)
        self._job_exitstatus = {}

        self._stop_events = {} # Mapping from id to threading.Event
        self._processes = {} # Mapping from id to subprocess.Popen
        self._error_text_lock = threading.Lock()


    def push(self, command, env, job_id, stdin=None, hash_type='md5'):
        """
        Starts `command`; `stdin`: bytes written to its standard input (a
        --files-from-raw list), `hash_type`: the hash it prints (HASH_WIDTHS)
        """
        if hash_type not in HASH_WIDTHS:
            raise KeyError(hash_type)
        if self._job_id_exists(job_id):
            raise KeyError("Job with ID {} already submitted to {}".format(job_id, self.__class__))

        self._stop_events[job_id] = threading.Event()

        try:
            self._execute_interactive(command, env, job_id, stdin, hash_type)
        except subprocess.CalledProcessError as e:
            raise RcloneException(e)

        return job_id


    def hashsum_text(self, job_id):
        return self._job_status[job_id]

    def hashsum_error_text(self, job_id):
        return self._job_error_text[job_id]

    def hashsum_percent(self, job_id):
        return self._job_percent[job_id]

    def hashsum_stop(self, job_id):
        self._stop_events[job_id].set()

    def hashsum_finished(self, job_id):
        return self._stop_events[job_id].is_set()

    def hashsum_exitstatus(self, job_id):
        return self._job_exitstatus.get(job_id, -1)

    def hashsum_delete(self, job_id):
        self.hashsum_stop(job_id)
        del self._job_status[job_id]


    def _job_id_exists(self, job_id):
        return job_id in self._job_status


    def _execute_interactive(self, command, env, job_id, stdin=None, hash_type='md5'):
        thread = threading.Thread(target=self.__execute_interactive, kwargs={
            'command': command,
            'env': env,
            'job_id': job_id,
            'stdin': stdin,
            'hash_type': hash_type,
        })
        thread.daemon = True
        thread.start()


    def __execute_interactive(self, command, env, job_id, stdin=None, hash_type='md5'):
        stop_event = self._stop_events[job_id]
        full_env = user_process_env(env)

        with signals_unblocked():
            process = subprocess.Popen(
                command,
                env=full_env,
                stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True, # Own process group, see terminate_all()
            )
        self._processes[job_id] = process

        # Drain stderr concurrently, otherwise rclone blocks once the pipe buffer is full
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process, job_id),
            daemon=True,
        )
        stderr_thread.start()
        if stdin is not None:
            threading.Thread(target=self._write_stdin, args=(process, stdin), daemon=True).start()

        try:
            self.__read_stdout(process, job_id, hash_type)
        except Exception as e:
            logging.exception(e)
            self._append_error_text(job_id, str(e))
        finally:
            exitstatus = process.wait()
            stderr_thread.join(timeout=10)
            self._job_exitstatus[job_id] = exitstatus
            self._job_percent[job_id] = 100
            logging.info("Hashsum process exited with exit status {}".format(exitstatus))
            stop_event.set()


    @staticmethod
    def _write_stdin(process, data):
        try:
            process.stdin.write(data)
        except (BrokenPipeError, OSError): # rclone exited early: its exit status says why
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def __read_stdout(self, process, job_id, hash_type='md5'):
        stop_event = self._stop_events[job_id]

        while not stop_event.is_set():
            line = process.stdout.readline().decode('utf-8', errors='replace')

            if len(line) == 0:
                if process.poll() is not None:
                    break
                time.sleep(0.5)
                continue

            entry = parse_line(line, hash_type)
            if entry is None:
                logging.info("No match in {}".format(line))
                continue

            self._job_status[job_id].append(entry)


    def terminate_all(self):
        """
        Kill every rclone process group started by this queue (used when the task is revoked)
        """
        for process in self._processes.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


    def _append_error_text(self, job_id, text):
        with self._error_text_lock:
            self._job_error_text[job_id] += text
            self._job_error_text[job_id] += '\n'
            # Restrict size to 10000 characters
            self._job_error_text[job_id] = self._job_error_text[job_id][-10000:]


    def _read_stderr(self, process, job_id):
        for raw_line in process.stderr:
            self._append_error_text(job_id, raw_line.decode('utf-8', errors='replace').strip())

