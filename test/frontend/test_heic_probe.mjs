// Run with: node --test test/frontend/
import test from 'node:test';
import assert from 'node:assert/strict';

import { probeBytes, probeNativeHeic } from '../../src/frontend/js/utils/heicProbe.js';

test('the probe picture is a HEIC file (ftyp heic) of 551 bytes', () => {
    const bytes = probeBytes();
    assert.equal(bytes.length, 551);
    assert.equal(new TextDecoder().decode(bytes.subarray(4, 12)), 'ftypheic');
});

test('the browser is probed once, whatever the answer; a failing decode means "no"', async () => {
    let calls = 0;
    const decode = async (blob) => { calls++; assert.equal(blob.type, 'image/heic'); return null; };
    assert.equal(await probeNativeHeic(decode), false);
    assert.equal(await probeNativeHeic(async () => ({width: 1, height: 1})), false); // remembered
    assert.equal(calls, 1);
});
