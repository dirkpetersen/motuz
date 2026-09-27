import { RSAA } from 'redux-api-middleware';

import { withAuth } from 'reducers/reducers.jsx';

export const VIEW_DOCUMENT_REQUEST = '@@api/VIEW_DOCUMENT_REQUEST';
export const VIEW_DOCUMENT_SUCCESS = '@@api/VIEW_DOCUMENT_SUCCESS';
export const VIEW_DOCUMENT_FAILURE = '@@api/VIEW_DOCUMENT_FAILURE';

function intHeader(res, name) {
    const value = parseInt(res.headers.get(name), 10);
    return Number.isFinite(value) ? value : null;
}

/**
 * The bytes of a document for the document viewer (PDF, DOCX, XLSX, PPTX, ...):
 * {connection_id, path} for the whole file (at most MOTUZ_VIEW_DOCUMENT_MAX_BYTES), or
 * with {offset, length} a range of a PDF (pdf.js range loading). The promise resolves
 * with the SUCCESS action, whose payload is {buffer, container, size, start}
 * (container: 'pdf', 'zip' or 'cfb', detected by the server from the file's first
 * bytes; size: the whole file's size; start: the offset the bytes start at), or the
 * FAILURE action (payload.status 413, 415, ...; undefined when logged out). The token
 * goes in the header, never in a URL. No reducer handles these actions.
 */
export const viewDocument = (data) => ({
    [RSAA]: {
        endpoint: '/api/system/files/view/document/',
        method: 'POST',
        body: JSON.stringify(data),
        headers: withAuth({ 'Content-Type': 'application/json' }),
        types: [
            VIEW_DOCUMENT_REQUEST,
            {
                type: VIEW_DOCUMENT_SUCCESS,
                payload: async (action, state, res) => {
                    const buffer = await res.arrayBuffer();
                    return {
                        buffer,
                        container: res.headers.get('X-Motuz-Document-Type') || '',
                        size: intHeader(res, 'X-Motuz-File-Size'),
                        start: intHeader(res, 'X-Motuz-Range-Start') || 0,
                    };
                },
            },
            VIEW_DOCUMENT_FAILURE,
        ],
    }
});

const LOAD_ERROR = 'The file could not be read. Please try again.';

/** {status, error} of a failed viewDocument (or a thrown error) */
export function documentErrorOf(action) {
    const payload = action && action.payload;
    const response = (payload && payload.response) || {};
    return {
        status: (payload && payload.status) || null,
        error: (typeof response.message === 'string' && response.message) || LOAD_ERROR,
    };
}

/**
 * Dispatches viewDocument and returns its payload, or throws an Error with `status`
 * and the server's message
 */
export async function fetchDocument(dispatch, data) {
    let action;
    try {
        action = await dispatch(viewDocument(data));
    } catch (e) {
        action = {error: true, payload: e};
    }
    if (action && !action.error && action.payload && action.payload.buffer) {
        return action.payload;
    }
    const {status, error} = documentErrorOf(action);
    const exception = new Error(error);
    exception.status = status;
    throw exception;
}
