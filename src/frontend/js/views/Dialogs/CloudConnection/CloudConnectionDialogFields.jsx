import OauthSignIn, { oauthProviderForType } from 'views/Dialogs/CloudConnection/OauthSignIn.jsx';
import GdriveSection from 'views/Dialogs/CloudConnection/GdriveSection.jsx';
import LocalCredentialPicker, { describeProfile, profileKey } from 'views/Dialogs/CloudConnection/LocalCredentialPicker.jsx';
import React from 'react';
import classnames from 'classnames';

// rclone's public OneDrive app (oauth_manager.RCLONE_CLIENT_ID)
const RCLONE_ONEDRIVE_CLIENT_ID = 'b15665d9-eda6-4092-8539-0eec376afd59';

const CONNECTION_TYPES = [
    {
        label: 'Amazon S3 (or S3 compatible)',
        value: 's3',
    },
    {
        label: 'Azure Blob Storage',
        value: 'azureblob',
    },
    {
        label: 'Google Cloud Storage',
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
        label: 'Microsoft OneDrive / SharePoint (beta)',
        value: 'onedrive',
    },
    {
        label: 'Google Drive',
        value: 'drive',
    },
]

const S3_CONNECTION_TYPES = [
    {
        label: 'Access key',
        value: 'key'
    },
    {
        label: 'Temporary credentials (STS session token)',
        value: 'sts'
    }
]

const AZURE_CONNECTION_TYPES = [
    {
        label: 'Storage account and key',
        value: 'key',
    },
    {
        label: 'Shared Access Signature (SAS) URL',
        value: 'sas',
    },
]

const SFTP_CONNECTION_TYPES = [
    {
        label: 'Password',
        value: 'password',
    },
    {
        label: 'SSH private key',
        value: 'key',
    },
]

// Subtype of a type when the connection does not have one yet
const DEFAULT_SUBTYPES = {
    s3: 'key',
    azureblob: 'key',
    sftp: 'password',
}


/**
 * The fields of the New and Edit Cloud Connection dialogs, top to bottom: Type,
 * Connection Name, the type's own part (sign-in, credentials found in the home
 * directory, or credentials) and its advanced options, collapsed.
 *
 * For OneDrive and Google Drive a new connection is made by signing in (OauthSignIn):
 * onModeChange tells the dialog which provider's sign-in is shown (or null), and
 * onOauthChange / onOauthError what the sign-in reports, so that the dialog's footer
 * button creates the connection.
 */
class CloudConnectionDialogFields extends React.Component {
    constructor(props) {
        super(props);
        this.state = CloudConnectionDialogFields.initialState;
    }

    render() {
        const { data } = this.props;
        const type = this._type();
        const subtype = this.state.subtype || data.subtype || DEFAULT_SUBTYPES[type];

        return (
            <div className="container">
                <input type="hidden" name='id' value={data.id}/>

                <div className="row mb-3 required">
                    <div className="col-4 text-end control-label">
                        <b className='form-label'>Type</b>
                    </div>
                    <div className="col-8">
                        <select
                            className="form-select"
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
                        placeholder: 'Your name for this connection',
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
                {type === 'onedrive' && this._renderOauthSection('onedrive', this._renderOnedriveManualFields())}
                {type === 'drive' && this._renderOauthSection('gdrive', this._renderGdriveManualFields())}
                {type === 'webdav' && this._renderWebdavSection()}
            </div>
        );
    }

    componentDidMount() {
        this._reportMode();
    }

    componentDidUpdate() {
        this._reportMode();
    }

    _type() {
        return this.state.type || this.props.data.type || 's3';
    }

    _name() {
        return this.state.name === null ? (this.props.data.name || '') : this.state.name;
    }

    // A name suggested by the sign-in (the chosen drive) replaces the Connection Name
    // until the user types their own
    _suggestName(name) {
        const current = this._name().trim();
        if (!current || current === this.state.autoName) {
            this.setState({name, autoName: name});
        }
    }

    // The provider whose "Sign in" makes the new connection, or null when the dialog
    // creates it from the fields (other types, editing, "Advanced: paste a token")
    _signInProvider() {
        if (this.props.isSanitized || this.state.oauthManual) {
            return null;
        }
        return oauthProviderForType(this._type()) || null;
    }

