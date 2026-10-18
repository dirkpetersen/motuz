// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {describeKept, describeOverwrite} from '../../src/frontend/js/utils/overwritePolicy.js';

test('the overwrite decision of a job', () => {
    assert.equal(describeOverwrite({overwrite: false}), 'Kept (this destination was not written by Motuz before)');
    assert.equal(describeOverwrite({overwrite: true}), 'Overwritten when changed');
    assert.equal(describeOverwrite({overwrite: null}), null, 'jobs from before the policy');
    assert.equal(describeOverwrite({overwrite: false, upload: {mode: 'skip'}}), null, 'uploads have their mode');
    assert.equal(describeOverwrite(null), null);
});

test('kept files', () => {
    assert.equal(describeKept(0, []), null);
    assert.equal(describeKept(null, null), null);
    assert.equal(describeKept(1, ['x.txt']), '1 existing file was kept (not overwritten): x.txt');
    assert.equal(describeKept(2, ['a', 'b']), '2 existing files were kept (not overwritten): a, b');
    assert.equal(describeKept(3, []), '3 existing files were kept (not overwritten)');
    const names = Array.from({length: 100}, (_, i) => `f${i}`);
    const text = describeKept(1000, names);
    assert.ok(text.startsWith('1000 existing files were kept (not overwritten): f0, f1, '));
    assert.ok(text.endsWith('f19 … and 980 more'), text);
    assert.equal(describeKept(5, ['a', 7, 'b'], 1), '5 existing files were kept (not overwritten): a … and 4 more');
});
