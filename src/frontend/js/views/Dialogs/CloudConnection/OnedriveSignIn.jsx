import React from 'react';
import { Button } from 'react-bootstrap'


// Query parameter set by the backend after an automatic (callback) sign-in
export const oauthStateFromUrl = () => new URLSearchParams(window.location.search).get('oauth_state');


class OnedriveSignIn extends React.Component {
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

    componentDidMount() {
        const flowState = oauthStateFromUrl();
        if (flowState) {
            window.history.replaceState(null, '', window.location.pathname);
            this.call(this.props.onRetrieve(flowState), payload => this.showDrives(payload));
        }
    }

    render() {
        const {step, busy, error} = this.state;
        return (
            <div className='card mb-4'>
                <div className='card-body'>
                    <h5 className='card-title text-primary'>Sign in with Microsoft</h5>
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
        return (
            <React.Fragment>
                <p className='card-text'>
                    Motuz signs in to your OneDrive or SharePoint for you. Nothing needs to be
                    installed on your computer.
                </p>
                <Button variant='primary' disabled={this.state.busy} onClick={() => this.handleStart()}>
                    Sign in with Microsoft
                </Button>
            </React.Fragment>
        );
    }

    renderPaste() {
        if (this.state.redirectMode === 'callback') {
            return (
                <p className='card-text'>
                    Sign in with your university account in the tab that just opened
                    (<a href={this.state.authorizeUrl} target='_blank' rel='noopener noreferrer'>open it again</a>).
                    Motuz continues in that tab afterwards.
                </p>
            );
        }
        return (
            <React.Fragment>
                <ol className='pl-3'>
                    <li className='mb-1'>
                        Sign in with your university account in the tab that just opened
                        (<a href={this.state.authorizeUrl} target='_blank' rel='noopener noreferrer'>open it again</a>).
                    </li>
                    <li className='mb-1'>
                        The tab then shows an error like <i>"This site can't be reached"</i> at
                        {' '}<tt>localhost:53682</tt>. That is expected.
                    </li>
                    <li className='mb-1'>
                        Copy the complete address from that tab's address bar and paste it here:
                    </li>
                </ol>
                <textarea
                    className='form-control mb-2'
                    rows={3}
                    placeholder='http://localhost:53682/?code=...&state=...'
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
        const {drives, driveId, name} = this.state;
        return (
            <React.Fragment>
                <p className='card-text text-success'>Signed in. Choose the drive for this connection:</p>
                <div className='form-group'>
                    <label><b>Drive</b></label>
                    <select
                        className='form-control'
                        value={driveId}
                        onChange={event => this.setState({driveId: event.target.value})}
                    >
                        {drives.map(drive => (
                            <option key={drive.id} value={drive.id}>
                                {drive.name} [{drive.drive_type}]
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
                    Create OneDrive connection
                </Button>
            </React.Fragment>
        );
    }

    handleStart() {
        // Open the tab synchronously, browsers block pop-ups opened after an await
        const tab = window.open('about:blank', '_blank');
        this.call(this.props.onStart(), payload => {
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
        this.call(this.props.onFinish(this.state.redirectUrl.trim()), payload => this.showDrives(payload));
    }

    handleConnect() {
        const {flowState, driveId, name} = this.state;
        this.call(this.props.onConnect({state: flowState, drive_id: driveId, name}), () => {});
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
            name: drive ? drive.name : 'OneDrive',
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

OnedriveSignIn.defaultProps = {
    onStart: () => {},
    onFinish: (redirectUrl) => {},
    onRetrieve: (state) => {},
    onConnect: (data) => {},
}

import {connect} from 'react-redux';
import {startOnedriveSignin, finishOnedriveSignin, retrieveOnedriveSignin, connectOnedrive} from 'actions/apiActions.jsx';

const mapDispatchToProps = dispatch => ({
    onStart: () => dispatch(startOnedriveSignin()),
    onFinish: (redirectUrl) => dispatch(finishOnedriveSignin(redirectUrl)),
    onRetrieve: (state) => dispatch(retrieveOnedriveSignin(state)),
    onConnect: (data) => dispatch(connectOnedrive(data)),
});

export default connect(null, mapDispatchToProps)(OnedriveSignIn);
