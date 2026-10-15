"""
The HEIC/HEIF -> JPEG converter of the image viewer. It is NOT imported by the server:
heic_convert.py runs this file's text (`python -I -c`) in a child process, as the
logged-in user, never as root and never as the service account, because it feeds an
untrusted file to C code (libheif, libde265).

stdin: the HEIC/HEIF file. stdout: a JPEG of its primary image, upright (the HEIF
rotation and mirror transforms, irot/imir, are applied), without any metadata (EXIF, GPS,
XMP, thumbnails, depth maps are all dropped; only the ICC color profile is kept so the
colors stay right). argv[1]: a directory with pi-heif and Pillow (put first on
sys.path; '' = the interpreter's own packages). The exit code says what happened (EXIT_*).

Limits, set in the child before it reads anything (each one to the wanted value or the
lower hard limit it was started with; if one cannot be set nothing is decoded, EXIT_LIMITS):
address space, CPU seconds, no file may be written, few open files, no core dumps; plus a pixel cap checked from the header
before any pixel is decoded, and a cap on the long side of the output.
"""
import io
import sys

EXIT_OK = 0
EXIT_TOO_LARGE = 10 # more pixels than MAX_PIXELS
EXIT_MEMORY = 11 # ran out of memory (RLIMIT_AS)
EXIT_NOT_DECODABLE = 12 # not a HEIC/HEIF image libheif can decode
EXIT_UNAVAILABLE = 13 # pi-heif / Pillow cannot be imported
EXIT_LIMITS = 14 # a resource limit could not be set: nothing is decoded without them

MAX_PIXELS = 100 * 1000 * 1000 # 100 megapixels (an iPhone's 48 MP photo is half of it)
MAX_SIDE = 8192 # px of the longer side of the JPEG
QUALITY = 85
LIMITS = { # resource name -> (soft = hard limit)
    'RLIMIT_AS': 2 * 1024 ** 3,
    'RLIMIT_CPU': 60,
    'RLIMIT_FSIZE': 0,
    'RLIMIT_NOFILE': 64,
    'RLIMIT_CORE': 0,
}


def set_limits(limits=LIMITS):
    """
    Sets each limit on its own, to the wanted value or, if the hard limit the process was
    started with (pam_limits, a container) is lower, to that. Returns the names of the
    limits that could not be applied; main() decodes nothing unless that list is empty.
    """
    try:
        import resource
    except ImportError: # not Linux/Unix: no limits can be set
        return list(limits)
    failed = []
    for name, wanted in limits.items():
        try:
            which = getattr(resource, name)
            hard = resource.getrlimit(which)[1]
            target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
            resource.setrlimit(which, (target, target))
            soft, hard = resource.getrlimit(which)
            if soft > wanted or hard > wanted:
                failed.append(name)
        except (AttributeError, TypeError, ValueError, OSError):
            failed.append(name)
    return failed


def flatten(image):
    """An RGB (or L) image for JPEG: transparency goes onto white, the ICC profile stays"""
    from PIL import Image
    icc = image.info.get('icc_profile')
    if 'A' in image.mode or image.mode == 'P':
        rgba = image.convert('RGBA') # JPEG has no alpha: on white
        image = Image.new('RGB', rgba.size, (255, 255, 255))
        image.paste(rgba, mask=rgba.getchannel('A'))
    elif image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')
    if icc:
        image.info['icc_profile'] = icc
    return image


def convert(data, max_pixels=MAX_PIXELS, max_side=MAX_SIDE, quality=QUALITY):
    """
    JPEG bytes of the primary image of `data` (HEIC/HEIF). Raises ImportError (no
    library), MemoryError, ValueError('too large') or OSError/other for a file that
    cannot be decoded. pi_heif and PIL are imported here, not at module level.
    """
    import pi_heif
    from PIL import Image, ImageOps

    pi_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)
    Image.MAX_IMAGE_PIXELS = max_pixels
    image = Image.open(io.BytesIO(data), formats=['HEIF'])
    width, height = image.size
    if width * height > max_pixels:
        raise ValueError('too large')
    image.load() # the primary image only; libheif applies irot/imir
    image = ImageOps.exif_transpose(image) # a leftover EXIF orientation (once, never twice)
    image = flatten(image)
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    options = {'format': 'JPEG', 'quality': quality}
    icc = image.info.get('icc_profile')
    if icc:
        options['icc_profile'] = icc
    image.save(out, exif=b'', **options)
    return out.getvalue()


def main(argv=None, stdin=None, stdout=None):
    argv = sys.argv if argv is None else argv
    site = argv[1] if len(argv) > 1 else ''
    if site:
        sys.path.insert(0, site)
    if set_limits(): # before anything is read: no limits, no decoding
        return EXIT_LIMITS
    data = (stdin or sys.stdin.buffer).read()
    try:
        jpeg = convert(data)
    except ImportError:
        return EXIT_UNAVAILABLE
    except MemoryError:
        return EXIT_MEMORY
    except ValueError as e:
        return EXIT_TOO_LARGE if str(e) == 'too large' else EXIT_NOT_DECODABLE
    except Exception as e: # unreadable / malformed / unsupported
        if type(e).__name__ == 'DecompressionBombError':
            return EXIT_TOO_LARGE
        # libheif reports a failed allocation (RLIMIT_AS) as an error with this word in it
        return EXIT_MEMORY if 'memory' in str(e).lower() else EXIT_NOT_DECODABLE
    (stdout or sys.stdout.buffer).write(jpeg)
    (stdout or sys.stdout.buffer).flush()
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
