import React from 'react';
import { Modal, Button } from 'react-bootstrap'
import upath from 'upath';

import formatBytes from 'utils/formatBytes.jsx'
import { imageNeighbors, rowIndexOf } from 'utils/imageNeighbors.js'
import { convertFirst, describeType, isHeifType, needsConversion } from 'utils/heifConvert.js'

// The types the server sends (utils/image_view.py detects them from the file's first
// bytes); anything else is not shown, whatever the response says. AVIF is shown if the
// browser decodes it (decodeCheck below); a HEIC/HEIF image natively (Safari) or, in the
// other browsers, as the JPEG the server converts it to (utils/heifConvert.js).
export const IMAGE_TYPES = {
    'image/png': 'PNG',
    'image/jpeg': 'JPEG',
    'image/gif': 'GIF',
    'image/webp': 'WebP',
    'image/heic': 'HEIC',
    'image/heif': 'HEIF',
    'image/avif': 'AVIF',
};

// Whether this browser decoded a HEIC/HEIF image itself: null = not known yet. Safari
// does; the others get a JPEG converted by the server (see utils/heifConvert.js).
let nativeHeic = null;

const LOAD_ERROR = 'The image could not be loaded. Please try again.';
const DECODE_TIMEOUT_MS = 15000;

export function imageErrorOf(payload) {
    const response = (payload && payload.response) || {};
    return {
        status: (payload && payload.status) || null,
        error: (typeof response.message === 'string' && response.message) || LOAD_ERROR,
    };
}

/** Resolves to `promise`'s value, or `{timedOut: true}` if it takes longer than `ms` */
function withTimeout(promise, ms) {
    return new Promise((resolve) => {
        const timer = setTimeout(() => resolve({timedOut: true}), ms);
        promise.then(
            (value) => { clearTimeout(timer); resolve(value); },
            () => { clearTimeout(timer); resolve(null); },
        );
    });
}

/**
 * Whether this browser can actually decode `blob`: {width, height} of the decoded
 * image, or null. Tries createImageBitmap first (no extra blob: URL needed), then falls back to
 * Image().decode() for a browser without it.
 */
export async function decodeCheck(blob) {
    if (typeof createImageBitmap === 'function') {
        try {
            const bitmap = await createImageBitmap(blob);
            const dims = {width: bitmap.width, height: bitmap.height};
            if (bitmap.close) {
                bitmap.close();
            }
            return dims;
        } catch (e) {
            return null;
        }
    }
    return new Promise((resolve) => {
        let url;
        try {
            url = URL.createObjectURL(blob);
        } catch (e) {
            resolve(null);
            return;
        }
        const img = new Image();
        img.onload = () => { URL.revokeObjectURL(url); resolve({width: img.naturalWidth, height: img.naturalHeight}); };
        img.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
        img.src = url;
    });
}

/**
 * Read-only image viewer (double-click on a .png, .jpg/.jpeg, .gif, .webp, .heic,
 * .heif or .avif file in a pane). The image comes from
 * POST /api/system/files/view/image/ (read as the user, the type detected by the
 * server from the file's first bytes; SVG is never an image here) with the token in
 * the header, and is shown through a blob: URL, revoked when no longer needed. Fit to
 * the window by default; a click on the image, the button or Z toggles 100%
 * (scrolling). Esc closes.
 *
 * Previous/Next (buttons, ←/→, PageUp/PageDown) step over the image files of the pane
 * this was opened from, in the pane's displayed order (utils/imageNeighbors.js), with
 * no wrap-around at the ends. Every image is checked with decodeCheck before it is
 * shown (createImageBitmap / Image().decode()): the file's actual bytes may be PNG/
 * JPEG/GIF/WebP (always decodable), AVIF (every current browser) or HEIC/HEIF (Safari
 * only): a HEIC/HEIF image the browser cannot decode is fetched again with
 * convert=jpeg, i.e. as the JPEG the server makes of it (as the logged-in user, in a
 * child process: api/utils/heic_convert.py), shown as "HEIC, converted to JPEG"; any
 * other image the browser cannot decode shows a message, not a broken image. Fetched
 * images are cached by path (`this.cache`) so stepping back is instant and blob:
 * URLs are revoked as soon as they fall outside the small window kept around the
 * current image (itself and the preloaded next one); a fetch already in flight for a
 * path is shared (`this.pending`), and a `loadSeq` token makes sure a slow, now-stale
 * fetch can never overwrite what is displayed (its result is still cached correctly).
 */
