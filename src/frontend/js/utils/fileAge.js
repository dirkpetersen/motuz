// File ages for the panes. The API sends modification times in UTC: ISO 8601 with a
// `Z` suffix ('2026-09-27T04:57:00Z'), see src/backend/api/utils/file_times.py.
// Plain JS (no JSX), so test/frontend can import it.

export const UNKNOWN = '—'; // em dash

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const MONTH = 30.44 * DAY;
const YEAR = 365.25 * DAY;

/**
 * Milliseconds since the epoch of a modification time, or null if unknown.
 * Accepts the API's ISO 8601 strings (only with a zone, never a naive local time)
 * and epoch seconds.
 */
export function parseModified(modified) {
    if (typeof modified === 'number') {
        return Number.isFinite(modified) ? modified * 1000 : null;
    }
    if (typeof modified !== 'string' || !/(Z|[+-]\d\d:?\d\d)$/i.test(modified)) {
        return null;
    }
    const ms = Date.parse(modified);
    return Number.isNaN(ms) ? null : ms;
}

function plural(n, unit) {
    return `${n} ${unit}${n === 1 ? '' : 's'} ago`;
}

/**
 * "12 sec ago", "5 min ago", "3 h ago", "4 days ago", "2 months ago", "3 years ago".
 * Computed from UTC instants (Date.now()), so it never depends on the time zone or DST.
 */
export function formatAge(modified, nowMs = Date.now()) {
    const ms = parseModified(modified);
    if (ms === null) {
        return UNKNOWN;
    }
    const seconds = Math.floor((nowMs - ms) / 1000);
    if (seconds < -60) {
        return 'in the future'; // clock skew between the storage and this browser
    }
    if (seconds < 1) {
        return 'just now';
    }
    if (seconds < MINUTE) {
        return `${seconds} sec ago`;
    }
    if (seconds < HOUR) {
        return `${Math.floor(seconds / MINUTE)} min ago`;
    }
    if (seconds < DAY) {
        return `${Math.floor(seconds / HOUR)} h ago`;
    }
    if (seconds < MONTH) {
        return plural(Math.floor(seconds / DAY), 'day');
    }
    if (seconds < YEAR) {
        return plural(Math.max(1, Math.floor(seconds / MONTH)), 'month');
    }
    return plural(Math.floor(seconds / YEAR), 'year');
}

/**
 * The exact modification time in the browser's time zone, with the zone's name so it
 * is clear that it is local, e.g. "Sep 26, 2026, 9:57:00 PM PDT". '' if unknown.
 */
export function formatLocalDateTime(modified, locale = undefined, timeZone = undefined) {
    const ms = parseModified(modified);
    if (ms === null) {
        return '';
    }
    return new Date(ms).toLocaleString(locale, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        timeZoneName: 'short',
        timeZone,
    });
}
