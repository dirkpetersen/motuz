import React from 'react';
import { Modal, Button } from 'react-bootstrap'

import formatBytes from 'utils/formatBytes.jsx'
import {
    appendChunk,
    describeRange,
    loadedRange,
    positionPercent,
    prependChunk,
    replaceChunks,
} from 'utils/viewerChunks.js'

// Load the next (previous) chunk when the viewport is this close to the bottom (top)
// of what is loaded: two screens, at least 1500 px
const PREFETCH_SCREENS = 2;
const PREFETCH_MIN_PX = 1500;

const LOAD_ERROR = 'The file could not be loaded. Please try again.';

function errorOf(payload) {
    const response = (payload && payload.response) || {};
    return {
        status: (payload && payload.status) || null,
        error: (typeof response.message === 'string' && response.message) || LOAD_ERROR,
    };
}

/**
 * Read-only viewer for text files (double-click on a file in a pane), a pager like
 * `less`: it loads the file in chunks of whole lines (POST /api/system/files/view/chunk/)
 * as the user scrolls, forward and backward, and keeps at most MAX_CHUNKS of them
 * (utils/viewerChunks.js), so multi-GB logs stay responsive. "Jump to bottom" loads the
 * last chunk (tail), "Jump to top" the first. The scroll position is kept when chunks
 * are added or dropped above the viewport. The content is rendered as text by React
 * (escaped), never as HTML.
 */
