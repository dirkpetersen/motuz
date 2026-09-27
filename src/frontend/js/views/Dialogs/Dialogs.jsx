import React from 'react';

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
