"""
Fixtures of the document viewer (PDF, DOCX, XLSX, PPTX), written into the folder given
as argv[1] (run as the user, e.g. `sudo -u alice python3 - /home/alice/docs <
document_fixtures.py`); used by e2e_test.py (local files), cred_test.py (uploaded to
Azurite) and ui/ui_test.mjs. Stdlib only (the app image has no document libraries): the
files are minimal but valid, written by hand, and carry the hostile parts the viewer
must neutralize (PDF JavaScript, javascript: links, external pictures, an HTML altChunk,
zip bombs).
"""
import io
import os
import struct
import sys
import zipfile
import zlib

MiB = 1024 * 1024
PDF_PAGES = 5
NEEDLE = 'motuz-needle'


# ---- PDF ----

def _pdf_string(text):
    return '(' + text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ')'


def pdf(pages=PDF_PAGES, padding=0, hostile=True):
    """
    A PDF with `pages` pages ("Page N of M" in Helvetica). Page 1 has link annotations:
    https (kept), a javascript: URI, a JavaScript action and a Launch action (all three
    must not become links), and an internal link to page 3; page 2 contains NEEDLE
    (find). The document opens with a JavaScript OpenAction that must never run.
    `padding` bytes of an unreferenced stream make the file large (range loading).
    """
    objects = {}
    page_ids = [10 + 2 * i for i in range(pages)]
    kids = ' '.join('{} 0 R'.format(n) for n in page_ids)
    catalog = '<< /Type /Catalog /Pages 2 0 R'
    if hostile:
        catalog += ' /OpenAction 3 0 R /Names << /JavaScript << /Names [(init) 3 0 R] >> >>'
    objects[1] = catalog + ' >>'
    objects[2] = '<< /Type /Pages /Kids [{}] /Count {} >>'.format(kids, pages)
    objects[3] = '<< /S /JavaScript /JS {} >>'.format(_pdf_string('window.__motuzPdfJs = "openaction"; app.alert(1);'))
    objects[4] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
    annots = []
    if hostile:
        links = [
            ((72, 600, 320, 624), '/A << /S /URI /URI (https://example.org/motuz-pdf) >>'),
            ((72, 560, 320, 584), '/A << /S /URI /URI {} >>'.format(_pdf_string('javascript:window.__motuzPdfJs="uri"'))),
            ((72, 520, 320, 544), '/A << /S /JavaScript /JS {} >>'.format(_pdf_string('window.__motuzPdfJs="action"'))),
            ((72, 480, 320, 504), '/A << /S /Launch /F (calc.exe) >>'),
            ((72, 440, 320, 464), '/Dest [{} 0 R /XYZ 0 792 0]'.format(page_ids[min(2, pages - 1)])),
        ]
        for i, ((x1, y1, x2, y2), action) in enumerate(links):
            objects[5 + i] = '<< /Type /Annot /Subtype /Link /Rect [{} {} {} {}] /Border [0 0 1] {} >>'.format(
                x1, y1, x2, y2, action)
            annots.append('{} 0 R'.format(5 + i))
    for index, page_id in enumerate(page_ids):
        number = index + 1
        lines = ['Page {} of {}'.format(number, pages)]
        if number == 1 and hostile:
            lines += ['', 'Safe link: https://example.org/motuz-pdf', 'javascript: URI link', 'JavaScript action link',
                      'Launch action link', 'Go to page 3']
        if number == 2:
            lines += ['', 'Find this word: {}'.format(NEEDLE)]
        text = ['BT', '/F1 24 Tf', '72 700 Td', '28 TL']
        for line in lines:
            text.append('{} Tj T*'.format(_pdf_string(line)))
        text.append('ET')
        stream = '\n'.join(text).encode('latin-1')
        objects[page_id + 1] = stream
        page = ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> '
                '/Contents {} 0 R'.format(page_id + 1))
        if number == 1 and annots:
            page += ' /Annots [{}]'.format(' '.join(annots))
        if number == 1 and hostile:
            page += ' /AA << /O 3 0 R >>'
        objects[page_id] = page + ' >>'
    if padding:
        objects[9] = b'\x00' * padding # never referenced

    out = io.BytesIO()
    out.write(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
    offsets = {}
    for number in sorted(objects):
        offsets[number] = out.tell()
        body = objects[number]
        out.write('{} 0 obj\n'.format(number).encode())
        if isinstance(body, bytes):
            out.write('<< /Length {} >>\nstream\n'.format(len(body)).encode() + body + b'\nendstream')
        else:
            out.write(body.encode('latin-1'))
        out.write(b'\nendobj\n')
    size = max(objects) + 1
    xref = out.tell()
    out.write('xref\n0 {}\n'.format(size).encode())
    out.write(b'0000000000 65535 f \n')
    for number in range(1, size):
        if number in offsets:
            out.write('{:010d} 00000 n \n'.format(offsets[number]).encode())
        else:
            out.write(b'0000000000 65535 f \n')
    out.write('trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n'.format(size, xref).encode())
    return out.getvalue()


# ---- ZIP based (OOXML) ----

def _zip(files, compression=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression) as z:
        for name, data in files.items():
            z.writestr(name, data.encode('utf-8') if isinstance(data, str) else data)
    return out.getvalue()


def png(width=64, height=48):
    """An RGB gradient PNG (stdlib only)"""
    rows = b''.join(b'\x00' + b''.join(bytes((30 + 3 * x, 60 + 3 * y, 200 - 2 * x)) for x in range(width))
                    for y in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


PKG_RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PIC = 'http://schemas.openxmlformats.org/drawingml/2006/picture'
WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'


def _content_types(overrides, defaults=()):
    items = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
             '<Default Extension="xml" ContentType="application/xml"/>',
             '<Default Extension="png" ContentType="image/png"/>']
    items += ['<Default Extension="{}" ContentType="{}"/>'.format(e, t) for e, t in defaults]
    items += ['<Override PartName="{}" ContentType="{}"/>'.format(p, t) for p, t in overrides]
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' + ''.join(items) + '</Types>')


def _rels(rels):
    items = []
    for rid, kind, target, external in rels:
        items.append('<Relationship Id="{}" Type="{}/{}" Target="{}"{}/>'.format(
            rid, REL, kind, target, ' TargetMode="External"' if external else ''))
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{}">{}</Relationships>'.format(
        PKG_RELS, ''.join(items))