class FileViewerDialog extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            chunks: [],
            size: null,
            encoding: null,
            loading: 'first', // 'first' | 'jump' | 'next' | 'prev' | null
            error: null,
            status: null,
            percent: 0,
        };
        this.scroller = null;
        this.generation = 0; // a jump makes answers to earlier requests obsolete
        this.pendingScroll = null; // 'top' | 'bottom' after a jump
        this.pendingFocus = true;
        this.frame = null;
        this.unmounted = false;
    }

    componentDidMount() {
        this.load('first', {offset: 0});
    }

    componentWillUnmount() {
        this.unmounted = true;
        if (this.frame) {
            cancelAnimationFrame(this.frame);
        }
    }

    getSnapshotBeforeUpdate(prevProps, prevState) {
        // Chunks added or dropped: remember where the chunk at the top of the viewport
        // is, to put it back there (a jump scrolls to the top or bottom instead)
        if (prevState.chunks === this.state.chunks || this.pendingScroll || !this.scroller) {
            return null;
        }
        const top = this.scroller.getBoundingClientRect().top;
        for (const chunk of prevState.chunks) {
            const el = this.chunkElement(chunk.key);
            if (el) {
                const rect = el.getBoundingClientRect();
                if (rect.bottom > top) {
                    return {key: chunk.key, top: rect.top - top};
                }
            }
        }
        return null;
    }

    componentDidUpdate(prevProps, prevState, anchor) {
        const scroller = this.scroller;
        if (!scroller) {
            return;
        }
        if (this.pendingScroll) {
            scroller.scrollTop = this.pendingScroll === 'bottom' ? scroller.scrollHeight : 0;
            this.pendingScroll = null;
        } else if (anchor) {
            const el = this.chunkElement(anchor.key);
            if (el) {
                const moved = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - anchor.top;
                scroller.scrollTop += moved;
            }
        }
        if (this.pendingFocus) {
            this.pendingFocus = false;
            scroller.focus({preventScroll: true}); // PageDown, arrows, Space, End work at once
        }
        if (prevState.chunks !== this.state.chunks || prevState.loading !== this.state.loading) {
            this.updatePercent();
            this.maybeLoad();
        }
    }

    chunkElement(key) {
        return this.scroller && this.scroller.querySelector(`[data-chunk="${key}"]`);
    }

    async load(kind, params) {
        if (kind === 'first' || kind === 'jump') {
            this.generation += 1;
        }
        const generation = this.generation;
        this.setState(kind === 'jump' ? {loading: kind, error: null, status: null} : {loading: kind});

        const {connectionId, path} = this.props.data;
        let action;
        try {
            action = await this.props.fetchChunk({connection_id: connectionId, path, ...params});
        } catch (e) {
            action = {error: true, payload: e};
        }
        if (this.unmounted || generation !== this.generation) {
            return; // closed, or a jump meanwhile
        }
        const result = action && !action.error ? action.payload : null;
        if (!result || typeof result.content !== 'string') {
            this.setState({loading: null, ...errorOf(action && action.payload)});
            return;
        }

        this.setState(state => {
            const common = {loading: null, size: result.size, encoding: result.encoding};
            if (kind === 'first' || kind === 'jump') {
                this.pendingScroll = params.from_end ? 'bottom' : 'top';
                return {...common, chunks: replaceChunks(result), error: null, status: null};
            }
            const {chunks} = kind === 'next' ? appendChunk(state.chunks, result) : prependChunk(state.chunks, result);
            return {...common, chunks};
        });
    }

    /** Loads the next or previous chunk when the viewport nears the end of what is loaded */
    maybeLoad() {
        const scroller = this.scroller;
        const range = loadedRange(this.state.chunks);
        if (!scroller || !range || this.state.loading || this.state.error) {
            return;
        }
        const margin = Math.max(PREFETCH_SCREENS * scroller.clientHeight, PREFETCH_MIN_PX);
        const below = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
        if (!range.eof && below < margin) {
            this.load('next', {offset: range.end});
        } else if (!range.bof && scroller.scrollTop < margin) {
            this.load('prev', {before: range.offset});
        }
    }

    updatePercent() {
        const scroller = this.scroller;
        if (!scroller) {
            return;
        }
        const percent = positionPercent(loadedRange(this.state.chunks), this.state.size, scroller);
        if (percent !== this.state.percent) {
            this.setState({percent});
        }
    }

    onScroll() {
        if (this.frame) {
            return;
        }
        this.frame = requestAnimationFrame(() => {
            this.frame = null;
            this.updatePercent();
            this.maybeLoad();
        });
    }

    onKeyDown(event) {
        // Home/End scroll to the top/bottom of what is loaded (the browser does that);
        // Ctrl+Home/Ctrl+End go to the start/end of the file
        if (!(event.ctrlKey || event.metaKey)) {
            return;
        }
        if (event.key === 'End') {
            event.preventDefault();
            this.jumpToBottom();
        } else if (event.key === 'Home') {
            event.preventDefault();
            this.jumpToTop();
        }
    }

    jumpToTop() {
        this.pendingFocus = true;
        this.load('jump', {offset: 0});
    }

    jumpToBottom() {
        this.pendingFocus = true;
        this.load('jump', {from_end: true});
    }

    render() {
        const {name, path, host} = this.props.data;
        const hostName = host && host.id ? host.name : null;
        const {chunks, loading, error, status, size, encoding} = this.state;
        const range = loadedRange(chunks);

        return (
            <Modal
                show={true}
                size="xl"
                onHide={() => this.props.onClose()}
                dialogClassName='file-viewer-dialog'
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
                <Modal.Body className='file-viewer-body'>
                    {error && this.renderError(error, status)}
                    {!range && loading && <div className='file-viewer-loading text-muted'>Loading...</div>}
                    {range && this.renderPager(range)}
                </Modal.Body>
                <Modal.Footer>
                    {range && (
                        <span className='file-viewer-info text-muted me-auto'>
                            {formatBytes(size != null ? size : range.end, this.props.useSiUnits)}
                            {encoding && ` · ${encoding}`}
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
                        The viewer shows text files only. Copy the file somewhere to open it with another program.
                    </div>
                )}
            </div>
        );
    }

    renderPager(range) {
        const {chunks, loading, size, percent} = this.state;
        if (range.bof && range.eof && chunks.every(c => c.content === '')) {
            return <div className='text-muted file-viewer-empty'>The file is empty.</div>;
        }
        const jumping = loading === 'jump';
        return (
            <React.Fragment>
                <div className='file-viewer-toolbar'>
                    <span className='file-viewer-status text-muted' role='status'>
                        Showing {describeRange(range, size)}
                        <span className='file-viewer-percent'> · {percent}%</span>
                    </span>
                    <span className='file-viewer-jumps'>
                        <Button size='sm' variant='outline-secondary' className='file-viewer-jump-top'
                                disabled={jumping} title='Ctrl+Home' onClick={() => this.jumpToTop()}>
                            Jump to top
                        </Button>
                        <Button size='sm' variant='outline-secondary' className='file-viewer-jump-bottom'
                                disabled={jumping} title='Ctrl+End' onClick={() => this.jumpToBottom()}>
                            Jump to bottom
                        </Button>
                    </span>
                </div>
                <div
                    className='file-viewer-scroll'
                    tabIndex={0}
                    ref={el => { this.scroller = el; }}
                    onScroll={() => this.onScroll()}
                    onKeyDown={e => this.onKeyDown(e)}
                    aria-label={`Content of ${this.props.data.name}`}
                    aria-busy={!!loading}
                >
                    <div className='file-viewer-marker file-viewer-marker-top' aria-hidden='true'>
                        {range.bof ? 'Beginning of file'
                            : loading === 'prev' ? 'Loading earlier lines…' : 'Scroll up for earlier lines'}
                    </div>
                    <pre className='file-viewer-content'>
                        {chunks.map(chunk => (
                            <span key={chunk.key} data-chunk={chunk.key} data-offset={chunk.offset} data-end={chunk.end}>
                                {chunk.content}
                            </span>
                        ))}
                    </pre>
                    <div className='file-viewer-marker file-viewer-marker-bottom'>
                        {range.eof ? <span className='file-viewer-eof'>End of file</span>
                            : loading === 'next' ? <span className='file-viewer-loading-more'>Loading more…</span>
                            : 'Scroll down for more'}
                    </div>
                </div>
                <div className='file-viewer-hint text-muted'>
                    PgUp/PgDn, ↑/↓, Space: scroll · Home/End: top/bottom of the loaded part ·
                    Ctrl+Home/Ctrl+End or the buttons: start/end of the file
                </div>
            </React.Fragment>
        );
    }
}

FileViewerDialog.defaultProps = {
    data: {},
    useSiUnits: false,
    onClose: () => {},
    fetchChunk: async () => undefined,
}

import {connect} from 'react-redux';
import {hideFileViewerDialog} from 'actions/dialogActions.jsx'
import {viewFileChunk} from 'actions/apiActions.jsx'

const mapStateToProps = state => ({
    data: state.dialog.fileViewerDialogData,
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideFileViewerDialog()),
    fetchChunk: data => dispatch(viewFileChunk(data)),
});

export default connect(mapStateToProps, mapDispatchToProps)(FileViewerDialog);
