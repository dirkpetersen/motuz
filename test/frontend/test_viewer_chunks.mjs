// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    MAX_CHUNKS,
    appendChunk,
    describeRange,
    loadedRange,
    positionPercent,
    prependChunk,
    replaceChunks,
} from '../../src/frontend/js/utils/viewerChunks.js';

const SIZE = 1000;
const chunk = (offset, end) => ({offset, end, content: `${offset}-${end}\n`, bof: offset === 0, eof: end === SIZE});

test('a window starts with one chunk', () => {
    const chunks = replaceChunks(chunk(0, 100));
    assert.equal(chunks.length, 1);
    assert.deepEqual(loadedRange(chunks), {offset: 0, end: 100, bof: true, eof: false});
    assert.equal(loadedRange([]), null);
});

test('appending keeps the window contiguous and drops chunks from the top', () => {
    let chunks = replaceChunks(chunk(0, 100));
    for (let offset = 100; offset < SIZE; offset += 100) {
        ({chunks} = appendChunk(chunks, chunk(offset, offset + 100), 4));
    }
    assert.deepEqual(chunks.map(c => c.offset), [600, 700, 800, 900]);
    assert.deepEqual(loadedRange(chunks), {offset: 600, end: 1000, bof: false, eof: true});
    const result = appendChunk(chunks, chunk(1000, 1000), 4);
    assert.equal(result.dropped, 1); // an empty chunk at EOF still counts, but keeps contiguity
});

test('an answer for another window is ignored', () => {
    const chunks = replaceChunks(chunk(100, 200));
    assert.equal(appendChunk(chunks, chunk(300, 400)).chunks, chunks);
    assert.equal(prependChunk(chunks, chunk(0, 50)).chunks, chunks);
    assert.equal(appendChunk([], chunk(0, 100)).chunks.length, 0);
});

test('prepending drops chunks from the bottom', () => {
    let chunks = replaceChunks(chunk(900, 1000));
    let dropped = 0;
    for (let end = 900; end > 0; end -= 100) {
        const r = prependChunk(chunks, chunk(end - 100, end), 3);
        chunks = r.chunks;
        dropped += r.dropped;
    }
    assert.deepEqual(chunks.map(c => c.offset), [0, 100, 200]);
    assert.equal(dropped, 7);
    assert.deepEqual(loadedRange(chunks), {offset: 0, end: 300, bof: true, eof: false});
});

test('the default cap is 16 chunks', () => {
    let chunks = replaceChunks({offset: 0, end: 1, content: 'x', bof: true});
    for (let i = 1; i < 40; i++) {
        ({chunks} = appendChunk(chunks, {offset: i, end: i + 1, content: 'x'}));
    }
    assert.equal(MAX_CHUNKS, 16);
    assert.equal(chunks.length, MAX_CHUNKS);
    assert.equal(chunks[0].offset, 40 - MAX_CHUNKS);
});

test('position in percent', () => {
    const range = {offset: 0, end: 500, bof: true, eof: false};
    assert.equal(positionPercent(range, 1000, {scrollTop: 0, scrollHeight: 1000, clientHeight: 100}), 5);
    assert.equal(positionPercent(range, 1000, {scrollTop: 900, scrollHeight: 1000, clientHeight: 100}), 50);
    const tail = {offset: 800, end: 1000, bof: false, eof: true};
    assert.equal(positionPercent(tail, 1000, {scrollTop: 900, scrollHeight: 1000, clientHeight: 100}), 100);
    assert.equal(positionPercent(tail, 1000, {scrollTop: 0, scrollHeight: 1000, clientHeight: 100}), 82);
    assert.equal(positionPercent(range, 0, {scrollTop: 0, scrollHeight: 0, clientHeight: 0}), 100);
});

test('range description', () => {
    assert.equal(describeRange({offset: 0, end: 262144}, 5500000), 'bytes 0–262,144 of 5,500,000');
    assert.equal(describeRange(null, 5), '');
});
