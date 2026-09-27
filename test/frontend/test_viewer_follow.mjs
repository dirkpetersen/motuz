// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CLOUD_POLL_MS,
    LOCAL_POLL_MS,
    MAX_POLL_MS,
    basePollDelay,
    countLines,
    followDecision,
    formatAgo,
    pollDelay,
} from '../../src/frontend/js/utils/viewerFollow.js';
import {MAX_CHUNKS, appendFollowChunk, loadedRange, replaceChunks} from '../../src/frontend/js/utils/viewerChunks.js';

const answer = (offset, content, size, eof = true) => ({
    offset, end: offset + Buffer.byteLength(content), content, size, bof: offset === 0, eof,
});

test('polls start at 2 s for local files and 10 s for cloud files', () => {
    assert.equal(basePollDelay(0), LOCAL_POLL_MS);
    assert.equal(basePollDelay(undefined), LOCAL_POLL_MS);
    assert.equal(basePollDelay(7), CLOUD_POLL_MS);
    assert.equal(LOCAL_POLL_MS, 2000);
    assert.equal(CLOUD_POLL_MS, 10000);
});

test('backoff: grows by 1.5 while nothing changes, up to 30 s', () => {
    const local = [0, 1, 2, 3, 4, 5, 6, 7, 8, 20].map(n => pollDelay(LOCAL_POLL_MS, n));
    assert.deepEqual(local, [2000, 3000, 4500, 6750, 10125, 15188, 22781, 30000, 30000, 30000]);
    const cloud = [0, 1, 2, 3, 4].map(n => pollDelay(CLOUD_POLL_MS, n));
    assert.deepEqual(cloud, [10000, 15000, 22500, 30000, 30000]);
    assert.equal(MAX_POLL_MS, 30000);
    assert.equal(pollDelay(LOCAL_POLL_MS, -3), LOCAL_POLL_MS); // reset
    // non-decreasing, never above the cap
    for (let n = 1; n < 30; n++) {
        assert.ok(pollDelay(LOCAL_POLL_MS, n) >= pollDelay(LOCAL_POLL_MS, n - 1));
        assert.ok(pollDelay(LOCAL_POLL_MS, n) <= MAX_POLL_MS);
    }
});

test('decision: new lines are appended, nothing new is unchanged', () => {
    assert.deepEqual(followDecision(100, answer(100, 'a\nb\n', 104)), {kind: 'append', more: false});
    assert.deepEqual(followDecision(100, answer(100, '', 100)), {kind: 'unchanged', more: false});
    // an incomplete last line (size > end) is not news yet
    assert.deepEqual(followDecision(100, answer(100, '', 107)), {kind: 'unchanged', more: false});
    // more than a chunk new: poll again at once
    assert.deepEqual(followDecision(100, answer(100, 'x\n'.repeat(10), 900000, false)), {kind: 'append', more: true});
});

test('decision: a file smaller than what the viewer has was truncated or rotated', () => {
    assert.deepEqual(followDecision(5000, {offset: 40, end: 40, content: '', size: 40, eof: true}),
        {kind: 'truncated', more: false});
    assert.deepEqual(followDecision(5000, {offset: 0, end: 0, content: '', size: 0, eof: true}),
        {kind: 'truncated', more: false});
    // same size is not truncated
    assert.equal(followDecision(40, answer(40, '', 40)).kind, 'unchanged');
});

test('decision: an answer for another offset or an error is ignored', () => {
    assert.equal(followDecision(100, answer(90, 'a\n', 200)).kind, 'stale');
    assert.equal(followDecision(100, undefined).kind, 'stale');
    assert.equal(followDecision(100, {status: 500}).kind, 'stale');
});

test('merging appended chunks: small answers join the last chunk, which keeps its key', () => {
    let chunks = replaceChunks({offset: 1000, end: 2000, content: 'tail\n', bof: false, eof: true});
    const key = chunks[0].key;
    let offset = 2000;
    for (let i = 0; i < 100; i++) {
        const r = appendFollowChunk(chunks, answer(offset, `line ${i}\n`, offset + 10));
        assert.equal(r.appended, true);
        chunks = r.chunks;
        offset = chunks[chunks.length - 1].end;
    }
    assert.equal(chunks.length, 1);
    assert.equal(chunks[0].key, key);
    assert.equal(chunks[0].content.split('\n').length - 1, 101);
    assert.deepEqual(loadedRange(chunks), {offset: 1000, end: offset, bof: false, eof: true});
});

test('merging stops at the merge size; the memory cap drops chunks from the top', () => {
    let chunks = replaceChunks({offset: 0, end: 0, content: '', bof: true, eof: true});
    let offset = 0;
    const line = 'y'.repeat(99) + '\n';
    for (let i = 0; i < 50; i++) {
        ({chunks} = appendFollowChunk(chunks, answer(offset, line.repeat(3), offset + 300), 4, 1000));
        offset += 300;
    }
    assert.equal(chunks.length, 4);
    assert.ok(chunks.every(c => c.end - c.offset <= 1000));
    assert.equal(chunks[chunks.length - 1].end, 50 * 300);
    for (let i = 1; i < chunks.length; i++) {
        assert.equal(chunks[i].offset, chunks[i - 1].end); // contiguous
    }
    assert.ok(MAX_CHUNKS === 16);
});

test('an answer that does not continue the window is not appended (only counted)', () => {
    const chunks = replaceChunks({offset: 0, end: 500, content: 'x\n', bof: true, eof: false});
    const r = appendFollowChunk(chunks, answer(900, 'new\n', 904));
    assert.equal(r.appended, false);
    assert.equal(r.chunks, chunks);
    assert.equal(appendFollowChunk([], answer(0, 'a\n', 2)).appended, false);
    // an empty answer at the end changes nothing
    const same = appendFollowChunk(chunks, answer(500, '', 500));
    assert.equal(same.chunks, chunks);
});

test('counting new lines', () => {
    assert.equal(countLines(''), 0);
    assert.equal(countLines('a\n'), 1);
    assert.equal(countLines('a\nb\nc\n'), 3);
    assert.equal(countLines('a\nb'), 2); // a split long line counts too
    assert.equal(countLines('\n\n'), 2);
});

test('"updated ... ago"', () => {
    assert.equal(formatAgo(0), 'just now');
    assert.equal(formatAgo(3400), '3 s ago');
    assert.equal(formatAgo(125000), '2 min ago');
    assert.equal(formatAgo(2 * 3600 * 1000 + 5), '2 h ago');
    assert.equal(formatAgo(-50), 'just now');
});
