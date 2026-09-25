import React from 'react';

import OauthSignIn from 'views/Dialogs/CloudConnection/OauthSignIn.jsx';

// rclone's public Drive app (oauth_manager.GDRIVE.rclone_client_id)
const RCLONE_GDRIVE_CLIENT_ID = '202264815644.apps.googleusercontent.com';


// Google Drive part of CloudConnectionDialogFields: "Sign in with Google", or a token
// pasted from `rclone config`. `Field` is the dialog's CloudConnectionField.
class GdriveSection extends React.Component {
    render() {
        const manualFields = this.renderManualFields();
        if (this.props.isSanitized) { // Editing an existing connection
            // The token broker refreshes the token with the OAuth client that issued it
            const clientId = this.props.data.gdrive_client_id;
            const app = !clientId || clientId === RCLONE_GDRIVE_CLIENT_ID
                ? "rclone's app"
                : `Motuz app (${clientId})`;
            return (
                <React.Fragment>
                    <p className='text-muted'>
                        Signed in through: {app}. Pasting a new token from rclone switches
                        this connection to rclone's app.
                    </p>
                    {manualFields}
                </React.Fragment>
            );
        }
        return (
            <React.Fragment>
                <OauthSignIn provider='gdrive' />
                <details>
                    <summary className='text-primary h5 mt-2 mb-3'>
                        Advanced: paste a token from rclone instead
                    </summary>
                    {manualFields}
                </details>
            </React.Fragment>
        );
    }

    renderManualFields() {
        const {data, errors, verifySuccess, isSanitized, Field} = this.props;
        return (
            <React.Fragment>
                <details open={!isSanitized}>
                    <summary className='text-primary h6 mt-2 mb-2'>
                        How to get these values
                    </summary>

                    <ol>
                        <li className='mb-1'>
                            On your own computer (it needs a web browser), install
                            {' '}<a href='https://rclone.org/install/' target='_blank' rel='noopener noreferrer'>rclone</a>{' '}
                            and run
                            <pre className='mb-1'>rclone config</pre>
                        </li>
                        <li className='mb-1'>
                            Answer: <tt>n</tt> (new remote), name <tt>gdrive</tt>, storage <tt>drive</tt> (Google Drive).
                        </li>
                        <li className='mb-1'>
                            Leave <tt>client_id</tt> and <tt>client_secret</tt> empty (press Enter).
                            Motuz can only refresh tokens of rclone's own app.
                        </li>
                        <li className='mb-1'>
                            <tt>scope</tt>: <tt>1</tt> (full access to all files).
                            Press Enter for <tt>service_account_file</tt> and <tt>n</tt> for advanced config.
                        </li>
                        <li className='mb-1'>
                            If rclone warns that its shared client_id is being retired, answer <tt>y</tt> to
                            continue with it.
                        </li>
                        <li className='mb-1'>
                            Answer <tt>y</tt> to authenticate in the web browser, sign in with your Google
                            account and allow access.
                        </li>
                        <li className='mb-1'>
                            <i>Configure this as a Shared Drive?</i> <tt>n</tt> for your own My Drive,
                            or <tt>y</tt> and pick the shared drive. Keep the remote with <tt>y</tt> and
                            quit with <tt>q</tt>.
                        </li>
                        <li className='mb-1'>
                            Show the values to paste below:
                            <pre className='mb-1'>rclone config show gdrive</pre>
                            Paste everything after <tt>token = </tt>, from <tt>{'{'}</tt> to <tt>{'}'}</tt>,
                            including the <tt>refresh_token</tt>, and the <tt>team_drive</tt> value if there is one.
                        </li>
                    </ol>
                    <p className='text-muted'>
                        Motuz keeps the token refreshed, so it only needs to be pasted once. rclone's
                        shared Google app is heavily rate limited and being retired by rclone; prefer
                        Sign in with Google.
                    </p>
                </details>

                <Field
                    label='Shared Drive ID'
                    input={{
                        name: 'gdrive_team_drive',
                        defaultValue: data.gdrive_team_drive,
                        placeholder: 'team_drive, e.g. 0ABCdefGHIjklUk9PVA (empty: My Drive)',
                    }}
                    error={errors.gdrive_team_drive}
                    isValid={verifySuccess}
                />

                <Field
                    label='Root Folder ID'
                    input={{
                        name: 'gdrive_root_folder_id',
                        defaultValue: data.gdrive_root_folder_id,
                        placeholder: 'Optional: start in this folder (id from its URL)',
                    }}
                    error={errors.gdrive_root_folder_id}
                    isValid={verifySuccess}
                />

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <Field
                    label='Token'
                    input={{
                        name: 'gdrive_token',
                        defaultValue: data.gdrive_token,
                        // When editing, the stored token is kept unless a new one is pasted
                        required: !isSanitized,
                        type: 'password',
                        placeholder: isSanitized
                            ? 'Leave empty to keep the stored token'
                            : '{"access_token":"ya29...","refresh_token":"1//...",...}',
                    }}
                    error={errors.gdrive_token}
                    isValid={verifySuccess}
                    isSanitized={isSanitized}
                />
            </React.Fragment>
        );
    }
}

GdriveSection.defaultProps = {
    data: {},
    errors: {},
    verifySuccess: false,
    isSanitized: false,
}

export default GdriveSection;
