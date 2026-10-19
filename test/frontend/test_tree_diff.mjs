// Comparing the file trees of an integrity check (utils/treeDiff.js)
import test from 'node:test';
import assert from 'node:assert/strict';

import {compareTrees, NodeType} from '../../src/frontend/js/utils/treeDiff.js';

const leaf = (title, hash) => ({title, hash, isLeaf: true});
const folder = (title, children) => ({title, children});

test('identical trees', () => {
    const left = [folder('d', [leaf('a', '1'), leaf('b', '2')])];
    const right = [folder('d', [leaf('a', '1'), leaf('b', '2')])];
    assert.equal(compareTrees(left, right), NodeType.IDENTICAL);
    assert.equal(compareTrees([], []), NodeType.IDENTICAL);
    assert.equal(compareTrees(null, undefined), NodeType.IDENTICAL);
    assert.equal(left[0].type, undefined);
});

test('a different hash marks both files and their folders', () => {
    const left = [folder('d', [leaf('a', '1')])];
    const right = [folder('d', [leaf('a', '9')])];
    assert.equal(compareTrees(left, right), NodeType.MODIFY);
    assert.equal(left[0].type, 'modify');
    assert.equal(right[0].children[0].type, 'modify');
});

test('a hash missing on either side cannot be determined', () => {
    for (const [l, r] of [['1', null], [null, '1'], ['', '1']]) {
        const left = [folder('d', [leaf('a', l)])];
        const right = [folder('d', [leaf('a', r)])];
        assert.equal(compareTrees(left, right), NodeType.MISSING, `${l} vs ${r}`);
        assert.equal(left[0].type, 'missing');
        assert.equal(right[0].children[0].type, 'missing');
    }
    // the worst difference wins
    assert.equal(compareTrees([leaf('a', null), leaf('b', '1')], [leaf('a', '1'), leaf('b', '2')]), NodeType.MISSING);
});

test('files on one side only get a hidden placeholder on the other', () => {
    const left = [leaf('a', '1'), leaf('c', '3')];
    const right = [leaf('b', '2'), leaf('c', '3'), leaf('d', '4')];
    assert.equal(compareTrees(left, right), NodeType.MODIFY);
    assert.deepEqual(left.map(n => n.type), ['insert', 'hidden', undefined, 'hidden']);
    assert.deepEqual(right.map(n => n.type), ['hidden', 'insert', undefined, 'insert']);
    assert.equal(left.length, right.length);
    // the matching file after the differing ones is still compared with its partner
    assert.equal(compareTrees([leaf('a', '1'), leaf('c', '3')], [leaf('b', '2'), leaf('c', 'x')]), NodeType.MODIFY);
    assert.equal(compareTrees([leaf('a', '1'), leaf('c', '3')], [leaf('b', '2'), leaf('c', null)]), NodeType.MISSING);
});

test('a file against a folder is a difference', () => {
    const left = [leaf('x', '1')];
    const right = [folder('x', [leaf('y', '1')])];
    assert.equal(compareTrees(left, right), NodeType.MODIFY);
    assert.equal(left[0].type, 'modify');
});
