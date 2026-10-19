// The hash an integrity check compared and the files it could not check, from the
// hashsum job's hash_type, md5_fallback, not_checkable and not_checkable_sample
// (utils/job_runner.py run_hashsum). Plain JS (test/frontend/test_hash_check.mjs).

const LABELS = {md5: 'MD5', sha256: 'SHA-256', crc32c: 'CRC32C'};

/** "CRC32C", "MD5", ...; null for checks from before the hash was stored */
export function hashLabel(hashType) {
    return LABELS[hashType] || null;
}

/** The "Compared" line of the job details, or null */
export function describeHashCheck(job) {
    const label = hashLabel(job && job.hash_type);
    if (!label) {
        return null;
    }
    // Between two local paths CRC32C is just the hash of both sides (no cloud_id: local)
    if (job.hash_type !== 'crc32c' || (!job.src_cloud_id && !job.dst_cloud_id)) {
        return label;
    }
    const fallback = Number.isInteger(job.md5_fallback) ? job.md5_fallback : 0;
    const text = 'CRC32C stored by S3 (one HEAD request per object, nothing downloaded)';
    return fallback > 0 ? `${text}; MD5 for ${fallback} ${fallback === 1 ? 'file' : 'files'} without one` : text;
}

/** The number of files neither identical nor different (no comparable hash), 0 if none */
export function notCheckableCount(job) {
    return job && Number.isInteger(job.not_checkable) && job.not_checkable > 0 ? job.not_checkable : 0;
}

/** The status title of a finished check that found no difference */
export function identicalStatus(job) {
    const count = notCheckableCount(job);
    return count > 0 ? `IDENTICAL, ${count} NOT CHECKABLE` : 'IDENTICAL';
}
