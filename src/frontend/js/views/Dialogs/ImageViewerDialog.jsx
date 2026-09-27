import React from 'react';
import { Modal, Button } from 'react-bootstrap'

import formatBytes from 'utils/formatBytes.jsx'

// The types the server sends (utils/image_view.py detects them from the file's first
// bytes); anything else is not shown, whatever the response says
export const IMAGE_TYPES = {
    'image/png': 'PNG',
    'image/jpeg': 'JPEG',
    'image/gif': 'GIF',
    'image/webp': 'WebP',
};

const LOAD_ERROR = 'The image could not be loaded. Please try again.';

export function imageErrorOf(payload) {
    const response = (payload && payload.response) || {};
    return {
        status: (payload && payload.status) || null,
        error: (typeof response.message === 'string' && response.message) || LOAD_ERROR,
    };
}

/**
 * Read-only image viewer (double-click on a .png, .jpg/.jpeg, .gif or .webp file in a
 * pane). The image comes from POST /api/system/files/view/image/ (read as the user,
 * the type detected by the server from the file's first bytes; SVG is never an image
 * here) with the token in the header, and is shown through a blob: URL, revoked when
 * the dialog closes. Fit to the window by default; a click on the image, the button or
 * Z toggles 100% (scrolling). Esc closes.
 */
class ImageViewerDialog extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            loading: true,
            error: null,
            status: null,
            url: null,
            type: null,
            size: null,
            width: null,
            height: null,
            decodeError: false,
            actualSize: false,
        };
        this.url = null;
        this.unmounted = false;
        this.stage = null;
    }

    componentDidMount() {
        this.load();
    }

    componentWillUnmount() {
        this.unmounted = true;
        this.revoke();
    }

    revoke() {
        if (this.url) {
            URL.revokeObjectURL(this.url);
            this.url = null;
        }
    }

    async load() {
        const {connectionId, path} = this.props.data;
        let action;
        try {
            action = await this.props.fetchImage({connection_id: connectionId, path});
        } catch (e) {
            action = {error: true, payload: e};
        }
        if (this.unmounted) {
            return;
        }
        const result = action && !action.error ? action.payload : null;
        if (!result || !result.blob) {
            this.setState({loading: false, ...imageErrorOf(action && action.payload)});
            return;
        }
        if (!IMAGE_TYPES[result.type]) {
            this.setState({loading: false, status: 415, error: 'The server did not send a PNG, JPEG, GIF or WebP image.'});
            return;
        }
        // The blob gets the detected type, whatever the response carried
        this.url = URL.createObjectURL(new Blob([result.blob], {type: result.type}));
        this.setState({loading: false, url: this.url, type: result.type, size: result.size});
    }

    /** The image is decoded: its natural size (from onLoad, or the ref if it was already complete) */
    onImageLoad(img) {
        if (!img || !img.complete || !img.naturalWidth || this.state.width === img.naturalWidth) {
            return;
        }
        this.setState({width: img.naturalWidth, height: img.naturalHeight, decodeError: false});
        if (this.stage) {
            this.stage.focus({preventScroll: true});
        }
    }

    onImageError() {
        this.setState({decodeError: true});
    }

    toggleSize() {
        this.setState(state => ({actualSize: !state.actualSize}));
    }

    onKeyDown(event) {
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        if (event.key === 'z' || event.key === 'Z' || event.key === '1') {
            event.preventDefault();
            this.toggleSize();
        }
    }

    render() {
        const {name, path, host} = this.props.data;
        const hostName = host && host.id ? host.name : null;
        const {loading, error, status, url, type, size, width, height, decodeError, actualSize} = this.state;
        const shown = url && !decodeError;

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
                    {error && this.renderError(error, status)}
                    {loading && <div className='file-viewer-loading text-muted'>Loading...</div>}
                    {decodeError && (
                        <div className='alert alert-warning file-viewer-error image-viewer-decode-error' role='alert'>
                            The image could not be decoded. The file may be damaged or use a variant of
                            the format this browser does not support.
                        </div>
                    )}
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
                            {IMAGE_TYPES[type]}
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
                        The image viewer shows PNG, JPEG, GIF and WebP images, recognized by their content,
                        not their name. SVG files open as text.
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
}

ImageViewerDialog.defaultProps = {
    data: {},
    animation: true,
    useSiUnits: false,
    onClose: () => {},
    fetchImage: async () => undefined,
}

import {connect} from 'react-redux';
import {hideFileViewerDialog} from 'actions/dialogActions.jsx'
import {viewImage} from 'actions/apiActions.jsx'

const mapStateToProps = state => ({
    data: state.dialog.fileViewerDialogData,
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideFileViewerDialog()),
    fetchImage: data => dispatch(viewImage(data)),
});

export default connect(mapStateToProps, mapDispatchToProps)(ImageViewerDialog);
