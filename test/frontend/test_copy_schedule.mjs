// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    RUNS_PER_MONTH, parseTime, formatTime, toUtcSchedule, fromUtcSchedule, describeSchedule, formatLocalDateTime,
    timeZoneName, defaultNewGb, monthlyEgress, describeScheduleEgress, schedulePayload,
} from '../../src/frontend/js/utils/copySchedule.js';

const LA_SUMMER = 420;   // UTC = local + 7 h (PDT)
const LA_WINTER = 480;   // PST
const PARIS = -60;       // CET
const INDIA = -330;      // UTC+05:30

// The first run's estimate, as GET /api/copy-jobs/egress-estimate/ returns it
const INTERNET = {
    applies: true, kind: 'internet', bytes: 3.4e11, files: 1200, low_usd: 17, high_usd: 30.6,
    rate_low_per_gb: 0.05, rate_high_per_gb: 0.09, free_gb_per_month: 100,
};
const INTER_REGION = {...INTERNET, kind: 'inter-region', rate_low_per_gb: 0.02, rate_high_per_gb: 0.02};

test('times: HH:MM in and out', () => {
    assert.deepEqual(parseTime('02:30'), {hour: 2, minute: 30});
    assert.deepEqual(parseTime(' 7:05 '), {hour: 7, minute: 5});
    for (const bad of ['', '24:00', '12:60', '1230', 'ab:cd', null]) {
        assert.equal(parseTime(bad), null, String(bad));
    }
    assert.equal(formatTime(2, 5), '02:05');
});

test('local to UTC: daily, hourly, and the day shift of weekly schedules', () => {
    assert.deepEqual(toUtcSchedule({frequency: 'daily', hour: 2, minute: 30}, LA_SUMMER),
                     {fields: {frequency: 'daily', minute: 30, hour: 9}, error: null});
    assert.deepEqual(toUtcSchedule({frequency: 'daily', hour: 20, minute: 0}, LA_SUMMER).fields,
                     {frequency: 'daily', minute: 0, hour: 3}, 'the next day in UTC, still daily');
    assert.deepEqual(toUtcSchedule({frequency: 'hourly', minute: 15}, INDIA).fields, {frequency: 'hourly', minute: 45});
    assert.deepEqual(toUtcSchedule({frequency: 'hourly', minute: 15, hour: 9}, LA_SUMMER).fields,
                     {frequency: 'hourly', minute: 15}, 'only the minute of an hourly schedule');
    // Sunday 20:00 in Los Angeles is Monday 03:00 UTC
    assert.deepEqual(toUtcSchedule({frequency: 'weekly', weekday: 6, hour: 20, minute: 0}, LA_SUMMER).fields,
                     {frequency: 'weekly', minute: 0, hour: 3, weekday: 0});
    // Monday 00:30 in Paris is Sunday 23:30 UTC
    assert.deepEqual(toUtcSchedule({frequency: 'weekly', weekday: 0, hour: 0, minute: 30}, PARIS).fields,
                     {frequency: 'weekly', minute: 30, hour: 23, weekday: 6});
    assert.equal(toUtcSchedule({frequency: 'yearly'}, 0).error, 'Pick how often the copy repeats');
});

test('local to UTC: monthly days stay within 1-28 in UTC', () => {
    assert.deepEqual(toUtcSchedule({frequency: 'monthly', dayOfMonth: 5, hour: 20, minute: 0}, LA_SUMMER).fields,
                     {frequency: 'monthly', minute: 0, hour: 3, day_of_month: 6});
    assert.deepEqual(toUtcSchedule({frequency: 'monthly', dayOfMonth: 28, hour: 10, minute: 0}, LA_SUMMER).fields,
                     {frequency: 'monthly', minute: 0, hour: 17, day_of_month: 28});
    const late = toUtcSchedule({frequency: 'monthly', dayOfMonth: 28, hour: 20, minute: 0}, LA_SUMMER);
    assert.equal(late.fields, null);
    assert.match(late.error, /day 28 is day 29 in UTC/);
    const early = toUtcSchedule({frequency: 'monthly', dayOfMonth: 1, hour: 0, minute: 30}, PARIS);
    assert.equal(early.fields, null);
    assert.match(early.error, /last day of the previous month/);
});