    _reportMode() {
        const provider = this._signInProvider();
        if (provider !== this._reportedProvider) {
            this._reportedProvider = provider;
            this.props.onModeChange(provider);
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
                    <div className='row mb-3'>
                        <div className='col-4 text-end control-label'>
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

    _renderSubtypeSelect(label, options, subtype) {
        return (
            <div className="row mb-3 required">
                <div className="col-4 text-end control-label">
                    <b className='form-label'>{label}</b>
                </div>
                <div className="col-8">
                    <select
                        className="form-select"
                        name="subtype"
                        value={subtype}
                        onChange={(event => this.setState({subtype: event.target.value}))}
                    >
                        {options.map(d => (
                            <option
                                key={d.value}
                                value={d.value}
                            >{d.label}</option>
                        ))}
                    </select>
                </div>
            </div>
        );
    }

    _renderAdvanced(children, label='Advanced options') {
        return (
            <details className='mt-3 advanced-options'>
                <summary className='text-muted mb-3'>{label}</summary>
                {children}
            </details>
        );
    }

    _renderS3Section(subtype) {
        const profile = this._profile();
        const pickedRegion = this.state.profile && this.state.profile.region;
        return (
            <React.Fragment>
                {this._renderProfilePicker('s3')}
                {profile
                    ? this._renderProfileInputs(profile)
                    : this._renderS3Credentials(subtype)
                }
                <CloudConnectionField
                    label='Bucket'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        title: "Must be a valid AWS S3 bucket name (or left blank)",
                        pattern: "(?=.{3,63}$)(?!xn--)[a-z0-9][a-z0-9\\-]*[a-z0-9]",
                        placeholder: 'Optional, e.g. my-lab-bucket (empty: all your buckets)',
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
                        placeholder: 'e.g. us-west-2',
                        // this regex works in python but not js, and it's not futureproof....
                        // pattern: "^(us(-gov)?|ap|ca|cn|eu|sa)-(central|(north|south)?(east|west)?)-\d$",
                    }}
                    error={this.props.errors.s3_region}
                    isValid={this.props.verifySuccess}
                />

                {this._renderAdvanced(
                    <React.Fragment>
                        <CloudConnectionField
                            label='KMS Encryption Key ARN'
                            input={{
                                name: 'kms_encryption_key_arn',
                                defaultValue: this.props.data.kms_encryption_key_arn,
                                placeholder: 'Leave empty unless your bucket requires one',
                                title: "If you are not sure what this is, leave it blank.\nIf you run into errors, search for\n`motuz KMS` on sciwiki.fredhutch.org,\nand email `scicomp` if you still have issues.",
                            }}
                            error={this.props.errors.kms_encryption_key_arn}
                            isValid={this.props.verifySuccess}
                        />
                        <CloudConnectionField
                            label='Endpoint URL'
                            input={{
                                name: 's3_endpoint',
                                defaultValue: this.props.data.s3_endpoint,
                                placeholder: 'Only for S3 compatible storage (not AWS)',
                            }}
                            error={this.props.errors.s3_endpoint}
                            isValid={this.props.verifySuccess}
                        />
                    </React.Fragment>
                )}
            </React.Fragment>
        )
    }

