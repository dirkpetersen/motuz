// Overwrite policy (backend managers/overwrite_policy.py, README "Overwriting existing
// files"): what the job details say about existing destination files.
// Plain functions (no JSX) so test/frontend can run them with node --test.

export const SHOWN_KEPT = 20;

/**
 * "Existing files: ..." of a copy job: `overwrite` false = kept (--ignore-existing),
 * true = overwritten when changed, null/undefined = not recorded (uploads, older jobs).
 */
export function describeOverwrite(job) {
    if (!job || job.upload) {
        return null;
    }
    if (job.overwrite === false) {
        return 'Kept (this destination was not written by Motuz before)';
    }
    if (job.overwrite === true) {
        return 'Overwritten when changed';
    }
    return null;
}

/**
 * "2 existing files were kept (not overwritten): a.txt, b.txt" with at most `shown`
 * names and "... and N more"; null if none were kept.
 */
export function describeKept(count, sample, shown=SHOWN_KEPT) {
    if (!Number.isInteger(count) || count <= 0) {
        return null;
    }
    const names = (Array.isArray(sample) ? sample : []).filter(n => typeof n === 'string').slice(0, shown);
    const head = count === 1 ? '1 existing file was kept (not overwritten)'
        : `${count} existing files were kept (not overwritten)`;
    if (names.length === 0) {
        return head;
    }
    const more = count - names.length;
    return `${head}: ${names.join(', ')}${more > 0 ? ` … and ${more} more` : ''}`;
}