def _root_rels(main):
    return _rels([('rId1', 'officeDocument', main, False)])


def _w_run(text, bold=False):
    props = '<w:rPr><w:b/></w:rPr>' if bold else ''
    return '<w:r>{}<w:t xml:space="preserve">{}</w:t></w:r>'.format(props, text)


def _w_para(content, style=None):
    props = '<w:pPr><w:pStyle w:val="{}"/></w:pPr>'.format(style) if style else ''
    return '<w:p>{}{}</w:p>'.format(props, content)


def _w_image(rid, attr='r:embed', name='picture'):
    emu_w, emu_h = 64 * 9525 * 3, 48 * 9525 * 3
    return ('<w:r><w:drawing><wp:inline><wp:extent cx="{w}" cy="{h}"/><wp:docPr id="1" name="{n}"/>'
            '<a:graphic><a:graphicData uri="{pic}"><pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="{n}"/><pic:cNvPicPr/>'
            '</pic:nvPicPr><pic:blipFill><a:blip {attr}="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{w}" cy="{h}"/></a:xfrm><a:prstGeom prst="rect"/></pic:spPr>'
            '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>').format(
                w=emu_w, h=emu_h, n=name, pic=PIC, attr=attr, rid=rid)


def docx():
    """
    Heading, text, a table, a picture from the file, a safe link, a javascript: link, a
    HYPERLINK field to javascript:, a bookmark link, a linked (external) picture that
    must never be fetched, and an HTML altChunk with a script that must never run.
    """
    table = ('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
             '<w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>'
             + ''.join('<w:tr><w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>{}</w:tc>'
                       '<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>{}</w:tc></w:tr>'.format(
                           _w_para(_w_run(a, bold=(a == 'Region'))), _w_para(_w_run(b, bold=(b == 'Revenue'))))
                       for a, b in (('Region', 'Revenue'), ('North', '1,200'), ('South', '3,400')))
             + '</w:tbl>')
    body = ''.join([
        _w_para(_w_run('Quarterly Report'), 'Heading1'),
        _w_para(_w_run('This document is rendered in the browser; nothing leaves Motuz.')),
        _w_para('<w:hyperlink r:id="rId2">{}</w:hyperlink>'.format(_w_run('Safe link to example.org'))),
        _w_para('<w:hyperlink r:id="rId3">{}</w:hyperlink>'.format(_w_run('Script link (must not work)'))),
        _w_para('<w:fldSimple w:instr=" HYPERLINK &quot;javascript:window.__motuzDocx=\'field\'&quot; ">{}</w:fldSimple>'
                .format(_w_run('Field link (must not work)'))),
        _w_para('<w:hyperlink w:anchor="results">{}</w:hyperlink>'.format(_w_run('Jump to the results'))),
        table,
        _w_para(_w_image('rId4', name='chart')),
        _w_para(_w_image('rId5', attr='r:link', name='tracker')),
        '<w:altChunk r:id="rId6"/>',
        _w_para('<w:bookmarkStart w:id="0" w:name="results"/>{}<w:bookmarkEnd w:id="0"/>'.format(_w_run('Results section')),
                'Heading1'),
        _w_para(_w_run('The end.')),
    ])
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<w:document xmlns:w="{}" xmlns:r="{}" xmlns:a="{}" xmlns:pic="{}" xmlns:wp="{}"><w:body>{}'
                '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
                'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>').format(
                    W, REL, A, PIC, WP, body)
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="{}">'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
              '<w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:b/><w:color w:val="2F5496"/>'
              '<w:sz w:val="36"/></w:rPr></w:style>'
              '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
              + ''.join('<w:{} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'.format(side)
                        for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))
              + '</w:tblBorders></w:tblPr></w:style></w:styles>').format(W)
    return _zip({
        '[Content_Types].xml': _content_types(
            [('/word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'),
             ('/word/styles.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml')],
            [('htm', 'text/html')]),
        '_rels/.rels': _root_rels('word/document.xml'),
        'word/document.xml': document,
        'word/styles.xml': styles,
        'word/_rels/document.xml.rels': _rels([
            ('rId1', 'styles', 'styles.xml', False),
            ('rId2', 'hyperlink', 'https://example.org/motuz-docx', True),
            ('rId3', 'hyperlink', "javascript:window.__motuzDocx='link'", True),
            ('rId4', 'image', 'media/image1.png', False),
            ('rId5', 'image', 'https://tracker.invalid/docx-pixel.png', True),
            ('rId6', 'aFChunk', 'afchunk.htm', False),
        ]),
        'word/media/image1.png': png(),
        'word/afchunk.htm': '<html><body><p>altChunk HTML</p><script>window.__motuzDocx="altchunk"</script>'
                            '<img src="x" onerror="window.__motuzDocx=\'onerror\'"></body></html>',
    })


