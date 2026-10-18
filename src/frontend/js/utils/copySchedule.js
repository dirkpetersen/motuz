// Scheduled repeating copies (the "Scheduled repeat" section of the New Copy Job dialog
// and the Schedules table): the user picks times in the browser's time zone, the
// server stores and runs them in UTC (src/backend/api/utils/schedule.py, README
// "Scheduled copies"). `offsetMinutes` is Date.prototype.getTimezoneOffset(): UTC =
// local time + offset (420 in California in summer, -60 in Paris in winter).

import { formatDecimalSize, formatRange, needsConfirmation } from './egressCost.js';

export const FREQUENCIES = ['hourly', 'daily', 'weekly', 'monthly'];
export const FREQUENCY_LABELS = {hourly: 'Every hour', daily: 'Every day', weekly: 'Every week', monthly: 'Every month'};
// Index = the server's weekday: 0 = Monday ... 6 = Sunday
export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
export const MAX_DAY_OF_MONTH = 28; // every month has it
// Average runs per month (365.25 / 12 days), as utils/schedule.py RUNS_PER_MONTH
export const RUNS_PER_MONTH = {hourly: 730.5, daily: 30.44, weekly: 4.35, monthly: 1};
const PER_RUN = {hourly: 'every hour', daily: 'every day', weekly: 'every week', monthly: 'every month'};

const MINUTES_PER_DAY = 24 * 60;
const GB = 1e9;

function mod(value, base) {
    return ((value % base) + base) % base;
}

function pad(value) {
    return String(value).padStart(2, '0');
}

/** "HH:MM" -> {hour, minute}, or null */
export function parseTime(text) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(String(text || '').trim());
    if (!match) {
        return null;
    }
    const hour = Number(match[1]);
    const minute = Number(match[2]);
    if (hour > 23 || minute > 59) {
        return null;
    }
    return {hour, minute};
}

export function formatTime(hour, minute) {
    return `${pad(hour)}:${pad(minute)}`;
}

/**
 * The request fields of a schedule picked in local time: {fields, error}. `local` is
 * {frequency, minute, hour, weekday (0 = Monday), dayOfMonth}; the fields are the
 * server's (UTC) {frequency, minute, hour, weekday, day_of_month}, only those the
 * frequency uses. A monthly day that is not day 1-28 in UTC is an error (the message
 * says what to pick instead).
 */
export function toUtcSchedule(local, offsetMinutes) {
    const frequency = local.frequency;
    if (!FREQUENCIES.includes(frequency)) {
        return {fields: null, error: 'Pick how often the copy repeats'};
    }
    const minute = Number(local.minute);
    if (frequency === 'hourly') {
        return {fields: {frequency, minute: mod(minute + offsetMinutes, 60)}, error: null};
    }
    const total = Number(local.hour) * 60 + minute + offsetMinutes;
    const dayShift = Math.floor(total / MINUTES_PER_DAY);
    const time = mod(total, MINUTES_PER_DAY);
    const fields = {frequency, minute: time % 60, hour: Math.floor(time / 60)};
    if (frequency === 'weekly') {
        fields.weekday = mod(Number(local.weekday) + dayShift, 7);
    } else if (frequency === 'monthly') {
        const day = Number(local.dayOfMonth) + dayShift;
        if (day < 1) {
            return {fields: null, error: 'At this time, day 1 is still the last day of the previous month in UTC, ' +
                'where Motuz runs schedules; pick a day from 2 to 28 or a later time.'};
        }
        if (day > MAX_DAY_OF_MONTH) {
            return {fields: null, error: `At this time, day ${local.dayOfMonth} is day ${day} in UTC, where Motuz ` +
                `runs schedules, and not every month has it; pick an earlier day or time.`};
        }
        fields.day_of_month = day;
    }
    return {fields, error: null};
}

/**
 * A stored (UTC) schedule in local time: {frequency, minute, hour, weekday, dayOfMonth};
 * dayOfMonth is null when the local day would not be a day 1-28 (shown in UTC then)
 */
export function fromUtcSchedule(schedule, offsetMinutes) {
    const frequency = schedule.frequency;
    if (frequency === 'hourly') {
        return {frequency, minute: mod(schedule.minute - offsetMinutes, 60), hour: null, weekday: null, dayOfMonth: null};
    }
    const total = schedule.hour * 60 + schedule.minute - offsetMinutes;
    const dayShift = Math.floor(total / MINUTES_PER_DAY);
    const time = mod(total, MINUTES_PER_DAY);
    const result = {frequency, minute: time % 60, hour: Math.floor(time / 60), weekday: null, dayOfMonth: null};
    if (frequency === 'weekly') {
        result.weekday = mod(schedule.weekday + dayShift, 7);
    } else if (frequency === 'monthly') {
        const day = schedule.day_of_month + dayShift;
        result.dayOfMonth = day >= 1 && day <= MAX_DAY_OF_MONTH ? day : null;
    }
    return result;
}

