import OnedriveSignIn from 'views/Dialogs/CloudConnection/OnedriveSignIn.jsx';
import GdriveSection from 'views/Dialogs/CloudConnection/GdriveSection.jsx';
import LocalCredentialPicker, { describeProfile, profileKey } from 'views/Dialogs/CloudConnection/LocalCredentialPicker.jsx';
import React from 'react';
import classnames from 'classnames';

// rclone's public OneDrive app (oauth_manager.RCLONE_CLIENT_ID)
const RCLONE_ONEDRIVE_CLIENT_ID = 'b15665d9-eda6-4092-8539-0eec376afd59';

const CONNECTION_TYPES = [
    {
        label: 'Amazon Simple Storage Service (s3)',
        value: 's3',
    },
    {
        label: 'Azure Blob Storage',
        value: 'azureblob',
    },
    {
        label: 'Google Cloud Bucket',
        value: 'google cloud storage',
    },
    {
        label: 'Swift',
        value: 'swift',
    },
    {
        label: 'SFTP',
        value: 'sftp',
    },
    {
        label: 'WebDAV',
        value: 'webdav',
    },
    {
        label: 'Dropbox (beta)',
        value: 'dropbox',
    },
    {
        label: 'Microsoft OneDrive (beta)',
        value: 'onedrive',
    },
    {
        label: 'Google Drive (beta)',
        value: 'drive',
    },
]

const S3_CONNECTION_TYPES = [
    {
        label: 'Access Key Credentials',
        value: 'key'
    },
    {
        label: 'Temporary Security Credentials (STS)',
        value: 'sts'
    }
]

const AZURE_CONNECTION_TYPES = [
    {
        label: 'Account & Key',
        value: 'key',
    },
    {
        label: 'Shared Access Signature (SAS)',
        value: 'sas',
    },
]

const SFTP_CONNECTION_TYPES = [
    {
        label: 'Password',
        value: 'password',
    },
    {
        label: 'SSH Private Key',
        value: 'key',
    },
]

class CloudConnectionDialogFields extends React.Component {
    constructor(props) {
        super(props);
        this.state = CloudConnectionDialogFields.initialState;
    }

    render() {
        const { data, errors } = this.props;
        const type = this.state.type || this.props.data.type || 's3';
        const subtype = this.state.subtype || this.props.data.subtype;

        return (
            <div className="container">
                <h5 className="text-primary mb-2">Basic</h5>

                <input type="hidden" name='id' value={data.id}/>

                <div className="row form-group required">
                    <div className="col-4 text-right control-label">
                        <b className='form-label'>Type</b>
                    </div>
                    <div className="col-8">
                        <select
                            className="form-control"
                            name="type"
                            value={type}
                            onChange={event => this.onTypeChange(event.target.value)}
                        >
                            {CONNECTION_TYPES.map(d => (
                                <option
                                    key={d.value}
                                    value={d.value}
                                >{d.label}</option>
                            ))}
                        </select>
                    </div>
                </div>

                <CloudConnectionField
                    label='Connection Name'
                    input={{
                        name: 'name',
                        value: this._name(),
                        onChange: event => this.setState({name: event.target.value}),
                        required: true,
                    }}
                    error={this.props.errors.name}
                    isValid={this.props.verifySuccess}
                />

                {type === 's3' && this._renderS3Section(subtype)}
                {type === 'azureblob' && this._renderAzureSection(subtype)}
                {type === 'swift' && this._renderSwiftSection()}
                {type === 'google cloud storage' && this._renderGCPSection()}
                {type === 'sftp' && this._renderSFTPSection(subtype)}
                {type === 'dropbox' && this._renderDropboxSection()}
                {type === 'onedrive' && this._renderOnedriveSection()}
                {type === 'drive' && this._renderGdriveSection()}
                {type === 'webdav' && this._renderWebdavSection()}
            </div>
        );
    }

    componentDidMount() {
        this._reportSignIn();
    }

    componentDidUpdate() {
        this._reportSignIn();
    }

    _name() {
        return this.state.name === null ? (this.props.data.name || '') : this.state.name;
    }

