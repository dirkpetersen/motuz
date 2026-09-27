import React from 'react';
import { Modal, Button } from 'react-bootstrap'

import formatBytes from 'utils/formatBytes.jsx'
import {
    MAX_CHUNKS,
    MERGE_BYTES,
    appendChunk,
    appendFollowChunk,
    describeRange,
    loadedRange,
    positionPercent,
    prependChunk,
    replaceChunks,
} from 'utils/viewerChunks.js'
import {
    IDLE_LIMIT_MS,
    basePollDelay,
    countLines,
    followDecision,
    formatAgo,
    pollDelay,
} from 'utils/viewerFollow.js'

// Load the next (previous) chunk when the viewport is this close to the bottom (top)
// of what is loaded: two screens, at least 1500 px
const PREFETCH_SCREENS = 2;
const PREFETCH_MIN_PX = 1500;
// Following: scrolling up by more than this pauses auto-scroll
const PAUSE_PX = 16;
// Following: when more than this is new at once (the file grew by more than a window),
// the tail is loaded instead of reading everything in between
const CATCH_UP_BYTES = MAX_CHUNKS * MERGE_BYTES;
// A busy poll (another request in flight) retries after this
const BUSY_RETRY_MS = 500;
const NOTICE_MS = 15000;

const LOAD_ERROR = 'The file could not be loaded. Please try again.';

function errorOf(payload) {
    const response = (payload && payload.response) || {};
    return {
        status: (payload && payload.status) || null,
        error: (typeof response.message === 'string' && response.message) || LOAD_ERROR,
    };
}

