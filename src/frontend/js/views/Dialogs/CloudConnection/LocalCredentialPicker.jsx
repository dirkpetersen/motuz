import React from 'react';


const SOURCE_LABELS = {
    'aws': 'AWS profile',
    'rclone': 'rclone remote',
    'azure-cli': 'Azure CLI',
};

export const profileKey = profile => profile ? `${profile.source}:${profile.name}` : '';

export const describeProfile = profile => {
    const details = [profile.label, profile.account, profile.region, profile.access_key_id].filter(d => d);
    const source = SOURCE_LABELS[profile.source] || profile.source;
    return `${source}: ${profile.name}` + (details.length ? ` (${details.join(', ')})` : '');
};


/**
 * "Found in your home directory": S3 / Azure credentials that Motuz found in the
 * user's ~/.aws and rclone.conf. The server only sends metadata (masked key ids), and
 * a connection created from a profile keeps reading it from the home directory.
 */
class LocalCredentialPicker extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            profiles: [],
            notes: [],
        };
    }

    componentDidMount() {
        this.load();
    }

    componentDidUpdate(prevProps) {
        if (prevProps.type !== this.props.type) {
            this.setState({profiles: [], notes: []});
            this.load();
        }
    }

    load() {
        const type = this.props.type;
        Promise.resolve(this.props.onLoad(type)).then(action => {
            if (!action || action.error || type !== this.props.type) {
                return;
            }
            this.setState({
                profiles: action.payload.profiles || [],
                notes: action.payload.notes || [],
            });
        });
    }

    render() {
        const {profiles, notes} = this.state;
        if (profiles.length === 0 && notes.length === 0) {
            return null;
        }
        const selected = this.props.selected;
        const unusable = profiles.filter(p => !p.usable);

        return (
            <div className='row mb-3 local-credentials'>
                <div className='col-4 text-end control-label'>
                    <b className='form-label'>Credentials</b>
                </div>
                <div className='col-8'>
                    {profiles.length > 0 &&
                        <select
                            className='form-select'
                            aria-label='Credentials found in your home directory'
                            value={profileKey(selected)}
                            onChange={event => this.handleChange(event.target.value)}
                        >
                            <option value=''>Enter them below</option>
                            <optgroup label='Found in your home directory'>
                                {profiles.map(profile => (
                                    <option
                                        key={profileKey(profile)}
                                        value={profileKey(profile)}
                                        disabled={!profile.usable}
                                    >
                                        {describeProfile(profile)}{profile.usable ? '' : ' - cannot be used'}
                                    </option>
                                ))}
                            </optgroup>
                        </select>
                    }
                    {profiles.length > 0 && !selected &&
                        <small className='form-text text-muted'>
                            {profiles.some(p => p.usable)
                                ? 'Motuz found credentials in your home directory. Choose one, or enter them below.'
                                : 'None of the credentials in your home directory can be used. Enter them below.'}
                        </small>
                    }
                    {selected && selected.source === 'azure-cli' &&
                        <small className='form-text text-muted'>
                            Motuz runs the Azure CLI as you (<tt>az account get-access-token</tt>) each time
                            it uses this connection. Your login stays in <tt>~/.azure</tt>; when it expires,
                            run <tt>az login</tt> again on a cluster node.
                            {selected.note && <React.Fragment><br/>{selected.note}</React.Fragment>}
                        </small>
                    }
                    {selected && selected.source !== 'azure-cli' &&
                        <small className='form-text text-muted'>
                            Motuz reads <tt>{selected.file}</tt> as you each time it uses this connection,
                            so updated keys are picked up. The keys are not copied into Motuz.
                            {selected.note && <React.Fragment><br/>{selected.note}</React.Fragment>}
                        </small>
                    }
                    {(unusable.length > 0 || notes.length > 0) &&
                        <details className='text-muted small mt-1'>
                            <summary>
                                {unusable.length > 0
                                    ? `Why ${unusable.length === 1 ? 'one entry' : unusable.length + ' entries'} cannot be used`
                                    : 'Notes about your home directory'}
                            </summary>
                            <ul className='mt-1 mb-0 ps-3'>
                                {unusable.map(profile => (
                                    <li key={profileKey(profile)}>
                                        <b>{profile.name}</b>: {profile.reason}
                                    </li>
                                ))}
                                {notes.map(note => <li key={note}>{note}</li>)}
                            </ul>
                        </details>
                    }
                </div>
            </div>
        );
    }

    handleChange(key) {
        const profile = this.state.profiles.find(p => profileKey(p) === key && p.usable) || null;
        this.props.onSelect(profile);
    }
}

LocalCredentialPicker.defaultProps = {
    type: 's3',
    selected: null,
    onLoad: (type) => {},
    onSelect: (profile) => {},
};

import {connect} from 'react-redux';
import {listLocalCredentials} from 'actions/apiActions.jsx';

const mapDispatchToProps = dispatch => ({
    onLoad: (type) => dispatch(listLocalCredentials(type)),
});

export default connect(null, mapDispatchToProps)(LocalCredentialPicker);