    // True while a new OneDrive connection is made with "Sign in with Microsoft", which has its
    // own button, so the dialog hides its Verify/Create buttons (they need the pasted token)
    _isSignIn() {
        const type = this.state.type || this.props.data.type || 's3';
        return type === 'onedrive' && !this.props.isSanitized && !this.state.onedriveManual;
    }

    _reportSignIn() {
        const signIn = this._isSignIn();
        if (signIn !== this._reportedSignIn) {
            this._reportedSignIn = signIn;
            this.props.onSignInChange(signIn);
        }
    }

    // Credentials from the user's home directory: picked in a new connection, or stored
    // (subtype 'profile') when editing one
    _profile() {
        const data = this.props.data;
        if (this.props.isSanitized) {
            return data.subtype === 'profile'
                ? {source: data.profile_source, name: data.profile_name, label: 'from your home directory'}
                : null;
        }
        return this.state.profile;
    }

    _renderProfilePicker(type) {
        if (this.props.isSanitized) {
            return null;
        }
        return (
            <LocalCredentialPicker
                type={type}
                selected={this.state.profile}
                onSelect={profile => this.setState({profile})}
            />
        );
    }

    _renderProfileInputs(profile) {
        return (
            <React.Fragment>
                <input type='hidden' name='subtype' value='profile'/>
                <input type='hidden' name='profile_source' value={profile.source}/>
                <input type='hidden' name='profile_name' value={profile.name}/>
                {this.props.isSanitized &&
                    <div className='row form-group'>
                        <div className='col-4 text-right control-label'>
                            <b className='form-label'>Credentials</b>
                        </div>
                        <div className='col-8 pt-2'>
                            {describeProfile(profile)}
                        </div>
                    </div>
                }
            </React.Fragment>
        );
    }

    _renderS3Section(subtype) {
        const profile = this._profile();
        const pickedRegion = this.state.profile && this.state.profile.region;
        return (
            <React.Fragment>
                {this._renderProfilePicker('s3')}
                {profile && this._renderProfileInputs(profile)}
                <CloudConnectionField
                    label='Bucket Name'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        title: "Must be a valid AWS S3 bucket name (or left blank)",
                        pattern: "^(?=^.{3,63}$)(?!xn--)([a-z0-9](?:[a-z0-9-]*)[a-z0-9])$",
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />
                <CloudConnectionField
                    key={'region-' + profileKey(this.state.profile)}
                    label='Region'
                    input={{
                        name: 's3_region',
                        defaultValue: pickedRegion || this.props.data.s3_region,
                        title: "Must be a valid AWS region",
                        // this regex works in python but not js, and it's not futureproof....
                        // pattern: "^(us(-gov)?|ap|ca|cn|eu|sa)-(central|(north|south)?(east|west)?)-\d$",
                    }}
                    error={this.props.errors.s3_region}
                    isValid={this.props.verifySuccess}
                />
                <CloudConnectionField
                    label='KMS Encryption key ARN'
                    input={{
                        name: 'kms_encryption_key_arn',
                        defaultValue: this.props.data.kms_encryption_key_arn,
                        title: "If you are not sure what this is, leave it blank.\nIf you run into errors, search for\n`motuz KMS` on sciwiki.fredhutch.org,\nand email `scicomp` if you still have issues.",
                    }}
                    error={this.props.errors.kms_encryption_key_arn}
                    isValid={this.props.verifySuccess}
                />

                {!profile && this._renderS3Credentials(subtype)}

                <details>
                    <summary className='text-primary h5 mt-5 mb-2'>
                        S3 Compatible Storage
                    </summary>

                    <CloudConnectionField
                        label='Endpoint URL'
                        input={{
                            name: 's3_endpoint',
                            defaultValue: this.props.data.s3_endpoint,
                        }}
                        error={this.props.errors.s3_endpoint}
                        isValid={this.props.verifySuccess}
                    />
                </details>

            </React.Fragment>
        )
    }

