"""
Document viewer: POST /api/system/files/view/document/ returns the bytes of a PDF,
Office Open XML / OpenDocument (ZIP) or legacy Office (OLE2) file, read as the
logged-in user like the image viewer (image_view.py) and the pager (file_view.py):
LocalConnection.view_document / RcloneConnection.view_document. The browser renders
them (pdf.js, docx-preview, SheetJS, the PPTX outline); the server never converts or
interprets a document.

Two kinds of request:
- the whole file (no `offset`/`length`), for DOCX, XLSX, PPTX, ODS and XLS: refused
  with 413 above MOTUZ_VIEW_DOCUMENT_MAX_BYTES, checked by stat before reading and on
  what was read (at most cap + 1 bytes);
- a byte range (`offset` and `length`, at most RANGE_MAX_BYTES), for PDFs only: pdf.js
  reads the parts it needs, so a PDF of any size opens quickly and is never read whole.

The container type is decided on the file's first bytes (magic numbers), for every
request, never on its name: `%PDF-` in the first 1 KiB, `PK\\x03\\x04` (ZIP) or the OLE2
signature; anything else is 415. Which Office format a ZIP holds is checked by the
browser ([Content_Types].xml) before it renders anything. The response headers are
the image viewer's (nosniff, `Content-Security-Policy: default-src 'none'; sandbox`,
no-store), with a generic type per container. File contents are never logged.
"""
import os

from . import file_view
from . import image_view


MiB = 1024 * 1024
DEFAULT_MAX_BYTES = 50 * MiB
MIN_MAX_BYTES = 1024
MAX_MAX_BYTES = 512 * MiB # a whole document is held in memory by an app worker
ENV_MAX_BYTES = 'MOTUZ_VIEW_DOCUMENT_MAX_BYTES'

# One range request of the PDF viewer (utils/pdfRanges.js RANGE_MAX_BYTES)
RANGE_MAX_BYTES = 4 * MiB
# PDF readers accept the header anywhere in the first 1024 bytes
HEAD_BYTES = 1024

PDF = 'pdf'
ZIP = 'zip'
CFB = 'cfb'
CONTAINERS = (PDF, ZIP, CFB)
CONTENT_TYPES = {
    PDF: 'application/pdf',
    ZIP: 'application/zip',
    CFB: 'application/x-cfb',
}
_CFB_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


class DocumentConfigError(ValueError):
    """An invalid MOTUZ_VIEW_DOCUMENT_MAX_BYTES; stops the app at startup"""


class NotADocumentError(file_view.ViewError):
    """Not a PDF, ZIP or OLE2 file (or a range of something that is not a PDF)"""
    status = 415


def parse_max_bytes(value):
    """
    The document size cap from MOTUZ_VIEW_DOCUMENT_MAX_BYTES: bytes, or with a binary
    unit (50M, 50MiB). Unset or empty: DEFAULT_MAX_BYTES. Raises DocumentConfigError.
    """
    return image_view.parse_size_setting(value, ENV_MAX_BYTES, DEFAULT_MAX_BYTES, MIN_MAX_BYTES, MAX_MAX_BYTES,
                                         error=DocumentConfigError)


def max_bytes(environ=None):
    environ = os.environ if environ is None else environ
    return parse_max_bytes(environ.get(ENV_MAX_BYTES))


def detect_container(head):
    """'pdf', 'zip' or 'cfb' by the first bytes of a file, or None"""
    if b'%PDF-' in head[:HEAD_BYTES]:
        return PDF
    if head.startswith(b'PK\x03\x04'):
        return ZIP
    if head.startswith(_CFB_SIGNATURE):
        return CFB
    return None


class DocumentRequest:
    """A validated request: the whole file, or `length` bytes from `offset` (a PDF range)"""
    def __init__(self, offset=None, length=None):
        self.offset = offset
        self.length = length

    @property
    def is_range(self):
        return self.offset is not None


def _int(data, name, minimum, maximum=None):
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise file_view.ViewError("'{}' must be an integer".format(name))
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise file_view.ViewError("'{}' must be at least {}".format(name, minimum))
        raise file_view.ViewError("'{}' must be between {} and {}".format(name, minimum, maximum))
    return value


def parse_document_request(data):
    """DocumentRequest from the request JSON; file_view.ViewError (400) for invalid parameters"""
    if not isinstance(data, dict):
        raise file_view.ViewError('Invalid request')
    offset = _int(data, 'offset', 0)
    length = _int(data, 'length', 1, RANGE_MAX_BYTES)
    if (offset is None) != (length is None):
        raise file_view.ViewError("Use 'offset' and 'length' together (a range), or neither (the whole file)")
    return DocumentRequest(offset, length)


def too_large(name, size, cap):
    return image_view.too_large(name, size, cap, 'documents')


def check_size(name, size, cap):
    image_view.check_size(name, size, cap, 'documents')


def check_range(name, request, size):
    """ViewError (400) for a range that starts at or after the end of the file"""
    if size is not None and request.offset >= size:
        raise file_view.ViewError('Offset {} is beyond the end of the file ({} bytes)'.format(request.offset, size))


def _not_a_document(name):
    return NotADocumentError("'{}' is not a PDF, Office or OpenDocument file".format(name))


def document_result(name, data, size, cap):
    """
    (bytes, container) of a whole file read with a limit of cap + 1 bytes: 413 when
    more than `cap` bytes arrived (the file grew since the stat), 415 for anything
    but a PDF, ZIP or OLE2 file.
    """
    if len(data) > cap:
        raise too_large(name, max(len(data), size or 0), cap)
    container = detect_container(data[:HEAD_BYTES])
    if container is None:
        raise _not_a_document(name)
    return data, container


def range_result(name, head, data):
    """(bytes, 'pdf') of a range whose file starts with `head`; 415 unless it is a PDF"""
    container = detect_container(head)
    if container is None:
        raise _not_a_document(name)
    if container != PDF:
        raise NotADocumentError("'{}' is not a PDF; only PDFs are read in ranges".format(name))
    return data, container


def response_headers(path, container, size, start):
    """Headers of a document response; `size` is the whole file's, `start` the range's offset"""
    if container not in CONTAINERS:
        raise ValueError('not a container the viewer serves')
    ascii_name, utf8_name = image_view.safe_filename(path)
    headers = dict(image_view.BASE_HEADERS)
    headers['Content-Type'] = CONTENT_TYPES[container]
    # The browser fetches the bytes (never navigates to them): attachment, not inline
    headers['Content-Disposition'] = 'attachment; filename="{}"; filename*=UTF-8\'\'{}'.format(ascii_name, utf8_name)
    headers['X-Motuz-Document-Type'] = container
    headers['X-Motuz-File-Size'] = str(int(size))
    headers['X-Motuz-Range-Start'] = str(int(start))
    return headers
