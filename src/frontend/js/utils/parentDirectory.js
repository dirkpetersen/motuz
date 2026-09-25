import upath from 'upath'

// Connection types whose top level ("/") lists buckets or containers, not files:
// nothing can be copied there.
export const BUCKET_TYPES = ['s3', 'azureblob', 'swift', 'google cloud storage'];

/**
 * The parent directory of `path` in a pane showing `host`, as a copy destination
 * (a drop onto the ".." row), or null if there is none: at "/" (or the empty/"."
 * top of a Google Cloud Storage path, which has no leading slash), and at the root
 * of a bucket, whose parent is the bucket list.
 *
 * Plain function (no JSX) so test/frontend can run it with node --test.
 */
export function parentDirectory(path, host={}) {
    if (typeof path !== 'string') {
        return null;
    }
    const trimmed = path.replace(/\/+$/, '');
    if (trimmed === '' || trimmed === '.') {
        return null; // "/" (or "") has no parent
    }
    let parent = upath.dirname(trimmed);
    if (parent === '.') {
        parent = ''; // "bucket" without a leading slash (Google Cloud Storage)
    }
    const isBucketType = BUCKET_TYPES.includes(host && host.type);
    if (parent === '' || (parent === '/' && isBucketType)) {
        return null;
    }
    return parent;
}
