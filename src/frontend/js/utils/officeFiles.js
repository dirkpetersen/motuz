/**
 * Office Open XML helpers for the document viewer: which kind of document a ZIP is
 * ([Content_Types].xml, or the `mimetype` entry of OpenDocument files), package
 * relationships, image types by magic number, and the PPTX outline (slide titles,
 * text, tables, pictures and notes), which the viewer shows instead of rendering
 * slides. Plain functions on {name: Uint8Array} maps (zipSafe.readZip), no DOM, so
 * they run in the document worker and under node --test.
 */
import {
    XmlError, attr, attrNS, child, children, documentElement, parseXml, textOf,
} from './xmlLite.js';

const utf8 = new TextDecoder('utf-8');

export class DocumentError extends Error {
    constructor(message) {
        super(message);
        this.name = 'DocumentError';
    }
}

// Main part content types per viewer kind (transitional and strict OOXML use the same)
const MAIN_TYPES = {
    docx: [
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml',
        'application/vnd.ms-word.document.macroEnabled.main+xml',
        'application/vnd.ms-word.template.macroEnabledTemplate.main+xml',
    ],
    sheet: [
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml',
        'application/vnd.ms-excel.sheet.macroEnabled.main+xml',
        'application/vnd.ms-excel.template.macroEnabled.main+xml',
        'application/vnd.ms-excel.sheet.binary.macroEnabled.main',
    ],
    pptx: [
        'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml',
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml',
        'application/vnd.openxmlformats-officedocument.presentationml.template.main+xml',
        'application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml',
        'application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml',
        'application/vnd.ms-powerpoint.template.macroEnabled.main+xml',
    ],
};
const KIND_NAMES = {docx: 'Word document', sheet: 'spreadsheet', pptx: 'PowerPoint presentation'};
const ODS_MIMETYPE = 'application/vnd.oasis.opendocument.spreadsheet';

const REL_NS = [
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'http://purl.oclc.org/ooxml/officeDocument/relationships',
];

export function xmlOf(files, name) {
    const bytes = files[name];
    if (!bytes) {
        return null;
    }
    try {
        return parseXml(utf8.decode(bytes));
    } catch (e) {
        if (e instanceof XmlError) {
            throw new DocumentError(`'${name}' is not valid XML: ${e.message}`);
        }
        throw e;
    }
}

/** The main part's content type from [Content_Types].xml, or null */
export function mainContentType(files) {
    const doc = xmlOf(files, '[Content_Types].xml');
    const types = doc && documentElement(doc);
    if (!types) {
        return null;
    }
    const all = children(types, 'Override').map(o => (attr(o, 'ContentType') || '').trim());
    for (const kind of Object.keys(MAIN_TYPES)) {
        const found = all.find(type => MAIN_TYPES[kind].includes(type));
        if (found) {
            return found;
        }
    }
    return null;
}

/** The kind of Office document ('docx', 'sheet', 'pptx' or 'ods') in a ZIP, or null */
export function detectOfficeKind(files) {
    const type = mainContentType(files);
    if (type) {
        return Object.keys(MAIN_TYPES).find(kind => MAIN_TYPES[kind].includes(type));
    }
    if (files.mimetype && utf8.decode(files.mimetype).trim() === ODS_MIMETYPE) {
        return 'ods';
    }
    return null;
}

/**
 * Throws a DocumentError unless the ZIP is the kind of document its name suggests
 * (`expected`: 'docx', 'sheet' (XLSX or ODS) or 'pptx'); the server only knows it is
 * a ZIP, the content types say what is in it.
 */
export function checkOfficeKind(files, expected) {
    const kind = detectOfficeKind(files);
    if (kind === expected || (expected === 'sheet' && kind === 'ods')) {
        return kind;
    }
    const what = KIND_NAMES[expected] || 'document';
    if (!kind) {
        throw new DocumentError(`This is a ZIP file, but not a ${what} (no matching [Content_Types].xml)`);
    }
    throw new DocumentError(`The file is a ${KIND_NAMES[kind] || kind}, not a ${what}`);
}

/** Resolves a relationship target against the folder of the part that has it */
export function resolveTarget(partName, target) {
    let parts;
    if (target.startsWith('/')) {
        parts = [];
        target = target.slice(1);
    } else {
        parts = partName.split('/').slice(0, -1);
    }
    for (const piece of target.split('/')) {
        if (piece === '..') {
            parts.pop();
        } else if (piece && piece !== '.') {
            parts.push(piece);
        }
    }
    return parts.join('/');
}

