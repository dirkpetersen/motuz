/**
 * Previous/Next navigation for the image viewer: stepping over the image files of the
 * pane the viewer was opened from (utils/viewerKind.js image extensions, PNG/JPEG/GIF/
 * WebP/HEIC/HEIF/AVIF by extension; the server still decides the real type), in the
 * pane's displayed (sorted) order (state.pane.files[side]). Plain functions for
 * node --test; ImageViewerDialog.jsx keeps the actual fetching/caching.
 */
import { viewerKind } from './viewerKind.js';

/** Indexes into `files` (pane order) that the image viewer can open: images, not folders */
export function imageIndexes(files) {
    const indexes = [];
    (files || []).forEach((file, index) => {
        if (file && file.type !== 'dir' && viewerKind(file.name) === 'image') {
            indexes.push(index);
        }
    });
    return indexes;
}

/**
 * Navigation info for the image at `files[currentIndex]` among the pane's image files:
 * {position, total, prevIndex, nextIndex} — `position` is 1-based (e.g. 3 of 12),
 * `prevIndex`/`nextIndex` are indexes into `files`, or null at an end (never wraps
 * around). Returns null if `currentIndex` is not one of the pane's image files right
 * now (e.g. the listing changed while the viewer was open).
 */
export function imageNeighbors(files, currentIndex) {
    const indexes = imageIndexes(files);
    const at = indexes.indexOf(currentIndex);
    if (at < 0) {
        return null;
    }
    return {
        position: at + 1,
        total: indexes.length,
        prevIndex: at > 0 ? indexes[at - 1] : null,
        nextIndex: at < indexes.length - 1 ? indexes[at + 1] : null,
    };
}

/**
 * The index in `files` of the file called `name` (null if it is not there): the pane
 * may have been refreshed while the viewer was open, so a position remembered from
 * before is looked up again by name.
 */
export function rowIndexOf(files, name) {
    const index = (files || []).findIndex(file => file && file.name === name);
    return index < 0 ? null : index;
}
