import React from 'react';
import classnames from 'classnames';

import serializeForm from 'utils/serializeForm.jsx';

import 'logo.png';


class Login extends React.Component {
    constructor(props) {
        super(props);
    }

    render() {
        const { loading, errors } = this.props;

        return (
            <div className="form-wrapper">
                <div className="login-column">
                    <form
                        className="form-signin"
                        onSubmit={event => this.onSubmit(event)}
                    >
                        <img
                            className="mb-4"
                            src="/img/logo.png"
                            width={102}
                            height={122}
                        />
                        <h1 className="h1 mb-5 font-weight-normal">Motuz</h1>

                        <div className='form-group'>
                            <label htmlFor="inputEmail" className="sr-only">Username</label>
                            <input
                                name='username'
                                type="text"
                                className={classnames({
                                    "form-control":true,
                                    'is-valid': loading,
                                    "is-invalid": errors.message,
                                })}
                                placeholder="Username"
                                required
                                autoFocus
                                autoComplete="off"
                            />
                        </div>
                        <div className="form-group">
                            <label htmlFor="inputPassword" className="sr-only">Password</label>
                            <input
                                name='password'
                                type="password"
                                className={classnames({
                                    "form-control":true,
                                    'is-valid': loading,
                                    "is-invalid": errors.message,
                                })}
                                placeholder="Password"
                                required
                                autoComplete="off"
                            />
                            <span className="invalid-feedback">
                                {errors.message}
                            </span>
                            <span className="valid-feedback">
                                Checking Credentials...
                            </span>
                        </div>
                        <div className="form-group">
                            <button className="btn btn-lg btn-primary btn-block" type="submit">Sign in</button>
                        </div>
                    </form>
                    <p className="login-about text-muted">
                        Motuz is a web interface for large data transfers between on-premise
                        storage and cloud storage (Amazon S3, Azure Blob Storage, Google Cloud
                        Storage, Google Drive, Microsoft OneDrive, Dropbox, SFTP, WebDAV, ...).
                        It uses <a href="https://rclone.org/">rclone</a> to copy and verify
                        files, and runs every operation as your own user account. Sign in with
                        your institutional user name and password.
                    </p>
                    <footer className="login-footer text-muted">
                        <p className="mb-1">&copy; 2019 Fred Hutchinson Cancer Research Center</p>
                        <nav>
                            <a href="/privacy">Privacy Policy</a>
                            <span aria-hidden="true"> &middot; </span>
                            <a href="/terms">Terms of Service</a>
                        </nav>
                    </footer>
                </div>
            </div>
        );
    }

    componentDidMount() {

    }

    onSubmit(event) {
        event.preventDefault()
        const form = event.target;
        const data = serializeForm(form)
        this.props.onLogin(data['username'], data['password']);
    }
}

Login.defaultProps = {
    errors: {},
    loading: false,
    onLogin: (username, password) => {},
}

import {connect} from 'react-redux';
import {login} from 'actions/authActions.jsx'

const mapStateToProps = state => ({
    loading: state.auth.loading,
    errors: state.auth.errors,
});

const mapDispatchToProps = dispatch => ({
    onLogin: (username, password) => dispatch(login(username, password)),
});

export default connect(mapStateToProps, mapDispatchToProps)(Login);
