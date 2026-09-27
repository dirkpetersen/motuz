import React, { Suspense } from 'react';
import { Modal, Button } from 'react-bootstrap';

import 'documentViewer.css';
import formatBytes from 'utils/formatBytes.jsx';
import { DOCUMENT_LABELS, documentKind } from 'utils/documentKinds.js';
import { fetchDocument } from 'actions/documentActions.jsx';
import { runDocumentWorker } from 'views/Dialogs/DocumentViewer/documentWorkerClient.js';

// Every renderer is its own chunk, loaded on the first document of its kind
const PdfView = React.lazy(() => import(/* webpackChunkName: "viewer-pdf" */ 'views/Dialogs/DocumentViewer/PdfView.jsx'));
const DocxView = React.lazy(() => import(/* webpackChunkName: "viewer-docx" */ 'views/Dialogs/DocumentViewer/DocxView.jsx'));
const SheetView = React.lazy(() => import(/* webpackChunkName: "viewer-sheet" */ 'views/Dialogs/DocumentViewer/SheetView.jsx'));
const PptxView = React.lazy(() => import(/* webpackChunkName: "viewer-pptx" */ 'views/Dialogs/DocumentViewer/PptxView.jsx'));

class ViewerBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = {error: null};
    }

    static getDerivedStateFromError(error) {
        return {error};
    }

    render() {
        if (this.state.error) {
            return (
                <div className='alert alert-warning file-viewer-error document-viewer-error' role='alert'>
                    The viewer could not be loaded ({String(this.state.error.message || this.state.error)}).
                    Reload the page and try again.
                </div>
            );
        }
        return this.props.children;
    }
}

/**
 * Read-only previews of PDF, Word (DOCX), spreadsheet (XLSX, XLSM, XLSB, XLS, ODS)
 * and PowerPoint (PPTX) files, opened by a double-click on such a file in a pane
 * (utils/documentKinds.js). Everything is rendered in the browser; the file is read
 * as the user by POST /api/system/files/view/document/ and never sent anywhere else.
 * PDFs are read in ranges by pdf.js (PdfView); the other formats are read whole (at
 * most MOTUZ_VIEW_DOCUMENT_MAX_BYTES) and parsed in a Web Worker with ZIP limits
 * (workers/documentWorker.js) before a renderer sees them. Legacy binary formats
 * (.doc, .ppt) get a message instead.
 */
class DocumentViewerDialog extends React.Component {
    constructor(props) {
        super(props);
        const kind = documentKind(props.data.name);
        this.state = {
            kind,
            phase: kind === 'unsupported' ? 'unsupported' : (kind === 'pdf' ? 'ready' : 'reading'),
            error: null,
            status: null,
            result: null,
            info: {},
        };
        this.keys = null;
        this.unmounted = false;
        this.abort = typeof AbortController !== 'undefined' ? new AbortController() : null;
    }

    componentDidMount() {
        if (this.state.phase === 'reading') {
            this.load();
        }
    }

    componentWillUnmount() {
        this.unmounted = true;
        if (this.abort) this.abort.abort();
    }

    fetch(params = {}) {
        const {connectionId, path} = this.props.data;
        return fetchDocument(this.props.dispatch, {connection_id: connectionId, path, ...params});
    }

    async load() {
        const {kind} = this.state;
        let file;
        try {
            file = await this.fetch();
        } catch (e) {
            if (!this.unmounted) this.setState({phase: 'error', error: e.message, status: e.status});
            return;
        }
        if (this.unmounted) return;
        if (file.container === 'pdf') {
            this.setState({phase: 'error', error: 'This file is a PDF, not the document its name says', status: 415});
            return;
        }
        this.setState({phase: 'opening', info: {size: file.size}});
        let result;
        try {
            result = await runDocumentWorker(kind, file.buffer, file.container, {signal: this.abort && this.abort.signal});
        } catch (e) {
            if (!this.unmounted) this.setState({phase: 'error', error: e.message, status: null});
            return;
        }
        if (!this.unmounted) this.setState({phase: 'ready', result});
    }

    onInfo(info) {
        if (!this.unmounted) this.setState(state => ({info: {...state.info, ...info}}));
    }

