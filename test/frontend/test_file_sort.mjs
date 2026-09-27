// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    compareFiles,
    nextSort,
    normalizeSort,
    resortWithSelection,
    sortFiles,
} from '../../src/frontend/js/utils/fileSort.js';

const UP = {name: '..', type: 'dir', size: 'Folder'};
const file = (name, size, modified) => ({name, type: 'file', size, modified});
const dir = (name, modified) => ({name, type: 'dir', size: 4096, modified});

const FILES = [
    file('file10.txt', 10, '2026-09-20T00:00:00Z'),
    dir('Zeta', '2026-09-01T00:00:00Z'),
    file('File2.txt', 300, '2026-09-27T00:00:00Z'),
    file('big.bin', 5000, null),
    dir('alpha', '2026-09-25T00:00:00Z'),
    UP,
    file('file1.txt', 300, '2026-01-01T00:00:00Z'),
    {name: 'Loading...'},
];
const names = files => files.map(f => f.name);

test('name: .. first, folders first, case-insensitive natural order', () => {
    assert.deepEqual(names(sortFiles(FILES, {column: 'name', asc: true})),
        ['..', 'alpha', 'Zeta', 'big.bin', 'file1.txt', 'File2.txt', 'file10.txt', 'Loading...']);
    assert.deepEqual(names(sortFiles(FILES, {column: 'name', asc: false})),
        ['..', 'Zeta', 'alpha', 'file10.txt', 'File2.txt', 'file1.txt', 'big.bin', 'Loading...']);
});

test('age: ascending is newest first, unknown times last in both directions', () => {
    assert.deepEqual(names(sortFiles(FILES, {column: 'age', asc: true})),
        ['..', 'alpha', 'Zeta', 'File2.txt', 'file10.txt', 'file1.txt', 'big.bin', 'Loading...']);
    assert.deepEqual(names(sortFiles(FILES, {column: 'age', asc: false})),
        ['..', 'Zeta', 'alpha', 'file1.txt', 'file10.txt', 'File2.txt', 'big.bin', 'Loading...']);
});

test('size: files by size (ties by name), folders stay A-Z', () => {
    assert.deepEqual(names(sortFiles(FILES, {column: 'size', asc: true})),
        ['..', 'alpha', 'Zeta', 'file10.txt', 'file1.txt', 'File2.txt', 'big.bin', 'Loading...']);
    assert.deepEqual(names(sortFiles(FILES, {column: 'size', asc: false})),
        ['..', 'alpha', 'Zeta', 'big.bin', 'file1.txt', 'File2.txt', 'file10.txt', 'Loading...']);
    // Unknown sizes (null, e.g. a broken symlink) last
    const withUnknown = [file('a', null), file('b', 1), file('c', '20')];
    assert.deepEqual(names(sortFiles(withUnknown, {column: 'size', asc: false})), ['c', 'b', 'a']);
});

test('comparator basics', () => {
    assert.ok(compareFiles(UP, dir('a'), {column: 'name', asc: false}) < 0);
    assert.ok(compareFiles(dir('z'), file('a', 1), {column: 'name', asc: false}) < 0);
    assert.equal(compareFiles(file('a', 1), file('a', 1)), 0);
    assert.ok(compareFiles(file('a2', 1), file('a10', 1)) < 0);
});

test('sortFiles does not modify its input', () => {
    const copy = FILES.slice();
    sortFiles(FILES, {column: 'size', asc: false});
    assert.deepEqual(FILES, copy);
});

test('header clicks: the same column toggles, another starts ascending', () => {
    assert.deepEqual(nextSort({column: 'name', asc: true}, 'name'), {column: 'name', asc: false});
    assert.deepEqual(nextSort({column: 'name', asc: false}, 'name'), {column: 'name', asc: true});
    assert.deepEqual(nextSort({column: 'name', asc: false}, 'age'), {column: 'age', asc: true});
    assert.deepEqual(nextSort(undefined, 'size'), {column: 'size', asc: true});
    assert.deepEqual(normalizeSort({column: 'bogus'}), {column: 'name', asc: true});
});

test('re-sorting keeps the same files selected', () => {
    const files = sortFiles(FILES, {column: 'name', asc: true});
    // select file1.txt (focus) and File2.txt
    const focus = files.findIndex(f => f.name === 'file1.txt');
    const other = files.findIndex(f => f.name === 'File2.txt');
    const result = resortWithSelection(files, {column: 'size', asc: false}, focus, {[focus]: true, [other]: true});
    assert.equal(result.files[result.fileFocusIndex].name, 'file1.txt');
    assert.deepEqual(Object.keys(result.fileMultiFocusIndexes).map(i => result.files[i].name).sort(),
        ['File2.txt', 'file1.txt']);
    // The '..' row selected (the default) stays selected
    const up = resortWithSelection(files, {column: 'age', asc: false}, 0, {0: true});
    assert.equal(up.files[up.fileFocusIndex].name, '..');
});