    _renderS3Credentials(subtype) {
        return (
            <React.Fragment>
                {this._renderSubtypeSelect('Key Type', S3_CONNECTION_TYPES, subtype)}

                <CloudConnectionField
                    label='Access Key ID'
                    input={{
                        name: 's3_access_key_id',
                        defaultValue: this.props.data.s3_access_key_id,
                        title: "Must be a valid AWS Access Key ID (20 characters, usually starts with 'AKIA').",
                        required: true,
                        maxLength: 20,
                        pattern: "[A-Z0-9]{20}",
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
                        pattern: "[A-Za-z0-9\\/+=]{40}"
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
                            pattern: "[A-Za-z0-9\\/+=]*"
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

    _renderAzureContainer() {
        return (
            <CloudConnectionField
                label='Container'
                input={{
                    name: 'bucket',
                    defaultValue: this.props.data.bucket,
                    placeholder: 'Optional, e.g. mycontainer (empty: all containers)',
                }}
                error={this.props.errors.bucket}
                isValid={this.props.verifySuccess}
            />
        );
    }

    _renderAzureProfileSubsection(profile) {
        return (
            <React.Fragment>
                {this._renderProfileInputs(profile)}
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
                {this._renderAzureContainer()}
            </React.Fragment>
        )
    }

    _renderAzureManualSubsection(subtype) {
        return (
            <React.Fragment>
                {this._renderSubtypeSelect('Authentication', AZURE_CONNECTION_TYPES, subtype)}
                {subtype === 'key' && this._renderAzureKeySubsection()}
                {subtype === 'sas' && this._renderAzureSasSubsection()}
            </React.Fragment>
        )
    }

    _renderAzureKeySubsection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Storage Account'
                    input={{
                        name: 'azure_account',
                        defaultValue: this.props.data.azure_account,
                        required: true,
                        placeholder: 'mystorageaccount',
                    }}
                    error={this.props.errors.azure_account}
                    isValid={this.props.verifySuccess}
                />

                <CloudConnectionField
                    label='Account Key'
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

                {this._renderAzureContainer()}
            </React.Fragment>
        )
    }

    _renderAzureSasSubsection() {
        return (
            <CloudConnectionField
                label='SAS URL'
                input={{
                    name: 'azure_sas_url',
                    defaultValue: this.props.data.azure_sas_url,
                    placeholder: this.props.isSanitized ? '**********' : 'https://account.blob.core.windows.net/container?sv=...',
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

                <CloudConnectionField
                    label='Container'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        placeholder: 'Optional (empty: all containers)',
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />
            </React.Fragment>
        )
    }

    _renderGCPSection() {
        return (
            <React.Fragment>
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

                <CloudConnectionField
                    label='Bucket'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        required: true,
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
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
                        placeholder: 'e.g. sftp.example.org',
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
                    label='Username'
                    input={{
                        name: 'sftp_user',
                        defaultValue: this.props.data.sftp_user,
                        required: true,
                    }}
                    error={this.props.errors.sftp_user}
                    isValid={this.props.verifySuccess}
                />

                {this._renderSubtypeSelect('Authentication', SFTP_CONNECTION_TYPES, subtype)}

                {subtype === 'password' && this._renderSFTPPasswordSubsection()}
                {subtype === 'key' && this._renderSFTPKeySubsection()}

                <CloudConnectionField
                    label='Initial Path'
                    input={{
                        name: 'bucket',
                        defaultValue: this.props.data.bucket,
                        placeholder: 'Optional, e.g. /data (empty: /)',
                    }}
                    error={this.props.errors.bucket}
                    isValid={this.props.verifySuccess}
                />
            </React.Fragment>
        )
    }

    _renderSFTPPasswordSubsection() {
        return (
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
        )
    }

    _renderSFTPKeySubsection() {
        return (
            <CloudConnectionField
                label='SSH Private Key Path'
                input={{
                    name: 'sftp_key_file',
                    defaultValue: this.props.data.sftp_key_file,
                    required: true,
                    placeholder: 'e.g. /home/you/.ssh/id_ed25519',
                }}
                error={this.props.errors.sftp_key_file}
                isValid={this.props.verifySuccess}
                isSanitized={this.props.isSanitized}
            />
        )
    }


    _renderDropboxSection() {
        return (
            <React.Fragment>
                <CloudConnectionField
                    label='Token'
                    input={{
                        name: 'dropbox_token',
                        defaultValue: this.props.data.dropbox_token,
                        required: true,
                        type: 'password',
                        placeholder: this.props.isSanitized ? '**********' : '{"access_token":"...","token_type":"bearer",...}',
                    }}
                    error={this.props.errors.dropbox_token}
                    isValid={this.props.verifySuccess}
                    isSanitized={this.props.isSanitized}
                />

                {this._renderAdvanced(
                    <ol>
                        <li className='mb-1'>
                            On your own computer, install
                            {' '}<a href='https://rclone.org/install/' target='_blank' rel='noopener noreferrer'>rclone</a>{' '}
                            and run
                            <pre className='mb-1'>rclone authorize "dropbox"</pre>
                        </li>
                        <li className='mb-1'>
                            Allow rclone to access your Dropbox in the browser.
                        </li>
                        <li className='mb-1'>
                            Paste the token rclone prints into <i>Token</i> above. It looks like
                            <pre className='mb-1'>
                                {"{"}"access_token":"HdysS-asd...dt3","token_type":"bearer","expiry":"0001-01-01T00:00:00Z"{"}"}
                            </pre>
                        </li>
                    </ol>,
                    'How to get a token'
                )}
            </React.Fragment>
        )
    }

    // OneDrive and Google Drive. New connection: "Sign in with ...", or (collapsed) the
    // token pasted from `rclone config`. Editing: the pasted-token fields, collapsed.
    _renderOauthSection(provider, manualFields) {
        if (this.props.isSanitized) {
            return this._renderOauthEdit(provider, manualFields);
        }
        const manual = this.state.oauthManual;
        return (
            <React.Fragment>
                {/* Hidden, not removed, while pasting a token: closing Advanced returns to the sign-in */}
                <div className={manual ? 'd-none' : ''}>
                    <OauthSignIn
                        key={provider}
                        provider={provider}
                        onChange={this.props.onOauthChange}
                        onError={this.props.onOauthError}
                        onSuggestName={name => this._suggestName(name)}
                    />
                </div>
                <details
                    className='mt-3 oauth-manual'
                    open={manual}
                    onToggle={event => {
                        // React also passes on the toggle of a nested <details>
                        if (event.target === event.currentTarget && event.target.open !== manual) {
                            this.setState({oauthManual: event.target.open});
                        }
                    }}
                >
                    <summary className='text-muted mb-3'>
                        {manual
                            ? 'Advanced: paste a token from rclone (close to sign in instead)'
                            : 'Advanced: paste a token from rclone instead of signing in'}
                    </summary>
                    {/* Only while open: its required fields must not block the sign-in */}
                    {manual && manualFields}
                </details>
            </React.Fragment>
        );
    }

    _renderOauthEdit(provider, manualFields) {
        const data = this.props.data;
        const clientId = provider === 'onedrive' ? data.onedrive_client_id : data.gdrive_client_id;
        const rcloneClientId = provider === 'onedrive' ? RCLONE_ONEDRIVE_CLIENT_ID : GdriveSection.RCLONE_CLIENT_ID;
        // The token broker refreshes the token with the app that issued it
        const app = !clientId || clientId === rcloneClientId ? "rclone's app" : `Motuz app (${clientId})`;
        const service = provider === 'onedrive' ? 'Microsoft' : 'Google';
        return (
            <React.Fragment>
                <p className='text-muted'>
                    To use another {service} account or drive, create a new connection and sign in again.
                </p>
                {this._renderAdvanced(
                    <React.Fragment>
                        <p className='text-muted small'>
                            Signed in through: {app}. Pasting a new token from rclone switches
                            this connection to rclone's app.
                        </p>
                        {manualFields}
                    </React.Fragment>,
                    'Advanced: drive and token'
                )}
            </React.Fragment>
        );
    }

    _renderOnedriveManualFields() {
        return (
            <React.Fragment>
                <details className='mb-3'>
                    <summary className='text-primary mb-2'>
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
                        className="form-select"
                        name="onedrive_drive_type"
                        defaultValue={this.props.data.onedrive_drive_type || 'business'}
                    >
                        <option value='business'>business (OneDrive for work or school)</option>
                        <option value='personal'>personal (OneDrive Personal)</option>
                        <option value='documentLibrary'>documentLibrary (SharePoint)</option>
                    </select>
                </CloudConnectionField>

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

    _renderGdriveManualFields() {
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
                    label='URL'
                    input={{
                        name: 'webdav_url',
                        defaultValue: this.props.data.webdav_url,
                        required: true,
                        placeholder: 'https://webdav.example.org/remote.php/webdav/',
                    }}
                    error={this.props.errors.webdav_url}
                    isValid={this.props.verifySuccess}
                />

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
        const name = this._name();
        this.setState({
            type,
            subtype: DEFAULT_SUBTYPES[type] || '',
            profile: null,
            oauthManual: false,
            // A drive name suggested by a sign-in does not fit another type
            name: name && name === this.state.autoName ? '' : this.state.name,
            autoName: null,
        })
    }

}

CloudConnectionDialogFields.defaultProps = {
    onModeChange: (provider) => {},
    onOauthChange: (signIn) => {},
    onOauthError: (message) => {},
    verifySuccess: false,
    data: {},
    errors: {},
    isSanitized: false,
}

CloudConnectionDialogFields.initialState = {
    type: '',
    subtype: '',
    profile: null, // picked from LocalCredentialPicker
    name: null, // Connection Name as edited, null = data.name
    autoName: null, // the name the sign-in suggested (the chosen drive)
    oauthManual: false, // OneDrive / Google Drive: "Advanced: paste a token" is open
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
            <div className={`row mb-3 ${input.required ? 'required' : ''}`}>
                <div className="col-4 text-end control-label">
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
                            aria-label={label}
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
