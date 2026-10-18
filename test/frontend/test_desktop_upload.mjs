// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {droppedItems, collectFiles, uploadPathError, topLevelNames} from '../../src/frontend/js/utils/desktopDrop.js';
import {runUpload} from '../../src/frontend/js/utils/uploadRunner.js';

// FileSystemEntry fakes: readEntries returns at most 2 entries per call, like browsers do in batches
function file(name, data='') {
    return {name, isFile: true, isDirectory: false, file: ok => ok({name, size: data.length, data})};
}
function folder(name, children) {
    return {
        name, isFile: false, isDirectory: true,
        createReader: () => {
            let position = 0;
            return {readEntries: ok => { const batch = children.slice(position, position + 2); position += batch.length; ok(batch); }};
        },
    };
}
function unreadable(name) {
    return {name, isFile: true, isDirectory: false, file: (ok, fail) => fail(new Error('NotReadableError'))};
}

test('folders are walked, with paths relative to the drop', async () => {
    const dropped = {entries: [
        folder('photos', [file('a.jpg', 'aa'), folder('sub', [file('b.txt'), file('c.txt'), file('d.txt')]), folder('empty', [])]),
        file('top.txt', 'x'),
    ]};
    const files = await collectFiles(dropped);
    assert.deepEqual(files.map(f => f.path), ['photos/a.jpg', 'photos/sub/b.txt', 'photos/sub/c.txt', 'photos/sub/d.txt', 'top.txt']);
    assert.equal(files[0].file.size, 2);
});

test('the limit and unreadable files are errors for the user', async () => {
    await assert.rejects(collectFiles({entries: [folder('f', [file('a'), file('b'), file('c')])]}, 2), /more than 2 files/);
    await assert.rejects(collectFiles({entries: [folder('f', [unreadable('secret')])]}), /Could not read "f\/secret"/);
});

test('without entries, the plain files are used', async () => {
    const dataTransfer = {items: [{kind: 'file'}], files: [{name: 'a.txt', size: 1}]};
    const dropped = droppedItems(dataTransfer);
    assert.deepEqual(await collectFiles(dropped), [{path: 'a.txt', file: {name: 'a.txt', size: 1}}]);
    const withEntries = droppedItems({items: [{kind: 'file', webkitGetAsEntry: () => file('x')}, {kind: 'string'}]});
    assert.equal(withEntries.entries.length, 1);
});

test('names the server refuses', () => {
    for (const path of ['', '../x', 'a/../b', 'a//b', './a', 'a/', 'tab\there', 'x'.repeat(256), 'é'.repeat(128)]) {
        assert.ok(uploadPathError(path), path);
    }
    for (const path of ['a', 'photos/2026/IMG 0001.HEIC', 'é'.repeat(127), '.hidden', 'a..b']) {
        assert.equal(uploadPathError(path), null, path);
    }
});

test('top-level names', () => {
    assert.deepEqual(topLevelNames(['photos/a', 'photos/b/c', 'top.txt']), ['photos', 'top.txt']);
});

// A server that takes the chunks of each file in order (managers/upload_manager.put_chunk)
function server({sizes, failAt={}, networkErrors=0, stopAfter=Infinity}) {
    const written = {}, received = {};
    let errors = networkErrors, chunks = 0;
    const putChunk = async (index, offset, blob) => {
        if (errors > 0) {
            errors--;
            return {ok: false, status: null, body: null};
        }
        if (chunks++ >= stopAfter) {
            return {ok: false, status: 409, body: {message: 'The job is stopped', job_state: 'STOPPED'}};
        }
        if (offset === 0) {
            written[index] = 0;
            received[index] = '';
        }
        if (written[index] === undefined || offset !== written[index]) {
            return {ok: false, status: 409, body: {written: written[index] ?? null}};
        }
        written[index] += blob.length;
        received[index] += blob;
        const done = written[index] === sizes[index];
        const state = done ? (failAt[index] ? 'failed' : 'done') : 'pending';
        return {ok: true, status: 200, body: {file_state: state, written: written[index], job_state: 'PROGRESS'}};
    };
    return {putChunk, received};
}

// A string has slice() and length, like a Blob has slice() and size
const asFile = data => ({size: data.length, slice: (start, end) => data.slice(start, end)});

test('files are sent in chunks, in order', async () => {
    const data = ['0123456789', '', 'abc'];
    const {putChunk, received} = server({sizes: data.map(d => d.length)});
    const outcome = await runUpload({files: data.map(d => ({file: asFile(d)})), pending: [0, 1, 2], chunkBytes: 4, putChunk});
    assert.deepEqual(outcome, {done: 3, failed: 0, skipped: 0, stopped: false});
    assert.deepEqual(received, {0: '0123456789', 1: '', 2: 'abc'});
});

test('network errors are retried', async () => {
    const delays = [];
    const {putChunk, received} = server({sizes: [10], networkErrors: 2});
    const outcome = await runUpload({files: [{file: asFile('0123456789')}], pending: [0], chunkBytes: 4, putChunk,
                                     sleep: async ms => delays.push(ms), retryDelays: [1, 2, 3]});
    assert.equal(outcome.done, 1);
    assert.deepEqual(delays, [1, 2]);
    assert.equal(received[0], '0123456789');
});

test('too many network errors fail the file', async () => {
    const {putChunk} = server({sizes: [10], networkErrors: 5});
    const outcome = await runUpload({files: [{file: asFile('0123456789')}], pending: [0], chunkBytes: 4, putChunk,
                                     sleep: async () => {}, retryDelays: [1, 2]});
    assert.deepEqual(outcome, {done: 0, failed: 1, skipped: 0, stopped: false});
});

test('the client goes on where the server is', async () => {
    let first = true;
    const inner = server({sizes: [10]});
    // The first chunk arrived only in part: the next one gets 409 with written
    const putChunk = async (index, offset, blob) => {
        if (first && offset === 4) {
            first = false;
            await inner.putChunk(index, offset, blob.slice(0, 2));
            return {ok: false, status: null, body: null};
        }
        return inner.putChunk(index, offset, blob);
    };
    const outcome = await runUpload({files: [{file: asFile('0123456789')}], pending: [0], chunkBytes: 4, putChunk,
                                     sleep: async () => {}});
    assert.equal(outcome.done, 1);
    assert.equal(inner.received[0], '0123456789');
});

test('a stopped job stops all files; a failed file is counted', async () => {
    const {putChunk} = server({sizes: [10, 10, 10, 10], stopAfter: 3});
    const files = ['0123456789', '0123456789', '0123456789', '0123456789'].map(d => ({file: asFile(d)}));
    const outcome = await runUpload({files, pending: [0, 1, 2, 3], chunkBytes: 4, putChunk, concurrency: 1});
    assert.equal(outcome.stopped, true);
    assert.equal(outcome.done, 1, 'the first file took the 3 chunks before the stop');

    const failing = server({sizes: [3], failAt: {0: true}});
    assert.deepEqual(await runUpload({files: [{file: asFile('abc')}], pending: [0], chunkBytes: 4, putChunk: failing.putChunk}),
                     {done: 0, failed: 1, skipped: 0, stopped: false});
});

test('client errors are not retried', async () => {
    let calls = 0;
    const putChunk = async () => { calls++; return {ok: false, status: 404, body: {message: 'not found'}}; };
    const outcome = await runUpload({files: [{file: asFile('abc')}], pending: [0], chunkBytes: 4, putChunk});
    assert.equal(outcome.failed, 1);
    assert.equal(calls, 1);
});
