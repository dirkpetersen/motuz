"""
Image viewer: POST /api/system/files/view/image/ returns the bytes of a PNG, JPEG, GIF,
WebP, HEIC/HEIF or AVIF image, read as the logged-in user like the text viewer
(file_view.py): LocalConnection.view_image / RcloneConnection.view_image.

The type is decided on the file's first bytes (magic numbers; HEIC/HEIF/AVIF from the
ISO-BMFF 'ftyp' box's brands), never on its name or on what a cloud reports. Anything
else is refused with 415, SVG in particular: an SVG document can carry scripts, and one
opened from a blob: URL would run in the app's origin. The size is checked before
reading (stat) and again on what was read, against MOTUZ_VIEW_IMAGE_MAX_BYTES (413
above it). The response headers keep a browser from treating the bytes as anything but
the detected image. Image contents are never logged.

Motuz sends HEIC/HEIF/AVIF bytes as detected and lets the browser decide whether it can
show them; it never decodes or transcodes them server- or client-side. AVIF (AV1) is
royalty-free and decodes natively in current Chrome, Firefox and Safari. HEIC/HEIF
(HEVC/H.265) decodes natively only where the OS provides an HEVC decoder (currently
Safari/macOS/iOS); Motuz deliberately does not bundle a software HEVC decoder (HEVC is
patent pooled across MPEG-LA, Access Advance and Velos Media, unlike AV1 — see README
"File viewer"), so in a browser without native HEIC support the viewer shows a message
instead of the picture.
"""
import os
import re
import unicodedata
from urllib.parse import quote

from . import file_view


MiB = 1024 * 1024
DEFAULT_MAX_BYTES = 25 * MiB
MIN_MAX_BYTES = 1024
MAX_MAX_BYTES = 256 * MiB # the whole image is held in memory by an app worker
ENV_MAX_BYTES = 'MOTUZ_VIEW_IMAGE_MAX_BYTES'

# The types the viewer shows: raster formats a browser can decode in an <img>. PNG,
# JPEG, GIF and WebP decode everywhere. HEIC/HEIF (Apple's photo format, HEVC/H.265
# coded) and AVIF (AV1 coded) are also served as their detected type — Motuz never
# decodes or transcodes them itself, so whether one is actually shown depends entirely
# on the browser: AVIF (royalty-free under AOM's patent cross-license) decodes natively
# in current Chrome, Firefox and Safari; HEIC/HEIF decodes natively only where the OS
# provides an HEVC decoder (currently Safari/macOS/iOS). Motuz deliberately does not
# bundle a software HEVC decoder (see README "File viewer" and CLAUDE.md): HEVC is
# patent pooled (MPEG-LA, Access Advance, Velos Media) and shipping a decoder would be
# a licensing decision for the operator, not something to guess at in code.
PNG = 'image/png'
JPEG = 'image/jpeg'
GIF = 'image/gif'
WEBP = 'image/webp'
HEIC = 'image/heic'
HEIF = 'image/heif'
AVIF = 'image/avif'
TYPES = (PNG, JPEG, GIF, WEBP, HEIC, HEIF, AVIF)
SNIFF_BYTES = 64 # enough for an ISO-BMFF 'ftyp' box's major and compatible brands

# Sent with every image: the type is the detected one and must not be sniffed, the
# bytes can never run anything (a sandboxed, empty policy even if opened as a page),
# and nothing is cached (the contents are the user's, read with their permissions)
BASE_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; sandbox",
    'Cache-Control': 'no-store',
    'Cross-Origin-Resource-Policy': 'same-origin',
}


class ImageConfigError(ValueError):
    """An invalid MOTUZ_VIEW_IMAGE_MAX_BYTES; stops the app at startup"""


class TooLargeError(file_view.ViewError):
    status = 413


class NotAnImageError(file_view.ViewError):
    """Not PNG, JPEG, GIF, WebP, HEIC/HEIF or AVIF (SVG included)"""
    status = 415


_SIZE_RE = re.compile(r'([0-9]{1,12})\s*([KMG]i?B?|B)?', re.IGNORECASE)
_UNITS = {'': 1, 'B': 1, 'K': 1024, 'M': MiB, 'G': 1024 * MiB}


def parse_size_setting(value, env, default, minimum, maximum, error=ImageConfigError):
    """
    A size setting from the environment (`env`, for the messages): bytes, or with a
    binary unit (25M, 25MiB, 512K). Unset or empty: `default`. Raises `error` for a
    value that is not a size or not between `minimum` and `maximum`.
    """
    value = (value or '').strip()
    if not value:
        return default
    match = _SIZE_RE.fullmatch(value)
    if not match:
        raise error('{} must be a size in bytes, e.g. {} or {}, not {!r}'.format(
            env, default, describe_size(default).replace(' MiB', 'M').replace('.0', ''), value))
    number, unit = match.groups()
    size = int(number) * _UNITS[(unit or '')[:1].upper()]
    if not minimum <= size <= maximum:
        raise error('{} must be between {} and {} bytes ({}), not {}'.format(
            env, minimum, maximum, describe_size(maximum).replace(' MiB', 'M').replace('.0', ''), value))
    return size


def parse_max_bytes(value):
    """
    The image size cap from MOTUZ_VIEW_IMAGE_MAX_BYTES: bytes, or with a binary unit
    (25M, 25MiB, 512K). Unset or empty: DEFAULT_MAX_BYTES. Raises ImageConfigError.
    """
    return parse_size_setting(value, ENV_MAX_BYTES, DEFAULT_MAX_BYTES, MIN_MAX_BYTES, MAX_MAX_BYTES)


