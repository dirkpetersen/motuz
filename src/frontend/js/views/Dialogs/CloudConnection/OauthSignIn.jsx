import React from 'react';
import { Button } from 'react-bootstrap'


// "Sign in with Microsoft/Google" for the providers of the backend's
// oauth_manager.PROVIDERS. One component for all of them: only the texts differ.
export const OAUTH_PROVIDERS = {
    onedrive: {
        name: 'onedrive',
        connectionType: 'onedrive',
        signInLabel: 'Sign in with Microsoft',
        intro: 'Motuz signs in to your OneDrive or SharePoint for you. Nothing needs to be installed on your computer.',
        account: 'your university account',
        // rclone's public OneDrive app
        loopback: 'localhost:53682',
        placeholder: 'http://localhost:53682/?code=...&state=...',
        warnings: null,
        showDriveType: true,
        defaultName: 'OneDrive',
    },
    gdrive: {
        name: 'gdrive',
        connectionType: 'drive',
        signInLabel: 'Sign in with Google',
        intro: 'Motuz signs in to your Google Drive for you. Nothing needs to be installed on your computer.',
        account: 'your Google account',
        // rclone's public Drive app (oauthutil.RedirectURL)
        loopback: '127.0.0.1:53682',
        placeholder: 'http://127.0.0.1:53682/?state=...&code=...&scope=...',
        warnings: (
            <li className='mb-1 text-muted'>
                Google may warn that the app is unverified or ask you to confirm access to all of your
                Drive files: Motuz copies any file you choose. If your organization blocks the app,
                ask your Google Workspace administrator.
            </li>
        ),
        showDriveType: false,
        defaultName: 'Google Drive',
    },
}

// Provider name ('onedrive', 'gdrive') of a connection type, or undefined
export const oauthProviderForType = type => {
    const provider = Object.values(OAUTH_PROVIDERS).find(p => p.connectionType === type);
    return provider ? provider.name : undefined;
};

const CALLBACK_ERRORS = {
    expired: 'The sign-in took too long or was already used. Please sign in again.',
    failed: 'The sign-in did not work. Please sign in again.',
};

// Set by the backend after an automatic (callback) sign-in:
// /clouds?oauth_state=...&oauth_provider=... or ?oauth_error=expired|failed&oauth_provider=...
// OneDrive's callback has no oauth_provider.
export const oauthProviderFromUrl = () => {
    const params = new URLSearchParams(window.location.search);
    if (!params.get('oauth_state') && !params.get('oauth_error')) {
        return null;
    }
    return params.get('oauth_provider') || 'onedrive';
};

// Connection type to preselect when the page was opened by a callback
export const oauthConnectionTypeFromUrl = () => {
    const provider = oauthProviderFromUrl();
    if (!provider) {
        return undefined;
    }
    return OAUTH_PROVIDERS[provider] ? OAUTH_PROVIDERS[provider].connectionType : 'onedrive';
};


/**
 * The sign-in part of the New Cloud Connection dialog: a "Sign in" button, then (paste
 * mode) the address to paste, then the account and a Drive select. It has no name field
 * and no create button: the dialog's Connection Name and its footer button create the
 * connection with what this component reports through onChange.
 */