class ImageViewerDialog extends React.Component {
    constructor(props) {
        super(props);
        const list = (props.files || []).slice();
        const opened = rowIndexOf(list, props.data.name);
        this.state = {
            index: opened != null ? opened : props.data.index,
            loading: true,
            error: null,
            status: null,
            url: null,
            type: null,
            size: null,
            width: null,
            height: null,
            decodeError: false,
            timedOut: false,
            convertedFrom: null,
            actualSize: false,
        };
        // The pane's files as they were when the dialog opened: the pane may be refreshed
        // meanwhile (a finished job), but what is stepped over, shown and cached stays
        // consistent. Everything is keyed by path, not by position.
        this.list = list;
        this.cache = new Map(); // path -> resolved entry (see fetchEntry)
        this.pending = new Map(); // path -> Promise of the fetch in flight
        this.loadSeq = 0;
        this.unmounted = false;
        this.stage = null;
    }

    componentDidMount() {
        this.showIndex(this.state.index, {initial: true});
    }

    componentWillUnmount() {
        this.unmounted = true;
        for (const entry of this.cache.values()) {
            if (entry && entry.url) {
                URL.revokeObjectURL(entry.url);
            }
        }
        this.cache.clear();
    }

    pathFor(index) {
        const file = this.list[index];
        if (!file || typeof this.props.data.folder !== 'string') {
            return null;
        }
        return upath.join(this.props.data.folder, file.name);
    }

    /** The resolved entry for `index` (cached, in flight, or freshly fetched) */
    fetchEntry(index) {
        const key = this.pathFor(index);
        if (this.cache.has(key)) {
            return Promise.resolve(this.cache.get(key));
        }
        if (this.pending.has(key)) {
            return this.pending.get(key);
        }
        const promise = this._fetchEntry(index).finally(() => this.pending.delete(key));
        this.pending.set(key, promise);
        return promise;
    }

    async _fetchEntry(index) {
        const path = this.pathFor(index);
        if (!path) {
            return {error: LOAD_ERROR, status: null};
        }
        const name = this.list[index].name;
        // HEIC/HEIF: the plain image first (Safari shows it), or the server's JPEG at once
        // when this browser has already failed to decode one (utils/heifConvert.js)
        let convert = convertFirst(name, nativeHeic);
        let entry = null;
        for (let attempt = 0; attempt < 2 && !entry; attempt++) {
            const request = {connection_id: this.props.data.connectionId, path};
            if (convert) {
                request.convert = 'jpeg';
            }
            let action;
            try {
                action = await this.props.fetchImage(request);
            } catch (e) {
                action = {error: true, payload: e};
            }
            const result = !this.unmounted && action && !action.error ? action.payload : null;
            if (this.unmounted) {
                break;
            } else if (!result || !result.blob) {
                entry = {...imageErrorOf(action && action.payload)};
            } else if (!IMAGE_TYPES[result.type]) {
                entry = {status: 415, error: 'The server did not send an image type this viewer knows.'};
            } else {
                const outcome = await withTimeout(decodeCheck(result.blob), DECODE_TIMEOUT_MS);
                const decoded = !!(outcome && !outcome.timedOut);
                if (isHeifType(result.type)) {
                    nativeHeic = decoded;
                }
                if (this.unmounted) {
                    break;
                } else if (needsConversion(result.type, decoded) && !convert) {
                    convert = true; // ask the server for a JPEG
                } else if (decoded) {
                    const url = URL.createObjectURL(new Blob([result.blob], {type: result.type}));
                    entry = {url, type: result.type, size: result.size, width: outcome.width, height: outcome.height,
                             convertedFrom: result.convertedFrom || null};
                } else {
                    entry = {decodeError: true, timedOut: !!(outcome && outcome.timedOut), type: result.type, size: result.size};
                }
            }
        }
        if (entry && !this.unmounted) {
            this.cache.set(path, entry);
        }
        return entry || {error: LOAD_ERROR, status: null};
    }

    /** Only the current image and the preloaded next one keep a blob: URL alive */
    prune(index) {
        const keep = new Set([this.pathFor(index)]);
        const neighbors = imageNeighbors(this.list, index);
        if (neighbors && neighbors.nextIndex != null) {
            keep.add(this.pathFor(neighbors.nextIndex));
        }
        for (const [key, entry] of this.cache) {
            if (!keep.has(key)) {
                if (entry && entry.url) {
                    URL.revokeObjectURL(entry.url);
                }
                this.cache.delete(key);
            }
        }
    }

    preloadNext(index) {
        const neighbors = imageNeighbors(this.list, index);
        const next = neighbors && neighbors.nextIndex;
        const key = next != null ? this.pathFor(next) : null;
        if (next != null && !this.cache.has(key) && !this.pending.has(key)) {
            this.fetchEntry(next).then(() => { if (!this.unmounted) this.prune(this.state.index); });
        }
    }

