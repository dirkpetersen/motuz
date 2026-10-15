// Run with: node --test test/frontend/
import test from 'node:test';
import assert from 'node:assert/strict';

import { blobUrlsIn, retypeBlobUrl } from '../../src/frontend/js/utils/docxPictures.js';

const PNG = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 1, 2]);
const HTML = new TextEncoder().encode('<html><script>alert(1)</script></html>');

function deps(files) {
    const log = {made: [], revoked: []};
    return {
        log,
        fetchBytes: async url => { if (!(url in files)) throw new Error('gone'); return files[url]; },
        makeUrl: (bytes, mime) => { log.made.push(mime); return `blob:new/${log.made.length}`; },
        revoke: url => log.revoked.push(url),
    };
}

test('a picture is re-typed from its bytes, whatever the document declared', async () => {
    const d = deps({'blob:old/1': PNG});
    const result = await retypeBlobUrl('blob:old/1', d);
    assert.deepEqual(result, {url: 'blob:new/1', mime: 'image/png'});
    assert.deepEqual(d.log.made, ['image/png']);
    assert.deepEqual(d.log.revoked, ['blob:old/1']);
});

test('HTML (or anything that is not PNG/JPEG/GIF/WebP) is dropped and its URL revoked', async () => {
    const d = deps({'blob:old/2': HTML, 'blob:old/3': new Uint8Array(4)});
    assert.equal(await retypeBlobUrl('blob:old/2', d), null);
    assert.equal(await retypeBlobUrl('blob:old/3', d), null);
    assert.equal(await retypeBlobUrl('blob:old/missing', d), null);
    assert.deepEqual(d.log.made, []);
    assert.deepEqual(d.log.revoked, ['blob:old/2', 'blob:old/3', 'blob:old/missing']);
});

test('blob: URLs in CSS', () => {
    assert.deepEqual(blobUrlsIn('a{background:url(blob:https://x/1)} b{background:url("blob:https://x/2")} c{}'),
        ['blob:https://x/1', 'blob:https://x/2']);
    assert.deepEqual(blobUrlsIn(''), []);
    assert.deepEqual(blobUrlsIn(undefined), []);
});
