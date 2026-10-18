import { RSAA } from 'redux-api-middleware';
import upath from 'upath';

import { withAuth } from 'reducers/reducers.jsx';
import { listCopyJobs } from 'actions/apiActions.jsx';
import { showAlert } from 'actions/alertActions.jsx';
import { getCurrentPane } from 'managers/paneManager.jsx';
import { parentDirectory } from 'utils/parentDirectory.js';
import { collectFiles, topLevelNames, uploadPathError } from 'utils/desktopDrop.js';
import { runUpload } from 'utils/uploadRunner.js';

/*
 * Uploads from the desktop: files and folders dropped onto a pane become a copy job
 * whose data the browser sends (managers/upload_manager.py). The job is in the job table
 * like any copy job; this tab sends the files, so closing it fails the job (the page
 * asks before it closes while an upload runs).
 */

export const UPLOAD_CONFLICTS_REQUEST = '@@api/UPLOAD_CONFLICTS_REQUEST';
export const UPLOAD_CONFLICTS_SUCCESS = '@@api/UPLOAD_CONFLICTS_SUCCESS';
export const UPLOAD_CONFLICTS_FAILURE = '@@api/UPLOAD_CONFLICTS_FAILURE';

export const CREATE_UPLOAD_REQUEST = '@@api/CREATE_UPLOAD_REQUEST';
export const CREATE_UPLOAD_SUCCESS = '@@api/CREATE_UPLOAD_SUCCESS';
export const CREATE_UPLOAD_FAILURE = '@@api/CREATE_UPLOAD_FAILURE';

export const UPLOAD_CHUNK_REQUEST = '@@api/UPLOAD_CHUNK_REQUEST';
export const UPLOAD_CHUNK_SUCCESS = '@@api/UPLOAD_CHUNK_SUCCESS';
export const UPLOAD_CHUNK_FAILURE = '@@api/UPLOAD_CHUNK_FAILURE';

export const SHOW_UPLOAD_CONFLICT_DIALOG = '@@dialog/SHOW_UPLOAD_CONFLICT_DIALOG';
export const HIDE_UPLOAD_CONFLICT_DIALOG = '@@dialog/HIDE_UPLOAD_CONFLICT_DIALOG';

const checkUploadConflicts = (data) => ({
    [RSAA]: {
        endpoint: '/api/copy-jobs/uploads/conflicts/',
        method: 'POST',
        body: JSON.stringify(data),
        headers: withAuth({ 'Content-Type': 'application/json' }),
        types: [ UPLOAD_CONFLICTS_REQUEST, UPLOAD_CONFLICTS_SUCCESS, UPLOAD_CONFLICTS_FAILURE ],
    }
});

const createUpload = (data) => ({
    [RSAA]: {
        endpoint: '/api/copy-jobs/uploads/',
        method: 'POST',
        body: JSON.stringify(data),
        headers: withAuth({ 'Content-Type': 'application/json' }),
        types: [ CREATE_UPLOAD_REQUEST, CREATE_UPLOAD_SUCCESS, CREATE_UPLOAD_FAILURE ],
    }
});

const putUploadChunk = (jobId, index, offset, blob) => ({
    [RSAA]: {
        endpoint: `/api/copy-jobs/${jobId}/uploads/${index}?offset=${offset}`,
        method: 'PUT',
        body: blob,
        headers: withAuth({ 'Content-Type': 'application/octet-stream' }),
        types: [ UPLOAD_CHUNK_REQUEST, UPLOAD_CHUNK_SUCCESS, UPLOAD_CHUNK_FAILURE ],
    }
});

// The dropped files of a conflict dialog, by id: File objects stay out of the store
const waiting = new Map();
let nextWaitingId = 1;

// Uploads this tab is sending
let activeUploads = 0;
if (typeof window !== 'undefined') {
    window.addEventListener('beforeunload', event => {
        if (activeUploads > 0) {
            event.preventDefault();
            event.returnValue = ''; // The browser shows its own "Leave site?" question
        }
    });
}

