// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CUSTOM, parseSize, parseInteger, formatSize, describeSize, validatePerformance,
    estimateMemory, presetValues, matchPreset, toRequest, describePerformance,
} from '../../src/frontend/js/utils/copyPerformance.js';

const MiB = 1024 * 1024;
const GiB = 1024 * MiB;

// What GET /api/copy-jobs/performance/?dst_cloud_id=<s3> returns with rclone's defaults
const S3_INFO = {
    dst_type: 's3',
    buffer_size: 16 * MiB,
    memory_budget: 8 * GiB,
    fields: [
        {name: 'transfers', label: 'Parallel transfers', kind: 'int', min: 1, max: 64, default: 4},
        {name: 'checkers', label: 'Parallel checkers', kind: 'int', min: 1, max: 128, default: 8},
        {name: 'multi_thread_streams', label: 'Multi-thread streams', kind: 'int', min: 0, max: 32, default: 4},
        {name: 'multi_thread_cutoff', label: 'Multi-thread cutoff', kind: 'size', min: '1Mi', max: '1Ti', default: '256Mi'},
        {name: 's3_upload_concurrency', label: 'S3 upload concurrency', kind: 'int', min: 1, max: 64, default: 4},
        {name: 's3_chunk_size', label: 'S3 chunk size', kind: 'size', min: '5Mi', max: '5Gi', default: '5Mi'},
    ],
    presets: [
        {id: 'default', label: 'Default', values: {}, available: true},
        {id: 'small_files', label: 'Many small files', values: {transfers: 32, checkers: 64}, available: true},
        {id: 'large_files', label: 'Few large files', available: true, values: {
            transfers: 4, multi_thread_streams: 16, multi_thread_cutoff: '64Mi', s3_upload_concurrency: 16, s3_chunk_size: '64Mi'}},
        {id: 'maximum', label: 'Maximum', available: true, values: {
            transfers: 10, checkers: 128, multi_thread_streams: 16, multi_thread_cutoff: '64Mi', s3_upload_concurrency: 16, s3_chunk_size: '32Mi'}},
    ],
};
const FIELDS = S3_INFO.fields;

test('sizes', () => {
    assert.equal(parseSize('64M'), 64 * MiB);
    assert.equal(parseSize('64Mi'), 64 * MiB);
    assert.equal(parseSize('64MiB'), 64 * MiB);
    assert.equal(parseSize('1.5G'), 1536 * MiB);
    assert.equal(parseSize('100B'), 100);
    assert.equal(parseSize(1024), 1024);
    for (const bad of ['64', '64 M', ' 64M', '64M\n', '-1M', '64M --rc', 'M', '', null, -1, 1.5]) {
        assert.equal(parseSize(bad), null, JSON.stringify(bad));
    }
    assert.equal(formatSize(64 * MiB), '64Mi');
    assert.equal(formatSize(GiB), '1Gi');
    assert.equal(describeSize(8 * GiB), '8 GiB');
    assert.equal(describeSize(2.5 * GiB), '2.5 GiB');
    assert.equal(describeSize(336 * MiB), '336 MiB');
});

test('integers', () => {
    assert.equal(parseInteger('32'), 32);
    assert.equal(parseInteger(32), 32);
    for (const bad of ['4 --config=/etc/shadow', '--foo', '4\n', '4.5', '-1', '', 4.5, null]) {
        assert.equal(parseInteger(bad), null, JSON.stringify(bad));
    }
});

test('validation', () => {
    assert.deepEqual(validatePerformance({transfers: '32', s3_chunk_size: '64M', checkers: ''}, FIELDS), {});
    assert.deepEqual(validatePerformance({transfers: ' 32 '}, FIELDS), {}); // trimmed
    const errors = validatePerformance({
        transfers: '4 --config=/etc/shadow', checkers: '129', multi_thread_streams: '--foo',
        s3_chunk_size: '1M', multi_thread_cutoff: '64',
    }, FIELDS);
    assert.equal(errors.transfers, 'Parallel transfers must be a whole number');
    assert.equal(errors.checkers, 'Parallel checkers must be between 1 and 128');
    assert.equal(errors.multi_thread_streams, 'Multi-thread streams must be a whole number');
    assert.equal(errors.s3_chunk_size, 'S3 chunk size must be between 5Mi and 5Gi');
    assert.equal(errors.multi_thread_cutoff, 'Multi-thread cutoff must be a size like 64M or 1G');
});

test('memory estimate matches the server', () => {
    // rclone's defaults: 4 * (4 * 16 MiB + 4 * 5 MiB)
    assert.equal(estimateMemory({}, S3_INFO), 4 * (4 * 16 + 4 * 5) * MiB);
    const maximum = presetValues(S3_INFO.presets[3], FIELDS);
    assert.equal(estimateMemory(maximum, S3_INFO), 10 * (16 * 16 + 16 * 32) * MiB);
    // 16 * (16 * 16 MiB + 16 * 64 MiB) = 20 GiB
    assert.equal(estimateMemory({transfers: '16', multi_thread_streams: '16', s3_upload_concurrency: '16', s3_chunk_size: '64M'}, S3_INFO), 20 * GiB);
    assert.equal(estimateMemory({transfers: 'x'}, S3_INFO), null);
    const local = {...S3_INFO, dst_type: null, fields: FIELDS.slice(0, 4)};
    assert.equal(estimateMemory({transfers: '32'}, local), 32 * 4 * 16 * MiB);
});

test('presets fill the fields and are recognized', () => {
    const small = presetValues(S3_INFO.presets[1], FIELDS);
    assert.deepEqual(small, {transfers: '32', checkers: '64', multi_thread_streams: '', multi_thread_cutoff: '',
                             s3_upload_concurrency: '', s3_chunk_size: ''});
    assert.equal(matchPreset(small, S3_INFO.presets, FIELDS), 'small_files');
    assert.equal(matchPreset(presetValues(null, FIELDS), S3_INFO.presets, FIELDS), 'default');
    assert.equal(matchPreset({...small, transfers: '33'}, S3_INFO.presets, FIELDS), CUSTOM);
    // the same size written differently is the same preset
    const large = {...presetValues(S3_INFO.presets[2], FIELDS), s3_chunk_size: '64M'};
    assert.equal(matchPreset(large, S3_INFO.presets, FIELDS), 'large_files');
});

test('request body', () => {
    assert.equal(toRequest(presetValues(null, FIELDS), FIELDS), null);
    assert.deepEqual(toRequest(presetValues(S3_INFO.presets[3], FIELDS), FIELDS), {
        transfers: 10, checkers: 128, multi_thread_streams: 16, multi_thread_cutoff: '64Mi',
        s3_upload_concurrency: 16, s3_chunk_size: '32Mi'});
    assert.deepEqual(toRequest({transfers: ' 8 ', s3_chunk_size: ' 64M'}, FIELDS), {transfers: 8, s3_chunk_size: '64M'});
});

test('job detail', () => {
    assert.equal(describePerformance(null), null);
    assert.equal(describePerformance({}), null);
    assert.equal(describePerformance({transfers: 32, checkers: 64, s3_chunk_size: '64Mi', s3_upload_concurrency: 16}),
                 '32 transfers · 64 checkers · S3 parts in parallel 16 · S3 chunk 64Mi');
});