function relsName(partName) {
    const slash = partName.lastIndexOf('/');
    return `${partName.slice(0, slash + 1)}_rels/${partName.slice(slash + 1)}.rels`;
}

/**
 * The relationships of a part: {id: {type, target, external}}, `target` resolved to
 * a part name for internal ones. External targets (URLs) are never loaded.
 */
export function relationships(files, partName) {
    const doc = xmlOf(files, relsName(partName));
    const rels = Object.create(null);
    const root = doc && documentElement(doc);
    if (!root) {
        return rels;
    }
    for (const rel of children(root, 'Relationship')) {
        const id = attr(rel, 'Id');
        const target = attr(rel, 'Target') || '';
        const external = (attr(rel, 'TargetMode') || '').toLowerCase() === 'external';
        if (id) {
            rels[id] = {
                type: attr(rel, 'Type') || '',
                target: external ? target : resolveTarget(partName, target),
                external,
            };
        }
    }
    return rels;
}

function relOfType(rels, suffix) {
    return Object.values(rels).find(rel => !rel.external && rel.type.endsWith(suffix)) || null;
}

/** The image type of `bytes` by magic number (PNG, JPEG, GIF, WebP) or null */
export function imageMime(bytes) {
    if (!bytes || bytes.length < 12) return null;
    const b = bytes;
    if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47 && b[4] === 0x0d && b[5] === 0x0a
            && b[6] === 0x1a && b[7] === 0x0a) return 'image/png';
    if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
    if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x38 && (b[4] === 0x37 || b[4] === 0x39)
            && b[5] === 0x61) return 'image/gif';
    if (b.length >= 16 && b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46
            && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return 'image/webp';
    return null;
}

// ---- PPTX outline ----

export const PPTX_LIMITS = Object.freeze({maxSlides: 2000, maxImagesPerSlide: 50});

const TITLE_TYPES = new Set(['title', 'ctrTitle']);
// Placeholders that repeat on every slide and say nothing about it
const SKIPPED_TYPES = new Set(['dt', 'ftr', 'sldNum', 'hdr', 'sldImg']);

function paragraphText(p) {
    let text = '';
    for (const node of p.children) {
        if (!node.name) continue;
        if (node.local === 'r' || node.local === 'fld') {
            const t = child(node, 't');
            if (t) text += textOf(t);
        } else if (node.local === 'br') {
            text += '\n';
        }
    }
    return text;
}

function paragraphsOf(txBody) {
    const result = [];
    for (const p of children(txBody, 'p')) {
        const pPr = child(p, 'pPr');
        const level = Math.max(0, Math.min(8, parseInt(pPr && attr(pPr, 'lvl'), 10) || 0));
        const text = paragraphText(p);
        if (text.trim()) {
            result.push({level, text});
        }
    }
    return result;
}

function placeholderType(shape) {
    const nv = child(shape, 'nvSpPr') || child(shape, 'nvPicPr') || child(shape, 'nvGraphicFramePr');
    const nvPr = nv && child(nv, 'nvPr');
    const ph = nvPr && child(nvPr, 'ph');
    if (!ph) return null;
    return attr(ph, 'type') || 'body';
}

function tableOf(tbl) {
    return children(tbl, 'tr').map(tr => children(tr, 'tc').map(tc => {
        const txBody = child(tc, 'txBody');
        return txBody ? paragraphsOf(txBody).map(p => p.text).join('\n') : '';
    }));
}

/**
 * Walks the shapes of a slide's shape tree in document order: text shapes, tables,
 * pictures, groups (recursively) and alternate content (the fallback, which older
 * readers use and which usually holds a picture)
 */
function walkShapes(tree, visit) {
    for (const node of tree.children) {
        if (!node.name) continue;
        switch (node.local) {
        case 'sp':
        case 'pic':
        case 'graphicFrame':
        case 'cxnSp':
            visit(node);
            break;
        case 'grpSp':
            walkShapes(node, visit);
            break;
        case 'AlternateContent': {
            const branch = child(node, 'Fallback') || child(node, 'Choice');
            if (branch) walkShapes(branch, visit);
            break;
        }
        default:
            break;
        }
    }
}