/** "Following… (updated 3 s ago)", re-rendered every second on its own */
function FollowAgo({since}) {
    const [now, setNow] = React.useState(Date.now());
    React.useEffect(() => {
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, []);
    return <React.Fragment>updated {formatAgo(now - since)}</React.Fragment>;
}

/**
 * Read-only viewer for text files (double-click on a file in a pane), a pager like
 * `less`: it loads the file in chunks of whole lines (POST /api/system/files/view/chunk/)
 * as the user scrolls, forward and backward, and keeps at most MAX_CHUNKS of them
 * (utils/viewerChunks.js), so multi-GB logs stay responsive. "Jump to bottom" loads the
 * last chunk (tail), "Jump to top" the first. The scroll position is kept when chunks
 * are added or dropped above the viewport. The content is rendered as text by React
 * (escaped), never as HTML.
 *
 * Follow (the button or F, like `less +F` / `tail -f`) shows the end of the file and
 * polls it for new complete lines (utils/viewerFollow.js): every 2 s for local files,
 * 10 s for cloud files, backing off to 30 s while nothing changes. New lines are
 * appended and scrolled into view; scrolling up pauses the auto-scroll (the lines keep
 * coming and are counted), Resume goes back to the end. A truncated file reloads the
 * tail, a deleted one stops following. Requests never overlap (one queue), the next
 * poll is scheduled after the answer, and polling stops when the dialog closes, while
 * the tab is hidden and after an hour without activity.
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
            // follow mode (mirrors this.following / this.paused for rendering)
            following: false,
            paused: false,
            newLines: 0,
            newLinesMore: false, // more than counted (the viewer skipped ahead to the tail)
            lastUpdate: null,
            followError: null,
            notice: null, // {kind: 'truncated' | 'missing' | 'stopped' | 'idle', text}
        };
        this.scroller = null;
        this.generation = 0; // a jump makes answers to earlier requests obsolete
        this.pendingScroll = null; // 'top' | 'bottom' after a jump
        this.pendingFocus = true;
        this.frame = null;
        this.unmounted = false;
        // One request at a time: every read goes through this queue
        this.queue = Promise.resolve();
        this.inFlight = 0;
        // Follow mode
        this.following = false;
        this.paused = false;
        this.followEnd = null; // the end of what the viewer has seen of the file
        this.followGeneration = 0; // turning follow off or on makes pending polls obsolete
        this.pollTimer = null;
        this.idlePolls = 0;
        this.lastActivity = Date.now();
        this.noticeTimer = null;
        this.onVisibilityChange = this.onVisibilityChange.bind(this);
    }

    componentDidMount() {
        document.addEventListener('visibilitychange', this.onVisibilityChange);
        this.load('first', {offset: 0});
    }

    componentWillUnmount() {
        this.unmounted = true;
        this.following = false;
        document.removeEventListener('visibilitychange', this.onVisibilityChange);
        clearTimeout(this.pollTimer);
        clearTimeout(this.noticeTimer);
        if (this.frame) {
            cancelAnimationFrame(this.frame);
        }
    }

    getSnapshotBeforeUpdate(prevProps, prevState) {
        // Chunks added or dropped: remember where the chunk at the top of the viewport
        // is, to put it back there (a jump scrolls to the top or bottom instead). The
        // chunk must still be there: while following, chunks are dropped at the top.
        if (prevState.chunks === this.state.chunks || this.pendingScroll || !this.scroller) {
            return null;
        }
        const keep = new Set(this.state.chunks.map(chunk => chunk.key));
        const top = this.scroller.getBoundingClientRect().top;
        for (const chunk of prevState.chunks) {
            const el = keep.has(chunk.key) && this.chunkElement(chunk.key);
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
        if (this.following && !this.paused && prevState.chunks !== this.state.chunks) {
            this.pendingScroll = 'bottom'; // following: the newest line stays in view
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

    /** One chunk request, after the ones before it: never two at once. null once closed. */
    request(params) {
        const {connectionId, path} = this.props.data;
        const run = this.queue.then(async () => {
            if (this.unmounted) {
                return null;
            }
            this.inFlight += 1;
            try {
                return await this.props.fetchChunk({connection_id: connectionId, path, ...params});
            } catch (e) {
                return {error: true, payload: e};
            } finally {
                this.inFlight -= 1;
            }
        });
        this.queue = run.catch(() => null);
        return run;
    }

    /** Loads a chunk into the window; the answer (the chunk) or null */
    async load(kind, params) {
        if (kind === 'first' || kind === 'jump') {
            this.generation += 1;
        }
        const generation = this.generation;
        this.setState(kind === 'jump' ? {loading: kind, error: null, status: null} : {loading: kind});

        const action = await this.request(params);
        if (this.unmounted || !action || generation !== this.generation) {
            return null; // closed, or a jump meanwhile
        }
        const result = action && !action.error ? action.payload : null;
        if (!result || typeof result.content !== 'string') {
            this.setState({loading: null, ...errorOf(action && action.payload)});
            return null;
        }

        if (this.following) {
            // The tail, or reading down to the end: what follow polls continue from
            if (params.from_end) {
                this.followEnd = result.end;
            } else if (kind === 'next' && result.eof && result.end > this.followEnd) {
                this.followEnd = result.end;
            }
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
        return result;
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
        // While following, the polls read on from followEnd themselves
        const polled = this.following && range.end === this.followEnd;
        if (!range.eof && !polled && below < margin) {
            this.load('next', {offset: range.end});
        } else if (!range.bof && scroller.scrollTop < margin && !(this.following && !this.paused)) {
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
        const scroller = this.scroller;
        if (this.following && scroller && !this.pendingScroll) {
            const below = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
            if (!this.paused && below > PAUSE_PX) {
                this.pause();
            } else if (this.paused && below < 2 && this.windowAtFollowEnd() && !this.state.loading) {
                this.unpause(); // scrolled back down to the newest line
            }
        }
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
        this.noteActivity();
        if (event.ctrlKey || event.metaKey) {
            // Home/End scroll to the top/bottom of what is loaded (the browser does
            // that); Ctrl+Home/Ctrl+End go to the start/end of the file
            if (event.key === 'End') {
                event.preventDefault();
                this.jumpToBottom();
            } else if (event.key === 'Home') {
                event.preventDefault();
                this.jumpToTop();
            }
            return;
        }
        if (event.altKey) {
            return;
        }
        if (event.key === 'f' || event.key === 'F') {
            event.preventDefault();
            this.toggleFollow();
        } else if (this.following && !this.paused && ['PageUp', 'Home', 'ArrowUp'].includes(event.key)) {
            this.pause();
        }
    }

    noteActivity() {
        this.lastActivity = Date.now();
    }

    onVisibilityChange() {
        if (document.hidden) {
            clearTimeout(this.pollTimer); // an answer on its way schedules nothing either
            this.pollTimer = null;
        } else if (this.following) {
            this.noteActivity();
            this.idlePolls = 0;
            this.schedulePoll(0);
        }
    }

    jumpToTop() {
        this.pendingFocus = true;
        if (this.following) {
            this.pause();
        }
        this.load('jump', {offset: 0});
    }

    jumpToBottom() {
        this.pendingFocus = true;
        if (this.following) {
            this.unpause();
        }
        this.load('jump', {from_end: true});
    }

    // ------------------------------------------------------------------ follow mode

    toggleFollow() {
        this.noteActivity();
        if (this.following) {
            this.stopFollow();
        } else {
            this.startFollow();
        }
    }

    async startFollow() {
        this.following = true;
        this.paused = false;
        this.followGeneration += 1;
        const followGeneration = this.followGeneration;
        this.idlePolls = 0;
        this.pendingFocus = true;
        this.showNotice(null);
        this.setState({following: true, paused: false, newLines: 0, newLinesMore: false,
                       lastUpdate: Date.now(), followError: null});
        const range = loadedRange(this.state.chunks);
        if (range && range.eof && !this.state.error && !this.state.loading) {
            // The end of the file is loaded: scroll there and check for news at once
            this.followEnd = range.end;
            this.pendingScroll = 'bottom';
            this.forceUpdate();
            this.schedulePoll(0);
            return;
        }
        this.followEnd = null;
        const tail = await this.load('jump', {from_end: true});
        if (!this.following || followGeneration !== this.followGeneration) {
            return;
        }
        if (!tail) {
            this.stopFollow();
            return;
        }
        this.schedulePoll(this.baseDelay());
    }

    stopFollow(notice = null) {
        this.following = false;
        this.paused = false;
        this.followGeneration += 1;
        clearTimeout(this.pollTimer);
        this.pollTimer = null;
        this.setState({following: false, paused: false, newLines: 0, newLinesMore: false, followError: null});
        this.showNotice(notice);
    }

    pause() {
        if (this.following && !this.paused) {
            this.paused = true;
            this.setState({paused: true, newLines: 0, newLinesMore: false});
        }
    }

    unpause() {
        if (this.paused) {
            this.paused = false;
            this.setState({paused: false, newLines: 0, newLinesMore: false});
        }
    }

    /** Resume: back to the newest line; the tail is loaded unless the window reaches it */
    resume() {
        this.noteActivity();
        this.pendingFocus = true;
        if (this.windowAtFollowEnd()) {
            this.unpause();
            this.pendingScroll = 'bottom';
            this.forceUpdate();
        } else {
            this.jumpToBottom();
        }
    }

    /** The window reaches what follow has seen: new lines can be appended to it */
    windowAtFollowEnd() {
        const range = loadedRange(this.state.chunks);
        return !!range && range.eof && range.end === this.followEnd;
    }

    baseDelay() {
        return basePollDelay(this.props.data.connectionId);
    }

    showNotice(notice) {
        clearTimeout(this.noticeTimer);
        this.noticeTimer = null;
        if (this.unmounted) {
            return;
        }
        this.setState({notice});
        if (notice && notice.kind === 'truncated') {
            this.noticeTimer = setTimeout(() => this.setState({notice: null}), NOTICE_MS);
        }
    }

    /** The next poll after `delay` ms, unless not following, closed or the tab is hidden */
    schedulePoll(delay) {
        clearTimeout(this.pollTimer);
        this.pollTimer = null;
        if (!this.following || this.unmounted || document.hidden) {
            return;
        }
        this.pollTimer = setTimeout(() => this.poll(), delay);
    }

    async poll() {
        this.pollTimer = null;
        if (!this.following || this.unmounted || document.hidden) {
            return;
        }
        if (Date.now() - this.lastActivity > IDLE_LIMIT_MS) {
            this.stopFollow({kind: 'idle', text: 'Follow stopped after an hour without activity. Press F to follow again.'});
            return;
        }
        if (this.state.loading || this.inFlight > 0 || this.followEnd == null) {
            this.schedulePoll(BUSY_RETRY_MS); // a jump or a scroll load first
            return;
        }
        const followGeneration = this.followGeneration;
        const generation = this.generation;
        const offset = this.followEnd;
        const action = await this.request({offset, follow: true});
        if (!action || this.unmounted || !this.following || followGeneration !== this.followGeneration) {
            return;
        }
        if (generation !== this.generation || offset !== this.followEnd) {
            this.schedulePoll(0); // a jump meanwhile: poll from where it ended
            return;
        }
        const base = this.baseDelay();
        const result = action.error ? null : action.payload;
        if (!result || typeof result.content !== 'string') {
            const {status, error} = errorOf(action.payload);
            if (status === 404) {
                this.stopFollow({kind: 'missing', text: 'The file no longer exists. Follow stopped.'});
            } else if (status === 400 || status === 403 || status === 415) {
                this.stopFollow({kind: 'stopped', text: `${error} Follow stopped.`});
            } else {
                this.idlePolls += 1; // a server or network error: try again later
                this.setState({followError: error});
                this.schedulePoll(pollDelay(base, this.idlePolls));
            }
            return;
        }

        const decision = followDecision(offset, result);
        if (decision.kind === 'truncated') {
            this.idlePolls = 0;
            this.showNotice({kind: 'truncated', text: 'File was truncated, showing the new end'});
            await this.reloadTail(followGeneration);
            return;
        }
        if (decision.kind !== 'append') {
            this.idlePolls += 1;
            if (result.size !== this.state.size || this.state.followError) {
                this.setState({size: result.size, followError: null});
            }
            this.schedulePoll(pollDelay(base, this.idlePolls));
            return;
        }

        this.idlePolls = 0;
        if (decision.more && result.size - result.end > CATCH_UP_BYTES) {
            // Far behind (the file grew by more than a window): go to the tail
            await this.reloadTail(followGeneration, countLines(result.content));
            return;
        }
        const contiguous = this.windowAtFollowEnd();
        const lines = countLines(result.content);
        this.followEnd = result.end;
        if (!contiguous && !this.paused) {
            await this.reloadTail(followGeneration);
            return;
        }
        this.setState(state => ({
            chunks: contiguous ? appendFollowChunk(state.chunks, result).chunks : state.chunks,
            size: result.size,
            lastUpdate: Date.now(),
            followError: null,
            newLines: this.paused ? state.newLines + lines : 0,
        }));
        this.schedulePoll(decision.more ? 0 : base);
    }

    /**
     * Loads the tail while following (after a truncation, or to catch up). While paused
     * the window stays and only the count of new lines grows (`skipped` counted lines
     * plus the tail's).
     */
    async reloadTail(followGeneration, skipped = 0) {
        if (this.paused) {
            const action = await this.request({from_end: true});
            if (!action || !this.following || followGeneration !== this.followGeneration) {
                return;
            }
            const tail = action.error ? null : action.payload;
            if (!tail || typeof tail.content !== 'string') {
                this.stopFollow({kind: 'stopped', text: `${errorOf(action.payload).error} Follow stopped.`});
                return;
            }
            this.followEnd = tail.end;
            this.setState(state => ({
                size: tail.size, lastUpdate: Date.now(),
                newLines: state.newLines + skipped + countLines(tail.content), newLinesMore: true,
            }));
        } else {
            const tail = await this.load('jump', {from_end: true});
            if (!this.following || followGeneration !== this.followGeneration) {
                return;
            }
            if (!tail) {
                this.stopFollow();
                return;
            }
            this.setState({lastUpdate: Date.now()});
        }
        this.schedulePoll(this.baseDelay());
    }

    // ------------------------------------------------------------------ rendering

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
                <Modal.Body
                    className='file-viewer-body'
                    onKeyDown={e => this.onKeyDown(e)}
                    onMouseDown={() => this.noteActivity()}
                    onMouseMove={() => this.noteActivity()}
                    onWheel={() => this.noteActivity()}
                    onTouchStart={() => this.noteActivity()}
                >
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

    renderFollowStatus() {
        const {following, paused, lastUpdate, followError} = this.state;
        if (!following) {
            return null;
        }
        if (paused) {
            return (
                <span className='file-viewer-follow-status paused' role='status'>
                    <span className='file-viewer-follow-dot' aria-hidden='true' />
                    Following paused
                </span>
            );
        }
        return (
            <span className='file-viewer-follow-status' role='status' title={followError || undefined}>
                <span className='file-viewer-follow-dot' aria-hidden='true' />
                Following… (<FollowAgo since={lastUpdate || Date.now()} />)
                {followError && <span className='file-viewer-follow-error'> · last check failed</span>}
            </span>
        );
    }

    renderPausedBar() {
        const {following, paused, newLines, newLinesMore} = this.state;
        if (!following || !paused) {
            return null;
        }
        const count = `${newLines.toLocaleString('en-US')}${newLinesMore ? '+' : ''}`;
        return (
            <div className='file-viewer-paused' role='status'>
                Paused: <span className='file-viewer-new-lines'>{count}</span> new {newLines === 1 && !newLinesMore ? 'line' : 'lines'} below
                {' · '}
                <button type='button' className='btn btn-link btn-sm file-viewer-resume' onClick={() => this.resume()}>
                    Resume
                </button>
            </div>
        );
    }

    renderNotice() {
        const {notice} = this.state;
        if (!notice) {
            return null;
        }
        return (
            <div className={`file-viewer-notice file-viewer-notice-${notice.kind}`} role='alert'>
                <span>{notice.text}</span>
                <button type='button' className='btn-close btn-sm' aria-label='Dismiss'
                        onClick={() => this.showNotice(null)} />
            </div>
        );
    }

    renderPager(range) {
        const {chunks, loading, size, percent, following} = this.state;
        const empty = range.bof && range.eof && chunks.every(c => c.content === '');
        const jumping = loading === 'jump';
        return (
            <React.Fragment>
                <div className='file-viewer-toolbar'>
                    <span className='file-viewer-status text-muted' role='status'>
                        Showing {describeRange(range, size)}
                        <span className='file-viewer-percent'> · {percent}%</span>
                    </span>
                    {this.renderFollowStatus()}
                    <span className='file-viewer-jumps'>
                        <Button size='sm' variant={following ? 'primary' : 'outline-secondary'}
                                className='file-viewer-follow' aria-pressed={following}
                                title='F: show new lines as they are written (like tail -f)'
                                onClick={() => this.toggleFollow()}>
                            {following ? 'Following' : 'Follow'}
                        </Button>
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
                <div className='file-viewer-scroll-wrap'>
                    {this.renderNotice()}
                    <div
                        className='file-viewer-scroll'
                        tabIndex={0}
                        ref={el => { this.scroller = el; }}
                        onScroll={() => this.onScroll()}
                        aria-label={`Content of ${this.props.data.name}`}
                        aria-busy={!!loading}
                    >
                        {empty ? (
                            <div className='text-muted file-viewer-empty'>
                                The file is empty.{following ? ' Waiting for new lines…' : ''}
                            </div>
                        ) : (
                            <React.Fragment>
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
                                    {range.eof ? <span className='file-viewer-eof'>{following ? 'End of file · following' : 'End of file'}</span>
                                        : loading === 'next' ? <span className='file-viewer-loading-more'>Loading more…</span>
                                        : 'Scroll down for more'}
                                </div>
                            </React.Fragment>
                        )}
                    </div>
                    {this.renderPausedBar()}
                </div>
                <div className='file-viewer-hint text-muted'>
                    PgUp/PgDn, ↑/↓, Space: scroll · Home/End: top/bottom of the loaded part ·
                    Ctrl+Home/Ctrl+End or the buttons: start/end of the file · F: follow
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
