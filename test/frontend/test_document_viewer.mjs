// Run with: node --test test/frontend/   (from the repository root)
// Helpers of the document viewer (PDF, DOCX, XLSX, PPTX previews): dispatch by
// extension, link filtering, ZIP limits (zip bombs), the XML reader, the PPTX outline
// and the spreadsheet grid. Fixtures are built here with fflate.
import test from 'node:test';
import assert from 'node:assert/strict';
import {deflateSync, strToU8, zipSync} from 'fflate';
import * as XLSX from 'xlsx';

import {documentKind, fragmentTarget, safeLinkUrl} from '../../src/frontend/js/utils/viewerKind.js';
import {stripCssUrls} from '../../src/frontend/js/utils/cssSafe.js';
import {
    ZIP_LIMITS, ZipError, ZipLimitError, extractEntry, isZip, readCentralDirectory, readZip, rezip,
} from '../../src/frontend/js/utils/zipSafe.js';
import {XmlError, parseXml, documentElement, children, textOf} from '../../src/frontend/js/utils/xmlLite.js';
import {
    DocumentError, checkOfficeKind, detectOfficeKind, imageMime, pptxOutline, resolveTarget,
} from '../../src/frontend/js/utils/officeFiles.js';
import {
    columnName, columnOffsets, columnWidths, visibleRange, workbookToSheets,
} from '../../src/frontend/js/utils/sheetGrid.js';
import {splitRange} from '../../src/frontend/js/utils/pdfRanges.js';

// ---- dispatch by extension ----

test('document kinds by extension', () => {
    assert.equal(documentKind('Report.PDF'), 'pdf');
    assert.equal(documentKind('/a/b/letter.docx'), 'docx');
    assert.equal(documentKind('macro.docm'), 'docx');
    assert.equal(documentKind('data.xlsx'), 'sheet');
    assert.equal(documentKind('old.xls'), 'sheet');
    assert.equal(documentKind('macros.xlsm'), 'sheet');
    assert.equal(documentKind('calc.ods'), 'sheet');
    assert.equal(documentKind('talk.pptx'), 'pptx');
    assert.equal(documentKind('show.ppsx'), 'pptx');
    assert.equal(documentKind('legacy.doc'), 'unsupported');
    assert.equal(documentKind('legacy.ppt'), 'unsupported');
    assert.equal(documentKind('notes.txt'), null);
    assert.equal(documentKind('image.png'), null);
    assert.equal(documentKind('README'), null);
    assert.equal(documentKind('.pdf'), null); // a dot file, no extension
    assert.equal(documentKind('archive.pdf.gz'), null);
    assert.equal(documentKind('x.constructor'), null);
    assert.equal(documentKind('x.__proto__'), null);
    assert.equal(documentKind(null), null);
});

// ---- links and CSS ----

test('document links: only http, https and mailto, or bookmarks', () => {
    // pdf.js and docx-preview hand over URLs from the file
    assert.equal(safeLinkUrl('https://example.org/a?b=1#c'), 'https://example.org/a?b=1#c');
    assert.equal(safeLinkUrl('mailto:someone@example.org'), 'mailto:someone@example.org');
    for (const bad of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'java\u200bscript:alert(1)', 'vbscript:x',
                       'data:text/html,x', 'file:///etc/passwd', 'blob:https://x/1', 'https://user@evil.example/',
                       'relative.html', '#anchor']) {
        assert.equal(safeLinkUrl(bad), null, bad);
    }
    assert.equal(fragmentTarget('#_Toc123'), '_Toc123');
    assert.equal(fragmentTarget('#'), null);
});