def max_bytes(environ=None):
    environ = os.environ if environ is None else environ
    return parse_max_bytes(environ.get(ENV_MAX_BYTES))


def detect_type(data):
    """The image type of `data` by its magic number (PNG, JPEG, GIF, WebP, HEIC/HEIF, AVIF), or None"""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return PNG
    if data.startswith(b'\xff\xd8\xff'):
        return JPEG
    if data.startswith((b'GIF87a', b'GIF89a')):
        return GIF
    if len(data) >= 16 and data[:4] == b'RIFF' and data[8:12] == b'WEBP' and data[12:16] in (b'VP8 ', b'VP8L', b'VP8X'):
        return WEBP
    return _isobmff_type(data)


# ISO-BMFF ('ftyp' box) brand codes that identify a HEIC/HEIF/AVIF file (ISO/IEC
# 14496-12 + 23008-12 for HEIF, and the AVIF spec). Checked against the major brand and
# every compatible brand, never the file's name: an HEIC file's major brand is often
# the generic 'mif1'/'msf1' with 'heic' only listed as a compatible brand.
_HEIC_BRANDS = frozenset((b'heic', b'heix', b'heim', b'heis', b'hevc', b'hevx'))
_HEIF_BRANDS = frozenset((b'mif1', b'msf1'))
_AVIF_BRANDS = frozenset((b'avif', b'avis'))


def _isobmff_brands(data):
    """(major_brand, [compatible_brands]) of a leading ISO-BMFF 'ftyp' box, or (None, [])"""
    if len(data) < 12 or data[4:8] != b'ftyp':
        return None, []
    major = bytes(data[8:12])
    box_size = int.from_bytes(data[0:4], 'big')
    # Compatible brands are 4-byte codes after the major brand + minor version (from
    # offset 16), up to the box's declared size or what we have, whichever is smaller;
    # a size that looks wrong (0, the 64-bit-extended marker 1, or past what we read)
    # is ignored in favor of scanning everything we were given.
    end = box_size if 16 <= box_size <= len(data) else len(data)
    compatible = [bytes(data[i:i + 4]) for i in range(16, end - 3, 4)]
    return major, compatible


def _isobmff_type(data):
    """'image/heic', 'image/heif', 'image/avif' or None, from the ftyp box's brands"""
    major, compatible = _isobmff_brands(data)
    if major is None:
        return None
    brands = {major, *compatible}
    if brands & _AVIF_BRANDS:
        return AVIF
    if brands & _HEIC_BRANDS:
        return HEIC
    if brands & _HEIF_BRANDS:
        return HEIF
    return None


def _looks_like_svg(data):
    head = data[:1024].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    return head.startswith((b'<svg', b'<?xml', b'<!doctype svg', b'<!--')) and b'<svg' in data[:65536].lower()


def describe_size(size):
    if size >= MiB:
        return '{:.1f} MiB'.format(size / MiB)
    if size >= 1024:
        return '{:.0f} KiB'.format(size / 1024)
    return '{} bytes'.format(size)


def too_large(name, size, cap, what='images'):
    return TooLargeError("'{}' is {}; the viewer shows {} up to {}".format(
        name, describe_size(size), what, describe_size(cap)))


def check_size(name, size, cap, what='images'):
    """Raises TooLargeError (413) when a file of `size` bytes (None: unknown) exceeds the cap"""
    if size is not None and size > cap:
        raise too_large(name, size, cap, what)


def image_result(name, data, size, cap):
    """
    (bytes, type) of an image read with a limit of cap + 1 bytes. Raises TooLargeError
    when more than `cap` bytes arrived (the file grew since the stat) and
    NotAnImageError (415) for anything but PNG, JPEG, GIF, WebP, HEIC/HEIF or AVIF.
    """
    if len(data) > cap:
        raise too_large(name, max(len(data), size or 0), cap)
    mime = detect_type(data[:SNIFF_BYTES])
    if mime is None:
        if _looks_like_svg(data):
            raise NotAnImageError("'{}' is an SVG image; SVG is shown as text, not as an image".format(name))
        raise NotAnImageError("'{}' is not a PNG, JPEG, GIF, WebP, HEIC/HEIF or AVIF image".format(name))
    return data, mime


def safe_filename(path):
    """
    The file name for Content-Disposition: the last path component with control
    characters, quotes, backslashes and separators replaced. Returns (ASCII fallback,
    RFC 5987 encoded UTF-8 name).
    """
    name = str(path).rstrip('/').split('/')[-1] or 'image'
    name = ''.join('_' if unicodedata.category(c).startswith('C') or c in '"\\/;' else c for c in name)[:200]
    name = name.strip() or 'image'
    ascii_name = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    ascii_name = re.sub(r'[^A-Za-z0-9._ ()+,=@-]', '_', ascii_name).strip() or 'image'
    return ascii_name, quote(name, safe='')


def response_headers(path, mime):
    """Headers of an image response (the type was detected by detect_type)"""
    if mime not in TYPES:
        raise ValueError('not an image type the viewer serves')
    ascii_name, utf8_name = safe_filename(path)
    headers = dict(BASE_HEADERS)
    headers['Content-Type'] = mime
    headers['Content-Disposition'] = 'inline; filename="{}"; filename*=UTF-8\'\'{}'.format(ascii_name, utf8_name)
    return headers
