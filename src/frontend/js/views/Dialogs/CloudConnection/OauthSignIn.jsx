import React from 'react';
import { Button } from 'react-bootstrap'


// "Sign in with ..." for a provider of the backend's oauth_manager.PROVIDERS. The same
// steps as OnedriveSignIn.jsx, with the provider's texts; OneDrive can move onto this
// component too.
export const OAUTH_PROVIDERS = {
    gdrive: {
        name: 'gdrive',
        connectionType: 'drive',
        title: 'Sign in with Google',
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
        drivesLabel: 'Drive',
        showDriveType: false,
        createLabel: 'Create Google Drive connection',
        defaultName: 'Google Drive',
    },
}


// Set by the backend after an automatic (callback) sign-in: ?oauth_state=...&oauth_provider=...
// OneDrive's callback has no oauth_provider.
export const oauthProviderFromUrl = () => {
    const params = new URLSearchParams(window.location.search);
    if (!params.get('oauth_state')) {
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


class OauthSignIn extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            step: 'start', // start -> paste -> choose
            redirectMode: 'paste',
            redirectUrl: '',
            flowState: null,
            drives: [],
            driveId: '',
            name: '',
            busy: false,
            error: null,
        };
    }

    provider() {
        return OAUTH_PROVIDERS[this.props.provider];
    }

    componentDidMount() {
        if (oauthProviderFromUrl() === this.props.provider) {
            const flowState = new URLSearchParams(window.location.search).get('oauth_state');
            window.history.replaceState(null, '', window.location.pathname);
            this.call(this.props.onRetrieve(this.props.provider, flowState), payload => this.showDrives(payload));
        }
    }

    render() {
        const {step, busy, error} = this.state;
        return (
            <div className='card mb-4'>
                <div className='card-body'>
                    <h5 className='card-title text-primary'>{this.provider().title}</h5>
                    {step === 'start' && this.renderStart()}
                    {step === 'paste' && this.renderPaste()}
                    {step === 'choose' && this.renderChoose()}
                    {busy && <div className='text-muted mt-2'>Working...</div>}
                    {error && <div className='alert alert-danger mt-3 mb-0'>{error}</div>}
                </div>
            </div>
        );
    }

    renderStart() {
        const provider = this.provider();
        return (
            <React.Fragment>
                <p className='card-text'>{provider.intro}</p>
                <Button variant='primary' disabled={this.state.busy} onClick={() => this.handleStart()}>
                    {provider.title}
                </Button>
            </React.Fragment>
        );
    }

    renderPaste() {
        const provider = this.provider();
        const openAgain = (
            <React.Fragment>
                (<a href={this.state.authorizeUrl} target='_blank' rel='noopener noreferrer'>open it again</a>)
            </React.Fragment>
        );
        if (this.state.redirectMode === 'callback') {
            return (
                <React.Fragment>
                    <p className='card-text'>
                        Sign in with {provider.account} in the tab that just opened {openAgain}.
                        Motuz continues in that tab afterwards.
                    </p>
                    <ul className='pl-3'>{provider.warnings}</ul>
                </React.Fragment>
            );
        }
        return (
            <React.Fragment>
                <ol className='pl-3'>
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
                    rows={3}
                    placeholder={provider.placeholder}
                    value={this.state.redirectUrl}
                    onChange={event => this.setState({redirectUrl: event.target.value})}
                />
                <Button
                    variant='primary'
                    disabled={this.state.busy || !this.state.redirectUrl.trim()}
                    onClick={() => this.handleFinish()}
                >
                    Continue
                </Button>
                <Button variant='link' onClick={() => this.setState({step: 'start', error: null})}>
                    Start over
                </Button>
            </React.Fragment>
        );
    }

    renderChoose() {
        const provider = this.provider();
        const {drives, driveId, name} = this.state;
        return (
            <React.Fragment>
                <p className='card-text text-success'>Signed in. Choose the drive for this connection:</p>
                <div className='form-group'>
                    <label><b>{provider.drivesLabel}</b></label>
                    <select
                        className='form-control'
                        value={driveId}
                        onChange={event => this.setState({driveId: event.target.value})}
                    >
                        {drives.map(drive => (
                            <option key={drive.id} value={drive.id}>
                                {drive.name}{provider.showDriveType ? ` [${drive.drive_type}]` : ''}
                            </option>
                        ))}
                    </select>
                </div>
                <div className='form-group'>
                    <label><b>Connection name</b></label>
                    <input
                        className='form-control'
                        value={name}
                        onChange={event => this.setState({name: event.target.value})}
                    />
                </div>
                <Button
                    variant='success'
                    disabled={this.state.busy || !driveId}
                    onClick={() => this.handleConnect()}
                >
                    {provider.createLabel}
                </Button>
            </React.Fragment>
        );
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

    handleConnect() {
        const {flowState, driveId, name} = this.state;
        this.call(this.props.onConnect(this.props.provider, {state: flowState, drive_id: driveId, name}), () => {});
    }

    showDrives(payload) {
        const drives = payload.drives || [];
        const driveId = payload.default_drive_id || (drives[0] && drives[0].id) || '';
        const drive = drives.find(d => d.id === driveId);
        this.setState({
            step: 'choose',
            flowState: payload.state,
            drives,
            driveId,
            name: drive ? drive.name : this.provider().defaultName,
        });
    }

    call(promise, onSuccess, onFailure=() => {}) {
        this.setState({busy: true, error: null});
        Promise.resolve(promise).then(action => {
            this.setState({busy: false});
            if (!action) {
                this.setState({error: 'Your Motuz session was refreshed, please try again.'});
                onFailure();
            } else if (action.error) {
                const response = (action.payload && action.payload.response) || {};
                this.setState({error: response.message || (action.payload && action.payload.message) || 'Request failed'});
                onFailure();
            } else {
                onSuccess(action.payload);
            }
        });
    }
}

OauthSignIn.defaultProps = {
    provider: 'gdrive',
    onStart: (provider) => {},
    onFinish: (provider, redirectUrl) => {},
    onRetrieve: (provider, state) => {},
    onConnect: (provider, data) => {},
}

import {connect} from 'react-redux';
import {startOauthSignin, finishOauthSignin, retrieveOauthSignin, connectOauth} from 'actions/apiActions.jsx';

const mapDispatchToProps = dispatch => ({
    onStart: (provider) => dispatch(startOauthSignin(provider)),
    onFinish: (provider, redirectUrl) => dispatch(finishOauthSignin(provider, redirectUrl)),
    onRetrieve: (provider, state) => dispatch(retrieveOauthSignin(provider, state)),
    onConnect: (provider, data) => dispatch(connectOauth(provider, data)),
});

export default connect(null, mapDispatchToProps)(OauthSignIn);
