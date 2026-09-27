// Run with: node --test test/frontend/   (bin/ci/frontend_unittest.sh runs it with TZ=UTC
// and with TZ=America/New_York: the relative age must not depend on the time zone)
import test from 'node:test';
import assert from 'node:assert/strict';

import {formatAge, formatLocalDateTime, parseModified, UNKNOWN} from '../../src/frontend/js/utils/fileAge.js';

const NOW = Date.UTC(2026, 8, 27, 12, 0, 0); // 2026-09-27T12:00:00Z
const ago = seconds => new Date(NOW - seconds * 1000).toISOString().replace(/\.\d+Z$/, 'Z');

test('parses the API format (UTC, Z) and epoch seconds, never naive times', () => {
    assert.equal(parseModified('2026-09-27T12:00:00Z'), NOW);
    assert.equal(parseModified('2026-09-27T05:00:00-07:00'), NOW);
    assert.equal(parseModified(NOW / 1000), NOW);
    assert.equal(parseModified('2026-09-27T12:00:00'), null); // naive: would be local time
    for (const unknown of [null, undefined, '', 'yesterday', NaN, {}]) {
        assert.equal(parseModified(unknown), null);
    }
});

test('relative ages', () => {
    const cases = [
        [0, 'just now'],
        [12, '12 sec ago'],
        [59, '59 sec ago'],
        [60, '1 min ago'],
        [5 * 60 + 30, '5 min ago'],
        [3 * 3600 + 59, '3 h ago'],
        [23 * 3600 + 3599, '23 h ago'],
        [86400, '1 day ago'],
        [4 * 86400, '4 days ago'],
        [29 * 86400, '29 days ago'],
        [31 * 86400, '1 month ago'],
        [200 * 86400, '6 months ago'],
        [364 * 86400, '11 months ago'],
        [366 * 86400, '1 year ago'],
        [3 * 365.25 * 86400 + 10, '3 years ago'],
    ];
    for (const [seconds, expected] of cases) {
        assert.equal(formatAge(ago(seconds), NOW), expected, `${seconds} s`);
    }
});

test('unknown and future times', () => {
    assert.equal(formatAge(null, NOW), UNKNOWN);
    assert.equal(formatAge('not a date', NOW), UNKNOWN);
    assert.equal(formatAge(ago(-30), NOW), 'just now'); // small clock skew
    assert.equal(formatAge(ago(-3600), NOW), 'in the future');
});

test('the same result in every time zone, across a DST change', () => {
    // US DST ended 2026-11-01; 'Z' times are instants, so local zones cannot matter
    const now = Date.UTC(2026, 10, 2, 12, 0, 0);
    assert.equal(formatAge('2026-10-31T12:00:00Z', now), '2 days ago');
    assert.equal(formatAge('2026-11-02T11:59:00Z', now), '1 min ago');
    assert.equal(formatAge('2026-11-01T08:30:00Z', now), '1 day ago');
    console.log(`TZ=${process.env.TZ || '(unset)'}: ${Intl.DateTimeFormat().resolvedOptions().timeZone}`);
});

test('tooltips show local time with the zone name', () => {
    const text = formatLocalDateTime('2026-09-27T04:57:00Z', 'en-US', 'America/Los_Angeles');
    assert.match(text, /Sep 26, 2026/);
    assert.match(text, /9:57:00\s?PM/);
    assert.match(text, /PDT/);
    assert.match(formatLocalDateTime('2026-12-27T04:57:00Z', 'en-US', 'America/Los_Angeles'), /PST/);
    assert.match(formatLocalDateTime('2026-09-27T04:57:00Z', 'en-US', 'UTC'), /4:57:00\s?AM UTC/);
    assert.equal(formatLocalDateTime(null), '');
});
