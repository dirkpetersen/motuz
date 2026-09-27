import React from 'react';
import { Modal, Button, ButtonGroup } from 'react-bootstrap'

import formatBytes from 'utils/formatBytes.jsx'
import { imageErrorOf, IMAGE_TYPES } from 'views/Dialogs/ImageViewerDialog.jsx'

// The renderer (react-markdown, remark-gfm) is a separate bundle, loaded on first use
const MarkdownContent = React.lazy(() => import(/* webpackChunkName: "markdown" */ 'views/Dialogs/MarkdownContent.jsx'));

// Rendered from at most the first 1 MiB (whole lines), read with the pager's endpoint
export const MARKDOWN_MAX_BYTES = 1024 * 1024;
// Images of the Markdown file (relative paths, same connection): at most this many,
// two at a time, through the image endpoint
const MAX_IMAGES = 50;
const IMAGE_CONCURRENCY = 2;

const LOAD_ERROR = 'The file could not be loaded. Please try again.';

function errorOf(payload) {
    const response = (payload && payload.response) || {};
    return {
        status: (payload && payload.status) || null,
        error: (typeof response.message === 'string' && response.message) || LOAD_ERROR,
    };
}

/** "Rendered | Source" in the header of the Markdown viewer and of the pager showing a Markdown file */
export function ViewerModeSwitch({mode, onChange}) {
    return (
        <ButtonGroup size='sm' className='viewer-mode-switch ms-auto me-3' aria-label='Show the Markdown file'>
            <Button variant={mode === 'rendered' ? 'secondary' : 'outline-secondary'} aria-pressed={mode === 'rendered'}
                    className='viewer-mode-rendered' onClick={() => mode !== 'rendered' && onChange('rendered')}>
                Rendered
            </Button>
            <Button variant={mode === 'source' ? 'secondary' : 'outline-secondary'} aria-pressed={mode === 'source'}
                    className='viewer-mode-source' onClick={() => mode !== 'source' && onChange('source')}>
                Source
            </Button>
        </ButtonGroup>
    );
}

/**
 * Markdown viewer (double-click on a .md or .markdown file in a pane): the text comes
 * from the pager's endpoint (POST /api/system/files/view/chunk/, read as the user, 415
 * for binary files), at most MARKDOWN_MAX_BYTES, and is rendered in the browser by
 * MarkdownContent (react-markdown + remark-gfm, no raw HTML: it is shown as text).
 * Links: only http, https and mailto, in a new tab. Images: remote ones are never
 * loaded; relative paths are loaded from the same connection through the image
 * endpoint (blob: URLs, revoked on close). "Source" switches to the pager.
 */