/** {ok, status, body} of a dispatched RSAA (status null for a network error) */
async function result(dispatch, action) {
    let response;
    try {
        response = await dispatch(action);
    } catch (e) {
        return {ok: false, status: null, body: null};
    }
    if (!response) {
        return {ok: false, status: 401, body: null}; // Logged out
    }
    if (!response.error) {
        return {ok: true, status: 200, body: response.payload};
    }
    const payload = response.payload || {};
    return {ok: false, status: payload.status || null, body: payload.response || null};
}

function messageOf(body, fallback) {
    return (body && typeof body.message === 'string' && body.message) || fallback;
}

/**
 * The drop of desktop files onto the pane `side` (onto its folder `dstSubdir`, '..' for
 * the parent folder). `dropped` is utils/desktopDrop.droppedItems() of the drop event.
 */
export const uploadDroppedFiles = (side, dstSubdir, dropped) => async (dispatch, getState) => {
    const pane = getCurrentPane(getState().pane, side);
    let folder = pane.path;
    if (dstSubdir === '..') {
        folder = parentDirectory(pane.path, pane.host);
    } else if (dstSubdir) {
        folder = upath.join(pane.path, dstSubdir);
    }
    if (folder === null) {
        return;
    }

    let files;
    try {
        files = await collectFiles(dropped);
    } catch (e) {
        dispatch(showAlert('Could not upload', e.message));
        return;
    }
    if (files.length === 0) {
        dispatch(showAlert('Nothing to upload', 'The drop has no files. Empty folders are not uploaded.'));
        return;
    }
    const invalid = files.map(f => uploadPathError(f.path)).find(Boolean);
    if (invalid) {
        dispatch(showAlert('Could not upload', invalid));
        return;
    }

    const destination = {dst_cloud_id: pane.host.id || 0, dst_resource_path: folder};
    const conflicts = await result(dispatch, checkUploadConflicts({
        ...destination, names: topLevelNames(files.map(f => f.path)),
    }));
    if (!conflicts.ok) {
        dispatch(showAlert('Could not upload', messageOf(conflicts.body, 'The destination could not be listed.')));
        return;
    }
    const upload = {destination, host: pane.host, files};
    if (conflicts.body.existing.length === 0) {
        await startUpload(dispatch, upload, 'overwrite');
        return;
    }
    const id = nextWaitingId++;
    waiting.set(id, upload);
    dispatch({
        type: SHOW_UPLOAD_CONFLICT_DIALOG,
        payload: {data: {
            id, existing: conflicts.body.existing, host: pane.host, folder, count: files.length,
            // false: the existing names are outside what Motuz wrote before (overwrite policy)
            overwrite: conflicts.body.overwrite !== false,
            overwriteMessage: conflicts.body.overwrite_message || null,
        }},
    });
};

/** The answer of the conflict dialog: 'overwrite', 'skip' or 'cancel' */
export const answerUploadConflict = (id, mode) => async (dispatch) => {
    dispatch({type: HIDE_UPLOAD_CONFLICT_DIALOG});
    const upload = waiting.get(id);
    waiting.delete(id);
    if (upload && mode !== 'cancel') {
        await startUpload(dispatch, upload, mode);
    }
};

async function startUpload(dispatch, {destination, files}, mode) {
    const created = await result(dispatch, createUpload({
        ...destination,
        mode,
        files: files.map(f => ({path: f.path, size: f.file.size})),
    }));
    if (!created.ok) {
        dispatch(showAlert('Could not upload', messageOf(created.body, 'The upload could not be started.')));
        return;
    }
    const {job, pending, chunk_bytes: chunkBytes} = created.body;
    dispatch(listCopyJobs(1)); // The job table polls the job while it runs

    activeUploads++;
    try {
        const outcome = await runUpload({
            files,
            pending,
            chunkBytes,
            putChunk: (index, offset, blob) => result(dispatch, putUploadChunk(job.id, index, offset, blob)),
        });
        if (outcome.failed > 0 && !outcome.stopped) {
            dispatch(showAlert(
                'Upload incomplete',
                `${outcome.failed} of ${pending.length} files of copy job #${job.id} could not be uploaded. `
                + 'The job shows the reasons.',
            ));
        }
    } finally {
        activeUploads--;
        dispatch(listCopyJobs(1));
    }
}
