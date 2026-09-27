/**
 * The window of chunks the file viewer (pager) keeps in memory. A chunk is the API's
 * answer to POST /api/system/files/view/chunk/: {offset, end, content, bof, eof}, where
 * offset/end is the byte range of the file its (whole-line) content covers. The window
 * is contiguous: each chunk starts where the previous one ends. At most `max` chunks
 * are kept; adding one at one end drops chunks from the other end, which are fetched
 * again when the user scrolls back.
 *
 * Plain functions (no JSX) so test/frontend can run them with node --test.
 */

// 16 chunks of at most 256 KiB (the server's chunk size): at most 4 MiB of text in the
// DOM, a few ten thousand lines, which keeps scrolling and layout responsive
export const MAX_CHUNKS = 16;

function toChunk(result) {
    return {
        key: `${result.offset}-${result.end}`,
        offset: result.offset,
        end: result.end,
        content: result.content || '',
        bof: !!result.bof,
        eof: !!result.eof,
    };
}

/** A new window with only this chunk (first load, jump to top or bottom) */
export function replaceChunks(result) {
    return [toChunk(result)];
}

/**
 * The window with `result` added after its last chunk, and the chunks dropped from
 * the top to keep at most `max`. The window is unchanged if `result` does not start
 * where the window ends (an answer for a window that has changed meanwhile).
 */
export function appendChunk(chunks, result, max = MAX_CHUNKS) {
    const last = chunks[chunks.length - 1];
    if (!last || result.offset !== last.end) {
        return {chunks, dropped: 0};
    }
    const next = [...chunks, toChunk(result)];
    const dropped = Math.max(0, next.length - max);
    return {chunks: dropped ? next.slice(dropped) : next, dropped};
}

// Follow mode appends many small answers (a few new lines each). They are merged into
// the last chunk while it stays within a server chunk (256 KiB), so the window still
// holds up to MAX_CHUNKS full chunks and not MAX_CHUNKS polls' worth of lines.
export const MERGE_BYTES = 256 * 1024;

/**
 * The window with a follow poll's `result` appended: merged into the last chunk (which
 * keeps its key, so the DOM keeps its place) when both fit into `mergeBytes`, else as a
 * new chunk, dropping chunks from the top to keep at most `max`. `appended` is false,
 * and the window unchanged, if `result` does not start where the window ends (the user
 * scrolled far enough up that the bottom was dropped; the viewer then only counts the
 * new lines).
 */
export function appendFollowChunk(chunks, result, max = MAX_CHUNKS, mergeBytes = MERGE_BYTES) {
    const last = chunks[chunks.length - 1];
    if (!last || result.offset !== last.end) {
        return {chunks, dropped: 0, appended: false};
    }
    if (result.end <= result.offset) {
        return {chunks, dropped: 0, appended: true};
    }
    if ((last.end - last.offset) + (result.end - result.offset) <= mergeBytes) {
        const merged = {...last, end: result.end, content: last.content + (result.content || ''), eof: !!result.eof};
        return {chunks: [...chunks.slice(0, -1), merged], dropped: 0, appended: true};
    }
    return {...appendChunk(chunks, result, max), appended: true};
}

/** Like appendChunk, before the first chunk, dropping chunks from the bottom */
export function prependChunk(chunks, result, max = MAX_CHUNKS) {
    const first = chunks[0];
    if (!first || result.end !== first.offset) {
        return {chunks, dropped: 0};
    }
    const next = [toChunk(result), ...chunks];
    const dropped = Math.max(0, next.length - max);
    return {chunks: dropped ? next.slice(0, next.length - dropped) : next, dropped};
}

/** {offset, end, bof, eof} of the window, or null if it is empty */
export function loadedRange(chunks) {
    if (!chunks || chunks.length === 0) {
        return null;
    }
    const first = chunks[0];
    const last = chunks[chunks.length - 1];
    return {offset: first.offset, end: last.end, bof: first.bof, eof: last.eof};
}

/**
 * How far into the file the bottom of the viewport is, in percent (0-100), estimated
 * from the scroll position within the loaded window.
 */
export function positionPercent(range, size, scroll) {
    if (!range || !size) {
        return 100;
    }
    const {scrollTop, scrollHeight, clientHeight} = scroll;
    const fraction = scrollHeight > 0 ? Math.min(1, (scrollTop + clientHeight) / scrollHeight) : 1;
    if (range.eof && scrollHeight - scrollTop - clientHeight < 2) {
        return 100;
    }
    const position = range.offset + fraction * (range.end - range.offset);
    return Math.max(0, Math.min(100, Math.floor(100 * position / size)));
}

/** "bytes 0–262,144 of 5,500,000" */
export function describeRange(range, size) {
    if (!range) {
        return '';
    }
    const n = value => Number(value).toLocaleString('en-US');
    return `bytes ${n(range.offset)}–${n(range.end)} of ${n(size != null ? size : range.end)}`;
}
