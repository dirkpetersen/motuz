from collections import defaultdict
import functools
import json
import logging
import re
import subprocess
import threading
import time
import os
import signal

from . import disk_full
from .abstract_connection import signals_unblocked, AbstractConnection, RcloneException, user_process_env

class CopyJobQueue:
    def __init__(self):
        self._job_status = defaultdict(functools.partial(defaultdict, str)) # Mapping from id to status dict

        self._job_text = defaultdict(str)
        self._job_error_text = defaultdict(str)
        self._job_percent = defaultdict(int)
        self._job_exitstatus = {}

        self._stop_events = {} # Mapping from id to threading.Event
        self._processes = {} # Mapping from id to subprocess.Popen
        self._error_text_lock = threading.Lock()
        self._latest_job_id = 0
        self._local_dst = {} # Mapping from id to the local destination path (None: a cloud)
        self._disk_full = {} # Mapping from id to (disk_full.FULL or QUOTA, rclone's last such line)


    def push(self, command, env, job_id, local_dst=None):
        """
        @param local_dst: the destination path if it is on the local filesystem: rclone is
                          stopped as soon as it reports that it is full (utils/disk_full.py)
        """
        if self._job_id_exists(job_id):
            raise KeyError("Job with ID {} already submitted to {}".format(job_id, self.__class__))

        self._stop_events[job_id] = threading.Event()
        self._local_dst[job_id] = local_dst

        try:
            self._execute_interactive(command, env, job_id)
        except subprocess.CalledProcessError as e:
            raise RcloneException(e)

        return job_id


    def copy_text(self, job_id):
        return self._job_text[job_id]

    def copy_error_text(self, job_id):
        full = self._disk_full.get(job_id)
        if full is None:
            return self._job_error_text[job_id]
        name, line = full
        message = disk_full.message(name, self._local_dst[job_id], self._files_done(job_id), line=line)
        return message + '\n\n' + self._job_error_text[job_id]

    def copy_disk_full(self, job_id):
        """disk_full.FULL or QUOTA if the job was stopped because its destination is full"""
        full = self._disk_full.get(job_id)
        return full[0] if full is not None else None

    def copy_percent(self, job_id):
        return self._job_percent[job_id]

    def copy_stop(self, job_id):
        self._stop_events[job_id].set()

    def copy_finished(self, job_id):
        return self._stop_events[job_id].is_set()

    def copy_exitstatus(self, job_id):
        return self._job_exitstatus.get(job_id, -1)



    def _job_id_exists(self, job_id):
        return job_id in self._job_status


    def _execute_interactive(self, command, env, job_id):
        thread = threading.Thread(target=self.__execute_interactive, kwargs={
            'command': command,
            'env': env,
            'job_id': job_id,
        })
        thread.daemon = True
        thread.start()


    def __execute_interactive(self, command, env, job_id):
        stop_event = self._stop_events[job_id]
        full_env = user_process_env(env)

        with signals_unblocked():
            process = subprocess.Popen(
                command,
                env=full_env,
                stdin=subprocess.DEVNULL,
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

        try:
            self.__read_stdout(process, job_id)
        except Exception as e:
            logging.exception(e)
            self._append_error_text(job_id, str(e))
        finally:
            exitstatus = process.wait()
            stderr_thread.join(timeout=10)
            if exitstatus == 0 and job_id in self._disk_full: # Stopped by us, never a success
                exitstatus = 1
            self._job_exitstatus[job_id] = exitstatus
            self._job_percent[job_id] = 100
            logging.info("Copy process exited with exit status {}".format(exitstatus))
            stop_event.set()


    def __read_stdout(self, process, job_id):
        stop_event = self._stop_events[job_id]

        reset_sequence1 = '\x1b[2K\x1b[0' # + 'G'
        reset_sequence2 = '\x1b[2K\x1b[A\x1b[2K\x1b[A\x1b[2K\x1b[A\x1b[2K\x1b[A\x1b[2K\x1b[A\x1b[2K\x1b[A\x1b[2K\x1b[0' # + 'G'

        while not stop_event.is_set():
            line = process.stdout.readline().decode('utf-8', errors='replace')

            if len(line) == 0:
                if process.poll() is not None:
                    break
                time.sleep(0.5)
                continue

            line = line.strip()

            q1 = line.find(reset_sequence1)
            if q1 != -1:
                line = line[q1 + len(reset_sequence1):]

            q2 = line.find(reset_sequence2)
            if q2 != -1:
                line = line[q2 + len(reset_sequence2):]

            line = line.replace(reset_sequence1, '')
            line = line.replace(reset_sequence2, '')
            self._check_disk_full(process, job_id, line)

            match = re.search(r'(ERROR.*)', line)
            if match is not None:
                error = match.groups()[0]
                logging.error(error)
                self._append_error_text(job_id, error)
                continue

            match = re.search(r'([A-Za-z ]+):\s*(.*)', line)
            if match is None:
                logging.info("No match in {}".format(line))
                continue

            key, value = match.groups()
            key = key.strip()
            if key == 'Transferred' and 'B/s' in value:
                # rclone prints two "Transferred" lines: bytes (with a rate) and file counts
                key = 'Transferred0'
            self._job_status[job_id][key] = value
            self.__process_copy_status(job_id)

        self.__process_copy_status(job_id)


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
            line = raw_line.decode('utf-8', errors='replace').strip()
            self._append_error_text(job_id, line)
            self._check_disk_full(process, job_id, line)


    def _check_disk_full(self, process, job_id, line):
        """
        A full local destination ends the job at once: rclone would fail every following
        file and retry them all (--retries). It is stopped like Stop does (its process
        group), and copy_error_text starts with the reason.
        """
        if self._local_dst.get(job_id) is None:
            return
        name = disk_full.kind(line)
        if name is None:
            return
        with self._error_text_lock:
            first = job_id not in self._disk_full
            self._disk_full[job_id] = (name, disk_full.clean(line)) # The last such line
        if not first:
            return
        logging.warning("Copy job %s: the local destination is full (%s), stopping rclone", job_id, name)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


    def _files_done(self, job_id):
        """The files rclone has copied so far (its "Transferred: 3 / 10, 30%" line)"""
        match = re.match(r'\s*(\d+)\s*/', self._job_status[job_id].get('Transferred', ''))
        return int(match[1]) if match is not None else 0


    def __process_copy_status(self, job_id):
        self.__process_copy_text(job_id)
        self.__process_copy_percent(job_id)


    def __process_copy_text(self, job_id):
        headers = [
            'GTransferred',
            'Errors',
            'Checks',
            'Transferred0',
            'Transferred',
            'Elapsed time',
            'Transferring',
        ]

        status = self._job_status[job_id]

        outliers = []
        for key in status.keys():
            if key not in headers:
                outliers.append(key)

        if len(outliers) == 1:
            value = status[outliers[0]]
            try:
                pos = value.index("Transferred:")
                transferring, transferred = value[:pos], value[pos:]
                transferring.replace("Transferring:", "")
                transferred.replace("Transferred:", "")
                transferring = transferring.strip()
                transferred = transferred.strip()
                status['Transferred0'] = transferred
                status['Transferring'] = transferring
                del status[outliers[0]]
            except ValueError:
                pass

            status['Transferred0'] = status['Transferred0'].replace("Transferred:", "").strip()

        if status['Transferred'] == "1 / 1, 100%":
            del status['Transferring']


        text = '\n'.join(
            '{:>12}: {}'.format(header, status[header])
            for header in headers
            if status[header]
        )
        text = text.replace("Transferred0", " Transferred")
        self._job_text[job_id] = text


    def __process_copy_percent(self, job_id):
        status = self._job_status[job_id]

        for key in ('GTransferred', 'Transferred0'):
            match = re.search(r'(\d+)\%', status[key])
            if match is not None:
                self._job_percent[job_id] = int(match[1])
                return
        # No percentage in this update, keep the last known value
