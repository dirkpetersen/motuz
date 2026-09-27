// Sorting of the pane listings. Plain JS (no JSX), so test/frontend can import it.
//
// Files are {name, type, size, modified}; `..` (type 'dir') and the placeholder rows
// (Loading..., ERROR: no type) are special. The panes keep their files in the sorted
// (displayed) order, so selection indexes always refer to what the user sees.

import {parseModified} from './fileAge.js';

export const SORT_COLUMNS = ['name', 'age', 'size'];
export const DEFAULT_SORT = Object.freeze({column: 'name', asc: true});

// Case-insensitive, natural ("file2" before "file10")
const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});

export function normalizeSort(sort) {
    if (!sort || !SORT_COLUMNS.includes(sort.column)) {
        return DEFAULT_SORT;
    }
    return {column: sort.column, asc: sort.asc !== false};
}

/** The sort after clicking a column header: the same column toggles, another starts ascending */
export function nextSort(sort, column) {
    const current = normalizeSort(sort);
    if (current.column === column) {
        return {column, asc: !current.asc};
    }
    return {column, asc: true};
}

function rank(file) {
    if (file.name === '..' && file.type === 'dir') {
        return 0; // always on top
    }
    if (!file.type) {
        return 3; // placeholder rows
    }
    return file.type === 'dir' ? 1 : 2; // folders before files, in every sort
}

function byName(a, b) {
    return collator.compare(a.name, b.name) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
}

function sizeOf(file) {
    const size = typeof file.size === 'number' ? file.size : Number.parseInt(file.size, 10);
    return Number.isFinite(size) && size >= 0 ? size : null;
}

/**
 * Compares by a column. Ascending means: name A-Z, age newest first (the smallest age),
 * size smallest first. Unknown ages and sizes go last in both directions, ties are
 * broken by name (A-Z). Folders have no meaningful size: sorted by size they stay A-Z.
 */
export function compareFiles(a, b, sort = DEFAULT_SORT) {
    const {column, asc} = normalizeSort(sort);
    const rankDiff = rank(a) - rank(b);
    if (rankDiff !== 0 || rank(a) === 0 || rank(a) === 3) {
        return rankDiff;
    }
    const direction = asc ? 1 : -1;

    if (column === 'name') {
        return byName(a, b) * direction;
    }

    let aValue, bValue;
    if (column === 'age') {
        // Newer (larger time) first when ascending by age
        aValue = parseModified(a.modified);
        bValue = parseModified(b.modified);
        if (aValue !== null) aValue = -aValue;
        if (bValue !== null) bValue = -bValue;
    } else if (a.type === 'dir') {
        return byName(a, b);
    } else {
        aValue = sizeOf(a);
        bValue = sizeOf(b);
    }

    if (aValue === null || bValue === null) {
        if (aValue === bValue) {
            return byName(a, b);
        }
        return aValue === null ? 1 : -1;
    }
    return (aValue - bValue) * direction || byName(a, b);
}

/** A new array, sorted; the original is not modified */
export function sortFiles(files, sort = DEFAULT_SORT) {
    const normalized = normalizeSort(sort);
    return files
        .map((file, index) => ({file, index}))
        .sort((x, y) => compareFiles(x.file, y.file, normalized) || x.index - y.index)
        .map(({file}) => file);
}

/**
 * Re-sorts `files` and moves the selection along: `multi` ({index: true}) and `focus`
 * (index) refer to `files` and are returned for the new order, so the same files stay
 * selected.
 */
export function resortWithSelection(files, sort, focus, multi) {
    const sorted = sortFiles(files, sort);
    const newIndex = new Map(sorted.map((file, index) => [file, index]));
    const moved = i => newIndex.has(files[i]) ? newIndex.get(files[i]) : null;

    const fileMultiFocusIndexes = {};
    for (const key of Object.keys(multi || {})) {
        const index = moved(Number(key));
        if (index !== null && multi[key]) {
            fileMultiFocusIndexes[index] = true;
        }
    }
    let fileFocusIndex = moved(focus);
    if (fileFocusIndex === null) {
        fileFocusIndex = 0;
    }
    if (Object.keys(fileMultiFocusIndexes).length === 0) {
        fileMultiFocusIndexes[fileFocusIndex] = true;
    }
    return {files: sorted, fileFocusIndex, fileMultiFocusIndexes};
}