class MarkdownViewerDialog extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            loading: true,
            error: null,
            status: null,
            text: null,
            size: null,
            encoding: null,
            truncated: false,
        };
        this.unmounted = false;
        this.urls = new Set();
        this.images = new Map(); // path -> promise of {url} | {error}
        this.active = 0;
        this.waiting = [];
        this.loadImage = this.loadImage.bind(this);
    }

    componentDidMount() {
        this.load();
    }

    componentWillUnmount() {
        this.unmounted = true;
        this.waiting = [];
        for (const url of this.urls) {
            URL.revokeObjectURL(url);
        }
        this.urls.clear();
    }

    async load() {
        const {connectionId, path} = this.props.data;
        const parts = [];
        let offset = 0;
        let last = null;
        while (true) {
            let action;
            try {
                action = await this.props.fetchChunk({connection_id: connectionId, path, offset});
            } catch (e) {
                action = {error: true, payload: e};
            }
            if (this.unmounted) {
                return;
            }
            const result = action && !action.error ? action.payload : null;
            if (!result || typeof result.content !== 'string') {
                this.setState({loading: false, ...errorOf(action && action.payload)});
                return;
            }
            parts.push(result.content);
            last = result;
            if (result.eof || result.end >= MARKDOWN_MAX_BYTES || result.end <= offset) {
                break;
            }
            offset = result.end;
        }
        this.setState({
            loading: false,
            text: parts.join(''),
            size: last.size,
            encoding: last.encoding,
            truncated: !last.eof,
        });
    }

    /** An image of the Markdown file by its resolved path: a promise of {url} or {error} */
    loadImage(path) {
        if (this.images.has(path)) {
            return this.images.get(path);
        }
        if (this.images.size >= MAX_IMAGES) {
            return Promise.resolve({error: `Only the first ${MAX_IMAGES} images are loaded.`});
        }
        const promise = this.whenFree(() => this.fetchImage(path));
        this.images.set(path, promise);
        return promise;
    }

    async fetchImage(path) {
        if (this.unmounted) {
            return {error: 'closed'};
        }
        let action;
        try {
            action = await this.props.fetchImage({connection_id: this.props.data.connectionId, path});
        } catch (e) {
            action = {error: true, payload: e};
        }
        const result = action && !action.error ? action.payload : null;
        if (!result || !result.blob) {
            return {error: imageErrorOf(action && action.payload).error};
        }
        if (!IMAGE_TYPES[result.type]) {
            return {error: 'Not a PNG, JPEG, GIF or WebP image.'};
        }
        if (this.unmounted) {
            return {error: 'closed'};
        }
        const url = URL.createObjectURL(new Blob([result.blob], {type: result.type}));
        this.urls.add(url);
        return {url};
    }

    /** Runs `task` when fewer than IMAGE_CONCURRENCY run */
    whenFree(task) {
        return new Promise(resolve => {
            const start = async () => {
                this.active += 1;
                try {
                    resolve(await task());
                } catch (e) {
                    resolve({error: String(e)});
                } finally {
                    this.active -= 1;
                    const next = this.waiting.shift();
                    if (next) {
                        next();
                    }
                }
            };
            if (this.active < IMAGE_CONCURRENCY) {
                start();
            } else {
                this.waiting.push(start);
            }
        });
    }

    render() {
        const {name, path, host} = this.props.data;
        const hostName = host && host.id ? host.name : null;
        const {loading, error, status, text, size, encoding, truncated} = this.state;

        return (
            <Modal
                show={true}
                size="xl"
                animation={this.props.animation}
                onHide={() => this.props.onClose()}
                dialogClassName='file-viewer-dialog markdown-viewer-dialog'
                aria-labelledby='file-viewer-title'
            >
                <Modal.Header closeButton>
                    <Modal.Title id='file-viewer-title' className='file-viewer-title'>
                        <span className='file-viewer-name'>{name}</span>
                        <small className='file-viewer-path' title={path}>
                            {hostName ? `${hostName}: ` : ''}{path}
                        </small>
                    </Modal.Title>
                    <ViewerModeSwitch mode='rendered' onChange={mode => this.props.onModeChange(mode)} />
                </Modal.Header>
                <Modal.Body className='file-viewer-body'>
                    {error && (
                        <div className='alert alert-warning file-viewer-error' role='alert'>
                            <div>{error}</div>
                            {status === 415 && (
                                <div className='small mt-1'>
                                    The viewer shows text files only. Copy the file somewhere to open it with another program.
                                </div>
                            )}
                        </div>
                    )}
                    {loading && <div className='file-viewer-loading text-muted'>Loading...</div>}
                    {truncated && (
                        <div className='alert alert-info markdown-viewer-truncated py-2 mb-0' role='status'>
                            The file is larger than 1 MiB; only its beginning is rendered.
                            {' '}
                            <button type='button' className='btn btn-link btn-sm p-0 align-baseline'
                                    onClick={() => this.props.onModeChange('source')}>
                                Show the whole file as text
                            </button>
                        </div>
                    )}
                    {text != null && (
                        <div className='markdown-viewer-scroll' tabIndex={0} aria-label={`Content of ${name}`}
                             ref={el => { if (el && !this.focused) { this.focused = true; el.focus({preventScroll: true}); } }}>
                            <React.Suspense fallback={<div className='file-viewer-loading text-muted'>Loading...</div>}>
                                <MarkdownContent text={text} markdownPath={path} loadImage={this.loadImage} />
                            </React.Suspense>
                        </div>
                    )}
                </Modal.Body>
                <Modal.Footer>
                    {text != null && (
                        <span className='file-viewer-info text-muted me-auto'>
                            {formatBytes(size != null ? size : text.length, this.props.useSiUnits)}
                            {encoding && ` · ${encoding}`}
                            {' · Markdown, HTML not rendered · read-only'}
                        </span>
                    )}
                    <Button variant="secondary" onClick={() => this.props.onClose()}>
                        Close
                    </Button>
                </Modal.Footer>
            </Modal>
        );
    }
}

MarkdownViewerDialog.defaultProps = {
    data: {},
    animation: true,
    useSiUnits: false,
    onClose: () => {},
    onModeChange: () => {},
    fetchChunk: async () => undefined,
    fetchImage: async () => undefined,
}

import {connect} from 'react-redux';
import {hideFileViewerDialog, setFileViewerMode} from 'actions/dialogActions.jsx'
import {viewFileChunk, viewImage} from 'actions/apiActions.jsx'

const mapStateToProps = state => ({
    data: state.dialog.fileViewerDialogData,
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideFileViewerDialog()),
    onModeChange: mode => dispatch(setFileViewerMode(mode)),
    fetchChunk: data => dispatch(viewFileChunk(data)),
    fetchImage: data => dispatch(viewImage(data)),
});

export default connect(mapStateToProps, mapDispatchToProps)(MarkdownViewerDialog);
