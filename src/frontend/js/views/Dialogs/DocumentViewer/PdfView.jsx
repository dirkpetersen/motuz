import React from 'react';
import { Button, Form } from 'react-bootstrap';
// The legacy build runs in every browser of the app's browserslist ("defaults"); the
// modern one needs features only the newest browsers have
import {
    AnnotationMode,
    PDFDataRangeTransport,
    PDFWorker,
    PasswordException,
    TextLayer,
    getDocument,
} from 'pdfjs-dist/legacy/build/pdf.min.mjs';

import { SAFE_LINK_REL, safeLinkUrl } from 'utils/safeLinks.js';
import {
    CLOUD_CHUNK_BYTES, INITIAL_BYTES, LOCAL_CHUNK_BYTES, RANGE_MAX_BYTES, joinPieces, limiter, splitRange,
} from 'utils/pdfRanges.js';

// Served from this build by webpack.common.js (PdfjsAssetsPlugin), never from a CDN
/* global PDFJS_ASSET_BASE */
const ASSET_BASE = typeof PDFJS_ASSET_BASE === 'string' ? PDFJS_ASSET_BASE : '/js/pdfjs/';

const CSS_UNITS = 96 / 72; // 100% = the page's size in CSS pixels
const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4];
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 5;
const MAX_CANVAS_PIXELS = 4096 * 4096; // the device-pixel ratio is lowered beyond this
const OPEN_TIMEOUT_MS = 60000;
const PAGE_MARGIN = 16;

// pdf.js link annotation subtype
const LINK = 'Link';

/**
 * PDF preview with pdf.js: one page at a time on a canvas, with a text layer (select
 * and find text) and link overlays. The file is read in ranges through
 * POST /api/system/files/view/document/ (pdf.js range loading, disableAutoFetch), so
 * page 1 of a large PDF opens after a few small reads. Every document gets its own
 * pdf.js worker, terminated when the dialog closes.
 *
 * Safety: PDF JavaScript is never run (the scripting sandbox is not loaded), XFA forms
 * are off, pdf.js 6 has no eval() code path, forms are only drawn (no inputs), and
 * link annotations become links only for http, https and mailto (utils/safeLinks.js),
 * opened in a new tab without opener or referrer; other actions (launch, JavaScript,
 * file links) are ignored. Nothing is loaded from other sites: fonts, character maps
 * and decoders come from this server.
 */