    _renderS3Credentials(subtype) {
        return (
            <React.Fragment>
                <div className="row form-group required">
                    <div className="col-4 text-right control-label">
                        <b className='form-label'>S3 Connection Type</b>
                    </div>
                    <div className="col-8">
                        <select
                            className="form-control"
                            name="subtype"
                            value={subtype}
                            onChange={(event => this.setState({subtype: event.target.value}))}
                        >
                            {S3_CONNECTION_TYPES.map(d => (
                                <option
                                    key={d.value}
                                    value={d.value}
                                >{d.label}</option>
                            ))}
                        </select>
                    </div>
                </div>

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Access Key ID'
                    input={{
                        name: 's3_access_key_id',
                        defaultValue: this.props.data.s3_access_key_id,
                        title: "Must be a valid AWS Access Key ID (20 characters, usually starts with 'AKIA').",
                        required: true,
                        maxLength: 20,
                        pattern: "^(?<![A-Z0-9])[A-Z0-9]{20}(?![A-Z0-9])$",
                        minLength: 20,

                    }}
                    error={this.props.errors.s3_access_key_id}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Secret Access Key'
                    input={{
                        name: 's3_secret_access_key',
                        defaultValue: this.props.data.s3_secret_access_key,
                        title: "Must be a valid AWS Secret Access Key (40 characters: letters, numbers and '/')",
                        required: true,
                        type: 'password',
                        minLength: 40,
                        maxLength: 40,
                        // could probably trim this regex:
                        pattern: "^(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])$"
                    }}
                    error={this.props.errors.s3_secret_access_key}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />

                {subtype === 'sts' &&
                    <CloudConnectionField
                        label='Session Token'
                        input={{
                            name: 's3_session_token',
                            defaultValue: this.props.data.s3_session_token,
                            title: "Must be a valid AWS Session Token",
                            required: true,
                            type: 'password',
                            minLength: 40,
                            pattern: "^([A-Za-z0-9/+=]*)$"
                        }}
                        error={this.props.errors.s3_session_token}
                        isValid={this.props.verifySuccess}
                        isSanitized={this.props.isSanitized}
                    />
                }
            </React.Fragment>
        )
    }

    _renderAzureSection(subtype) {
        const profile = this._profile();
        return (
            <React.Fragment>
                {this._renderProfilePicker('azureblob')}
                {profile
                    ? this._renderAzureProfileSubsection(profile)
                    : this._renderAzureManualSubsection(subtype)
                }
            </React.Fragment>
        )
    }

    _renderAzureProfileSubsection(profile) {
        return (
            <React.Fragment>
                {this._renderProfileInputs(profile)}
                <CloudConnectionField
                    label='Bucket Name'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        placeholder: 'container',
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />
                {profile.source === 'azure-cli' &&
                    // An Azure CLI login is an identity, not a storage account
                    <CloudConnectionField
                        label='Storage Account'
                        input={{
                            name: 'azure_account',
                            defaultValue: this.props.data.azure_account,
                            required: true,
                            placeholder: 'mystorageaccount',
                            pattern: '[a-z0-9]{3,24}',
                            title: '3 to 24 lower case letters and digits',
                        }}
                        error={this.props.errors.azure_account}
                        isValid={this.props.verifySuccess}
                    />
                }
            </React.Fragment>
        )
    }

    _renderAzureManualSubsection(subtype) {
        return (
            <React.Fragment>
                <div className="row form-group required">
                    <div className="col-4 text-right control-label">
                        <b className='form-label'>Azure Connection Type</b>
                    </div>
                    <div className="col-8">
                        <select
                            className="form-control"
                            name="subtype"
                            value={subtype}
                            onChange={(event => this.setState({subtype: event.target.value}))}
                        >
                            {AZURE_CONNECTION_TYPES.map(d => (
                                <option
                                    key={d.value}
                                    value={d.value}
                                >{d.label}</option>
                            ))}
                        </select>
                    </div>
                </div>

                {subtype === 'key' && this._renderAzureKeySubsection()}
                {subtype === 'sas' && this._renderAzureSasSubsection()}
            </React.Fragment>
        )
    }

