/**
 * Web Worker of the document viewer: everything that parses an untrusted DOCX, XLSX,
 * ODS, XLS or PPTX before anything is shown runs here, so a hostile or huge file
 * cannot freeze the page; the dialog terminates the worker after a timeout
 * (utils/documentViewer.js). Messages: {id, op, data, container} with op 'docx',
 * 'sheet' or 'pptx', `data` the file's bytes (transferred) and `container` the type
 * the server detected ('zip' or 'cfb'). Answers: {id, ok: true, result} or
 * {id, ok: false, error}.
 *
 * ZIP files are checked and inflated with limits (utils/zipSafe.js), and their
 * [Content_Types].xml must match the viewer (utils/officeFiles.js):
 * - docx: a new stored ZIP of the checked entries, for docx-preview on the page
 * - sheet: plain rows of formatted cell text (SheetJS, values only)
 * - pptx: the outline (utils/officeFiles.js pptxOutline) and its pictures
 */
import {readZip, rezip, ZipError} from '../utils/zipSafe.js';
import {checkOfficeKind, pptxOutline, DocumentError} from '../utils/officeFiles.js';
import {workbookToSheets, SHEET_LIMITS} from '../utils/sheetGrid.js';

const LEGACY_MESSAGE = 'This is an old binary Office file or a password-protected document; '
    + 'there is no preview for it';

function openZip(data, container, expected) {
    if (container !== 'zip') {
        throw new DocumentError(LEGACY_MESSAGE);
    }
    const {files} = readZip(data);
    const kind = checkOfficeKind(files, expected);
    return {files, kind};
}

function docx(data, container) {
    const {files} = openZip(data, container, 'docx');
    // Macros are never run, and docx-preview has no use for them
    delete files['word/vbaProject.bin'];
    const zip = rezip(files);
    return {result: {zip}, transfer: [zip.buffer]};
}

async function sheet(data, container) {
    let input = data;
    if (container === 'zip') {
        const {files} = openZip(data, container, 'sheet');
        delete files['xl/vbaProject.bin'];
        input = rezip(files);
    } else if (container !== 'cfb') {
        throw new DocumentError('This is not a spreadsheet file');
    }
    const XLSX = await import(/* webpackChunkName: "sheetjs" */ 'xlsx');
    let workbook;
    try {
        workbook = XLSX.read(input, {
            type: 'array',
            dense: true,
            sheetRows: SHEET_LIMITS.maxRows,
            cellFormula: false,
            cellHTML: false,
            cellStyles: false,
            cellDates: false,
            bookVBA: false,
            bookDeps: false,
            WTF: false,
        });
    } catch (e) {
        const text = String((e && e.message) || e);
        if (/password|encrypt/i.test(text)) {
            throw new DocumentError('The spreadsheet is password protected');
        }
        throw new DocumentError(`The spreadsheet could not be read: ${text.slice(0, 200)}`);
    }
    const sheets = workbookToSheets(XLSX, workbook, SHEET_LIMITS);
    return {
        result: {sheets, sheetCount: (workbook.SheetNames || []).length, limits: SHEET_LIMITS},
        transfer: [],
    };
}

function pptx(data, container) {
    const {files} = openZip(data, container, 'pptx');
    const {slides, images} = pptxOutline(files);
    const pictures = {};
    const transfer = [];
    for (const [part, {mime, bytes}] of Object.entries(images)) {
        const copy = bytes.slice();
        pictures[part] = {mime, bytes: copy};
        transfer.push(copy.buffer);
    }
    return {result: {slides, pictures}, transfer};
}

const OPERATIONS = {docx, sheet, pptx};

function errorMessage(e) {
    if (e instanceof ZipError || e instanceof DocumentError) {
        return {message: e.message, kind: e.name};
    }
    if (e instanceof RangeError) {
        return {message: 'The document is too large to preview (out of memory)', kind: 'RangeError'};
    }
    return {message: `The document could not be read: ${String((e && e.message) || e).slice(0, 300)}`, kind: 'Error'};
}

self.onmessage = async (event) => {
    const {id, op, data, container} = event.data || {};
    const operation = OPERATIONS[op];
    try {
        if (!operation) {
            throw new DocumentError(`Unknown operation ${op}`);
        }
        const {result, transfer} = await operation(new Uint8Array(data), container);
        self.postMessage({id, ok: true, result}, transfer);
    } catch (e) {
        const {message, kind} = errorMessage(e);
        self.postMessage({id, ok: false, error: message, errorKind: kind});
    }
};