export default class PdfView extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            status: 'loading',
            error: null,
            numPages: 0,
            page: 1,
            pageInput: '1',
            zoom: 'width', // 'width', 'page' or a factor (1 = 100%)
            scale: null,
            rendering: false,
            links: [],
            findOpen: false,
            query: '',
            searching: false,
            matches: null, // [{page, start, length}]
            matchIndex: -1,
            progress: null,
        };
        this.doc = null;
        this.loadingTask = null;
        this.worker = null;
        this.pdfWorker = null;
        this.renderTask = null;
        this.textLayer = null;
        this.renderSeq = 0;
        this.searchSeq = 0;
        this.pageTexts = new Map(); // page -> {text, starts}
        this.unmounted = false;
        this.stage = null;
        this.canvas = null;
        this.textDiv = null;
        this.findInput = null;
        this.onResize = () => this.scheduleRender();
    }

    componentDidMount() {
        this.open();
        window.addEventListener('resize', this.onResize);
        if (this.props.registerKeys) {
            this.props.registerKeys({
                escape: () => this.onEscape(),
                keydown: (event) => this.onKeyDown(event),
            });
        }
    }

    componentWillUnmount() {
        this.unmounted = true;
        window.removeEventListener('resize', this.onResize);
        clearTimeout(this.resizeTimer);
        this.searchSeq++;
        this.cancelRender();
        if (this.loadingTask) {
            this.loadingTask.destroy().catch(() => {});
        }
        if (this.pdfWorker) {
            this.pdfWorker.destroy();
        }
        if (this.worker) {
            this.worker.terminate();
        }
        if (this.transport) {
            this.transport.aborted = true;
        }
    }

    // ---- loading ----

    async readRange(begin, end) {
        const pieces = await Promise.all(splitRange(begin, end, RANGE_MAX_BYTES).map(([from, to]) => this.limit(async () => {
            const result = await this.props.fetchDocument({offset: from, length: to - from});
            if (result.container !== 'pdf') {
                throw new Error('The file is not a PDF');
            }
            if (this.size != null && result.size !== this.size) {
                throw new Error('The file changed while it was being read; close and open it again');
            }
            if (result.start !== from || (result.buffer.byteLength !== to - from)) {
                throw new Error('The file changed while it was being read; close and open it again');
            }
            return result.buffer;
        })));
        return joinPieces(pieces);
    }

    async open() {
        const cloud = !!this.props.cloud;
        this.limit = limiter(3);
        let first;
        try {
            first = await this.props.fetchDocument({offset: 0, length: INITIAL_BYTES});
        } catch (e) {
            this.fail(e.message, e.status);
            return;
        }
        if (this.unmounted) return;
        if (first.container !== 'pdf') {
            this.fail('This file is not a PDF (it does not start with %PDF-)', 415);
            return;
        }
        this.size = first.size;
        const initial = new Uint8Array(first.buffer);
        if (this.props.onInfo) this.props.onInfo({size: this.size});

        const view = this;
        class RangeTransport extends PDFDataRangeTransport {
            requestDataRange(begin, end) {
                view.readRange(begin, end).then((chunk) => {
                    if (!this.aborted) this.onDataRange(begin, chunk);
                }).catch((e) => {
                    if (!this.aborted && !view.unmounted) {
                        view.fail(`Part of the file could not be read: ${e.message}`, e.status);
                    }
                });
            }
        }

        this.worker = new Worker(new URL('pdfjs-dist/legacy/build/pdf.worker.min.mjs', import.meta.url), {type: 'module'});
        this.pdfWorker = new PDFWorker({port: this.worker});
        const params = {
            worker: this.pdfWorker,
            enableXfa: false,
            isEvalSupported: false, // no longer used by pdf.js 6, which has no eval path
            stopAtErrors: false,
            maxImageSize: 64 * 1024 * 1024, // pixels per image
            cMapUrl: `${ASSET_BASE}cmaps/`,
            cMapPacked: true,
            standardFontDataUrl: `${ASSET_BASE}standard_fonts/`,
            wasmUrl: `${ASSET_BASE}wasm/`,
            disableAutoFetch: true,
            disableStream: true,
            verbosity: 0,
        };
        if (initial.length >= this.size) {
            params.data = initial;
        } else {
            this.transport = new RangeTransport(this.size, initial);
            params.range = this.transport;
            params.rangeChunkSize = cloud ? CLOUD_CHUNK_BYTES : LOCAL_CHUNK_BYTES;
        }
        this.loadingTask = getDocument(params);
        this.loadingTask.onPassword = () => {
            this.fail('This PDF is password protected; there is no preview for it', 415);
            this.loadingTask.destroy().catch(() => {});
        };
        const timeout = setTimeout(() => {
            if (this.state.status === 'loading') {
                this.fail('Opening the PDF took too long; it may be damaged or too complex to preview');
                this.loadingTask.destroy().catch(() => {});
            }
        }, OPEN_TIMEOUT_MS);
        try {
            this.doc = await this.loadingTask.promise;
        } catch (e) {
            clearTimeout(timeout);
            if (!this.unmounted && this.state.status === 'loading') {
                this.fail(e instanceof PasswordException
                    ? 'This PDF is password protected; there is no preview for it'
                    : `The PDF could not be opened: ${e && e.message ? e.message : e}`);
            }
            return;
        }
        clearTimeout(timeout);
        if (this.unmounted) return;
        this.setState({status: 'ready', numPages: this.doc.numPages}, () => this.renderPage());
        if (this.props.onInfo) this.props.onInfo({size: this.size, pages: this.doc.numPages});
    }

    fail(error, status = null) {
        if (this.unmounted) return;
        this.cancelRender();
        this.setState({status: 'error', error, errorStatus: status});
    }

    // ---- rendering ----

    cancelRender() {
        this.renderSeq++;
        if (this.renderTask) {
            this.renderTask.cancel();
            this.renderTask = null;
        }
        if (this.textLayer) {
            this.textLayer.cancel();
            this.textLayer = null;
        }
    }

    scheduleRender() {
        clearTimeout(this.resizeTimer);
        this.resizeTimer = setTimeout(() => {
            if (this.state.status === 'ready' && typeof this.state.zoom === 'string') {
                this.renderPage();
            }
        }, 150);
    }

    computeScale(page) {
        const {zoom} = this.state;
        if (typeof zoom === 'number') {
            return zoom * CSS_UNITS;
        }
        const base = page.getViewport({scale: 1});
        const width = Math.max(100, (this.stage ? this.stage.clientWidth : 800) - 2 * PAGE_MARGIN);
        const height = Math.max(100, (this.stage ? this.stage.clientHeight : 600) - 2 * PAGE_MARGIN);
        const fitWidth = width / base.width;
        return zoom === 'page' ? Math.min(fitWidth, height / base.height) : fitWidth;
    }

    async renderPage() {
        if (!this.doc || !this.canvas) return;
        this.cancelRender();
        const seq = this.renderSeq;
        const number = this.state.page;
        this.setState({rendering: true});
        let page;
        try {
            page = await this.doc.getPage(number);
        } catch (e) {
            if (seq === this.renderSeq) this.setState({rendering: false, pageError: `Page ${number} could not be read`});
            return;
        }
        if (seq !== this.renderSeq || this.unmounted) return;

        const scale = this.computeScale(page);
        const viewport = page.getViewport({scale});
        let outputScale = window.devicePixelRatio || 1;
        const pixels = viewport.width * viewport.height * outputScale * outputScale;
        if (pixels > MAX_CANVAS_PIXELS) {
            outputScale = Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height));
        }
        const canvas = this.canvas;
        canvas.width = Math.max(1, Math.floor(viewport.width * outputScale));
        canvas.height = Math.max(1, Math.floor(viewport.height * outputScale));
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        const pageDiv = canvas.parentNode;
        pageDiv.style.width = `${Math.floor(viewport.width)}px`;
        pageDiv.style.height = `${Math.floor(viewport.height)}px`;
        pageDiv.style.setProperty('--scale-factor', String(viewport.scale));
        pageDiv.style.setProperty('--user-unit', String(page.userUnit || 1));
        pageDiv.style.setProperty('--total-scale-factor', String(viewport.scale * (page.userUnit || 1)));
        this.textDiv.replaceChildren();

        this.renderTask = page.render({
            canvasContext: canvas.getContext('2d'),
            viewport,
            transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null,
            annotationMode: AnnotationMode.ENABLE, // appearances drawn, no form fields
        });
        try {
            await this.renderTask.promise;
        } catch (e) {
            if (seq === this.renderSeq && !(e && e.name === 'RenderingCancelledException')) {
                this.setState({rendering: false, pageError: `Page ${number} could not be drawn: ${e && e.message}`});
            }
            return;
        }
        if (seq !== this.renderSeq || this.unmounted) return;
        this.renderTask = null;
        this.setState({rendering: false, scale, pageError: null});
        if (this.props.onPageRendered) this.props.onPageRendered(number);

        // Text layer (selection, find) and links; failures only lose those
        try {
            const textContent = await page.getTextContent();
            if (seq !== this.renderSeq) return;
            this.textLayer = new TextLayer({textContentSource: textContent, container: this.textDiv, viewport});
            await this.textLayer.render();
            if (seq !== this.renderSeq) return;
            this.highlightMatches();
        } catch (e) {
            // no text layer
        }
        try {
            const annotations = await page.getAnnotations({intent: 'display'});
            if (seq !== this.renderSeq) return;
            this.setState({links: this.linksOf(annotations, viewport)});
        } catch (e) {
            this.setState({links: []});
        }
    }

    linksOf(annotations, viewport) {
        const links = [];
        for (const annotation of annotations) {
            if (annotation.subtype !== LINK || !Array.isArray(annotation.rect)) continue;
            const [x1, y1, x2, y2] = viewport.convertToViewportRectangle(annotation.rect);
            const box = {
                left: Math.min(x1, x2),
                top: Math.min(y1, y2),
                width: Math.abs(x2 - x1),
                height: Math.abs(y2 - y1),
            };
            if (annotation.url) {
                const href = safeLinkUrl(annotation.url);
                if (href) links.push({...box, href, key: `u${links.length}`});
            } else if (annotation.dest) {
                links.push({...box, dest: annotation.dest, key: `d${links.length}`});
            }
            // Everything else (JavaScript, launch, remote-file actions) is not a link
        }
        return links;
    }

    async goToDest(dest) {
        try {
            const explicit = typeof dest === 'string' ? await this.doc.getDestination(dest) : dest;
            if (!Array.isArray(explicit)) return;
            const ref = explicit[0];
            const index = typeof ref === 'object' && ref !== null ? await this.doc.getPageIndex(ref)
                : (Number.isInteger(ref) ? ref : null);
            if (index !== null) this.goToPage(index + 1);
        } catch (e) {
            // a broken destination: stay
        }
    }

    // ---- navigation and zoom ----

    goToPage(number) {
        const page = Math.max(1, Math.min(this.state.numPages || 1, number));
        this.setState({page, pageInput: String(page), links: []}, () => {
            if (this.stage) this.stage.scrollTop = 0;
            this.renderPage();
        });
    }

    setZoom(zoom) {
        if (typeof zoom === 'number') {
            zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
        }
        this.setState({zoom}, () => this.renderPage());
    }

    stepZoom(direction) {
        const current = typeof this.state.zoom === 'number' ? this.state.zoom : (this.state.scale || CSS_UNITS) / CSS_UNITS;
        const next = direction > 0
            ? ZOOM_STEPS.find(step => step > current + 0.001) || MAX_ZOOM
            : [...ZOOM_STEPS].reverse().find(step => step < current - 0.001) || MIN_ZOOM;
        this.setZoom(next);
    }

    onPageInput(event) {
        this.setState({pageInput: event.target.value});
    }

    onPageSubmit(event) {
        event.preventDefault();
        const number = parseInt(this.state.pageInput, 10);
        if (Number.isFinite(number)) {
            this.goToPage(number);
        } else {
            this.setState({pageInput: String(this.state.page)});
        }
    }

    onKeyDown(event) {
        const inInput = event.target && /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName);
        if ((event.ctrlKey || event.metaKey) && (event.key === 'f' || event.key === 'F')) {
            event.preventDefault();
            this.setState({findOpen: true}, () => this.findInput && this.findInput.select());
            return;
        }
        if (inInput || event.altKey || this.state.status !== 'ready') return;
        const stage = this.stage;
        const atBottom = !stage || stage.scrollTop + stage.clientHeight >= stage.scrollHeight - 2;
        const atTop = !stage || stage.scrollTop <= 1;
        const {page, numPages} = this.state;
        if (event.key === 'ArrowRight' || event.key === 'n' || ((event.key === 'PageDown' || event.key === ' ') && atBottom)) {
            if (page < numPages) {
                event.preventDefault();
                this.goToPage(page + 1);
            }
        } else if (event.key === 'ArrowLeft' || event.key === 'p' || ((event.key === 'PageUp') && atTop)) {
            if (page > 1) {
                event.preventDefault();
                this.goToPage(page - 1);
            }
        } else if (event.key === 'Home' && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            this.goToPage(1);
        } else if (event.key === 'End' && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            this.goToPage(numPages);
        } else if (event.key === '+' || event.key === '=') {
            event.preventDefault();
            this.stepZoom(1);
        } else if (event.key === '-') {
            event.preventDefault();
            this.stepZoom(-1);
        }
    }

    onEscape() {
        if (this.state.findOpen) {
            this.closeFind();
            return true; // handled: the dialog stays open
        }
        return false;
    }

    // ---- find ----

    async pageText(number) {
        if (this.pageTexts.has(number)) return this.pageTexts.get(number);
        const page = await this.doc.getPage(number);
        const content = await page.getTextContent();
        let text = '';
        const starts = [];
        for (const item of content.items) {
            starts.push(text.length);
            text += (item.str || '') + (item.hasEOL ? '\n' : '');
        }
        const entry = {text: text.toLowerCase(), starts};
        this.pageTexts.set(number, entry);
        return entry;
    }

    async search(direction = 1) {
        const query = this.state.query.trim().toLowerCase();
        if (!query || !this.doc) return;
        if (this.state.matches && this.state.matchQuery === query && this.state.matches.length) {
            const count = this.state.matches.length;
            this.showMatch((this.state.matchIndex + direction + count) % count);
            return;
        }
        const seq = ++this.searchSeq;
        const matches = [];
        this.setState({searching: true, matches: null, matchIndex: -1, matchQuery: query});
        const {numPages} = this.state;
        for (let number = 1; number <= numPages; number++) {
            let entry;
            try {
                entry = await this.pageText(number);
            } catch (e) {
                continue;
            }
            if (seq !== this.searchSeq || this.unmounted) return;
            for (let at = entry.text.indexOf(query); at >= 0; at = entry.text.indexOf(query, at + query.length)) {
                matches.push({page: number, start: at, length: query.length});
            }
            if (number % 10 === 0) this.setState({progress: `Searching page ${number} of ${numPages}...`});
        }
        const current = this.state.page;
        let first = matches.findIndex(m => m.page >= current);
        if (first < 0) first = 0;
        this.setState({searching: false, progress: null, matches}, () => {
            if (matches.length) this.showMatch(first);
        });
    }

    showMatch(index) {
        const match = this.state.matches[index];
        this.setState({matchIndex: index}, () => {
            if (match.page !== this.state.page) {
                this.goToPage(match.page);
            } else {
                this.highlightMatches();
            }
        });
    }

    highlightMatches() {
        if (!this.textLayer) return;
        const divs = this.textLayer.textDivs || [];
        for (const div of divs) div.classList.remove('highlight', 'selected');
        const {matches, matchIndex, page} = this.state;
        if (!matches || !matches.length) return;
        const entry = this.pageTexts.get(page);
        if (!entry) return;
        let selectedDiv = null;
        matches.forEach((match, index) => {
            if (match.page !== page) return;
            const end = match.start + match.length;
            entry.starts.forEach((start, i) => {
                const itemEnd = i + 1 < entry.starts.length ? entry.starts[i + 1] : entry.text.length;
                if (start < end && itemEnd > match.start && divs[i]) {
                    divs[i].classList.add('highlight');
                    if (index === matchIndex) {
                        divs[i].classList.add('selected');
                        selectedDiv = selectedDiv || divs[i];
                    }
                }
            });
        });
        if (selectedDiv && this.stage) {
            const stageBox = this.stage.getBoundingClientRect();
            const box = selectedDiv.getBoundingClientRect();
            if (box.top < stageBox.top || box.bottom > stageBox.bottom) {
                this.stage.scrollTop += box.top - stageBox.top - stageBox.height / 3;
            }
        }
    }

    closeFind() {
        this.searchSeq++;
        this.setState({findOpen: false, matches: null, matchIndex: -1, searching: false, progress: null},
            () => this.highlightMatches());
        if (this.stage) this.stage.focus({preventScroll: true});
    }

    // ---- view ----

    render() {
        const {status, error, errorStatus, numPages, page, pageInput, zoom, rendering, links, pageError} = this.state;
        if (status === 'error') {
            return (
                <div className='alert alert-warning file-viewer-error document-viewer-error' role='alert'>
                    <div>{error}</div>
                    {errorStatus === 413 && <div className='small mt-1'>Copy the file somewhere to open it with another program.</div>}
                </div>
            );
        }
        const ready = status === 'ready';
        const zoomValue = typeof zoom === 'number' ? String(zoom) : zoom;
        return (
            <div className='document-viewer pdf-viewer'>
                <div className='file-viewer-toolbar document-viewer-toolbar'>
                    <span className='pdf-viewer-nav'>
                        <Button size='sm' variant='outline-secondary' disabled={!ready || page <= 1}
                                title='Previous page (←, PageUp at the top)' aria-label='Previous page'
                                onClick={() => this.goToPage(page - 1)}>‹</Button>
                        <form className='pdf-viewer-page-form' onSubmit={e => this.onPageSubmit(e)}>
                            <Form.Control size='sm' className='pdf-viewer-page-input' aria-label='Page number'
                                          value={pageInput} disabled={!ready}
                                          onChange={e => this.onPageInput(e)}
                                          onBlur={e => this.onPageSubmit(e)} />
                            <span className='pdf-viewer-page-count text-muted'>/ {numPages || '…'}</span>
                        </form>
                        <Button size='sm' variant='outline-secondary' disabled={!ready || page >= numPages}
                                title='Next page (→, PageDown at the bottom)' aria-label='Next page'
                                onClick={() => this.goToPage(page + 1)}>›</Button>
                    </span>
                    <span className='pdf-viewer-zoom'>
                        <Button size='sm' variant='outline-secondary' disabled={!ready} aria-label='Zoom out'
                                title='Zoom out (-)' onClick={() => this.stepZoom(-1)}>−</Button>
                        <Form.Select size='sm' aria-label='Zoom' value={ZOOM_STEPS.map(String).includes(zoomValue) || typeof zoom === 'string' ? zoomValue : 'custom'}
                                     disabled={!ready}
                                     onChange={e => this.setZoom(e.target.value === 'width' || e.target.value === 'page' ? e.target.value : parseFloat(e.target.value))}>
                            <option value='width'>Fit width</option>
                            <option value='page'>Fit page</option>
                            {typeof zoom === 'number' && !ZOOM_STEPS.includes(zoom) && (
                                <option value='custom' disabled>{Math.round(zoom * 100)}%</option>
                            )}
                            {ZOOM_STEPS.map(step => <option key={step} value={String(step)}>{Math.round(step * 100)}%</option>)}
                        </Form.Select>
                        <Button size='sm' variant='outline-secondary' disabled={!ready} aria-label='Zoom in'
                                title='Zoom in (+)' onClick={() => this.stepZoom(1)}>+</Button>
                        <Button size='sm' variant='outline-secondary' disabled={!ready} title='Find text (Ctrl+F)'
                                onClick={() => this.setState({findOpen: true}, () => this.findInput && this.findInput.focus())}>
                            Find
                        </Button>
                    </span>
                </div>
                {this.state.findOpen && this.renderFind()}
                {status === 'loading' && <div className='file-viewer-loading text-muted'>Opening the PDF...</div>}
                {pageError && <div className='alert alert-warning py-1 px-2 mb-0 small'>{pageError}</div>}
                <div className='pdf-viewer-stage' tabIndex={0} ref={el => { this.stage = el; }}
                     aria-label={`Page ${page} of ${numPages}`} aria-busy={rendering || status === 'loading'}>
                    <div className='pdf-viewer-page' data-page={page} hidden={!ready}>
                        <canvas className='pdf-viewer-canvas' ref={el => { this.canvas = el; }} />
                        <div className='textLayer' ref={el => { this.textDiv = el; }} />
                        <div className='pdf-viewer-links'>
                            {links.map(link => link.href ? (
                                <a key={link.key} className='pdf-viewer-link' href={link.href} target='_blank'
                                   rel={SAFE_LINK_REL} title={link.href}
                                   style={{left: link.left, top: link.top, width: link.width, height: link.height}} />
                            ) : (
                                <a key={link.key} className='pdf-viewer-link internal' href='#' title='Go to the linked page'
                                   onClick={e => { e.preventDefault(); this.goToDest(link.dest); }}
                                   style={{left: link.left, top: link.top, width: link.width, height: link.height}} />
                            ))}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    renderFind() {
        const {query, searching, matches, matchIndex, progress} = this.state;
        let status = '';
        if (searching) status = progress || 'Searching...';
        else if (matches) status = matches.length ? `${matchIndex + 1} of ${matches.length}` : 'No matches';
        return (
            <form className='document-viewer-find' role='search'
                  onSubmit={e => { e.preventDefault(); this.search(1); }}>
                <Form.Control size='sm' type='search' placeholder='Find in document' aria-label='Find in document'
                              ref={el => { this.findInput = el; }} value={query} autoFocus
                              onKeyDown={e => {
                                  if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); this.search(-1); }
                              }}
                              onChange={e => { this.searchSeq++; this.setState({query: e.target.value, matches: null, matchIndex: -1, searching: false}); }} />
                <Button size='sm' variant='outline-secondary' type='button' disabled={!query.trim()}
                        title='Previous match (Shift+Enter)' onClick={() => this.search(-1)}>↑</Button>
                <Button size='sm' variant='outline-secondary' type='submit' disabled={!query.trim()}
                        title='Next match (Enter)'>↓</Button>
                <span className='document-viewer-find-status text-muted' role='status'>{status}</span>
                <Button size='sm' variant='link' type='button' onClick={() => this.closeFind()}>Close</Button>
            </form>
        );
    }
}

PdfView.defaultProps = {
    cloud: false,
    fetchDocument: async () => { throw new Error('not connected'); },
    onInfo: null,
    registerKeys: null,
};