class OauthSignIn extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            step: 'start', // start -> paste -> choose
            redirectMode: 'paste',
            authorizeUrl: null,
            redirectUrl: '',
            flowState: null,
            account: null,
            drives: [],
            driveId: '',
            busy: false,
        };
    }

    provider() {
        return OAUTH_PROVIDERS[this.props.provider];
    }

    componentDidMount() {
        if (oauthProviderFromUrl() !== this.props.provider) {
            return;
        }
        const params = new URLSearchParams(window.location.search);
        window.history.replaceState(null, '', window.location.pathname);
        if (params.get('oauth_state')) {
            this.call(this.props.onRetrieve(this.props.provider, params.get('oauth_state')), payload => this.showDrives(payload));
        } else {
            this.props.onError(CALLBACK_ERRORS[params.get('oauth_error')] || CALLBACK_ERRORS.failed);
        }
    }

    componentWillUnmount() {
        this.props.onChange(null);
    }

    render() {
        const {step} = this.state;
        return (
            <div className='oauth-sign-in'>
                {step === 'start' && this.renderStart()}
                {step === 'paste' && this.renderPaste()}
                {step === 'choose' && this.renderChoose()}
            </div>
        );
    }

    renderRow(label, content, required=true) {
        return (
            <div className={`row mb-3 ${required ? 'required' : ''}`}>
                <div className='col-4 text-end control-label'>
                    <b className='form-label'>{label}</b>
                </div>
                <div className='col-8'>
                    {content}
                </div>
            </div>
        );
    }

    renderStart() {
        const provider = this.provider();
        return this.renderRow('Account', (
            <React.Fragment>
                <Button variant='primary' disabled={this.state.busy} onClick={() => this.handleStart()}>
                    {provider.signInLabel}
                </Button>
                {this.renderBusy()}
                <small className='form-text text-muted'>{provider.intro}</small>
            </React.Fragment>
        ));
    }

    renderPaste() {
        const provider = this.provider();
        const openAgain = (
            <React.Fragment>
                (<a href={this.state.authorizeUrl} target='_blank' rel='noopener noreferrer'>open it again</a>)
            </React.Fragment>
        );
        if (this.state.redirectMode === 'callback') {
            return this.renderRow('Account', (
                <React.Fragment>
                    <p className='mb-1 pt-2'>
                        Sign in with {provider.account} in the tab that just opened {openAgain}.
                        Motuz continues in that tab afterwards.
                    </p>
                    {provider.warnings && <ul className='ps-3 small'>{provider.warnings}</ul>}
                    {this.renderStartOver()}
                </React.Fragment>
            ));
        }
        return this.renderRow('Account', (
            <React.Fragment>
                <ol className='ps-3 pt-2 mb-2'>
                    <li className='mb-1'>
                        Sign in with {provider.account} in the tab that just opened {openAgain}.
                    </li>
                    {provider.warnings}
                    <li className='mb-1'>
                        The tab then shows an error like <i>"This site can't be reached"</i> at
                        {' '}<tt>{provider.loopback}</tt>. That is expected.
                    </li>
                    <li className='mb-1'>
                        Copy the complete address from that tab's address bar and paste it here:
                    </li>
                </ol>
                <textarea
                    className='form-control mb-2'
                    aria-label='Address from the sign-in tab'
                    rows={3}
                    placeholder={provider.placeholder}
                    value={this.state.redirectUrl}
                    onChange={event => this.setState({redirectUrl: event.target.value})}
                />
                <Button
                    variant='outline-primary'
                    disabled={this.state.busy || !this.state.redirectUrl.trim()}
                    onClick={() => this.handleFinish()}
                >
                    Continue
                </Button>
                {this.renderStartOver()}
                {this.renderBusy()}
            </React.Fragment>
        ));
    }

    renderChoose() {
        const provider = this.provider();
        const {drives, driveId, account} = this.state;
        return (
            <React.Fragment>
                {this.renderRow('Account', (
                    <div className='pt-2 oauth-account'>
                        <span className='text-success'>
                            {account ? <React.Fragment>Signed in as <b>{account}</b></React.Fragment> : 'Signed in'}
                        </span>
                        {this.renderStartOver('Use another account')}
                    </div>
                ))}
                {this.renderRow('Drive', (
                    <select
                        className='form-select'
                        aria-label='Drive'
                        value={driveId}
                        onChange={event => this.selectDrive(event.target.value)}
                    >
                        {drives.map(drive => (
                            <option key={drive.id} value={drive.id}>
                                {drive.name}{provider.showDriveType ? ` [${drive.drive_type}]` : ''}
                            </option>
                        ))}
                    </select>
                ))}
            </React.Fragment>
        );
    }

    renderStartOver(label='Start over') {
        return (
            <Button variant='link' size='sm' onClick={() => this.startOver()}>
                {label}
            </Button>
        );
    }

    renderBusy() {
        return this.state.busy ? <span className='text-muted ms-2'>Working...</span> : null;
    }

    startOver() {
        this.setState({step: 'start', redirectUrl: '', flowState: null, drives: [], driveId: '', account: null});
        this.props.onChange(null);
        this.props.onError(null);
    }

    handleStart() {
        // Open the tab synchronously, browsers block pop-ups opened after an await
        const tab = window.open('about:blank', '_blank');
        this.call(this.props.onStart(this.props.provider), payload => {
            if (tab) {
                tab.location = payload.authorize_url;
            } else {
                window.open(payload.authorize_url, '_blank');
            }
            this.setState({
                step: 'paste',
                authorizeUrl: payload.authorize_url,
                redirectMode: payload.redirect_mode,
            });
        }, () => tab && tab.close());
    }

    handleFinish() {
        this.call(this.props.onFinish(this.props.provider, this.state.redirectUrl.trim()), payload => this.showDrives(payload));
    }

    showDrives(payload) {
        const drives = payload.drives || [];
        const driveId = payload.default_drive_id || (drives[0] && drives[0].id) || '';
        this.setState({
            step: 'choose',
            flowState: payload.state,
            account: payload.account || null,
            drives,
        }, () => this.selectDrive(driveId));
    }

    driveName(driveId) {
        const drive = this.state.drives.find(d => d.id === driveId);
        return drive ? drive.name : this.provider().defaultName;
    }

    selectDrive(driveId) {
        this.setState({driveId});
        // The dialog's Connection Name follows the drive until the user types one
        this.props.onSuggestName(this.driveName(driveId));
        this.props.onChange(driveId ? {provider: this.props.provider, state: this.state.flowState, driveId} : null);
    }

    call(promise, onSuccess, onFailure=() => {}) {
        this.setState({busy: true});
        this.props.onError(null);
        Promise.resolve(promise).then(action => {
            this.setState({busy: false});
            const message = oauthErrorMessage(action);
            if (message) {
                this.props.onError(message);
                onFailure();
            } else {
                onSuccess(action.payload);
            }
        });
    }
}

// Plain-language error of a dispatched sign-in action, or null if it succeeded
export const oauthErrorMessage = action => {
    if (!action) {
        return 'Your Motuz session was refreshed. Please try again.';
    }
    if (!action.error) {
        return null;
    }
    const response = (action.payload && action.payload.response) || {};
    return response.message || 'Motuz could not reach the server. Please try again.';
};

OauthSignIn.defaultProps = {
    provider: 'onedrive',
    onChange: (signIn) => {}, // {provider, state, driveId} once a drive is chosen, else null
    onSuggestName: (name) => {},
    onError: (message) => {},
    onStart: (provider) => {},
    onFinish: (provider, redirectUrl) => {},
    onRetrieve: (provider, state) => {},
}

import {connect} from 'react-redux';
import {startOauthSignin, finishOauthSignin, retrieveOauthSignin} from 'actions/apiActions.jsx';

const mapDispatchToProps = dispatch => ({
    onStart: (provider) => dispatch(startOauthSignin(provider)),
    onFinish: (provider, redirectUrl) => dispatch(finishOauthSignin(provider, redirectUrl)),
    onRetrieve: (provider, state) => dispatch(retrieveOauthSignin(provider, state)),
});

export default connect(null, mapDispatchToProps)(OauthSignIn);
