// S3 options of the New/Edit Cloud Connection dialog and Check Integrity (utils/s3Providers.js)
import test from 'node:test';
import assert from 'node:assert/strict';

import {S3_PROVIDERS, isAws, canSkipChecks, canUseCrc32c, defaultSkipChecks, canCheckIntegrity, NON_AWS_DEFAULT_REGION}
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

test('CRC32C part checksums only for AWS and Ceph', () => {
    assert.equal(canUseCrc32c('AWS'), true);
    assert.equal(canUseCrc32c(undefined), true);
    assert.equal(canUseCrc32c('Ceph'), true);
    assert.equal(canUseCrc32c('Minio'), false);
});

test('New connections start with CRC32C on Amazon S3 only', () => {
    assert.equal(defaultSkipChecks('AWS'), 'crc32c');
    assert.equal(defaultSkipChecks(undefined), 'crc32c');
    assert.equal(defaultSkipChecks('Ceph'), ''); // the Ceph version is unknown
    assert.equal(defaultSkipChecks('Minio'), '');
});

test('Check Integrity: not for chunked uploads to S3', () => {
    assert.equal(canCheckIntegrity({type: 's3', s3_chunked: true}), false);
    assert.equal(canCheckIntegrity({type: 's3'}), false); // the default is chunked
    assert.equal(canCheckIntegrity({type: 's3', s3_chunked: false}), true);
    // CRC32C parts: S3 stores a CRC32C of the whole object, which the check compares
    assert.equal(canCheckIntegrity({type: 's3', s3_chunked: true, s3_skip_checks: 'crc32c'}), true);
    assert.equal(canCheckIntegrity({type: 's3', s3_provider: 'AWS', s3_skip_checks: 'crc32c'}), true);
    assert.equal(canCheckIntegrity({type: 's3', s3_provider: 'Ceph', s3_skip_checks: 'crc32c'}), true);
    assert.equal(canCheckIntegrity({type: 's3', s3_provider: 'Minio', s3_skip_checks: 'crc32c'}), false);
    assert.equal(canCheckIntegrity({type: 's3', s3_skip_checks: 'post_copy'}), false);
    assert.equal(canCheckIntegrity({type: 'azureblob'}), true);
    assert.equal(canCheckIntegrity({type: 'file'}), true);
    assert.equal(canCheckIntegrity(undefined), true);
});
