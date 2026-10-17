// S3 options of the New/Edit Cloud Connection dialog and Check Integrity (utils/s3Providers.js)
import test from 'node:test';
import assert from 'node:assert/strict';

import {S3_PROVIDERS, isAws, canSkipChecks, canUseCrc32c, canCheckIntegrity, NON_AWS_DEFAULT_REGION}
    from '../../src/frontend/js/utils/s3Providers.js';

test('Amazon S3 first, Ceph second, Other last', () => {
    assert.deepEqual(S3_PROVIDERS.slice(0, 2).map(p => p.value), ['AWS', 'Ceph']);
    assert.equal(S3_PROVIDERS.at(-1).value, 'Other');
    assert.equal(new Set(S3_PROVIDERS.map(p => p.value)).size, S3_PROVIDERS.length);
});

test('a connection without provider is AWS', () => {
    assert.equal(isAws(undefined), true);
    assert.equal(isAws('AWS'), true);
    assert.equal(isAws('Ceph'), false);
    assert.equal(NON_AWS_DEFAULT_REGION, 'us-east-1');
});

test('skipping checks only for AWS and Ceph', () => {
    assert.equal(canSkipChecks('AWS'), true);
    assert.equal(canSkipChecks('Ceph'), true);
    assert.equal(canSkipChecks(null), true);
    assert.equal(canSkipChecks('Minio'), false);
    assert.equal(canSkipChecks('Other'), false);
});

test('CRC32C part checksums only for AWS', () => {
    assert.equal(canUseCrc32c('AWS'), true);
    assert.equal(canUseCrc32c(undefined), true);
    assert.equal(canUseCrc32c('Ceph'), false);
    assert.equal(canUseCrc32c('Minio'), false);
});

test('Check Integrity: not for chunked uploads to S3', () => {
    assert.equal(canCheckIntegrity({type: 's3', s3_chunked: true}), false);
    assert.equal(canCheckIntegrity({type: 's3'}), false); // the default is chunked
    assert.equal(canCheckIntegrity({type: 's3', s3_chunked: false}), true);
    assert.equal(canCheckIntegrity({type: 'azureblob'}), true);
    assert.equal(canCheckIntegrity({type: 'file'}), true);
    assert.equal(canCheckIntegrity(undefined), true);
});
