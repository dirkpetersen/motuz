import React from 'react';
import { Modal, Button } from 'react-bootstrap';

import { hideFileViewerDialog } from 'actions/dialogActions.jsx';

import NewCopyJobDialog from 'views/Dialogs/NewCopyJobDialog.jsx';
import EditCopyJobDialog from 'views/Dialogs/EditCopyJobDialog.jsx';
import NewHashsumJobDialog from 'views/Dialogs/NewHashsumJobDialog.jsx';
import EditHashsumJobDialog from 'views/Dialogs/EditHashsumJobDialog.jsx';
import NewCloudConnectionDialog from 'views/Dialogs/CloudConnection/NewCloudConnectionDialog.jsx';
import EditCloudConnectionDialog from 'views/Dialogs/CloudConnection/EditCloudConnectionDialog.jsx';
import MkdirDialog from 'views/Dialogs/MkdirDialog.jsx';
import SettingsDialog from 'views/Dialogs/SettingsDialog.jsx';
import FileViewerDialog from 'views/Dialogs/FileViewerDialog.jsx';
import ImageViewerDialog from 'views/Dialogs/ImageViewerDialog.jsx';
import MarkdownViewerDialog from 'views/Dialogs/MarkdownViewerDialog.jsx';

// PDF, Word, spreadsheet and PowerPoint previews: the dialog and each renderer are
// loaded on the first double-click on such a file, never with the main bundle
const DocumentViewerDialog = React.lazy(() => import(
    /* webpackChunkName: "viewer-document" */ 'views/Dialogs/DocumentViewer/DocumentViewerDialog.jsx'
).catch(() => ({default: ViewerLoadFailed})));

// Shown when the viewer's chunk cannot be loaded (e.g. the app was updated meanwhile)
const ViewerLoadFailed = connect(null, dispatch => ({onClose: () => dispatch(hideFileViewerDialog())}))(
    ({onClose}) => (
        <Modal show={true} onHide={onClose}>
            <Modal.Header closeButton><Modal.Title>Viewer not available</Modal.Title></Modal.Header>
            <Modal.Body>The viewer could not be loaded. Reload the page and try again.</Modal.Body>
            <Modal.Footer><Button variant='secondary' onClick={onClose}>Close</Button></Modal.Footer>
        </Modal>
    ));

class Dialogs extends React.PureComponent {
    constructor(props) {
        super(props);
    }

    /**
     * The viewer for the file's kind (utils/viewerKind.js); a Markdown file switches
     * between the rendered view and the pager (Source) without the modal's animation
     */
    renderFileViewer() {
        const data = this.props.dialogs.fileViewerDialogData;
        const key = `${data.requestId}-${data.mode || ''}`;
        const animation = !data.switched;
        if (data.kind === 'image') {
            return <ImageViewerDialog key={key} animation={animation} />;
        }
        if (data.kind === 'markdown' && data.mode !== 'source') {
            return <MarkdownViewerDialog key={key} animation={animation} />;
        }
        if (data.kind === 'document') {
            return (
                <React.Suspense key={key} fallback={null}>
                    <DocumentViewerDialog animation={animation} />
                </React.Suspense>
            );
        }
        return <FileViewerDialog key={key} animation={animation} />;
    }

    render() {
        return (
            <React.Fragment>
                {this.props.dialogs.displayNewCopyJobDialog && <NewCopyJobDialog />}
                {this.props.dialogs.displayEditCopyJobDialog && <EditCopyJobDialog />}
                {this.props.dialogs.displayNewHashsumJobDialog && <NewHashsumJobDialog />}
                {this.props.dialogs.displayEditHashsumJobDialog && <EditHashsumJobDialog />}
                {this.props.dialogs.displayNewCloudConnectionDialog && <NewCloudConnectionDialog />}
                {this.props.dialogs.displayEditCloudConnectionDialog && <EditCloudConnectionDialog />}
                {this.props.dialogs.displayMkdirDialog && <MkdirDialog />}
                {this.props.dialogs.displaySettingsDialog && <SettingsDialog />}
                {this.props.dialogs.displayFileViewerDialog && this.renderFileViewer()}
            </React.Fragment>
        );
    }
}

Dialogs.defaultProps = {
    dialogs: {},
}

import {connect} from 'react-redux';

const mapStateToProps = state => ({
    dialogs: state.dialog,
});

const mapDispatchToProps = dispatch => ({
});

export default connect(mapStateToProps, mapDispatchToProps)(Dialogs);
