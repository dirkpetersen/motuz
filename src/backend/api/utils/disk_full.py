"""
A full local destination in rclone's output: a copy job (utils/copy_job_queue.py, also on
remote workers) or an upload from the browser (managers/upload_manager.py) to the local
filesystem ends at once with a clear message instead of rclone failing every following
file and retrying (--retries, low-level retries). Stdlib only.

rclone (local backend) logs Go's error text of the failed write, at the end of the line:
    NOTICE: f.bin: Removing partially written file on error: write /dst/f.bin: no space left on device
    ERROR : f.bin: Failed to copy: write /dst/f.bin: no space left on device
    ERROR : f.bin: Post request put error: write /dst/f.bin: no space left on device   (rcat)
and, when fallocate finds too little space for a file (lib/file/preallocate_unix.go):
    ERROR : f.bin: Failed to copy: preallocate: file too big for remaining disk space
EDQUOT is "disk quota exceeded". Only the end of a line is matched: file names are at
the start of rclone's lines.

RCLONE_LOCAL_FATAL_IF_NO_SPACE (FATAL_ENV) makes ENOSPC a fatal error in rclone >= 1.75.1
(exit status 7, no retries); an older rclone ignores the variable (a flag would make it
fail), and Motuz stops rclone itself anyway when it sees one of these lines.
"""
import re


FULL = 'full'
QUOTA = 'quota'

FATAL_ENV = {'RCLONE_LOCAL_FATAL_IF_NO_SPACE': 'true'}

_PATTERNS = (
    (FULL, re.compile(r': no space left on device\s*$', re.IGNORECASE)),
    (FULL, re.compile(r'preallocate: file too big for remaining disk space\s*$', re.IGNORECASE)),
    (QUOTA, re.compile(r': disk quota exceeded\s*$', re.IGNORECASE)),
)
# rclone's log prefix (2026/10/18 08:51:46 [.123456])
_TIMESTAMP = re.compile(r'^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d(\.\d+)?\s+')
_LINE_CHARS = 1000


def kind(line):
    """FULL or QUOTA if rclone's output `line` says the destination is full, else None"""
    for name, pattern in _PATTERNS:
        if pattern.search(line):
            return name
    return None


def scan(text):
    """(kind, line) of the last line of `text` that says the destination is full, or None"""
    for line in reversed(text.splitlines()):
        name = kind(line)
        if name is not None:
            return name, clean(line)
    return None


def clean(line):
    """rclone's line without its timestamp, shortened"""
    return _TIMESTAMP.sub('', line.strip())[-_LINE_CHARS:]


def message(name, path, done, copied='copied', line=None):
    """
    The error of a job whose destination `path` is full (`name`: FULL or QUOTA) after
    `done` files were `copied` (or 'uploaded'); `line` is rclone's error line
    """
    files = '1 file was' if done == 1 else '{} files were'.format(done)
    if name == QUOTA:
        text = ('Your disk quota on the destination filesystem is exceeded (disk quota exceeded) at {}: '
                '{} {} before. Free space, ask for a larger quota or choose another destination.')
    else:
        text = ('The destination filesystem is full (no space left on device) at {}: '
                '{} {} before. Free space or choose another destination.')
    text = text.format(path, files, copied)
    if line:
        text += '\nrclone: {}'.format(line)
    return text
