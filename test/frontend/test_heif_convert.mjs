// Run with: node --test test/frontend/
import test from 'node:test';
import assert from 'node:assert/strict';

import { convertFirst, describeType, isHeifName, isHeifType, needsConversion } from '../../src/frontend/js/utils/heifConvert.js';

test('HEIC/HEIF names and types', () => {
    for (const name of ['IMG_0001.heic', 'IMG_0001.HEIC', 'a.b.heif', 'x.HeIf']) assert.equal(isHeifName(name), true, name);
    for (const name of ['a.avif', 'a.png', 'heic', 'a.heic.txt', '', null, undefined]) assert.equal(isHeifName(name), false, String(name));
    assert.equal(isHeifType('image/heic'), true);
    assert.equal(isHeifType('image/heif'), true);
    assert.equal(isHeifType('image/avif'), false);
    assert.equal(isHeifType('image/jpeg'), false);
});

test('the JPEG is asked for first only once this browser could not decode a HEIC image', () => {
    assert.equal(convertFirst('a.heic', null), false); // not known yet: the plain image first (Safari)
    assert.equal(convertFirst('a.heic', true), false); // this browser decodes HEIC
    assert.equal(convertFirst('a.heic', false), true);
    assert.equal(convertFirst('a.jpg', false), false); // other names are never converted
});

test('a HEIC/HEIF image that did not decode natively is fetched again as a JPEG', () => {
    assert.equal(needsConversion('image/heic', false), true);
    assert.equal(needsConversion('image/heif', false), true);
    assert.equal(needsConversion('image/heic', true), false);
    assert.equal(needsConversion('image/jpeg', false), false);
    assert.equal(needsConversion('image/avif', false), false);
});

test('footer text', () => {
    assert.equal(describeType('JPEG', 'HEIC'), 'HEIC, converted to JPEG');
    assert.equal(describeType('PNG', null), 'PNG');
});
