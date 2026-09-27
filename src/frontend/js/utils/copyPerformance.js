// The "Performance" section of the New Copy Job dialog: validating the fields, the
// memory estimate and presets. Mirrors src/backend/api/utils/rclone_tuning.py, which
// validates again (and has the last word); the fields, limits and presets come from
// GET /api/copy-jobs/performance/.

const KiB = 1024;
const MiB = 1024 * KiB;
const GiB = 1024 * MiB;
const TiB = 1024 * GiB;
const UNITS = {B: 1, K: KiB, M: MiB, G: GiB, T: TiB, P: 1024 * TiB};

const INT_RE = /^[0-9]{1,7}$/;
// rclone's size syntax with a mandatory unit: 64M, 64Mi, 64MiB, 1.5G, 100B
const SIZE_RE = /^([0-9]{1,7})(?:\.([0-9]{1,3}))?(?:(B)|([KMGTP])(?:i?B?))$/i;

// Short names for the job detail
const SHORT_LABELS = {
    transfers: 'transfers',
    checkers: 'checkers',
    multi_thread_streams: 'streams per file',
    multi_thread_cutoff: 'multi-thread above',
    s3_upload_concurrency: 'S3 parts in parallel',
    s3_chunk_size: 'S3 chunk',
    azureblob_upload_concurrency: 'Azure blocks in parallel',
    azureblob_chunk_size: 'Azure chunk',
};

export const CUSTOM = 'custom';

/** Bytes of an rclone size ('64M', '1.5G'), a number of bytes as is; null if neither */
export function parseSize(value) {
    if (typeof value === 'number') {
        return Number.isInteger(value) && value >= 0 ? value : null;
    }
    if (typeof value !== 'string') {
        return null;
    }
    const match = SIZE_RE.exec(value);
    if (!match) {
        return null;
    }
    const [, whole, fraction, byte, unit] = match;
    const multiplier = UNITS[(byte || unit).toUpperCase()];
    let size = Number(whole) * multiplier;
    if (fraction) {
        size += Math.floor(Number(fraction) * multiplier / (10 ** fraction.length));
    }
    return size;
}

/** An integer from a number or a string of digits; null otherwise */
export function parseInteger(value) {
    if (typeof value === 'number') {
        return Number.isInteger(value) ? value : null;
    }
    if (typeof value === 'string' && INT_RE.test(value)) {
        return Number(value);
    }
    return null;
}

export function formatSize(size) {
    for (const [suffix, unit] of [['Ti', TiB], ['Gi', GiB], ['Mi', MiB], ['Ki', KiB]]) {
        if (size >= unit && size % unit === 0) {
            return `${size / unit}${suffix}`;
        }
    }
    return `${size}B`;
}

/** '2.5 GiB', '512 MiB' */
export function describeSize(size) {
    if (size >= GiB) {
        return `${(size / GiB).toFixed(1).replace(/\.0$/, '')} GiB`;
    }
    return `${Math.ceil(size / MiB)} MiB`;
}

function isEmpty(value) {
    return value === undefined || value === null || value === '';
}

function parseField(field, value) {
    return field.kind === 'int' ? parseInteger(value) : parseSize(value);
}

/**
 * Errors of the entered values ({name: message}); empty fields mean the server's
 * default. `fields` as returned by the server: {name, label, kind, min, max}.
 */
export function validatePerformance(values, fields) {
    const errors = {};
    for (const field of fields) {
        const value = values[field.name];
        if (isEmpty(value)) {
            continue;
        }
        const parsed = parseField(field, typeof value === 'string' ? value.trim() : value);
        if (parsed === null) {
            errors[field.name] = field.kind === 'int'
                ? `${field.label} must be a whole number`
                : `${field.label} must be a size like 64M or 1G`;
            continue;
        }
        const min = parseField(field, field.min);
        const max = parseField(field, field.max);
        if (parsed < min || parsed > max) {
            errors[field.name] = `${field.label} must be between ${field.min} and ${field.max}`;
        }
    }
    return errors;
}

/**
 * The rough memory estimate of the server (rclone_tuning.estimate_memory), bytes:
 * transfers * (max(1, streams) * buffer + max(upload concurrency, streams) * chunk),
 * the chunk term for S3 and Azure Blob destinations. Empty fields count with their
 * default. null while a value is invalid.
 */
export function estimateMemory(values, info) {
    const v = {};
    for (const field of info.fields) {
        const raw = isEmpty(values[field.name]) ? field.default : values[field.name];
        const parsed = parseField(field, typeof raw === 'string' ? raw.trim() : raw);
        if (parsed === null) {
            return null;
        }
        v[field.name] = parsed;
    }
    const streams = v.multi_thread_streams;
    let perTransfer = Math.max(1, streams) * info.buffer_size;
    const dst = info.dst_type;
    if (dst === 's3' || dst === 'azureblob') {
        perTransfer += Math.max(v[`${dst}_upload_concurrency`], streams) * v[`${dst}_chunk_size`];
    }
    return v.transfers * perTransfer;
}

/** The field values of a preset: its values, empty (server default) for the others */
export function presetValues(preset, fields) {
    const values = {};
    for (const field of fields) {
        const value = preset && preset.values ? preset.values[field.name] : undefined;
        values[field.name] = isEmpty(value) ? '' : String(value);
    }
    return values;
}

function sameValue(field, a, b) {
    if (isEmpty(a) || isEmpty(b)) {
        return isEmpty(a) && isEmpty(b);
    }
    return parseField(field, String(a).trim()) === parseField(field, String(b).trim());
}

/** The id of the preset these values are, or CUSTOM */
export function matchPreset(values, presets, fields) {
    for (const preset of presets) {
        if (!preset.available) {
            continue;
        }
        const target = presetValues(preset, fields);
        if (fields.every(field => sameValue(field, values[field.name], target[field.name]))) {
            return preset.id;
        }
    }
    return CUSTOM;
}

/** The `performance` of the POST body: set fields only, numbers and size strings; null if none */
export function toRequest(values, fields) {
    const result = {};
    for (const field of fields) {
        const value = values[field.name];
        if (isEmpty(value)) {
            continue;
        }
        const trimmed = String(value).trim();
        result[field.name] = field.kind === 'int' ? parseInteger(trimmed) : trimmed;
    }
    return Object.keys(result).length ? result : null;
}

/** "32 transfers · 64 checkers · S3 chunk 64Mi", or null for the server's defaults */
export function describePerformance(performance) {
    if (!performance || typeof performance !== 'object') {
        return null;
    }
    const parts = Object.keys(SHORT_LABELS)
        .filter(name => !isEmpty(performance[name]))
        .map(name => {
            const value = performance[name];
            const label = SHORT_LABELS[name];
            if (name === 'transfers' || name === 'checkers') {
                return `${value} ${label}`;
            }
            const isSize = name.endsWith('_size') || name === 'multi_thread_cutoff';
            const shown = isSize && typeof value === 'number' ? formatSize(value) : String(value);
            return `${label} ${shown}`;
        });
    return parts.length ? parts.join(' · ') : null;
}
