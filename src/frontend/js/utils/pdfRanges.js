/**
 * Byte ranges of the PDF viewer. pdf.js asks for the parts of the file it needs
 * (range loading, PDFDataRangeTransport), and the viewer reads them with
 * POST /api/system/files/view/document/ {offset, length}, which reads at most
 * RANGE_MAX_BYTES per request, as the user (rclone cat --offset --count for cloud
 * files). So page 1 of a multi-GB PDF opens after a few small reads.
 * Plain functions for node --test.
 */

// Must not exceed the server's document_view.RANGE_MAX_BYTES
export const RANGE_MAX_BYTES = 4 * 1024 * 1024;
// The first read: the start of the file (header, and for small files all of it)
export const INITIAL_BYTES = 1024 * 1024;
// pdf.js requests whole chunks; bigger chunks mean fewer rclone calls for cloud files
export const LOCAL_CHUNK_BYTES = 256 * 1024;
export const CLOUD_CHUNK_BYTES = 1024 * 1024;
// Requests in flight at once
export const MAX_PARALLEL = 3;

/** [begin, end) split into [[begin, end), ...] pieces of at most `max` bytes */
export function splitRange(begin, end, max = RANGE_MAX_BYTES) {
    if (!(max > 0)) {
        throw new RangeError('max must be positive');
    }
    const pieces = [];
    for (let at = begin; at < end; at += max) {
        pieces.push([at, Math.min(end, at + max)]);
    }
    return pieces;
}

/** Joins the pieces read for a range into one buffer */
export function joinPieces(pieces) {
    const length = pieces.reduce((sum, piece) => sum + piece.byteLength, 0);
    const out = new Uint8Array(length);
    let at = 0;
    for (const piece of pieces) {
        out.set(piece instanceof Uint8Array ? piece : new Uint8Array(piece), at);
        at += piece.byteLength;
    }
    return out;
}

/**
 * Runs async tasks with at most `limit` at once: `run(task)` returns the task's promise.
 */
export function limiter(limit = MAX_PARALLEL) {
    let active = 0;
    const waiting = [];
    const next = () => {
        if (active >= limit || !waiting.length) return;
        active++;
        const {task, resolve, reject} = waiting.shift();
        Promise.resolve().then(task).then(resolve, reject).finally(() => {
            active--;
            next();
        });
    };
    return (task) => new Promise((resolve, reject) => {
        waiting.push({task, resolve, reject});
        next();
    });
}
