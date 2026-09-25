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
            <div className='card mb-4 local-credentials'>
                <div className='card-body'>
                    <h5 className='card-title text-primary'>Found in your home directory</h5>
                    {profiles.length > 0 &&
                        <select
                            className='form-control'
                            aria-label='Credentials found in your home directory'
                            value={profileKey(selected)}
                            onChange={event => this.handleChange(event.target.value)}
                        >
                            <option value=''>Enter credentials manually</option>
                            {profiles.map(profile => (
                                <option
                                    key={profileKey(profile)}
                                    value={profileKey(profile)}
                                    disabled={!profile.usable}
                                >
                                    {describeProfile(profile)}{profile.usable ? '' : ' - cannot be used'}
                                </option>
                            ))}
                        </select>
                    }
                    {selected && selected.source === 'azure-cli' &&
                        <p className='card-text text-muted small mt-2 mb-0'>
                            Motuz runs the Azure CLI as you (<tt>az account get-access-token</tt>) each time
                            it uses this connection. Your login stays in <tt>~/.azure</tt>; when it expires,
                            run <tt>az login</tt> again on a cluster node.
                            {selected.note && <React.Fragment><br/>{selected.note}</React.Fragment>}
                        </p>
                    }
                    {selected && selected.source !== 'azure-cli' &&
                        <p className='card-text text-muted small mt-2 mb-0'>
                            Motuz reads <tt>{selected.file}</tt> as you each time it uses this connection,
                            so updated keys are picked up. The keys are not copied into Motuz.
                            {selected.note && <React.Fragment><br/>{selected.note}</React.Fragment>}
                        </p>
                    }
                    {(unusable.length > 0 || notes.length > 0) &&
                        <ul className='text-muted small mt-2 mb-0 pl-3'>
                            {unusable.map(profile => (
                                <li key={profileKey(profile)}>
                                    <b>{profile.name}</b>: {profile.reason}
                                </li>
                            ))}
                            {notes.map(note => <li key={note}>{note}</li>)}
                        </ul>
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
