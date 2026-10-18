/**
 * Sends the files of an upload job in chunks (actions/uploadActions.jsx), a few files at
 * a time and the chunks of a file in order. `putChunk(index, offset, blob)` resolves with
 * {ok, status, body}: status null for a network error.
 *
 * - 200: body.written is where the file is; the file is complete when body.file_state
 *   is no longer 'pending'
 * - 409: the job no longer runs (body.job_state), the file is finished
 *   (body.file_state), or the chunk is not where the file is: go on at body.written,
 *   or start again at 0 if it is null (the server lost the file)
 * - a network error, 5xx: try the same chunk again after a pause; the server answers
 *   409 with the right offset if part of the chunk arrived
 * - other errors: the file fails here (the server fails the job when nothing arrives)
 */

export const UPLOAD_CONCURRENCY = 3;
export const RETRY_DELAYS_MS = [1000, 2000, 5000, 10000, 20000, 30000, 30000, 30000];

const sleepFor = ms => new Promise(resolve => setTimeout(resolve, ms));

/**
 * Uploads the files `pending` (indexes into `files`, [{file}]) and resolves with
 * {done, failed, skipped, stopped}: counts and whether the job was stopped
 */
export async function runUpload({files, pending, chunkBytes, putChunk, sleep=sleepFor,
                                 concurrency=UPLOAD_CONCURRENCY, retryDelays=RETRY_DELAYS_MS}) {
    const result = {done: 0, failed: 0, skipped: 0, stopped: false};
    let next = 0;
    const worker = async () => {
        while (!result.stopped && next < pending.length) {
            const index = pending[next++];
            const state = await uploadFile(files[index].file, index, chunkBytes, putChunk, sleep, retryDelays);
            if (state === 'stopped') {
                result.stopped = true;
            } else if (state === 'done') {
                result.done++;
            } else if (state === 'skipped') {
                result.skipped++;
            } else {
                result.failed++;
            }
        }
    };
    await Promise.all(Array.from({length: Math.min(concurrency, pending.length)}, worker));
    return result;
}

/** 'done', 'failed', 'skipped' or 'stopped' */
async function uploadFile(file, index, chunkBytes, putChunk, sleep, retryDelays) {
    let offset = 0;
    let attempt = 0;
    let conflicts = 0; // in a row: e.g. the file changed size since the drop
    for (;;) {
        const end = Math.min(offset + chunkBytes, file.size);
        const {ok, status, body} = await putChunk(index, offset, file.slice(offset, end));
        if (ok) {
            if (body.file_state !== 'pending') {
                return body.file_state;
            }
            offset = body.written;
            attempt = 0;
            conflicts = 0;
            continue;
        }
        if (status === 409 && body) {
            if (body.job_state) {
                return 'stopped';
            }
            if (body.file_state) {
                return body.file_state;
            }
            if (++conflicts > 3) {
                return 'failed';
            }
            offset = typeof body.written === 'number' ? body.written : 0;
            continue;
        }
        if (status !== null && status < 500) {
            return 'failed';
        }
        if (attempt >= retryDelays.length) {
            return 'failed';
        }
        await sleep(retryDelays[attempt++]);
    }
}
