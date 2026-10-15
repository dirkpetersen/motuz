/**
 * Reading ZIP files (DOCX, XLSX, PPTX, ODS) in the document viewer without trusting
 * them: the central directory is checked against limits before anything is inflated
 * (entry count, declared sizes, compression methods, encryption, duplicate names), and
 * each entry is inflated in small steps into a buffer of its declared size, so an entry
 * that inflates to more than it declares (a zip bomb with a lying directory) is
 * stopped after at most one step. `rezip` writes the checked entries into a new,
 * uncompressed ZIP, so the renderers (docx-preview's JSZip, SheetJS) parse a file whose
 * every entry was inflated here with these limits, not the original one.
 *
 * Plain functions (no JSX, no DOM) so test/frontend can run them with node --test, and
 * the document worker (workers/documentWorker.js) can use them.
 */
import {Inflate, zipSync} from 'fflate';

const MiB = 1024 * 1024;

export const ZIP_LIMITS = Object.freeze({
    maxEntries: 10000,
    maxTotalUncompressed: 200 * MiB,
    maxEntryUncompressed: 200 * MiB,
});

// Compressed bytes inflated per step: deflate expands at most ~1032:1, so one step
// produces at most ~16 MiB before the size check runs again
const INFLATE_STEP = 16 * 1024;

const SIG_EOCD = 0x06054b50;
const SIG_ZIP64_LOCATOR = 0x07064b50;
const SIG_ZIP64_EOCD = 0x06064b50;
const SIG_CENTRAL = 0x02014b50;
const SIG_LOCAL = 0x04034b50;
const EOCD_SIZE = 22;
const MAX_COMMENT = 0xffff;

export class ZipError extends Error {
    constructor(message) {
        super(message);
        this.name = 'ZipError';
    }
}

/** A limit was exceeded (zip bomb protection); the message says which */
export class ZipLimitError extends ZipError {
    constructor(message) {
        super(message);
        this.name = 'ZipLimitError';
    }
}

function formatMiB(bytes) {
    return `${Math.round(bytes / MiB)} MiB`;
}

function toBytes(data) {
    if (data instanceof Uint8Array) {
        return data;
    }
    if (data instanceof ArrayBuffer) {
        return new Uint8Array(data);
    }
    if (ArrayBuffer.isView(data)) {
        return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    }
    throw new ZipError('Not binary data');
}

function big(view, offset) {
    const value = view.getBigUint64(offset, true);
    if (value > BigInt(Number.MAX_SAFE_INTEGER)) {
        throw new ZipError('Invalid ZIP file (size out of range)');
    }
    return Number(value);
}

/** True if `bytes` starts like a ZIP file (a local file header) */
export function isZip(data) {
    const bytes = toBytes(data);
    return bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 0x03 && bytes[3] === 0x04;
}

function findEocd(bytes, view) {
    const last = bytes.length - EOCD_SIZE;
    const first = Math.max(0, last - MAX_COMMENT);
    for (let i = last; i >= first; i--) {
        if (bytes[i] === 0x50 && bytes[i + 1] === 0x4b && view.getUint32(i, true) === SIG_EOCD
                && i + EOCD_SIZE + view.getUint16(i + 20, true) === bytes.length) {
            return i;
        }
    }
    throw new ZipError('Not a ZIP file (no central directory)');
}

/**
 * {count, size, offset} of the central directory, from the end of central directory
 * record (and the ZIP64 one when the classic fields overflow)
 */
function readDirectoryLocation(bytes, view) {
    const eocd = findEocd(bytes, view);
    let disk = view.getUint16(eocd + 4, true);
    let cdDisk = view.getUint16(eocd + 6, true);
    let count = view.getUint16(eocd + 10, true);
    let diskCount = view.getUint16(eocd + 8, true);
    let size = view.getUint32(eocd + 12, true);
    let offset = view.getUint32(eocd + 16, true);

    if (count === 0xffff || diskCount === 0xffff || size === 0xffffffff || offset === 0xffffffff) {
        const locator = eocd - 20;
        if (locator < 0 || view.getUint32(locator, true) !== SIG_ZIP64_LOCATOR) {
            throw new ZipError('Invalid ZIP64 file (no locator)');
        }
        const zip64 = big(view, locator + 8);
        if (zip64 + 56 > bytes.length || view.getUint32(zip64, true) !== SIG_ZIP64_EOCD) {
            throw new ZipError('Invalid ZIP64 file (no end of central directory)');
        }
        disk = view.getUint32(zip64 + 16, true);
        cdDisk = view.getUint32(zip64 + 20, true);
        diskCount = big(view, zip64 + 24);
        count = big(view, zip64 + 32);
        size = big(view, zip64 + 40);
        offset = big(view, zip64 + 48);
    }
    if (disk !== 0 || cdDisk !== 0 || diskCount !== count) {
        throw new ZipError('Split (multi-part) ZIP files are not supported');
    }
    if (offset + size > bytes.length) {
        throw new ZipError('Invalid ZIP file (central directory out of range)');
    }
    return {count, size, offset};
}

