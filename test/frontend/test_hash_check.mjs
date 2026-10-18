// The hash of an integrity check in the job details (utils/hashCheck.js)
import test from 'node:test';
import assert from 'node:assert/strict';

import {hashLabel, describeHashCheck, notCheckableCount, identicalStatus}
    from '../../src/frontend/js/utils/hashCheck.js';

test('labels', () => {
    assert.equal(hashLabel('md5'), 'MD5');
    assert.equal(hashLabel('sha256'), 'SHA-256');
    assert.equal(hashLabel('crc32c'), 'CRC32C');
    assert.equal(hashLabel(null), null);
    assert.equal(hashLabel('x'), null);
});

test('the compared line', () => {
    assert.equal(describeHashCheck({}), null);
    assert.equal(describeHashCheck({hash_type: 'md5'}), 'MD5');
    assert.match(describeHashCheck({hash_type: 'crc32c', md5_fallback: 0}), /^CRC32C stored by S3 \(one HEAD/);
    assert.match(describeHashCheck({hash_type: 'crc32c', md5_fallback: 1}), /MD5 for 1 file without one$/);
    assert.match(describeHashCheck({hash_type: 'crc32c', md5_fallback: 3}), /MD5 for 3 files without one$/);
});

test('not checkable files', () => {
    assert.equal(notCheckableCount({not_checkable: 2}), 2);
    assert.equal(notCheckableCount({not_checkable: null}), 0);
    assert.equal(notCheckableCount(undefined), 0);
    assert.equal(identicalStatus({not_checkable: 2}), 'IDENTICAL, 2 NOT CHECKABLE');
    assert.equal(identicalStatus({not_checkable: 0}), 'IDENTICAL');
});