test('CSS from documents loses every url() except the page\'s own blob: URLs', () => {
    const blob = 'blob:https://motuz.example/';
    assert.equal(stripCssUrls('p { color: red }', blob), 'p { color: red }');
    assert.equal(stripCssUrls('background: url(https://evil.example/x.png)', blob), 'background: none');
    assert.equal(stripCssUrls("background:url( 'http://evil/x' ) no-repeat", blob), 'background:none no-repeat');
    assert.equal(stripCssUrls('background: url("blob:https://motuz.example/1234")', blob),
        'background: url("blob:https://motuz.example/1234")');
    assert.equal(stripCssUrls('background: url(blob:https://other.example/1)', blob), 'background: none');
    assert.equal(stripCssUrls('@import url(https://evil/x.css); p{}', blob), ' p{}');
    assert.equal(stripCssUrls('@import "https://evil/x.css"; p{}', blob), ' p{}');
    assert.equal(stripCssUrls('background-image: image-set("https://evil/x.png" 1x)', blob), 'background-image: none');
    assert.equal(stripCssUrls('background: \\75 rl(https://evil/x)', blob), 'background: none');
    assert.ok(!/url\(/i.test(stripCssUrls('background: url(https://evil/x', blob)));
    assert.equal(stripCssUrls('font-family: x; src: url(data:font/woff2;base64,AAAA)', null), 'font-family: x; src: none');
    assert.equal(stripCssUrls(null), '');
});

// ---- ZIP limits ----

function zipOf(files, level = 6) {
    const input = Object.create(null);
    for (const [name, content] of Object.entries(files)) {
        input[name] = [typeof content === 'string' ? strToU8(content) : content, {level}];
    }
    return zipSync(input);
}

/** Offsets of the central directory entries of a ZIP */
function centralEntries(zip) {
    const view = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
    const offsets = [];
    for (let i = 0; i + 4 <= zip.length; i++) {
        if (view.getUint32(i, true) === 0x02014b50) offsets.push(i);
    }
    return offsets;
}

test('a normal ZIP is read, and rezip makes an equivalent stored ZIP', () => {
    const zip = zipOf({'a.txt': 'hello '.repeat(100), 'dir/b.xml': '<x/>'});
    assert.ok(isZip(zip));
    const {entries, totalUncompressed} = readCentralDirectory(zip);
    assert.deepEqual(entries.map(e => e.name).sort(), ['a.txt', 'dir/b.xml']);
    assert.equal(totalUncompressed, 604);
    const {files} = readZip(zip);
    assert.equal(new TextDecoder().decode(files['a.txt']), 'hello '.repeat(100));
    const again = readZip(rezip(files)).files;
    assert.deepEqual(Object.keys(again).sort(), ['a.txt', 'dir/b.xml']);
    assert.ok(readCentralDirectory(rezip(files)).entries.every(e => e.method === 0));
    assert.equal(isZip(strToU8('%PDF-1.7')), false);
});

test('too many entries are refused before anything is inflated', () => {
    const files = {};
    for (let i = 0; i < 30; i++) files[`f${i}`] = 'x';
    const zip = zipOf(files);
    assert.throws(() => readCentralDirectory(zip, {...ZIP_LIMITS, maxEntries: 29}), ZipLimitError);
    assert.equal(readCentralDirectory(zip, {...ZIP_LIMITS, maxEntries: 30}).entries.length, 30);
});

test('more declared uncompressed data than the limit is refused (zip bomb)', () => {
    const zip = zipOf({'a.bin': new Uint8Array(1000), 'b.bin': new Uint8Array(1000)});
    assert.throws(() => readCentralDirectory(zip, {...ZIP_LIMITS, maxTotalUncompressed: 1999}),
        (e) => e instanceof ZipLimitError && /uncompressed/.test(e.message));
    assert.throws(() => readCentralDirectory(zip, {...ZIP_LIMITS, maxEntryUncompressed: 999}), ZipLimitError);
    // A directory that declares 300 MiB for one entry
    const patched = zip.slice();
    const view = new DataView(patched.buffer);
    view.setUint32(centralEntries(patched)[0] + 24, 300 * 1024 * 1024, true);
    assert.throws(() => readCentralDirectory(patched), ZipLimitError);
});

test('an entry that inflates to more than it declares is stopped', () => {
    // 64 MiB of zeros deflate to ~64 KiB; the directory claims 1000 bytes
    const big = new Uint8Array(64 * 1024 * 1024);
    const zip = zipOf({'word/document.xml': big});
    const patched = zip.slice();
    const view = new DataView(patched.buffer);
    view.setUint32(centralEntries(patched)[0] + 24, 1000, true);
    const [entry] = readCentralDirectory(patched).entries;
    assert.equal(entry.uncompressedSize, 1000);
    const started = Date.now();
    assert.throws(() => extractEntry(patched, entry), ZipLimitError);
    assert.ok(Date.now() - started < 2000, 'stopped early');
    assert.throws(() => readZip(patched), ZipLimitError);
});

test('an entry shorter than declared, encryption, other methods and damage are refused', () => {
    const zip = zipOf({'a.txt': 'hello world hello world'});
    const offset = centralEntries(zip)[0];

    const longer = zip.slice();
    new DataView(longer.buffer).setUint32(offset + 24, 1000, true);
    assert.throws(() => readZip(longer), (e) => e instanceof ZipError && /shorter/.test(e.message));

    const encrypted = zip.slice();
    new DataView(encrypted.buffer).setUint16(offset + 8, 1, true);
    assert.throws(() => readCentralDirectory(encrypted), /encrypted/);

    const bzip2 = zip.slice();
    new DataView(bzip2.buffer).setUint16(offset + 10, 12, true);
    assert.throws(() => readCentralDirectory(bzip2), /compression method 12/);

    assert.throws(() => readCentralDirectory(zip.subarray(0, zip.length - 5)), ZipError);
    assert.throws(() => readCentralDirectory(strToU8('not a zip file at all, just text')), ZipError);
});

test('duplicate entry names are refused', () => {
    const zip = zipOf({'aa.xml': '<a/>', 'ab.xml': '<b/>'});
    const offsets = centralEntries(zip);
    const patched = zip.slice();
    patched[offsets[1] + 46 + 1] = 'a'.charCodeAt(0); // ab.xml -> aa.xml
    assert.throws(() => readCentralDirectory(patched), /duplicate/);
});

test('entry names are data, not object keys', () => {
    // fflate cannot write an entry named __proto__, so rename one after writing
    const zip = zipOf({'__proto_x': 'x', 'constructor': 'y'});
    const patched = zip.slice();
    for (const offset of centralEntries(patched)) {
        if (patched[offset + 46] === 0x5f) patched[offset + 46 + 8] = 0x5f; // __proto_x -> __proto__
    }
    const view = new DataView(patched.buffer);
    for (let i = 0; i + 4 <= patched.length; i++) {
        if (view.getUint32(i, true) === 0x04034b50 && patched[i + 30] === 0x5f) patched[i + 30 + 8] = 0x5f;
    }
    const {files} = readZip(patched);
    assert.equal(Object.getPrototypeOf(files), null);
    assert.equal(new TextDecoder().decode(files.__proto__), 'x');
    assert.equal(new TextDecoder().decode(files.constructor), 'y');
    assert.deepEqual(Object.keys(readZip(rezip(files)).files), ['constructor']);
});

// ---- XML ----

test('the XML reader handles entities, CDATA and namespaces, and refuses DTDs', () => {
    const doc = parseXml('<?xml version="1.0"?><a:r xmlns:a="urn:a" x="1&amp;2"><a:t>&lt;b&gt; &#x41;&#66;</a:t><![CDATA[<raw>]]><e/></a:r>');
    const root = documentElement(doc);
    assert.equal(root.local, 'r');
    assert.equal(root.attrs.x, '1&2');
    assert.equal(textOf(children(root, 't')[0]), '<b> AB');
    assert.equal(textOf(root), '<b> AB<raw>');
    assert.throws(() => parseXml('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'), XmlError);
    assert.throws(() => parseXml('<a><b></a>'), XmlError);
    assert.throws(() => parseXml('<a>'), XmlError);
    assert.throws(() => parseXml('<a>'.repeat(300) + '</a>'.repeat(300)), /nested too deeply/);
    assert.equal(textOf(documentElement(parseXml('<a>&unknown; &#0;</a>'))), '&unknown; \uFFFD');
});

// ---- PPTX ----

const PNG = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]);
const NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    + 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"';
const RELS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const PKG_RELS = 'http://schemas.openxmlformats.org/package/2006/relationships';