def xlsx(data_rows=6000, wide_cols=230):
    """
    Sheets Summary (strings, formatted numbers, a date, a formula whose cached value
    is not its result: shown as stored, never evaluated), Data (`data_rows` rows, more
    than the viewer's 5,000, and a row `wide_cols` wide, more than its 200 columns) and
    a hidden sheet.
    """
    def cell(ref, value, style=None):
        s = ' s="{}"'.format(style) if style is not None else ''
        if isinstance(value, str):
            return '<c r="{}" t="inlineStr"{}><is><t>{}</t></is></c>'.format(ref, s, value)
        return '<c r="{}"{}><v>{}</v></c>'.format(ref, s, value)

    def sheet(rows, cols='', dimension=None):
        dim = '<dimension ref="{}"/>'.format(dimension) if dimension else ''
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{}{}<sheetData>{}'
                '</sheetData></worksheet>').format(dim, cols, ''.join(rows))

    def col(i):
        name = ''
        i += 1
        while i:
            i, rest = divmod(i - 1, 26)
            name = chr(65 + rest) + name
        return name

    summary = [
        '<row r="1">{}{}{}</row>'.format(cell('A1', 'Name'), cell('B1', 'Amount'), cell('C1', 'Date')),
        '<row r="2">{}{}{}</row>'.format(cell('A2', 'alpha'), cell('B2', 1234.5, 1), cell('C2', 45000, 2)),
        '<row r="3">{}{}{}</row>'.format(cell('A3', 'beta'), cell('B3', 0.25, 3), cell('C3', 'n/a')),
        '<row r="4">{}<c r="B4"><f>1+1</f><v>42</v></c></row>'.format(cell('A4', 'cached formula')),
    ]
    data = ['<row r="1">{}</row>'.format(''.join(cell('{}1'.format(col(c)), 'col {}'.format(c + 1))
                                                   for c in range(wide_cols)))]
    for r in range(2, data_rows + 1):
        data.append('<row r="{0}"><c r="A{0}"><v>{0}</v></c>{1}</row>'.format(r, cell('B{}'.format(r), 'row {}'.format(r))))
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>'
              '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
              '</styleSheet>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{}"><sheets>'
                '<sheet name="Summary" sheetId="1" r:id="rId1"/><sheet name="Data" sheetId="2" r:id="rId2"/>'
                '<sheet name="Secret" sheetId="3" state="hidden" r:id="rId3"/></sheets></workbook>').format(REL)
    main = 'application/vnd.openxmlformats-officedocument.spreadsheetml'
    return _zip({
        '[Content_Types].xml': _content_types(
            [('/xl/workbook.xml', main + '.sheet.main+xml'), ('/xl/styles.xml', main + '.styles+xml')]
            + [('/xl/worksheets/sheet{}.xml'.format(i), main + '.worksheet+xml') for i in (1, 2, 3)]),
        '_rels/.rels': _root_rels('xl/workbook.xml'),
        'xl/workbook.xml': workbook,
        'xl/_rels/workbook.xml.rels': _rels([('rId{}'.format(i), 'worksheet', 'worksheets/sheet{}.xml'.format(i), False)
                                             for i in (1, 2, 3)] + [('rId9', 'styles', 'styles.xml', False)]),
        'xl/styles.xml': styles,
        'xl/worksheets/sheet1.xml': sheet(summary, '<cols><col min="1" max="1" width="24" customWidth="1"/></cols>'),
        'xl/worksheets/sheet2.xml': sheet(data, dimension='A1:{}{}'.format(col(wide_cols - 1), data_rows)),
        'xl/worksheets/sheet3.xml': sheet(['<row r="1">{}</row>'.format(cell('A1', 'hidden value'))]),
    })


