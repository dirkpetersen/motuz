import React from 'react';
import { Modal, Button } from 'react-bootstrap'

import UriResource from 'components/UriResource.jsx'

const SHOWN_NAMES = 20;

/**
 * Asked once per drop from the desktop when some of the dropped files or folders exist
 * in the destination folder (actions/uploadActions.jsx)
 */
class UploadConflictDialog extends React.Component {
    render() {
        const {existing, host, folder, count, overwrite, overwriteMessage} = this.props.data;
        const shown = existing.slice(0, SHOWN_NAMES);
        const more = existing.length - shown.length;

        return (
            <Modal show={true} size="lg" onHide={() => this.props.onAnswer('cancel')}>
                <Modal.Header closeButton>
                    <Modal.Title>Files already exist</Modal.Title>
                </Modal.Header>
                <Modal.Body className="upload-conflict-dialog">
                    <p>
                        You are uploading {count} {count === 1 ? 'file' : 'files'} to{' '}
                        <UriResource protocol={host.type} path={folder} />.
                        These names already exist there:
                    </p>
                    <ul className="upload-conflict-names">
                        {shown.map(name => <li key={name}>{name}</li>)}
                        {more > 0 && <li className="text-muted">and {more} more</li>}
                    </ul>
                    {overwrite ? (
                        <p className="mb-0">
                            <b>Overwrite all</b> replaces the existing files. <b>Skip existing</b> uploads
                            only the files that do not exist yet, also inside these folders.
                        </p>
                    ) : (
                        <p className="mb-0 upload-conflict-no-overwrite">
                            {overwriteMessage || 'Existing files cannot be overwritten here.'}{' '}
                            <b>Skip existing</b> uploads only the files that do not exist yet, also inside these folders.
                        </p>
                    )}
                </Modal.Body>
                <Modal.Footer>
                    <Button variant="secondary" onClick={() => this.props.onAnswer('cancel')}>
                        Cancel
                    </Button>
                    <Button variant={overwrite ? 'outline-primary' : 'primary'} onClick={() => this.props.onAnswer('skip')}>
                        Skip existing
                    </Button>
                    {overwrite && (
                        <Button variant="primary" onClick={() => this.props.onAnswer('overwrite')}>
                            Overwrite all
                        </Button>
                    )}
                </Modal.Footer>
            </Modal>
        );
    }
}

UploadConflictDialog.defaultProps = {
    data: {existing: [], host: {}, folder: '', count: 0, overwrite: true, overwriteMessage: null},
    onAnswer: (mode) => {},
}

import {connect} from 'react-redux';
import {answerUploadConflict} from 'actions/uploadActions.jsx'

const mapStateToProps = state => ({
    data: state.dialog.uploadConflictDialogData,
});

const mapDispatchToProps = dispatch => ({
    onAnswer: (id, mode) => dispatch(answerUploadConflict(id, mode)),
});

const mergeProps = (stateProps, dispatchProps) => ({
    data: stateProps.data,
    onAnswer: mode => dispatchProps.onAnswer(stateProps.data.id, mode),
});

export default connect(mapStateToProps, mapDispatchToProps, mergeProps)(UploadConflictDialog);