/** The ZIP64 extended information (extra field 0x0001) of a central directory entry */
function applyZip64Extra(view, start, length, entry) {
    let pos = start;
    const end = start + length;
    while (pos + 4 <= end) {
        const id = view.getUint16(pos, true);
        const size = view.getUint16(pos + 2, true);
        const data = pos + 4;
        if (data + size > end) {
            break;
        }
        if (id === 0x0001) {
            let field = data;
            const next = () => {
                if (field + 8 > data + size) {
                    throw new ZipError('Invalid ZIP64 extra field');
                }
                const value = big(view, field);
                field += 8;
                return value;
            };
            if (entry.uncompressedSize === 0xffffffff) entry.uncompressedSize = next();
            if (entry.compressedSize === 0xffffffff) entry.compressedSize = next();
            if (entry.localHeaderOffset === 0xffffffff) entry.localHeaderOffset = next();
            return;
        }
        pos = data + size;
    }
    if (entry.uncompressedSize === 0xffffffff || entry.compressedSize === 0xffffffff
            || entry.localHeaderOffset === 0xffffffff) {
        throw new ZipError('Invalid ZIP64 file (missing extra field)');
    }
}

const utf8 = new TextDecoder('utf-8');

/**
 * The entries of a ZIP file from its central directory, checked against `limits`
 * (ZIP_LIMITS) before anything is inflated: {entries, totalUncompressed}. Each entry
 * is {name, method, flags, compressedSize, uncompressedSize, localHeaderOffset}.
 * Throws ZipLimitError for too many entries or too much declared data, ZipError for
 * encrypted entries, compression other than stored/deflate, duplicate names or a
 * damaged directory.
 */
export function readCentralDirectory(data, limits = ZIP_LIMITS) {
    const bytes = toBytes(data);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const {count, size, offset} = readDirectoryLocation(bytes, view);
    if (count > limits.maxEntries) {
        throw new ZipLimitError(`The file has ${count} entries; at most ${limits.maxEntries} are allowed`);
    }

    const entries = [];
    const names = new Set();
    let totalUncompressed = 0;
    let pos = offset;
    const end = offset + size;
    for (let i = 0; i < count; i++) {
        if (pos + 46 > end || view.getUint32(pos, true) !== SIG_CENTRAL) {
            throw new ZipError('Invalid ZIP file (damaged central directory)');
        }
        const flags = view.getUint16(pos + 8, true);
        const nameLength = view.getUint16(pos + 28, true);
        const extraLength = view.getUint16(pos + 30, true);
        const commentLength = view.getUint16(pos + 32, true);
        const entry = {
            name: utf8.decode(bytes.subarray(pos + 46, pos + 46 + nameLength)),
            method: view.getUint16(pos + 10, true),
            flags,
            compressedSize: view.getUint32(pos + 20, true),
            uncompressedSize: view.getUint32(pos + 24, true),
            localHeaderOffset: view.getUint32(pos + 42, true),
        };
        if (pos + 46 + nameLength + extraLength + commentLength > end) {
            throw new ZipError('Invalid ZIP file (damaged central directory)');
        }
        applyZip64Extra(view, pos + 46 + nameLength, extraLength, entry);
        pos += 46 + nameLength + extraLength + commentLength;

        if (flags & 0x0001) {
            throw new ZipError('The file is encrypted (password protected)');
        }
        if (entry.method !== 0 && entry.method !== 8) {
            throw new ZipError(`Unsupported compression method ${entry.method} in '${entry.name}'`);
        }
        if (names.has(entry.name)) {
            throw new ZipError(`Invalid ZIP file (duplicate entry '${entry.name}')`);
        }
        names.add(entry.name);
        if (entry.uncompressedSize > limits.maxEntryUncompressed) {
            throw new ZipLimitError(`'${entry.name}' would be ${formatMiB(entry.uncompressedSize)} uncompressed; `
                + `at most ${formatMiB(limits.maxEntryUncompressed)} are allowed`);
        }
        totalUncompressed += entry.uncompressedSize;
        if (totalUncompressed > limits.maxTotalUncompressed) {
            throw new ZipLimitError(`The file would be more than ${formatMiB(limits.maxTotalUncompressed)} `
                + 'uncompressed, too large to preview');
        }
        if (entry.method === 0 && entry.compressedSize !== entry.uncompressedSize) {
            throw new ZipError(`Invalid ZIP file (sizes of stored entry '${entry.name}' differ)`);
        }
        if (entry.localHeaderOffset + 30 + entry.compressedSize > bytes.length) {
            throw new ZipError(`Invalid ZIP file ('${entry.name}' out of range)`);
        }
        entries.push(entry);
    }
    return {entries, totalUncompressed};
}