    onEscapeKeyDown(event) {
        if (this.keys && this.keys.escape && this.keys.escape()) {
            event.preventDefault(); // handled by the viewer (e.g. closes its find bar)
        }
    }

    onKeyDown(event) {
        if (this.keys && this.keys.keydown) this.keys.keydown(event);
    }

    renderViewer() {
        const {kind, phase, result} = this.state;
        const host = this.props.data.host;
        const common = {onInfo: info => this.onInfo(info)};
        if (kind === 'pdf') {
            return <PdfView {...common} cloud={!!(host && host.id)} fetchDocument={params => this.fetch(params)}
                            registerKeys={keys => { this.keys = keys; }} />;
        }
        if (phase !== 'ready' || !result) return null;
        if (kind === 'docx') return <DocxView {...common} zip={result.zip} />;
        if (kind === 'sheet') return <SheetView {...common} sheets={result.sheets} sheetCount={result.sheetCount} />;
        if (kind === 'pptx') return <PptxView {...common} slides={result.slides} pictures={result.pictures} />;
        return null;
    }

    renderInfo() {
        const {kind, info} = this.state;
        const parts = [DOCUMENT_LABELS[kind] || 'Document'];
        if (info.pages) parts.push(`${info.pages} ${info.pages === 1 ? 'page' : 'pages'}`);
        if (info.sheets) parts.push(`${info.sheets} ${info.sheets === 1 ? 'sheet' : 'sheets'}`);
        if (info.slides) parts.push(`${info.slides} ${info.slides === 1 ? 'slide' : 'slides'}`);
        if (info.size != null) parts.push(formatBytes(info.size, this.props.useSiUnits));
        parts.push('read-only');
        return parts.join(' · ');
    }

    render() {
        const {name, path, host} = this.props.data;
        const hostName = host && host.id ? host.name : null;
        const {phase, error, status} = this.state;
        return (
            <Modal
                show={true}
                size="xl"
                animation={this.props.animation}
                onHide={() => this.props.onClose()}
                onEscapeKeyDown={e => this.onEscapeKeyDown(e)}
                dialogClassName='file-viewer-dialog document-viewer-dialog'
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
                <Modal.Body className='file-viewer-body document-viewer-body' onKeyDown={e => this.onKeyDown(e)}>
                    {phase === 'unsupported' && (
                        <div className='alert alert-info document-viewer-unsupported' role='status'>
                            <div>No preview for this file type.</div>
                            <div className='small mt-1'>
                                Old binary Office files (.doc, .ppt) and similar formats cannot be shown in the
                                browser. PDF, DOCX, XLSX/XLS/ODS and PPTX files can.
                            </div>
                        </div>
                    )}
                    {phase === 'reading' && <div className='file-viewer-loading text-muted'>Reading the file...</div>}
                    {phase === 'opening' && <div className='file-viewer-loading text-muted'>Opening the document...</div>}
                    {phase === 'error' && (
                        <div className='alert alert-warning file-viewer-error document-viewer-error' role='alert'>
                            <div>{error}</div>
                            {status === 413 && (
                                <div className='small mt-1'>Copy the file somewhere to open it with another program.</div>
                            )}
                        </div>
                    )}
                    <ViewerBoundary>
                        <Suspense fallback={<div className='file-viewer-loading text-muted'>Loading the viewer...</div>}>
                            {this.renderViewer()}
                        </Suspense>
                    </ViewerBoundary>
                </Modal.Body>
                <Modal.Footer>
                    {phase !== 'unsupported' && (
                        <span className='file-viewer-info document-viewer-info text-muted me-auto'>{this.renderInfo()}</span>
                    )}
                    <Button variant="secondary" onClick={() => this.props.onClose()}>
                        Close
                    </Button>
                </Modal.Footer>
            </Modal>
        );
    }
}

DocumentViewerDialog.defaultProps = {
    data: {},
    animation: true,
    useSiUnits: false,
    onClose: () => {},
    dispatch: () => undefined,
};

import {connect} from 'react-redux';
import {hideFileViewerDialog} from 'actions/dialogActions.jsx'

const mapStateToProps = state => ({
    data: state.dialog.fileViewerDialogData,
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideFileViewerDialog()),
    dispatch,
});

export default connect(mapStateToProps, mapDispatchToProps)(DocumentViewerDialog);