def pptx():
    """Two slides: a title slide, and one with bullets (two levels), a table, a picture, notes"""
    ns = ('xmlns:a="{}" xmlns:r="{}" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
          .format(A, REL))

    def shape(ph, paragraphs, sid):
        phx = '<p:nvPr><p:ph type="{}"/></p:nvPr>'.format(ph) if ph else '<p:nvPr/>'
        ps = ''.join('<a:p>{}<a:r><a:t>{}</a:t></a:r></a:p>'.format('<a:pPr lvl="{}"/>'.format(l) if l else '', t)
                     for t, l in paragraphs)
        return ('<p:sp><p:nvSpPr><p:cNvPr id="{}" name="s{}"/><p:cNvSpPr/>{}</p:nvSpPr><p:spPr/>'
                '<p:txBody><a:bodyPr/>{}</p:txBody></p:sp>').format(sid, sid, phx, ps)

    def slide(content):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sld {}><p:cSld><p:spTree>'
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>{}'
                '</p:spTree></p:cSld></p:sld>').format(ns, content)

    table = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="t"/><p:cNvGraphicFramePr/><p:nvPr/>'
             '</p:nvGraphicFramePr><p:xfrm/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
             '<a:tbl>' + ''.join('<a:tr h="0">{}</a:tr>'.format(''.join(
                 '<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{}</a:t></a:r></a:p></a:txBody></a:tc>'.format(v) for v in row))
                 for row in (('Metric', 'Q3'), ('Users', '1,024'))) + '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    picture = ('<p:pic><p:nvPicPr><p:cNvPr id="6" name="Picture"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
               '<p:blipFill><a:blip r:embed="rId2"/></p:blipFill><p:spPr/></p:pic>'
               '<p:pic><p:nvPicPr><p:cNvPr id="7" name="Tracker"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
               '<p:blipFill><a:blip r:embed="rId4"/></p:blipFill><p:spPr/></p:pic>')
    slide1 = slide(shape('ctrTitle', [('Motuz Slides', 0)], 2) + shape('subTitle', [('Outline preview test', 0)], 3))
    slide2 = slide(shape('title', [('Results &amp; Plans', 0)], 2)
                   + shape('body', [('First point', 0), ('Detail of the first point', 1), ('Second point', 0)], 3)
                   + shape('sldNum', [('2', 0)], 4) + table + picture)
    notes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:notes {}><p:cSld><p:spTree>'
             '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>{}'
             '</p:spTree></p:cSld></p:notes>').format(ns, shape('body', [('Speaker note: mention the chart', 0)], 2))
    presentation = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:presentation {}>'
                    '<p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId3"/></p:sldIdLst>'
                    '<p:sldSz cx="9144000" cy="6858000"/></p:presentation>').format(ns)
    main = 'application/vnd.openxmlformats-officedocument.presentationml'
    return _zip({
        '[Content_Types].xml': _content_types(
            [('/ppt/presentation.xml', main + '.presentation.main+xml'),
             ('/ppt/slides/slide1.xml', main + '.slide+xml'), ('/ppt/slides/slide2.xml', main + '.slide+xml'),
             ('/ppt/notesSlides/notesSlide2.xml', main + '.notesSlide+xml')]),
        '_rels/.rels': _root_rels('ppt/presentation.xml'),
        'ppt/presentation.xml': presentation,
        'ppt/_rels/presentation.xml.rels': _rels([('rId2', 'slide', 'slides/slide1.xml', False),
                                                  ('rId3', 'slide', 'slides/slide2.xml', False)]),
        'ppt/slides/slide1.xml': slide1,
        'ppt/slides/slide2.xml': slide2,
        'ppt/slides/_rels/slide2.xml.rels': _rels([('rId2', 'image', '../media/image1.png', False),
                                                   ('rId3', 'notesSlide', '../notesSlides/notesSlide2.xml', False),
                                                   ('rId4', 'image', 'https://tracker.invalid/pptx-pixel.png', True)]),
        'ppt/notesSlides/notesSlide2.xml': notes,
        'ppt/media/image1.png': png(),
    })