    _renderAzureKeySubsection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Bucket Name'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Account'
                    input={{
                        name: 'azure_account',
                        defaultValue: this.props.data.azure_account,
                        required: true,
                    }}
                    error={this.props.errors.azure_account}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Key'
                    input={{
                        name: 'azure_key',
                        defaultValue: this.props.data.azure_key,
                        required: true,
                        type: 'password',
                    }}
                    error={this.props.errors.azure_key}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />
            </React.Fragment>
        )
    }

    _renderAzureSasSubsection() {
        return (
            <CloudConnectionField
                label='Shared Access Signature (SAS) URL'
                input={{
                    name: 'azure_sas_url',
                    defaultValue: this.props.data.azure_sas_url,
                }}
                error={this.props.errors.azure_sas_url}
                isValid={this.props.verifySuccess}
                isSanitized={this.props.isSanitized}
            />
        )
    }

    _renderSwiftSection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Bucket Name'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Auth URL'
                    input={{
                        name: 'swift_auth',
                        defaultValue: this.props.data.swift_auth,
                    }}
                    error={this.props.errors.swift_auth}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Tenant'
                    input={{
                        name: 'swift_tenant',
                        defaultValue: this.props.data.swift_tenant,
                    }}
                    error={this.props.errors.swift_tenant}
                    isValid={this.props.verifySuccess}
                />

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='User'
                    input={{
                        name: 'swift_user',
                        defaultValue: this.props.data.swift_user,
                        required: true,
                    }}
                    error={this.props.errors.swift_user}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Password / Key'
                    input={{
                        name: 'swift_key',
                        type: 'password',
                        defaultValue: this.props.data.swift_key,
                        required: true,
                    }}
                    error={this.props.errors.swift_key}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />
            </React.Fragment>
        )
    }

    _renderGCPSection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Bucket Name'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        required: true,
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Project Number'
                    input={{
                        name: 'gcp_project_number',
                        defaultValue: this.props.data.gcp_project_number,
                        required: true,
                    }}
                    error={this.props.errors.gcp_project_number}
                    isValid={this.props.verifySuccess}
                />

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Client ID'
                    input={{
                        name: 'gcp_client_id',
                        defaultValue: this.props.data.gcp_client_id,
                        required: true,
                    }}
                    error={this.props.errors.gcp_client_id}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Service Account Credentials (JSON)'
                    input={{
                        name: 'gcp_service_account_credentials',
                        defaultValue: this.props.data.gcp_service_account_credentials,
                        required: true,
                        type: 'password',
                    }}
                    error={this.props.errors.gcp_service_account_credentials}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />

                <input
                    type="hidden"
                    name='gcp_object_acl'
                    value='private'
                />

                <input
                    type="hidden"
                    name='gcp_bucket_acl'
                    value='private'
                />
            </React.Fragment>
        )
    }

    _renderSFTPSection(subtype) {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Host'
                    input={{
                        name: 'sftp_host',
                        defaultValue: this.props.data.sftp_host,
                        required: true,
                    }}
                    error={this.props.errors.sftp_host}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Port'
                    input={{
                        name: 'sftp_port',
                        defaultValue: this.props.data.sftp_port,
                        required: true,
                    }}
                    error={this.props.errors.sftp_port}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Initial Path'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        placeholder: '/',
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Username'
                    input={{
                        name: 'sftp_user',
                        defaultValue: this.props.data.sftp_user,
                        required: true,
                    }}
                    error={this.props.errors.sftp_user}
                    isValid={this.props.verifySuccess}
                />

                <div className="row form-group">
                    <div className="col-4 text-right control-label">
                        <b className='form-label'>Connection Protocol</b>
                    </div>
                    <div className="col-8">
                        <select
                            className="form-control"
                            name="subtype"
                            value={subtype}
                            onChange={(event => this.setState({subtype: event.target.value}))}
                        >
                            {SFTP_CONNECTION_TYPES.map(d => (
                                <option
                                    key={d.value}
                                    value={d.value}
                                >{d.label}</option>
                            ))}
                        </select>
                    </div>
                </div>

                {subtype === 'password' && this._renderSFTPPasswordSubsection()}
                {subtype === 'key' && this._renderSFTPKeySubsection()}


            </React.Fragment>
        )
    }

    _renderSFTPPasswordSubsection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Password'
                    input={{
                        name: 'sftp_pass',
                        defaultValue: this.props.data.sftp_pass,
                        type: 'password',
                        required: true,
                    }}
                    error={this.props.errors.sftp_pass}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />
            </React.Fragment>
        )
    }

    _renderSFTPKeySubsection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='SSH Private Key Path'
                    input={{
                        name: 'sftp_key_file',
                        defaultValue: this.props.data.sftp_key_file,
                        required: true,
                    }}
                    error={this.props.errors.sftp_key_file}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />
            </React.Fragment>
        )
    }


    _renderDropboxSection() {
        return (
            <React.Fragment>
                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Token'
                    input={{
                        name: 'dropbox_token',
                        defaultValue: this.props.data.dropbox_token,
                        required: true,
                        type: 'password',
                    }}
                    error={this.props.errors.dropbox_token}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />


                <details>
                    <summary className='text-primary h5 mt-5 mb-2'>
                        Instructions
                    </summary>

                    <ul>
                        <li className='mb-1'>
                            Open a terminal
                        </li>
                        <li className='mb-1'>
                            Paste the following command
                        </li>
                        <li className='mb-1'>
                            <pre className='mb-1'>
                                rclone authorize "dropbox"
                            </pre>
                        </li>
                        <li className='mb-1'>
                            Authorize rclone on Dropbox
                        </li>
                        <li className='mb-1'>
                            Copy the token and paste it in the box above. The token should be of the form
                        </li>
                        <li className='mb-1'>
                            <pre>
                                {"{"}"access_token":"HdysS-asdAKDJSLAKDJASDKAJWEKADJSALDKajsldkjwdoiasjdiasdasdas_dt3","token_type":"bearer","expiry":"0001-01-01T00:00:00Z"{"}"}
                            </pre>
                        </li>
                    </ul>
                </details>
            </React.Fragment>
        )
    }

    _renderOnedriveSection() {
        const manualFields = this._renderOnedriveManualFields();
        if (this.props.isSanitized) { // Editing an existing connection
            // The token broker refreshes the token with the app registration that issued it
            const clientId = this.props.data.onedrive_client_id;
            const app = !clientId || clientId === RCLONE_ONEDRIVE_CLIENT_ID
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
                <OnedriveSignIn
                    name={this._name()}
                    onNameChange={name => this.setState({name})}
                />
                <details
                    open={this.state.onedriveManual}
                    onToggle={event => {
                        // React also passes on the toggle of the nested "How to get these values"
                        if (event.target === event.currentTarget) {
                            this.setState({onedriveManual: event.target.open});
                        }
                    }}
                >
                    <summary className='text-primary h5 mt-2 mb-3'>
                        Advanced: paste a token from rclone instead
                    </summary>
                    {manualFields}
                </details>
            </React.Fragment>
        )
    }

    _renderOnedriveManualFields() {
        return (
            <React.Fragment>
                <details open={!this.props.isSanitized}>
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
                            Answer: <tt>n</tt> (new remote), name <tt>onedrive</tt>, storage <tt>onedrive</tt>.
                            Leave <tt>client_id</tt> and <tt>client_secret</tt> empty, region <tt>1</tt> (global),
                            press Enter for any other question and <tt>n</tt> for advanced config.
                        </li>
                        <li className='mb-1'>
                            Answer <tt>y</tt> to authenticate in the web browser and sign in with your
                            university account.
                        </li>
                        <li className='mb-1'>
                            Choose <i>OneDrive Personal or Business</i> (or a SharePoint site), pick your drive,
                            confirm with <tt>y</tt>, keep the remote with <tt>y</tt> and quit with <tt>q</tt>.
                        </li>
                        <li className='mb-1'>
                            Show the values to paste below:
                            <pre className='mb-1'>rclone config show onedrive</pre>
                            It prints <tt>drive_id</tt>, <tt>drive_type</tt> and a <tt>token</tt> line.
                            Paste everything after <tt>token = </tt>, from <tt>{'{'}</tt> to <tt>{'}'}</tt>,
                            including the <tt>refresh_token</tt>.
                        </li>
                    </ol>
                    <p className='text-muted'>
                        Motuz keeps the token refreshed, so it only needs to be pasted once. If the sign-in
                        page asks for admin approval, your institution does not allow rclone yet.
                    </p>
                </details>

                <CloudConnectionField
                    label='Drive ID'
                    input={{
                        name: 'onedrive_drive_id',
                        defaultValue: this.props.data.onedrive_drive_id,
                        required: true,
                        placeholder: 'drive_id, e.g. b!AbCd...',
                    }}
                    error={this.props.errors.onedrive_drive_id}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Drive Type'
                    input={{required: true}}
                    error={this.props.errors.onedrive_drive_type}
                >
                    <select
                        className="form-control"
                        name="onedrive_drive_type"
                        defaultValue={this.props.data.onedrive_drive_type || 'business'}
                    >
                        <option value='business'>business (OneDrive for work or school)</option>
                        <option value='personal'>personal (OneDrive Personal)</option>
                        <option value='documentLibrary'>documentLibrary (SharePoint)</option>
                    </select>
                </CloudConnectionField>

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Token'
                    input={{
                        name: 'onedrive_token',
                        defaultValue: this.props.data.onedrive_token,
                        // When editing, the stored token is kept unless a new one is pasted
                        required: !this.props.isSanitized,
                        type: 'password',
                        placeholder: this.props.isSanitized
                            ? 'Leave empty to keep the stored token'
                            : '{"access_token":"...","refresh_token":"...",...}',
                    }}
                    error={this.props.errors.onedrive_token}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />
            </React.Fragment>
        )
    }

    _renderGdriveSection() {
        return (
            <GdriveSection
                data={this.props.data}
                errors={this.props.errors}
                verifySuccess={this.props.verifySuccess}
                isSanitized={this.props.isSanitized}
                Field={CloudConnectionField}
            />
        )
    }

    _renderWebdavSection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Url'
                    input={{
                        name: 'webdav_url',
                        defaultValue: this.props.data.webdav_url,
                        required: true,
                    }}
                    error={this.props.errors.webdav_url}
                    isValid={this.props.verifySuccess}
                />

                <h5 className='text-primary mt-5 mb-2'>Credentials</h5>

                <CloudConnectionField
                    label='Username'
                    input={{
                        name: 'webdav_user',
                        defaultValue: this.props.data.webdav_user,
                        required: true,
                    }}
                    error={this.props.errors.webdav_user}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Password'
                    input={{
                        name: 'webdav_pass',
                        defaultValue: this.props.data.webdav_pass,
                        type: 'password',
                        required: true,
                    }}
                    error={this.props.errors.webdav_pass}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />
            </React.Fragment>
        )
    }

    onTypeChange(type) {
        let {subtype} = this.state
        if (type === 'sftp') {
            subtype = 'password'
        } else if (type === 'azureblob') {
            subtype = 'key'
        } else if (type === 's3') {
            subtype = 'key'
        }

        this.setState({type, subtype, profile: null})
    }

}