test('UTC back to local, and the round trip', () => {
    for (const offset of [0, LA_SUMMER, LA_WINTER, PARIS, INDIA]) {
        for (const local of [
            {frequency: 'hourly', minute: 7},
            {frequency: 'daily', hour: 23, minute: 59},
            {frequency: 'weekly', weekday: 6, hour: 22, minute: 0},
            {frequency: 'weekly', weekday: 0, hour: 1, minute: 0},
            {frequency: 'monthly', dayOfMonth: 15, hour: 12, minute: 0},
        ]) {
            const {fields} = toUtcSchedule(local, offset);
            const back = fromUtcSchedule(fields, offset);
            for (const [name, value] of Object.entries(local)) {
                assert.equal(back[name], value, `${offset} ${JSON.stringify(local)} ${name}`);
            }
        }
    }
    assert.equal(fromUtcSchedule({frequency: 'monthly', day_of_month: 1, hour: 3, minute: 0}, LA_SUMMER).dayOfMonth,
                 null, 'the last day of the previous month here');
});

test('the schedule in words, in local time', () => {
    assert.equal(describeSchedule({frequency: 'daily', hour: 9, minute: 30}, LA_SUMMER), 'Every day at 02:30');
    assert.equal(describeSchedule({frequency: 'hourly', minute: 45}, INDIA), 'Every hour at :15');
    assert.equal(describeSchedule({frequency: 'weekly', weekday: 0, hour: 3, minute: 0}, LA_SUMMER), 'Every Sunday at 20:00');
    assert.equal(describeSchedule({frequency: 'monthly', day_of_month: 6, hour: 3, minute: 0}, LA_SUMMER),
                 'Every month on day 5 at 20:00');
    assert.equal(describeSchedule({frequency: 'monthly', day_of_month: 1, hour: 3, minute: 0}, LA_SUMMER),
                 'Every month on day 1 at 03:00 UTC');
    assert.equal(describeSchedule(null, 0), '');
});

test('next run in the browser time zone', () => {
    const iso = '2026-10-19T09:30:00Z';
    const date = new Date(iso);
    const pad = n => String(n).padStart(2, '0');
    assert.equal(formatLocalDateTime(iso), `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
        `${pad(date.getHours())}:${pad(date.getMinutes())}`);
    if (process.env.TZ === 'UTC') {
        assert.equal(formatLocalDateTime(iso), '2026-10-19 09:30');
    }
    if (process.env.TZ === 'America/New_York') {
        assert.equal(formatLocalDateTime(iso), '2026-10-19 05:30');
    }
    assert.equal(formatLocalDateTime(null), '');
    assert.equal(formatLocalDateTime('garbage'), '');
    assert.ok(timeZoneName(new Date().getTimezoneOffset()).length > 0);
});

test('monthly egress: the same numbers as the server (egress_cost.repeated)', () => {
    assert.deepEqual(monthlyEgress(INTERNET, 10e9, 'daily'), {low: 15.22, high: 27.4, runs: 30.44});
    assert.deepEqual(monthlyEgress(INTERNET, 100e9, 'weekly'), {low: 21.75, high: 39.15, runs: 4.35});
    assert.deepEqual(monthlyEgress(INTERNET, null, 'hourly'), {low: null, high: null, runs: 730.5});
    assert.equal(RUNS_PER_MONTH.monthly, 1);
    assert.equal(defaultNewGb(INTERNET), 34, '10% of the current size');
    assert.equal(defaultNewGb({...INTERNET, bytes: null}), null);
});

test('the schedule warning and its confirmation', () => {
    assert.equal(describeScheduleEgress({applies: false}, 'daily', 1), null);
    assert.equal(describeScheduleEgress({...INTERNET, kind: 'same-region'}, 'daily', 1), null);
    const daily = describeScheduleEgress(INTERNET, 'daily', 34);
    assert.match(daily.text, /^This schedule copies out of AWS every day \(about 30 runs per month\)\. Only new files/);
    assert.match(daily.text, /If 34 GB of new data appears per run, this schedule costs about \$51.75–\$93.15 per month\.$/);
    assert.equal(daily.confirmLabel, 'I understand this schedule costs about $51.75–$93.15 per month at 34 GB of new data per run');
    const monthly = describeScheduleEgress(INTER_REGION, 'monthly', 500);
    assert.match(monthly.text, /between AWS regions every month \(1 run per month\)/);
    assert.match(monthly.text, /about \$10 per month/);
    const unknown = describeScheduleEgress(INTERNET, 'hourly', null);
    assert.match(unknown.text, /about 731 runs per month/);
    assert.match(unknown.text, /Each TB of new data per run costs about \$36,525–\$65,745 per month\./);
});

test('the request payload of a schedule out of AWS', () => {
    assert.equal(schedulePayload(null, true, 1), null, 'no AWS fee: nothing to confirm');
    const first = {confirmed: true, kind: 'internet', bytes: 3.4e11, files: 1200, low_usd: 17, high_usd: 30.6};
    assert.deepEqual(schedulePayload(first, true, 34), {...first, repeat_confirmed: true, new_bytes_per_run: 34e9});
    assert.deepEqual(schedulePayload(first, false, null), {...first, repeat_confirmed: false, new_bytes_per_run: null});
});