function notesOf(files, slidePart, slideRels) {
    const rel = relOfType(slideRels, '/notesSlide');
    const doc = rel && xmlOf(files, rel.target);
    const root = doc && documentElement(doc);
    const cSld = root && child(root, 'cSld');
    const tree = cSld && child(cSld, 'spTree');
    if (!tree) return [];
    const notes = [];
    walkShapes(tree, (shape) => {
        const type = placeholderType(shape);
        const txBody = child(shape, 'txBody');
        if (shape.local === 'sp' && txBody && (type === 'body' || type === null)) {
            notes.push(...paragraphsOf(txBody).map(p => p.text));
        }
    });
    return notes;
}

/**
 * The outline of a PPTX (files from zipSafe.readZip): {slides, images}. Each slide is
 * {number, title, hidden, items, notes}; items are {type: 'text', paragraphs:
 * [{level, text}]}, {type: 'table', rows: [[cell text]]}, {type: 'image', part} or
 * {type: 'unsupported', what}. `images` maps each shown picture's part name to
 * {mime, bytes}: only PNG, JPEG, GIF and WebP by magic number, from inside the file
 * (external pictures are links and never fetched).
 */
export function pptxOutline(files, limits = PPTX_LIMITS) {
    const rootRels = relationships(files, '');
    const mainRel = relOfType(rootRels, '/officeDocument');
    const presentationPart = mainRel ? mainRel.target : 'ppt/presentation.xml';
    const presDoc = xmlOf(files, presentationPart);
    const presentation = presDoc && documentElement(presDoc);
    if (!presentation) {
        throw new DocumentError('The presentation has no ppt/presentation.xml');
    }
    const presRels = relationships(files, presentationPart);
    const list = child(presentation, 'sldIdLst');
    const slideIds = list ? children(list, 'sldId') : [];
    if (slideIds.length > limits.maxSlides) {
        throw new DocumentError(`The presentation has ${slideIds.length} slides; at most ${limits.maxSlides} can be shown`);
    }

    const images = Object.create(null);
    const slides = [];
    slideIds.forEach((sldId, index) => {
        const rel = presRels[attrNS(sldId, 'id', REL_NS)];
        const slide = {number: index + 1, title: null, hidden: false, items: [], notes: []};
        slides.push(slide);
        const doc = rel && !rel.external ? xmlOf(files, rel.target) : null;
        const root = doc && documentElement(doc);
        if (!root) {
            slide.items.push({type: 'unsupported', what: 'missing slide'});
            return;
        }
        slide.hidden = attr(root, 'show') === '0';
        const slideRels = relationships(files, rel.target);
        const cSld = child(root, 'cSld');
        const tree = cSld && child(cSld, 'spTree');
        let imageCount = 0;
        if (tree) {
            walkShapes(tree, (shape) => {
                const type = placeholderType(shape);
                if (type && SKIPPED_TYPES.has(type)) return;
                if (shape.local === 'sp') {
                    const txBody = child(shape, 'txBody');
                    const paragraphs = txBody ? paragraphsOf(txBody) : [];
                    if (!paragraphs.length) return;
                    if (type && TITLE_TYPES.has(type) && slide.title === null) {
                        slide.title = paragraphs.map(p => p.text).join(' ').replace(/\s+/g, ' ').trim();
                    } else {
                        slide.items.push({type: 'text', paragraphs});
                    }
                } else if (shape.local === 'graphicFrame') {
                    const graphic = child(shape, 'graphic');
                    const data = graphic && child(graphic, 'graphicData');
                    const tbl = data && child(data, 'tbl');
                    if (tbl) {
                        slide.items.push({type: 'table', rows: tableOf(tbl)});
                    } else {
                        const uri = (data && attr(data, 'uri')) || '';
                        const what = /chart/i.test(uri) ? 'chart' : /diagram/i.test(uri) ? 'diagram' : 'object';
                        slide.items.push({type: 'unsupported', what});
                    }
                } else if (shape.local === 'pic') {
                    const blipFill = child(shape, 'blipFill');
                    const blip = blipFill && child(blipFill, 'blip');
                    const id = blip && attrNS(blip, 'embed', REL_NS);
                    const target = id && slideRels[id];
                    if (!target || target.external) {
                        slide.items.push({type: 'unsupported', what: 'linked picture'});
                        return;
                    }
                    const bytes = files[target.target];
                    const mime = imageMime(bytes);
                    if (!mime) {
                        slide.items.push({type: 'unsupported', what: 'picture in an unsupported format'});
                        return;
                    }
                    if (++imageCount > limits.maxImagesPerSlide) return;
                    images[target.target] = {mime, bytes};
                    slide.items.push({type: 'image', part: target.target});
                }
            });
        }
        slide.notes = notesOf(files, rel.target, slideRels);
    });
    return {slides, images};
}
