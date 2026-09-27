"""
Fixtures of the image and Markdown viewer, written into the folder given as argv[1] (run
as the user, e.g. `sudo -u alice python3 - /home/alice/viewer < viewer_fixtures.py`);
used by e2e_test.py (local files), cred_test.py (uploaded to Azurite) and ui/ui_test.mjs.
Stdlib only (the app image has no PIL): the PNG is generated, the small JPEG, GIF and
WebP were made once with PIL (64 x 48).
"""
import base64
import os
import struct
import sys
import zlib

PNG_WIDTH, PNG_HEIGHT = 2400, 1500

JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/'
    '2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAAwAEADASIAAhEBAxEB/8QA'
    'HwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
    'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG'
    'x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC'
    'AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE'
    'hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDFoor1GvRz'
    'jOP7N5Pc5ua/W1rW8n3PKwOB+t83vWtbpfe/meXUV6jRXhf64f8ATj/yb/7U9H+w/wDp5+H/AATy6ivUa841T/kLXn/Xd/8A0I17WUZ3/aU5Q9ny2V97/ojg'
    'xuX/AFWKlzXv5W/Uq0UUV9AeYFeo15dXo/8Aamnf8/8Abf8Af5f8a+J4tpTqex5It/Ft/wBun0GSzjH2nM7bfqWqKq/2pp3/AD/23/f5f8ahvNas4LSSWG6g'
    'lkVfkRZAxJ7cZ6V8SsLXbtyP7mfRRqQk1FNXfmSX+q2mnACdyXIyI1GWI/z6+lcLebbi9nnQkLLIzgEcgE5p000lxM00zl3c5LGmV9Jl8Z4G8qctXuejUyvD'
    '1opVlzW82vysV2jZeT0ptWqryLtbA6V9hl2YvEN06i94+RznJo4OKrUX7uzT6f8AAG0UUV7B80FOQ7XBptFTOCnFwez0NKVSVKpGpHdNP7i1RUCSFeOop/nL'
    '6Gvka2V4inK0Vddz9FwufYOtBOcuWXVP/MkqCRgz8dqVpSwwBgVHXq5bl86EvaVd+x8/necU8VBUKGsd2+/of//Z')
GIF = base64.b64decode(
    'R0lGODdhQAAwAIEAAPrIKB5uyNw8PAAAACwAAAAAQAAwAEAI0gADCBxIsKDBgwgTKlxoEIDDhxAjSpwYkaHFhhQzaqx4sWOAjSAfChhJsqTJkx5TGjzJsmVL'
    'lTAFupw5MqbNmypD6pSIE+HOnw5Z9gQqkabRkj0tHl2a9KbQplCjSp26kOhOqlZ1Ys0KcivXiEtJwvwaNKzLjl/NHqVKUK1RtgXdPoWLUC3du3jz6t3Lt+9e'
    'shq9AuY5dTBFwYYfIk4MYHFix2TXxkwsNydguUjRpsWM0uJmzp3hgp7LdnTo0qYz001dU+9ovwEqw277drbt2wUDAgA7')
WEBP = base64.b64decode(
    'UklGRloBAABXRUJQVlA4IE4BAABwCwCdASpAADAAPrVGn0qnI6KhsrJMyOAWiWwAvc2qfsH4Z7Ip0D8OebX1c8I4aziA5iXcAfsRvgH65dYB6AHSWfuF6IAT'
    'wAFlwOBO1vVE91roLMqPySZguao9mbzdG8FQ37AAAP7Kzn+wL9OW9Q6WYmeQoUBAFwqU8LxsKf5L50+v/IP1ruwcysX8wO1/U83hiZz/Bx2QPZjBCcd4YKm1'
    'ED8wgMLTS+RSCFPaLiacS7H454adFs4iI8kNEBduhPnNYNjj5OFbZyqzGx1a52UlWIM9+n0Vv+hYjBTJQIOVA42vtVJp+dVzj9ncj+dBYyDp+EDoxv3zAGdA'
    'udb0Nhgei+pDiQou7AjjHN0L5n7mnI3T3cAvEVCthowgpil19Dv1C2H9xqhj31O3Z8a62Lvhq3OhENJNCDxNRd4Om0TfU52S6t6lYX42T61n+7AA')

SVG = (b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
       b'<script>window.__svgXss = 1</script><rect width="10" height="10"/></svg>\n')

README = r"""# Viewer test heading

Some **bold** text, a [safe link](https://example.org/docs), a
[script link](javascript:window.__mdXss=3) and a [relative link](other.md).

| Name  | Value |
|-------|------:|
| alpha | 1     |
| beta  | 2     |

<script>window.__mdXss = 1</script>

<img src="x" onerror="window.__mdXss = 2">

Tracker: ![remote tracker](https://tracker.invalid/pixel.png)

![local picture](pic.png)

![vector logo](logo.svg)

```python
print("hello from a code block")
```

- [x] done item
- [ ] open item

> A quote with `inline code`.

A footnote reference[^1].

[^1]: The footnote.
"""


def png(width, height):
    """An RGB PNG: a gradient with a grid, compressed with zlib (stdlib only)"""
    rows = []
    for y in range(height):
        row = bytearray(b'\x00') # filter: none
        g = 40 + 160 * y // height
        for x in range(width):
            grid = x % 100 < 2 or y % 100 < 2
            row += bytes((20, 20, 20)) if grid else bytes((60 + 180 * x // width, g, 200 - 120 * x // width))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 9))
            + chunk(b'IEND', b''))


def fixtures():
    """{file name: contents}"""
    return {
        'pic.png': png(PNG_WIDTH, PNG_HEIGHT),
        'small.png': png(40, 30),
        'pic.jpg': JPEG,
        'PIC2.JPEG': JPEG,
        'pic.gif': GIF,
        'pic.webp': WEBP,
        'fake.png': b'this is text, not a PNG\n',
        'logo.svg': SVG,
        'svg-named.png': SVG,
        # 3 MiB, above the e2e stack's MOTUZ_VIEW_IMAGE_MAX_BYTES=2M
        'big.png': b'\x89PNG\r\n\x1a\n' + b'\x00' * (3 * 1024 * 1024),
        'README.md': README.encode(),
    }


def main(folder):
    os.makedirs(folder, exist_ok=True)
    for name, data in fixtures().items():
        with open(os.path.join(folder, name), 'wb') as f:
            f.write(data)


if __name__ == '__main__':
    main(sys.argv[1])