def zip_bomb(entries=None, zeros=0, main='word/document.xml'):
    """
    A DOCX (content types included) that is a zip bomb: `zeros` MiB of zeros in one
    entry (about 1000:1), or `entries` tiny entries. Written with a streaming
    compressor, so neither this script nor the viewer ever holds it inflated.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', _content_types(
            [('/' + main, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml')]))
        z.writestr('_rels/.rels', _root_rels(main))
        if zeros:
            with z.open(main, 'w', force_zip64=True) as f:
                block = b'\x00' * MiB
                for _ in range(zeros):
                    f.write(block)
        for i in range(entries or 0):
            z.writestr('word/media/pad{}.bin'.format(i), b'')
    return out.getvalue()


def fixtures():
    """{file name: contents}"""
    return {
        'report.pdf': pdf(),
        # 6 MiB, above the e2e stack's MOTUZ_VIEW_DOCUMENT_MAX_BYTES=4M: still opens (ranges)
        'big.pdf': pdf(pages=40, padding=6 * MiB, hostile=False),
        'letter.docx': docx(),
        'numbers.xlsx': xlsx(),
        'slides.pptx': pptx(),
        'legacy.doc': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 1024,
        'notreally.docx': b'plain text, not a document\n',
        'slides-named.docx': pptx(), # a PPTX named .docx: the content types say so
        'bomb.docx': zip_bomb(zeros=250), # 250 MiB of zeros declared: above the 200 MiB limit
        'many.xlsx': zip_bomb(entries=10001, main='xl/workbook.xml'),
        # 5 MiB, above MOTUZ_VIEW_DOCUMENT_MAX_BYTES=4M: 413 before reading
        'huge.xlsx': b'PK\x03\x04' + b'\x00' * (5 * MiB),
    }


def main(folder):
    os.makedirs(folder, exist_ok=True)
    for name, data in fixtures().items():
        with open(os.path.join(folder, name), 'wb') as f:
            f.write(data)


if __name__ == '__main__':
    main(sys.argv[1])
