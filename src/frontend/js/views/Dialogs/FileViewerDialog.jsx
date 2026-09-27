import React from 'react';
import { Modal, Button } from 'react-bootstrap'

import formatBytes from 'utils/formatBytes.jsx'

/**
 * Read-only viewer for text files (double-click on a file in a pane). The content is
 * rendered as text by React (escaped), never as HTML.
 */
class FileViewerDialog extends React.Component {
    render() {
        const {name, path, host, loading, result, error, status} = this.props.data;
        const hostName = host && host.id ? host.name : null;

        return (
            <Modal
                show={true}
                size="xl"
                scrollable={true}
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
                    {loading && <div className='file-viewer-loading text-muted'>Loading...</div>}
                    {!loading && error && this.renderError(error, status)}
                    {!loading && !error && result && this.renderContent(result)}
                </Modal.Body>
                <Modal.Footer>
                    {!loading && !error && result && (
                        <span className='file-viewer-info text-muted me-auto'>
                            {formatBytes(result.size, this.props.useSiUnits)}
                            {result.encoding && ` · ${result.encoding}`}
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
            <div className='alert alert-warning file-viewer-error mb-0' role='alert'>
                <div>{error}</div>
                {status === 415 && (
                    <div className='small mt-1'>
                        The viewer shows text files only. Copy the file somewhere to open it with another program.
                    </div>
                )}
            </div>
        );
    }

    renderContent(result) {
        const truncated = result.truncated ? (
            <div className='alert alert-info py-2 file-viewer-truncated' role='status'>
                Showing the first {formatBytes(1024 * 1024, false)} of {formatBytes(result.size, this.props.useSiUnits)}.
            </div>
        ) : null;

        return (
            <React.Fragment>
                {truncated}
                {result.content === ''
                    ? <div className='text-muted file-viewer-empty'>The file is empty.</div>
                    : <pre className='file-viewer-content' tabIndex={0}>{result.content}</pre>}
            </React.Fragment>
        );
    }
}

FileViewerDialog.defaultProps = {
    data: {},
    useSiUnits: false,
    onClose: () => {},
}

import {connect} from 'react-redux';
import {hideFileViewerDialog} from 'actions/dialogActions.jsx'

const mapStateToProps = state => ({
    data: state.dialog.fileViewerDialogData,
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideFileViewerDialog()),
});

export default connect(mapStateToProps, mapDispatchToProps)(FileViewerDialog);
