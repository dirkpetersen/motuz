// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    formatUsd, formatRate, formatDecimalSize, formatRange, needsConfirmation, describeEgress,
    confirmationPayload, describeConfirmed,
} from '../../src/frontend/js/utils/egressCost.js';

// What GET /api/copy-jobs/egress-estimate/ returns (src/backend/api/utils/egress_cost.py)
const INTERNET = {
    applies: true, kind: 'internet', bytes: 3.4e12, files: 1200, low_usd: 170, high_usd: 306,
    rate_low_per_gb: 0.05, rate_high_per_gb: 0.09, free_gb_per_month: 100,
    pricing_date: '2026-10', note: '...', runs_on: 'this server, which is not in AWS',
    reason: 'runs on this server, which is not in AWS, so the data leaves AWS',
};
const UNKNOWN = {...INTERNET, bytes: null, files: null, low_usd: null, high_usd: null};
const INTER_REGION = {
    applies: true, kind: 'inter-region', bytes: 1e12, files: 3, low_usd: 20, high_usd: 20,
    rate_low_per_gb: 0.02, rate_high_per_gb: 0.02, free_gb_per_month: 0, runs_on: 'an EC2 worker in us-east-1',
    reason: 'runs on an EC2 worker in us-east-1, so the data is read out of us-west-2',
};
const SAME_REGION = {...INTER_REGION, kind: 'same-region', low_usd: 0, high_usd: 0, rate_low_per_gb: 0, rate_high_per_gb: 0,
    runs_on: 'this server (AWS us-west-2)', reason: 'runs on this server (AWS us-west-2), in the region of the data'};

test('money: cents below $100 unless whole, whole dollars with separators above', () => {
    assert.equal(formatUsd(0), '$0');
    assert.equal(formatUsd(50), '$50');
    assert.equal(formatUsd(0.42), '$0.42');
    assert.equal(formatUsd(9.9), '$9.90');
    assert.equal(formatUsd(170), '$170');
    assert.equal(formatUsd(306.4), '$306');
    assert.equal(formatUsd(12345.6), '$12,346');
    assert.equal(formatUsd(null), '$?');
    assert.equal(formatUsd(NaN), '$?');
    assert.equal(formatRate(0.05), '$0.05');
    assert.equal(formatRate(0.085), '$0.085');
    assert.equal(formatRate(0.09), '$0.09');
});

test('sizes are decimal, as AWS bills', () => {
    assert.equal(formatDecimalSize(3.4e12), '3.4 TB');
    assert.equal(formatDecimalSize(1e12), '1 TB');
    assert.equal(formatDecimalSize(120e9), '120 GB');
    assert.equal(formatDecimalSize(1.5e9), '1.5 GB');
    assert.equal(formatDecimalSize(512), '512 B');
    assert.equal(formatDecimalSize(null), '');
});

test('ranges collapse when both ends are the same', () => {
    assert.equal(formatRange(170, 306), '$170–$306');
    assert.equal(formatRange(20, 20), '$20');
});

test('internet egress with a measured size', () => {
    assert.ok(needsConfirmation(INTERNET));
    const {text, confirmLabel} = describeEgress(INTERNET);
    assert.equal(text, 'Copying out of AWS: about $170–$306 in data transfer fees for 3.4 TB; ' +
        'this copy runs on this server, which is not in AWS, so the data leaves AWS ' +
        "(AWS charges $0.05–$0.09 per GB depending on the account's monthly volume; the first 100 GB " +
        'per month are free; discounts or an egress waiver may lower it).');
    assert.equal(confirmLabel, 'I understand this copy costs about $170–$306');
});

test('size not measured: the per-TB range', () => {
    const {text, confirmLabel} = describeEgress(UNKNOWN);
    assert.match(text, /^Copying out of AWS: the size could not be measured; the data transfer fees are about \$50–\$90 per TB/);
    assert.equal(confirmLabel, 'I understand this copy costs about $50–$90 per TB');
});

test('another region: one rate, still a confirmation; same region: a note only', () => {
    const inter = describeEgress(INTER_REGION);
    assert.equal(inter.text, 'Copying between AWS regions: about $20 in data transfer fees for 1 TB; ' +
        'this copy runs on an EC2 worker in us-east-1, so the data is read out of us-west-2 ' +
        '(AWS charges about $0.02 per GB between regions for this copy).');
    assert.equal(inter.confirmLabel, 'I understand this copy costs about $20');
    assert.match(describeEgress({...INTER_REGION, rate_high_per_gb: 0.04, high_usd: 40}).text, /about \$0\.04 per GB/);
    assert.ok(!needsConfirmation(SAME_REGION));
    assert.deepEqual(describeEgress(SAME_REGION), {
        text: 'No AWS egress fee: this copy runs on this server (AWS us-west-2), in the region of the data.',
        confirmLabel: null});
    assert.equal(describeEgress({...SAME_REGION, reason: undefined}).text, 'No AWS egress fee within the same region.');
    assert.equal(describeEgress({applies: false}), null);
    assert.equal(describeEgress(null), null);
});

test('integrity check wording', () => {
    const {text, confirmLabel} = describeEgress(INTERNET, 'check');
    assert.match(text, /^Reading out of AWS for the integrity check: about \$170–\$306/);
    assert.equal(confirmLabel, 'I understand this check costs about $170–$306');
});

test('the request carries the confirmation only once checked', () => {
    assert.equal(confirmationPayload(INTERNET, false), null);
    assert.equal(confirmationPayload(SAME_REGION, true), null);
    assert.equal(confirmationPayload({applies: false}, true), null);
    assert.deepEqual(confirmationPayload(INTERNET, true),
        {confirmed: true, kind: 'internet', bytes: 3.4e12, files: 1200, low_usd: 170, high_usd: 306});
    assert.deepEqual(confirmationPayload(UNKNOWN, true),
        {confirmed: true, kind: 'internet', bytes: null, files: null, low_usd: null, high_usd: null});
});

test('job details show what was confirmed', () => {
    assert.equal(describeConfirmed({...INTERNET, confirmed: true}),
        'About $170–$306 for 3.4 TB (out of AWS; on this server, which is not in AWS), confirmed');
    assert.equal(describeConfirmed({...UNKNOWN, runs_on: null, confirmed: true}),
        'About $50–$90 per TB (out of AWS, size not measured), confirmed');
    assert.equal(describeConfirmed({...INTER_REGION, confirmed: true}),
        'About $20 for 1 TB (between AWS regions; on an EC2 worker in us-east-1), confirmed');
    assert.equal(describeConfirmed(null), null);
});