    async showIndex(index, {initial = false} = {}) {
        const seq = ++this.loadSeq;
        const cached = this.cache.get(this.pathFor(index));
        this.setState({
            index,
            loading: !cached,
            error: (cached && cached.error) || null,
            status: (cached && cached.status) || null,
            url: (cached && cached.url) || null,
            type: (cached && cached.type) || null,
            size: cached && cached.size != null ? cached.size : null,
            width: cached && cached.width != null ? cached.width : null,
            height: cached && cached.height != null ? cached.height : null,
            decodeError: !!(cached && cached.decodeError),
            timedOut: !!(cached && cached.timedOut),
            convertedFrom: (cached && cached.convertedFrom) || null,
        });
        if (!initial && this.props.data.side) {
            // The pane's row of this file now (the listing may have been refreshed)
            const row = rowIndexOf(this.props.files, this.list[index] && this.list[index].name);
            if (row != null) {
                this.props.onSelectFile(this.props.data.side, row);
            }
        }
        const entry = await this.fetchEntry(index);
        if (seq !== this.loadSeq || this.unmounted) {
            return; // a later navigation is in charge; `entry` is still cached under `index`
        }
        this.setState({
            loading: false,
            error: entry.error || null,
            status: entry.status || null,
            url: entry.url || null,
            type: entry.type || null,
            size: entry.size != null ? entry.size : null,
            width: entry.width != null ? entry.width : null,
            height: entry.height != null ? entry.height : null,
            decodeError: !!entry.decodeError,
            timedOut: !!entry.timedOut,
            convertedFrom: entry.convertedFrom || null,
        });
        this.prune(index);
        this.preloadNext(index);
    }

    goTo(index) {
        if (index == null || index === this.state.index) {
            return;
        }
        this.showIndex(index);
    }

    onPrev() {
        const neighbors = imageNeighbors(this.list, this.state.index);
        if (neighbors && neighbors.prevIndex != null) {
            this.goTo(neighbors.prevIndex);
        }
    }

    onNext() {
        const neighbors = imageNeighbors(this.list, this.state.index);
        if (neighbors && neighbors.nextIndex != null) {
            this.goTo(neighbors.nextIndex);
        }
    }

    /** The image is decoded and painted (focuses the stage for keyboard navigation) */
    onImageLoad(img) {
        if (img && img.complete && this.stage) {
            this.stage.focus({preventScroll: true});
        }
    }

    onImageError() {
        if (!this.state.decodeError) {
            this.setState({decodeError: true});
        }
    }

    toggleSize() {
        this.setState(state => ({actualSize: !state.actualSize}));
    }