CloudConnectionDialogFields.defaultProps = {
    onSignInChange: (signIn) => {},
    verifySuccess: false,
    data: {},
    isSanitized: false,
}

CloudConnectionDialogFields.initialState = {
    type: '',
    subtype: '',
    profile: null, // picked from LocalCredentialPicker
    name: null, // Connection Name as edited, null = data.name
    onedriveManual: false, // "Advanced: paste a token" is open
}


class CloudConnectionField extends React.PureComponent {
    render() {
        const {
            label,
            input,
            error,
            isValid,
            isSanitized,
        } = this.props;

        return (
            <div className={`row form-group ${input.required && 'required'}`}>
                <div className="col-4 text-right control-label">
                    <b className='form-label'>{label}</b>
                </div>
                <div className="col-8">
                    {this.props.children || (
                        <input
                            type="text"
                            className={classnames({
                                'form-control': true,
                                'is-valid': isValid,
                                'is-invalid': error,
                            })}
                            autoComplete='off'
                            placeholder={isSanitized ? '**********' : null}
                            {...this.props.input}
                        />
                    )}
                    <span className="invalid-feedback">
                        {error}
                    </span>
                </div>
            </div>
        )
    }
}

CloudConnectionField.defaultProps = {
    isSanitized: false,
}


export default CloudConnectionDialogFields;