/** "Every day at 02:30", "Every hour at :15", "Every Monday at 08:00", "Every month on day 5 at 02:30" (local time) */
export function describeSchedule(schedule, offsetMinutes) {
    if (!schedule || !FREQUENCIES.includes(schedule.frequency)) {
        return '';
    }
    const local = fromUtcSchedule(schedule, offsetMinutes);
    if (local.frequency === 'hourly') {
        return `Every hour at :${pad(local.minute)}`;
    }
    const time = formatTime(local.hour, local.minute);
    if (local.frequency === 'daily') {
        return `Every day at ${time}`;
    }
    if (local.frequency === 'weekly') {
        return `Every ${WEEKDAYS[local.weekday]} at ${time}`;
    }
    if (local.dayOfMonth === null) { // e.g. day 1 in UTC is the last day of the month here
        return `Every month on day ${schedule.day_of_month} at ${formatTime(schedule.hour, schedule.minute)} UTC`;
    }
    return `Every month on day ${local.dayOfMonth} at ${time}`;
}

/** "2026-10-19 02:30" in the browser's time zone for an ISO 8601 UTC time, '' if none */
export function formatLocalDateTime(iso) {
    const date = iso ? new Date(iso) : null;
    if (!date || Number.isNaN(date.getTime())) {
        return '';
    }
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
        `${formatTime(date.getHours(), date.getMinutes())}`;
}

/** The browser's time zone name ("America/Los_Angeles"), else "UTC+02:00" from the offset */
export function timeZoneName(offsetMinutes) {
    try {
        const name = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (name) {
            return name;
        }
    } catch (e) {
        // fall through
    }
    const sign = offsetMinutes <= 0 ? '+' : '-';
    const value = Math.abs(offsetMinutes);
    return `UTC${sign}${pad(Math.floor(value / 60))}:${pad(value % 60)}`;
}

/** The default expected new data per run in GB: 10% of the measured size (null if not measured) */
export function defaultNewGb(estimate) {
    if (!estimate || typeof estimate.bytes !== 'number' || !Number.isFinite(estimate.bytes)) {
        return null;
    }
    return Math.round(estimate.bytes * 0.1 / GB * 10) / 10;
}

/** {low, high, runs} USD per month for `bytesPerRun` new bytes per run, as egress_cost.repeated */
export function monthlyEgress(estimate, bytesPerRun, frequency) {
    const runs = RUNS_PER_MONTH[frequency] || 0;
    if (!estimate || typeof bytesPerRun !== 'number' || !Number.isFinite(bytesPerRun) || bytesPerRun < 0) {
        return {low: null, high: null, runs};
    }
    const round = value => Math.round(value * 100) / 100;
    return {
        low: round(bytesPerRun / GB * runs * estimate.rate_low_per_gb),
        high: round(bytesPerRun / GB * runs * estimate.rate_high_per_gb),
        runs,
    };
}

/**
 * The warning of a schedule out of AWS: {text, confirmLabel}, null if no confirmation is
 * needed. The first run copies everything (the egress estimate of the dialog); later
 * runs only copy new files, so the monthly cost depends on how much new data appears.
 */
export function describeScheduleEgress(estimate, frequency, newGb) {
    if (!needsConfirmation(estimate)) {
        return null;
    }
    const runs = RUNS_PER_MONTH[frequency];
    const runsText = runs >= 10 ? `about ${Math.round(runs)} runs` : runs === 1 ? '1 run' : `about ${runs} runs`;
    const where = estimate.kind === 'inter-region' ? 'between AWS regions' : 'out of AWS';
    const lead = `This schedule copies ${where} ${PER_RUN[frequency]} (${runsText} per month). Only new files are ` +
        'copied after the first run, so the monthly cost depends on how much new data appears.';
    const bytes = typeof newGb === 'number' && Number.isFinite(newGb) && newGb >= 0 ? newGb * GB : null;
    if (bytes === null) {
        const perTb = monthlyEgress(estimate, 1e12, frequency);
        const amount = formatRange(perTb.low, perTb.high);
        return {
            text: `${lead} Each TB of new data per run costs about ${amount} per month.`,
            confirmLabel: `I understand this schedule costs about ${amount} per month for each TB of new data per run`,
        };
    }
    const monthly = monthlyEgress(estimate, bytes, frequency);
    const amount = formatRange(monthly.low, monthly.high);
    const size = formatDecimalSize(bytes);
    return {
        text: `${lead} If ${size} of new data appears per run, this schedule costs about ${amount} per month.`,
        confirmLabel: `I understand this schedule costs about ${amount} per month at ${size} of new data per run`,
    };
}

/** The schedule's `egress_estimate`: the first run's confirmation plus the repeated runs' (null if none) */
export function schedulePayload(firstRunPayload, repeatConfirmed, newGb) {
    if (!firstRunPayload) {
        return null;
    }
    const bytes = typeof newGb === 'number' && Number.isFinite(newGb) && newGb >= 0 ? Math.round(newGb * GB) : null;
    return {...firstRunPayload, repeat_confirmed: Boolean(repeatConfirmed), new_bytes_per_run: bytes};
}
