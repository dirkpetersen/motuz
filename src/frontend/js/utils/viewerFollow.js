/**
 * The file viewer's follow mode (like `tail -f` / `less +F`): the poll schedule and
 * what to do with a poll's answer. The viewer polls POST /api/system/files/view/chunk/
 * with {offset: <end of what it has>, follow: true}, one request at a time, with
 * setTimeout after each answer.
 *
 * Plain functions (no JSX) so test/frontend can run them with node --test.
 */

// First interval: a local read is one small process; a cloud read costs rclone calls
// (lsjson --stat, and cat when there is something new)
export const LOCAL_POLL_MS = 2000;
export const CLOUD_POLL_MS = 10000;
// While nothing changes the interval grows by this factor up to MAX_POLL_MS, and any
// new data resets it
export const BACKOFF_FACTOR = 1.5;
export const MAX_POLL_MS = 30000;
// Following stops after this long without any activity in the viewer (keys, mouse,
// wheel, the tab becoming visible), so forgotten tabs do not poll forever
export const IDLE_LIMIT_MS = 60 * 60 * 1000;

/** The first poll interval for a connection (0: the local filesystem) */
export function basePollDelay(connectionId) {
    return connectionId ? CLOUD_POLL_MS : LOCAL_POLL_MS;
}

/**
 * Delay before the next poll after `idlePolls` polls in a row without new data:
 * base, base * 1.5, base * 2.25, ... at most MAX_POLL_MS.
 */
export function pollDelay(base, idlePolls, max = MAX_POLL_MS) {
    const n = Math.max(0, idlePolls | 0);
    return Math.min(max, Math.round(base * Math.pow(BACKOFF_FACTOR, n)));
}

/**
 * What a follow poll's answer means, for a poll that read from `followEnd`:
 * - 'truncated': the file is now smaller than what the viewer has (truncated, rotated,
 *   replaced by a shorter file): show a notice and reload the tail.
 * - 'stale': the answer is for another offset (the viewer moved on meanwhile): ignore.
 * - 'append': new complete lines from followEnd; `more` if the server has more after
 *   them (more than a chunk was new), so the viewer polls again at once.
 * - 'unchanged': nothing new (an incomplete last line is not new until its newline).
 */
export function followDecision(followEnd, result) {
    if (!result || typeof result.content !== 'string') {
        return {kind: 'stale', more: false};
    }
    if (result.size != null && result.size < followEnd) {
        return {kind: 'truncated', more: false};
    }
    if (result.offset !== followEnd) {
        return {kind: 'stale', more: false};
    }
    const more = !result.eof;
    if (result.end > result.offset) {
        return {kind: 'append', more};
    }
    return {kind: 'unchanged', more};
}

/** Number of lines in newly appended content (whole lines: one per newline) */
export function countLines(content) {
    if (!content) {
        return 0;
    }
    let n = 0;
    for (let i = content.indexOf('\n'); i !== -1; i = content.indexOf('\n', i + 1)) {
        n++;
    }
    return content.endsWith('\n') ? n : n + 1;
}

/** "just now", "3 s ago", "2 min ago", "1 h ago" */
export function formatAgo(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 1) {
        return 'just now';
    }
    if (s < 60) {
        return `${s} s ago`;
    }
    if (s < 3600) {
        return `${Math.floor(s / 60)} min ago`;
    }
    return `${Math.floor(s / 3600)} h ago`;
}