    onKeyDown(event) {
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        const target = event.target;
        if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) {
            return;
        }
        if (event.key === 'z' || event.key === 'Z' || event.key === '1') {
            event.preventDefault();
            this.toggleSize();
        } else if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
            event.preventDefault();
            this.onPrev();
        } else if (event.key === 'ArrowRight' || event.key === 'PageDown') {
            event.preventDefault();
            this.onNext();
        }
    }

    render() {
        const {data} = this.props;
        const hostName = data.host && data.host.id ? data.host.name : null;
        const {loading, error, status, url, type, size, width, height, decodeError, timedOut, convertedFrom, actualSize, index} = this.state;
        const shown = url && !decodeError;
        const neighbors = imageNeighbors(this.list, index);
        const currentFile = this.list[index];
        const name = (currentFile && currentFile.name) || data.name;
        const path = this.pathFor(index) || data.path;

        return (
            <Modal
                show={true}
                size="xl"
                animation={this.props.animation}
                onHide={() => this.props.onClose()}
                dialogClassName='file-viewer-dialog image-viewer-dialog'
                aria-labelledby='file-viewer-title'
            >
                <Modal.Header closeButton>
                    <Modal.Title id='file-viewer-title' className='file-viewer-title'>
                        <span className='file-viewer-name'>{name}</span>
                        <small className='file-viewer-path' title={path}>
                            {hostName ? `${hostName}: ` : ''}{path}
                        </small>
                    </Modal.Title>
                </Modal.Header>
                <Modal.Body className='file-viewer-body' onKeyDown={e => this.onKeyDown(e)}>
                    {neighbors && neighbors.total > 1 && (
                        <div className='file-viewer-toolbar image-viewer-nav-toolbar'>
                            <span className='image-viewer-nav'>
                                <Button size='sm' variant='outline-secondary' disabled={neighbors.prevIndex == null}
                                        title='Previous image (←, PageUp)' aria-label='Previous image'
                                        onClick={() => this.onPrev()}>‹</Button>
                                <span className='image-viewer-position text-muted' role='status'>
                                    {neighbors.position} / {neighbors.total}
                                </span>
                                <Button size='sm' variant='outline-secondary' disabled={neighbors.nextIndex == null}
                                        title='Next image (→, PageDown)' aria-label='Next image'
                                        onClick={() => this.onNext()}>›</Button>
                            </span>
                        </div>
                    )}
                    {error && this.renderError(error, status)}
                    {loading && <div className='file-viewer-loading text-muted'>Loading...</div>}
                    {decodeError && this.renderDecodeError(timedOut)}
                    {url && (
                        <React.Fragment>
                            <div className='file-viewer-toolbar'>
                                <span className='file-viewer-status text-muted' role='status'>
                                    {shown && width ? (actualSize ? 'Actual size (100%)' : 'Fit to window') : ''}
                                </span>
                                <span className='file-viewer-jumps'>
                                    <Button size='sm' variant='outline-secondary' className='image-viewer-zoom'
                                            aria-pressed={actualSize} disabled={!shown}
                                            title='Z or a click on the image'
                                            onClick={() => this.toggleSize()}>
                                        {actualSize ? 'Fit to window' : '100%'}
                                    </Button>
                                </span>
                            </div>
                            <div
                                className={`image-viewer-stage ${actualSize ? 'actual' : 'fit'}`}
                                tabIndex={0}
                                ref={el => { this.stage = el; }}
                                aria-label={`Image ${name}`}
                                hidden={decodeError}
                            >
                                <img
                                    className='image-viewer-image'
                                    src={url}
                                    alt={name}
                                    draggable={false}
                                    ref={img => this.onImageLoad(img)}
                                    onLoad={e => this.onImageLoad(e.target)}
                                    onError={() => this.onImageError()}
                                    onClick={() => this.toggleSize()}
                                    title={actualSize ? 'Click to fit to the window' : 'Click for 100%'}
                                />
                            </div>
                        </React.Fragment>
                    )}
                </Modal.Body>
                <Modal.Footer>
                    {url && (
                        <span className='file-viewer-info image-viewer-info text-muted me-auto'>
                            {describeType(IMAGE_TYPES[type], convertedFrom && IMAGE_TYPES[convertedFrom])}
                            {width && height ? ` · ${width} × ${height} px` : ''}
                            {size != null ? ` · ${formatBytes(size, this.props.useSiUnits)}` : ''}
                            {' · read-only'}
                        </span>
                    )}
                    <Button variant="secondary" onClick={() => this.props.onClose()}>
                        Close
                    </Button>
                </Modal.Footer>
            </Modal>
        );
    }

    renderError(error, status) {
        return (
            <div className='alert alert-warning file-viewer-error' role='alert'>
                <div>{error}</div>
                {status === 415 && (
                    <div className='small mt-1'>
                        The image viewer shows PNG, JPEG, GIF, WebP, HEIC/HEIF and AVIF images, recognized
                        by their content, not their name. SVG files open as text.
                    </div>
                )}
                {status === 413 && (
                    <div className='small mt-1'>
                        Copy the file somewhere to open it with another program.
                    </div>
                )}
            </div>
        );
    }

    renderDecodeError(timedOut) {
        if (timedOut) {
            return (
                <div className='alert alert-warning file-viewer-error image-viewer-decode-error' role='alert'>
                    Decoding this image took too long; it may be damaged or too complex to preview.
                </div>
            );
        }
        return (
            <div className='alert alert-warning file-viewer-error image-viewer-decode-error' role='alert'>
                The image could not be decoded. The file may be damaged or use a variant of
                the format this browser does not support.
            </div>
        );
    }
}

ImageViewerDialog.defaultProps = {
    data: {},
    files: [],
    animation: true,
    useSiUnits: false,
    onClose: () => {},
    onSelectFile: () => {},
    fetchImage: async () => undefined,
}

import {connect} from 'react-redux';
import {hideFileViewerDialog} from 'actions/dialogActions.jsx'
import {fileFocusIndex} from 'actions/paneActions.jsx'
import {viewImage} from 'actions/apiActions.jsx'
import {getCurrentFiles} from 'managers/paneManager.jsx'

const mapStateToProps = state => ({
    data: state.dialog.fileViewerDialogData,
    files: getCurrentFiles(state.pane, state.dialog.fileViewerDialogData.side) || [],
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideFileViewerDialog()),
    fetchImage: data => dispatch(viewImage(data)),
    onSelectFile: (side, index) => dispatch(fileFocusIndex(side, index)),
});

export default connect(mapStateToProps, mapDispatchToProps)(ImageViewerDialog);