function shape(type, paragraphs) {
    const ph = type ? `<p:nvPr><p:ph type="${type}"/></p:nvPr>` : '<p:nvPr/>';
    const ps = paragraphs.map(([text, level]) => `<a:p>${level ? `<a:pPr lvl="${level}"/>` : ''}<a:r><a:t>${text}</a:t></a:r></a:p>`).join('');
    return `<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/>${ph}</p:nvSpPr><p:txBody><a:bodyPr/>${ps}</p:txBody></p:sp>`;
}

export function pptxFixture() {
    const slide1 = `<p:sld ${NS}><p:cSld><p:spTree>${shape('ctrTitle', [['Quarterly &amp; Review']])}`
        + `${shape('subTitle', [['Fred Hutch']])}${shape('sldNum', [['1']])}</p:spTree></p:cSld></p:sld>`;
    const slide2 = `<p:sld ${NS} show="0"><p:cSld><p:spTree>${shape('title', [['Results']])}`
        + `${shape(null, [['Point one'], ['Detail', 1]])}`
        + '<p:grpSp><p:nvGrpSpPr/><p:grpSpPr/>'
        + '<p:pic><p:nvPicPr><p:cNvPr id="4" name="p"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        + '<p:blipFill><a:blip r:embed="rId2"/></p:blipFill></p:pic>'
        + '<p:pic><p:nvPicPr><p:cNvPr id="5" name="q"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        + '<p:blipFill><a:blip r:embed="rId3"/></p:blipFill></p:pic>'
        + '<p:pic><p:nvPicPr><p:cNvPr id="6" name="e"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        + '<p:blipFill><a:blip r:embed="rId4"/></p:blipFill></p:pic></p:grpSp>'
        + '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="7" name="t"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
        + '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
        + '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>Name</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>Value</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
        + '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>x</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>1</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
        + '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
        + '</p:spTree></p:cSld></p:sld>';
    const notes = `<p:notes ${NS}><p:cSld><p:spTree>${shape('sldImg', [['x']])}${shape('body', [['Say hello']])}</p:spTree></p:cSld></p:notes>`;
    return {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + '<Default Extension="xml" ContentType="application/xml"/>'
            + '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>',
        '_rels/.rels': `<Relationships xmlns="${PKG_RELS}"><Relationship Id="rId1" Type="${RELS}/officeDocument" Target="ppt/presentation.xml"/></Relationships>`,
        'ppt/presentation.xml': `<p:presentation ${NS}><p:sldIdLst><p:sldId id="256" r:id="rId7"/><p:sldId id="257" r:id="rId8"/></p:sldIdLst></p:presentation>`,
        'ppt/_rels/presentation.xml.rels': `<Relationships xmlns="${PKG_RELS}">`
            + `<Relationship Id="rId8" Type="${RELS}/slide" Target="slides/slide2.xml"/>`
            + `<Relationship Id="rId7" Type="${RELS}/slide" Target="/ppt/slides/slide1.xml"/></Relationships>`,
        'ppt/slides/slide1.xml': slide1,
        'ppt/slides/slide2.xml': slide2,
        'ppt/slides/_rels/slide2.xml.rels': `<Relationships xmlns="${PKG_RELS}">`
            + `<Relationship Id="rId2" Type="${RELS}/image" Target="../media/image1.png"/>`
            + `<Relationship Id="rId3" Type="${RELS}/image" Target="../media/image2.emf"/>`
            + `<Relationship Id="rId4" Type="${RELS}/image" Target="https://evil.example/track.png" TargetMode="External"/>`
            + `<Relationship Id="rId5" Type="${RELS}/notesSlide" Target="../notesSlides/notesSlide2.xml"/></Relationships>`,
        'ppt/notesSlides/notesSlide2.xml': notes,
        'ppt/media/image1.png': PNG,
        'ppt/media/image2.emf': Uint8Array.from([1, 0, 0, 0, 0x6c, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    };
}

function encodeFiles(files) {
    const out = {};
    for (const [name, content] of Object.entries(files)) {
        out[name] = typeof content === 'string' ? strToU8(content) : content;
    }
    return out;
}

test('PPTX outline: slide order, titles, text levels, tables, pictures, notes', () => {
    const {files} = readZip(zipOf(pptxFixture()));
    assert.equal(checkOfficeKind(files, 'pptx'), 'pptx');
    const {slides, images} = pptxOutline(files);
    assert.equal(slides.length, 2);

    assert.equal(slides[0].title, 'Quarterly & Review');
    assert.equal(slides[0].hidden, false);
    // The subtitle is text; the slide number placeholder is skipped
    assert.deepEqual(slides[0].items, [{type: 'text', paragraphs: [{level: 0, text: 'Fred Hutch'}]}]);

    const second = slides[1];
    assert.equal(second.title, 'Results');
    assert.equal(second.hidden, true);
    assert.deepEqual(second.items[0], {type: 'text', paragraphs: [{level: 0, text: 'Point one'}, {level: 1, text: 'Detail'}]});
    assert.deepEqual(second.items[1], {type: 'image', part: 'ppt/media/image1.png'});
    assert.deepEqual(second.items[2], {type: 'unsupported', what: 'picture in an unsupported format'});
    assert.deepEqual(second.items[3], {type: 'unsupported', what: 'linked picture'});
    assert.deepEqual(second.items[4], {type: 'table', rows: [['Name', 'Value'], ['x', '1']]});
    assert.deepEqual(second.notes, ['Say hello']);

    assert.deepEqual(Object.keys(images), ['ppt/media/image1.png']);
    assert.equal(images['ppt/media/image1.png'].mime, 'image/png');
});

test('the content types must match the viewer', () => {
    const pptx = encodeFiles(pptxFixture());
    assert.equal(detectOfficeKind(pptx), 'pptx');
    assert.throws(() => checkOfficeKind(pptx, 'docx'), (e) => e instanceof DocumentError && /PowerPoint/.test(e.message));
    const plainZip = encodeFiles({'a.txt': 'x'});
    assert.throws(() => checkOfficeKind(plainZip, 'sheet'), /not a spreadsheet/);
    const ods = encodeFiles({mimetype: 'application/vnd.oasis.opendocument.spreadsheet'});
    assert.equal(checkOfficeKind(ods, 'sheet'), 'ods');
    const docx = encodeFiles({'[Content_Types].xml': '<Types><Override PartName="/word/document.xml" '
        + 'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'});
    assert.equal(checkOfficeKind(docx, 'docx'), 'docx');
});

test('relationship targets and image magic numbers', () => {
    assert.equal(resolveTarget('ppt/slides/slide1.xml', '../media/a.png'), 'ppt/media/a.png');
    assert.equal(resolveTarget('ppt/slides/slide1.xml', '/ppt/media/a.png'), 'ppt/media/a.png');
    assert.equal(resolveTarget('ppt/slides/slide1.xml', '../../../../x'), 'x');
    assert.equal(imageMime(PNG), 'image/png');
    assert.equal(imageMime(Uint8Array.from([0xff, 0xd8, 0xff, 0xe0, 0, 0, 0, 0, 0, 0, 0, 0])), 'image/jpeg');
    assert.equal(imageMime(strToU8('GIF89a......')), 'image/gif');
    assert.equal(imageMime(strToU8('RIFF\0\0\0\0WEBPVP8 ')), 'image/webp');
    assert.equal(imageMime(strToU8('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')), null);
    assert.equal(imageMime(undefined), null);
});

// ---- spreadsheet ----

test('column names, widths and the visible window', () => {
    assert.deepEqual([0, 1, 25, 26, 27, 51, 52, 701, 702].map(columnName),
        ['A', 'B', 'Z', 'AA', 'AB', 'AZ', 'BA', 'ZZ', 'AAA']);
    assert.deepEqual(columnWidths([{wpx: 10}, {wch: 20}, undefined, {hidden: true, wpx: 300}, {wpx: 5000}], 5),
        [32, 145, 96, 32, 480]);
    const offsets = columnOffsets([100, 100, 100, 100, 100]);
    assert.deepEqual(offsets, [0, 100, 200, 300, 400, 500]);
    assert.deepEqual(visibleRange({scrollTop: 0, scrollLeft: 0, height: 48, width: 150, rowCount: 1000, offsets, overscan: 0}),
        {firstRow: 0, lastRow: 2, firstCol: 0, lastCol: 1});
    assert.deepEqual(visibleRange({scrollTop: 240, scrollLeft: 250, height: 48, width: 100, rowCount: 1000, offsets, overscan: 1}),
        {firstRow: 9, lastRow: 13, firstCol: 1, lastCol: 4});
    assert.deepEqual(visibleRange({scrollTop: 0, scrollLeft: 0, height: 48, width: 100, rowCount: 0, offsets, overscan: 1}),
        {firstRow: 0, lastRow: -1, firstCol: 0, lastCol: -1});
});

test('workbooks become rows of formatted text, truncated to the limits', () => {
    const workbook = XLSX.utils.book_new();
    const data = [['Name', 'Amount', 'Date'], ['alpha', 1234.5, 45000], ['beta', '=1+1', null]];
    const sheet = XLSX.utils.aoa_to_sheet(data);
    sheet.B2.z = '#,##0.00';
    sheet.C2.z = 'yyyy-mm-dd';
    XLSX.utils.book_append_sheet(workbook, sheet, 'First');
    const wide = XLSX.utils.aoa_to_sheet(Array.from({length: 30}, (_, r) => Array.from({length: 12}, (_, c) => `${r}:${c}`)));
    XLSX.utils.book_append_sheet(workbook, wide, 'Wide');
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([]), 'Empty');
    const bytes = XLSX.write(workbook, {type: 'array', bookType: 'xlsx'});
    const read = XLSX.read(bytes, {type: 'array', dense: true, sheetRows: 21, cellFormula: false});

    const sheets = workbookToSheets(XLSX, read, {maxRows: 20, maxCols: 10, maxSheets: 10});
    assert.deepEqual(sheets.map(s => s.name), ['First', 'Wide', 'Empty']);
    const [first, second, empty] = sheets;
    assert.equal(first.rows[0][0], 'Name');
    assert.equal(first.rows[1][1], '1,234.50');
    assert.equal(first.rows[1][2], '2023-03-15');
    assert.equal(first.rows[2][1], '=1+1'); // text, never evaluated
    assert.equal(first.rows[2][2], undefined);
    assert.equal(first.rowCount, 3);
    assert.equal(second.rowCount, 20);
    assert.equal(second.totalRows, 30);
    assert.equal(second.colCount, 10);
    assert.equal(second.totalCols, 12);
    assert.equal(second.rows[19][9], '19:9');
    assert.equal(second.rows[0].length, 10);
    assert.equal(second.rowsKnown, true); // aoa_to_sheet writes a <dimension>
    assert.equal(empty.rowCount, 0);
    // Without a <dimension>: "more", not a count
    const noDimension = {SheetNames: ['S'], Sheets: {S: {'!ref': 'A1:A21', '!data': Array.from({length: 21}, (_, r) => [{t: 'n', v: r, w: String(r)}])}}};
    const [s] = workbookToSheets(XLSX, noDimension, {maxRows: 20, maxCols: 10, maxSheets: 10});
    assert.deepEqual([s.rowCount, s.totalRows, s.rowsKnown], [20, 21, false]);
});

// ---- PDF ranges ----

test('PDF ranges are split into requests of at most the server limit', () => {
    assert.deepEqual(splitRange(0, 10, 4), [[0, 4], [4, 8], [8, 10]]);
    assert.deepEqual(splitRange(5, 6, 4), [[5, 6]]);
    assert.deepEqual(splitRange(5, 5, 4), []);
    assert.throws(() => splitRange(0, 10, 0));
});

test('deflate bomb helper sanity: fflate compresses zeros well', () => {
    assert.ok(deflateSync(new Uint8Array(1024 * 1024)).length < 2048);
});