/** The compressed bytes of an entry, located through its local file header */
function entryData(bytes, view, entry) {
    const at = entry.localHeaderOffset;
    if (at + 30 > bytes.length || view.getUint32(at, true) !== SIG_LOCAL) {
        throw new ZipError(`Invalid ZIP file (no local header for '${entry.name}')`);
    }
    const start = at + 30 + view.getUint16(at + 26, true) + view.getUint16(at + 28, true);
    const end = start + entry.compressedSize;
    if (end > bytes.length) {
        throw new ZipError(`Invalid ZIP file ('${entry.name}' out of range)`);
    }
    return bytes.subarray(start, end);
}

/**
 * The uncompressed bytes of one entry (from readCentralDirectory). Deflated data is
 * inflated INFLATE_STEP compressed bytes at a time into a buffer of the declared size;
 * more output than declared stops it with a ZipLimitError, less is a ZipError.
 */
export function extractEntry(data, entry) {
    const bytes = toBytes(data);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const compressed = entryData(bytes, view, entry);
    if (entry.method === 0) {
        return compressed.slice();
    }
    const out = new Uint8Array(entry.uncompressedSize);
    let length = 0;
    const inflate = new Inflate((chunk) => {
        if (length + chunk.length > out.length) {
            throw new ZipLimitError(`'${entry.name}' inflates to more than the ${out.length} bytes it declares`);
        }
        out.set(chunk, length);
        length += chunk.length;
    });
    try {
        for (let pos = 0; pos < compressed.length; pos += INFLATE_STEP) {
            const next = Math.min(compressed.length, pos + INFLATE_STEP);
            inflate.push(compressed.subarray(pos, next), next === compressed.length);
        }
        if (compressed.length === 0) {
            inflate.push(new Uint8Array(0), true);
        }
    } catch (e) {
        if (e instanceof ZipError) {
            throw e;
        }
        throw new ZipError(`Invalid ZIP file ('${entry.name}' is damaged)`);
    }
    if (length !== out.length) {
        throw new ZipError(`Invalid ZIP file ('${entry.name}' is shorter than it declares)`);
    }
    return out;
}

/**
 * Checks the directory and extracts the entries for which `wanted(name)` is true (all
 * by default): {files: {name: Uint8Array}, entries}. Folder entries are skipped.
 */
export function readZip(data, {limits = ZIP_LIMITS, wanted = () => true} = {}) {
    const bytes = toBytes(data);
    const {entries} = readCentralDirectory(bytes, limits);
    const files = Object.create(null); // entry names are data, e.g. '__proto__'
    for (const entry of entries) {
        if (entry.name.endsWith('/') || !wanted(entry.name)) {
            continue;
        }
        files[entry.name] = extractEntry(bytes, entry);
    }
    return {files, entries};
}

/**
 * A new ZIP (stored, not compressed) with these files ({name: Uint8Array}). An entry
 * named '__proto__' (never an Office part) is left out: fflate collects entries in a
 * plain object.
 */
export function rezip(files) {
    const input = Object.create(null);
    for (const [name, content] of Object.entries(files)) {
        if (name !== '__proto__') {
            input[name] = [content, {level: 0}];
        }
    }
    return zipSync(input, {level: 0});
}
