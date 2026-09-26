import React from 'react';
import { Modal, Button } from 'react-bootstrap'

import CloudConnectionDialogFields from 'views/Dialogs/CloudConnection/CloudConnectionDialogFields.jsx';
import VerifyStatusButton, { DialogError, dialogErrorMessage } from 'views/Dialogs/CloudConnection/VerifyStatusButton.jsx'
import Icon from 'components/Icon.jsx'
import serializeForm from 'utils/serializeForm.jsx';
import { oauthConnectionTypeFromUrl, oauthErrorMessage } from 'views/Dialogs/CloudConnection/OauthSignIn.jsx';


/**
 * One Connection Name field (in the fields) and one primary button, "Create connection",
 * in the footer:
 * - OneDrive / Google Drive: enabled once the user signed in and a drive is chosen; it
 *   creates the connection through /api/oauth/<provider>/connect/. No test button:
 *   signing in proved access.
 * - everything else (and "Advanced: paste a token"): creates the connection from the
 *   fields, next to "Test connection".
 */
class NewCloudConnectionDialog extends React.Component {
    constructor(props) {
        super(props);
        this.formRef = React.createRef();
        this.state = {
            // The provider whose sign-in is shown (null: none, undefined: not reported yet)
            signInProvider: undefined,
            signIn: null, // {provider, state, driveId} once signed in and a drive is chosen
            error: null, // sign-in or connect error, plain language
            connecting: false,
        };
        // Back from an automatic "Sign in with Microsoft/Google": read once, because the
        // sign-in removes ?oauth_state from the address right after mounting and the
        // dialog re-renders later (e.g. when the drives arrive)
        this.oauthConnectionType = oauthConnectionTypeFromUrl();
    }

    render() {
        const {errors} = this.props;

        return (
            <Modal
                show={true}
                size="lg"
                onHide={() => this.handleClose()}
            >
                <form
                    action="#"
                    onSubmit={event => this.handleSubmit(event)}
                    ref={this.formRef}
                >
                    <Modal.Header closeButton>
                        <Modal.Title>
                            New Cloud Connection
                            <a
                                target="_blank"
                                href="https://sciwiki.fredhutch.org/compdemos/motuz/#add-a-new-cloud-connection-to-motuz"
                                className='ms-2'
                            >
                                <Icon name='question' verticalAlign='middle'></Icon>
                            </a>
                        </Modal.Title>
                    </Modal.Header>
                    <Modal.Body>
                        <CloudConnectionDialogFields
                            data={{
                                type: this.oauthConnectionType,
                                s3_region: 'us-west-2',
                                sftp_port: '22',
                            }}
                            errors={errors}
                            verifySuccess={(this.props.cloudConnectionVerification.success === true)}
                            isSanitized={false}
                            onModeChange={signInProvider => this.handleModeChange(signInProvider)}
                            onOauthChange={signIn => this.setState({signIn})}
                            onOauthError={error => this.setState({error})}
                        />
                    </Modal.Body>
                    {this.state.signInProvider ? this.renderSignInFooter() : this.renderFooter()}
                </form>
            </Modal>
        );
    }

    handleModeChange(signInProvider) {
        this.setState(state => ({
            signInProvider,
            // Keep an error of the first report: the error of an automatic sign-in that
            // came back failed (?oauth_error)
            error: state.signInProvider === undefined ? state.error : null,
        }));
    }

    signedIn() {
        const {signIn, signInProvider} = this.state;
        return signIn !== null && signIn.provider === signInProvider;
    }

    renderSignInFooter() {
        const ready = this.signedIn();
        return (
            <Modal.Footer>
                <DialogError message={this.state.error} />
                {!ready && <span className="me-auto text-muted footer-hint">Sign in first</span>}
                <Button variant="secondary" onClick={() => this.handleClose()}>
                    Cancel
                </Button>
                <Button variant="primary" type="submit" disabled={!ready || this.state.connecting}>
                    {this.state.connecting ? 'Creating...' : 'Create connection'}
                </Button>
            </Modal.Footer>
        );
    }

    renderFooter() {
        const verification = this.props.cloudConnectionVerification;
        return (
            <Modal.Footer>
                <DialogError message={dialogErrorMessage(null, this.props.errorMessage, verification)} />
                <div className="me-auto">
                    <Button variant="outline-secondary" onClick={() => this.handleVerify()}>
                        Test connection
                    </Button>
                    <VerifyStatusButton {...verification} />
                </div>
                <Button variant="secondary" onClick={() => this.handleClose()}>
                    Cancel
                </Button>
                <Button variant="primary" type="submit">
                    Create connection
                </Button>
            </Modal.Footer>
        );
    }

    handleClose() {
        this.props.onClose();
    }

    handleVerify() {
        const form = this.formRef.current;

        if (!form.checkValidity()) {
            form.reportValidity()
            return
        }

        const data = serializeForm(form)
        this.props.onVerify(data);
    }

    handleSubmit(event) {
        event.preventDefault();
        const data = serializeForm(event.target)
        if (this.state.signInProvider) {
            this.handleConnect(data.name);
        } else {
            this.props.onSubmit(data);
        }
    }

    // Creates the signed-in connection; success closes the dialog (CREATE_CLOUD_CONNECTION_SUCCESS)
    handleConnect(name) {
        const {signIn, connecting} = this.state;
        if (!this.signedIn() || connecting) { // e.g. Enter in the name field before signing in
            return;
        }
        this.setState({connecting: true, error: null});
        Promise.resolve(this.props.onConnect(signIn.provider, {
            state: signIn.state,
            drive_id: signIn.driveId,
            name: (name || '').trim(),
        })).then(action => {
            const error = oauthErrorMessage(action);
            if (error && this.mounted) {
                this.setState({connecting: false, error});
            }
        });
    }

    componentDidMount() {
        this.mounted = true;
    }

    componentWillUnmount() {
        this.mounted = false;
    }
}

NewCloudConnectionDialog.defaultProps = {
    errors: {},
    errorMessage: null,
    data: {},
    cloudConnectionVerification: {
        loading: false,
        success: null,
    },
    onClose: () => {},
    onSubmit: (data) => {},
    onVerify: (data) => {},
    onConnect: (provider, data) => {},
}

import {connect} from 'react-redux';
import {hideNewCloudConnectionDialog} from 'actions/dialogActions.jsx'
import {createCloudConnection, verifyCloudConnection, connectOauth} from 'actions/apiActions.jsx'

const mapStateToProps = state => ({
    errors: state.api.cloudErrors,
    errorMessage: state.api.cloudErrorMessage,
    data: state.dialog.newCloudConnectionDialogData,
    cloudConnectionVerification: state.api.cloudConnectionVerification,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideNewCloudConnectionDialog()),
    onSubmit: data => dispatch(createCloudConnection(data)),
    onVerify: data => dispatch(verifyCloudConnection(data)),
    onConnect: (provider, data) => dispatch(connectOauth(provider, data)),
});

export default connect(mapStateToProps, mapDispatchToProps)(NewCloudConnectionDialog);
