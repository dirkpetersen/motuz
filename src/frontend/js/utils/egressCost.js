// AWS egress cost note of the New Copy Job and New Hashsum Job dialogs: the text and the
// confirmation for an estimate of GET /api/copy-jobs/egress-estimate/ (or
// /api/hashsum-jobs/egress-estimate/). The prices come from the server
// (src/backend/api/utils/egress_cost.py), which also refuses a job out of AWS without
// the confirmation.

const TB = 1000;     // decimal GB per TB, as AWS bills
const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB'];

function finite(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function withThousands(digits) {
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** "$0.42", "$9.90", "$50", "$170", "$12,345": cents below $100 (unless whole), whole dollars above */
export function formatUsd(amount) {
    if (!finite(amount)) {
        return '$?';
    }
    if (amount < 100 && Math.round(amount * 100) % 100 !== 0) {
        const [whole, cents] = amount.toFixed(2).split('.');
        return `$${withThousands(whole)}.${cents}`;
    }
    return `$${withThousands(String(Math.round(amount)))}`;
}

/** A per-GB rate: "$0.05", "$0.085" */
export function formatRate(rate) {
    if (!finite(rate)) {
        return '$?';
    }
    const text = rate.toFixed(3).replace(/0$/, '');
    return `$${text}`;
}

/** Decimal size as AWS bills it: "3.4 TB", "120 GB", "512 B" */
export function formatDecimalSize(bytes) {
    if (!finite(bytes)) {
        return '';
    }
    let value = bytes;
    let unit = 0;
    while (value >= 1000 && unit < SIZE_UNITS.length - 1) {
        value /= 1000;
        unit += 1;
    }
    const digits = unit === 0 || value >= 100 ? 0 : 1;
    let text = value.toFixed(digits);
    if (text.endsWith('.0')) {
        text = text.slice(0, -2);
    }
    return `${text} ${SIZE_UNITS[unit]}`;
}

/** "$A–$B", or one amount if both are the same */
export function formatRange(low, high) {
    const a = formatUsd(low);
    const b = formatUsd(high);
    return a === b ? a : `${a}–${b}`;
}

/** Whether the job needs the user's confirmation before it can be created */
export function needsConfirmation(estimate) {
    return Boolean(estimate && estimate.applies &&
        (estimate.kind === 'internet' || estimate.kind === 'inter-region'));
}

/**
 * {text, confirmLabel} for an estimate (null if it does not apply). `action` is 'copy'
 * (New Copy Job) or 'check' (an integrity check with download). confirmLabel is null
 * when nothing has to be confirmed (same region). The server's `reason` says where the
 * job runs and why that costs ("runs on this server, which is not in AWS, so the data
 * leaves AWS"): rclone moves the data through that machine.
 */
export function describeEgress(estimate, action = 'copy') {
    if (!estimate || !estimate.applies) {
        return null;
    }
    const job = action === 'check' ? 'this check' : 'this copy';
    const why = estimate.reason ? `${job} ${estimate.reason}` : '';
    if (!needsConfirmation(estimate)) {
        return {
            text: why ? `No AWS egress fee: ${why}.` : 'No AWS egress fee within the same region.',
            confirmLabel: null,
        };
    }
    const known = finite(estimate.bytes) && finite(estimate.low_usd) && finite(estimate.high_usd);
    const perTb = formatRange(estimate.rate_low_per_gb * TB, estimate.rate_high_per_gb * TB) + ' per TB';
    const amount = known ? formatRange(estimate.low_usd, estimate.high_usd) : perTb;

    let lead;
    let detail;
    if (estimate.kind === 'internet') {
        lead = action === 'check' ? 'Reading out of AWS for the integrity check' : 'Copying out of AWS';
        const free = estimate.free_gb_per_month
            ? `; the first ${estimate.free_gb_per_month} GB per month are free` : '';
        detail = `AWS charges ${rateRange(estimate.rate_low_per_gb, estimate.rate_high_per_gb)} per GB ` +
            `depending on the account's monthly volume${free}; discounts or an egress waiver may lower it`;
    } else {
        lead = action === 'check' ? 'Reading between AWS regions for the integrity check' : 'Copying between AWS regions';
        detail = `AWS charges about ${formatRate(estimate.rate_high_per_gb)} per GB between regions for ${job}`;
    }
    const where = why ? `; ${why}` : '';
    const text = known
        ? `${lead}: about ${amount} in data transfer fees for ${formatDecimalSize(estimate.bytes)}${where} (${detail}).`
        : `${lead}: the size could not be measured; the data transfer fees are about ${perTb}${where} (${detail}).`;
    return {text, confirmLabel: `I understand ${job} costs about ${amount}`};
}

function rateRange(low, high) {
    const a = formatRate(low);
    const b = formatRate(high);
    return a === b ? a : `${a}–${b}`;
}

/** The `egress_estimate` of the create request once confirmed (null otherwise) */
export function confirmationPayload(estimate, confirmed) {
    if (!needsConfirmation(estimate) || !confirmed) {
        return null;
    }
    return {
        confirmed: true,
        kind: estimate.kind,
        bytes: finite(estimate.bytes) ? estimate.bytes : null,
        files: finite(estimate.files) ? estimate.files : null,
        low_usd: finite(estimate.low_usd) ? estimate.low_usd : null,
        high_usd: finite(estimate.high_usd) ? estimate.high_usd : null,
    };
}

/** One line for the job details: "about $170–$306 for 3.4 TB (out of AWS), confirmed" */
export function describeConfirmed(stored) {
    if (!stored || !stored.confirmed) {
        return null;
    }
    const where = (stored.kind === 'inter-region' ? 'between AWS regions' : 'out of AWS') +
        (stored.runs_on ? `; on ${stored.runs_on}` : '');
    if (finite(stored.bytes) && finite(stored.low_usd) && finite(stored.high_usd)) {
        return `About ${formatRange(stored.low_usd, stored.high_usd)} for ${formatDecimalSize(stored.bytes)} ` +
            `(${where}), confirmed`;
    }
    const perTb = formatRange(stored.rate_low_per_gb * TB, stored.rate_high_per_gb * TB);
    return `About ${perTb} per TB (${where}, size not measured), confirmed`;
}
