/**
 * Which document viewer a file opens in, by its name. The server checks the content
 * (POST /api/system/files/view/document/: PDF by `%PDF-`, ZIP, OLE2) and the worker
 * the ZIP's [Content_Types].xml, so a wrong extension ends in an error message, never
 * in the wrong renderer. Plain functions for node --test.
 */

export const DOCUMENT_KINDS = Object.freeze({
    pdf: 'pdf',
    docx: 'docx',
    sheet: 'sheet',
    pptx: 'pptx',
    unsupported: 'unsupported',
});

const EXTENSIONS = {
    pdf: DOCUMENT_KINDS.pdf,
    docx: DOCUMENT_KINDS.docx,
    docm: DOCUMENT_KINDS.docx, // macros are never run: docx-preview ignores them
    dotx: DOCUMENT_KINDS.docx,
    dotm: DOCUMENT_KINDS.docx,
    xlsx: DOCUMENT_KINDS.sheet,
    xlsm: DOCUMENT_KINDS.sheet, // values only: no macros, no formula evaluation
    xltx: DOCUMENT_KINDS.sheet,
    xltm: DOCUMENT_KINDS.sheet,
    xlsb: DOCUMENT_KINDS.sheet,
    xls: DOCUMENT_KINDS.sheet,
    ods: DOCUMENT_KINDS.sheet,
    pptx: DOCUMENT_KINDS.pptx,
    pptm: DOCUMENT_KINDS.pptx,
    ppsx: DOCUMENT_KINDS.pptx,
    ppsm: DOCUMENT_KINDS.pptx,
    potx: DOCUMENT_KINDS.pptx,
    potm: DOCUMENT_KINDS.pptx,
    // Legacy binary Office formats and other documents without a preview
    doc: DOCUMENT_KINDS.unsupported,
    dot: DOCUMENT_KINDS.unsupported,
    ppt: DOCUMENT_KINDS.unsupported,
    pps: DOCUMENT_KINDS.unsupported,
    pot: DOCUMENT_KINDS.unsupported,
    odt: DOCUMENT_KINDS.unsupported,
    odp: DOCUMENT_KINDS.unsupported,
    pages: DOCUMENT_KINDS.unsupported,
    numbers: DOCUMENT_KINDS.unsupported,
    key: DOCUMENT_KINDS.unsupported,
};

export const DOCUMENT_LABELS = Object.freeze({
    pdf: 'PDF',
    docx: 'Word document',
    sheet: 'Spreadsheet',
    pptx: 'Presentation',
    unsupported: 'Document',
});

/** The lower-case extension of a file name ('' for none; '.bashrc' has none) */
export function fileExtension(name) {
    if (typeof name !== 'string') return '';
    const base = name.split('/').pop();
    const dot = base.lastIndexOf('.');
    return dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
}

/** 'pdf', 'docx', 'sheet', 'pptx', 'unsupported' (no preview) or null (not a document) */
export function documentKind(name) {
    const extension = fileExtension(name);
    return Object.prototype.hasOwnProperty.call(EXTENSIONS, extension) ? EXTENSIONS[extension] : null;
}
