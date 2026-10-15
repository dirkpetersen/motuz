// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import { imageIndexes, imageNeighbors, rowIndexOf } from '../../src/frontend/js/utils/imageNeighbors.js';

const dir = (name) => ({name, type: 'dir'});
const file = (name) => ({name, type: 'file'});

const FILES = [
    dir('..'),
    dir('sub'),
    file('a.png'),
    file('notes.txt'),
    file('b.JPG'),
    file('c.heic'),
    file('logo.svg'), // not an image here (can carry scripts)
    file('d.avif'),
];

test('imageIndexes: images only, not folders, case-insensitive, SVG excluded', () => {
    assert.deepEqual(imageIndexes(FILES), [2, 4, 5, 7]);
    assert.deepEqual(imageIndexes([]), []);
    assert.deepEqual(imageIndexes(null), []);
    assert.deepEqual(imageIndexes(undefined), []);
});

test('imageNeighbors: position and neighbors among the image files, pane order', () => {
    assert.deepEqual(imageNeighbors(FILES, 2), {position: 1, total: 4, prevIndex: null, nextIndex: 4});
    assert.deepEqual(imageNeighbors(FILES, 4), {position: 2, total: 4, prevIndex: 2, nextIndex: 5});
    assert.deepEqual(imageNeighbors(FILES, 5), {position: 3, total: 4, prevIndex: 4, nextIndex: 7});
    assert.deepEqual(imageNeighbors(FILES, 7), {position: 4, total: 4, prevIndex: 5, nextIndex: null});
});

test('imageNeighbors: no wrap-around at the ends', () => {
    const first = imageNeighbors(FILES, 2);
    assert.equal(first.prevIndex, null);
    const last = imageNeighbors(FILES, 7);
    assert.equal(last.nextIndex, null);
});

test('imageNeighbors: a single image has no neighbors', () => {
    const files = [dir('..'), file('only.png')];
    assert.deepEqual(imageNeighbors(files, 1), {position: 1, total: 1, prevIndex: null, nextIndex: null});
});

test('imageNeighbors: null when the index is not an image (not found, a folder, or a non-image file)', () => {
    assert.equal(imageNeighbors(FILES, 0), null); // '..'
    assert.equal(imageNeighbors(FILES, 1), null); // a folder
    assert.equal(imageNeighbors(FILES, 3), null); // notes.txt
    assert.equal(imageNeighbors(FILES, 6), null); // logo.svg
    assert.equal(imageNeighbors(FILES, 99), null); // out of range
    assert.equal(imageNeighbors([], 0), null);
});

test('rowIndexOf: a remembered file is found again by name after the listing changed', () => {
    assert.equal(rowIndexOf(FILES, 'b.JPG'), 4);
    const refreshed = [dir('..'), file('new.png'), ...FILES.slice(1)]; // a file appeared above it
    assert.equal(rowIndexOf(refreshed, 'b.JPG'), 5);
    assert.equal(rowIndexOf(refreshed, 'gone.png'), null);
    assert.equal(rowIndexOf(null, 'a.png'), null);
});
