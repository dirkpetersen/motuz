import React from 'react';
import { Modal, Button } from 'react-bootstrap'
import Toggle from 'react-toggle'

import serializeForm from 'utils/serializeForm.jsx'
import {
    CUSTOM, describeSize, estimateMemory, matchPreset, presetValues, toRequest, validatePerformance,
} from 'utils/copyPerformance.js'
import CopyJobPerformance from 'views/Dialogs/CopyJobPerformance.jsx'
import { DialogError } from 'views/Dialogs/CloudConnection/VerifyStatusButton.jsx'

function apiErrorMessage(payload) {
    const response = (payload && payload.response) || {};
    if (typeof response.message === 'string' && response.message) {
        return response.message;
    }
    return (payload && payload.message) || 'The job could not be created';
}

class NewCopyJobDialog extends React.Component {
    constructor(props) {
        super(props);
        this.inputRef = React.createRef()
        this.state = {
            emailNotifications: props.emailNotificationsDefault,
            // Performance section (GET /api/copy-jobs/performance/)
            performanceInfo: null,
            performanceLoadError: false,
            performanceOpen: false,
            performanceValues: {},
            performancePreset: 'default',
            performanceErrors: {},
            submitError: null,
        };
    }

    render() {
        const { data, isLoading } = this.props;

        return (
            <div className='dialog-copy-job'>
                <Modal
                    show={true}
                    size="lg"
                    onHide={() => this.handleClose()}
                >
                    <form action="#" onSubmit={(event) => this.handleSubmit(event)}>
                        <Modal.Header closeButton>
                            <Modal.Title>New Copy Job</Modal.Title>
                        </Modal.Header>
                        <Modal.Body>
                            <div className="container">
                                {data.source_paths.map((sourcePath, i) => (
                                    <React.Fragment key={sourcePath}>
                                        <h5 className="text-primary mb-2">Source</h5>

                                        <div className="row">
                                            <div className="col-4 text-end">
                                                <b className='form-label'>Connection</b>
                                            </div>
                                            <div className="col-7">
                                                <span className="form-label">
                                                    {data['source_cloud'].name}
                                                </span>
                                            </div>
                                            <div className="col-1"></div>
                                        </div>
                                        <div className="row">
                                            <div className="col-4 text-end">
                                                <b className='form-label'>Resource</b>
                                            </div>
                                            <div className="col-7">
                                                <span className="form-label">
                                                    {sourcePath}
                                                </span>
                                            </div>
                                            <div className="col-1"></div>
                                        </div>

                                        <h5 className="text-primary mt-4 mb-2">Destination</h5>

                                        <div className="row">
                                            <div className="col-4 text-end">
                                                <b className='form-label'>Connection</b>
                                            </div>
                                            <div className="col-7">
                                                <span className="form-label">
                                                    {data['destination_cloud'].name}
                                                </span>
                                            </div>
                                            <div className="col-1"></div>
                                        </div>
                                        <div className="row">
                                            <div className="col-4 text-end">
                                                <b className='form-label'>Path</b>
                                            </div>
                                            <div className="col-7">
                                                <span className="form-label">
                                                    {data['destination_paths'][i]}
                                                </span>
                                            </div>
                                            <div className="col-1"></div>
                                        </div>

                                        <hr className='mt-4' />
                                    </React.Fragment>
                                ))}

                                <h5 className='text-primary mb-2'>Details</h5>

                                <div className="row mb-3">
                                    <div className="col-4 text-end">
                                        <b className='form-label'>Owner</b>
                                    </div>
                                    <div className="col-7">
                                        <span className="form-label">
                                            {this.props.username}
                                        </span>
                                    </div>
                                    <div className="col-1"></div>
                                </div>

                                <div className="row mb-3">
                                    <div className="col-4 text-end">
                                        <b className='form-label'>Description</b>
                                    </div>
                                    <div className="col-7">
                                        <input
                                            name="description"
                                            type="text"
                                            className="form-control"
                                            ref={this.inputRef}
                                        />
                                    </div>
                                    <div className="col-1"></div>
                                </div>

                                <details>
                                    <summary className='text-primary h5 mt-5 mb-2'>
                                        Advanced
                                    </summary>

                                    <div className="row mb-3">
                                        <div className="col-4 text-end">
                                            <b className='form-label'>Follow symlinks</b>
                                        </div>
                                        <div className="col-7">
                                            <Toggle
                                                name='copy_links'
                                                className='form-label'
                                                defaultChecked={this.props.followSymlinksDefault}
                                            />
                                        </div>
                                        <div className="col-1"></div>
                                    </div>

                                    <div className="row mb-3">
                                        <div className="col-4 text-end">
                                            <b className='form-label'>Email Notifications</b>
                                        </div>
                                        <div className="col-7">
                                            <Toggle
                                                className='form-label'
                                                defaultChecked={this.props.emailNotificationsDefault}
                                                onChange={(event) => this.setState({emailNotifications: event.target.checked})}
                                            />
                                        </div>
                                        <div className="col-1"></div>
                                    </div>

                                    {this.state.emailNotifications && (
                                        <div className="row mb-3">
                                            <div className="col-4 text-end">
                                                <b className='form-label'>Email Address</b>
                                            </div>
                                            <div className="col-7">
                                                <input
                                                    name="notification_email"
                                                    type="email"
                                                    className="form-control"
                                                    required={true}
                                                    defaultValue={this.props.emailAddressDefault}
                                                />
                                            </div>
                                            <div className="col-1"></div>
                                        </div>
                                    )}
                                </details>

                                <CopyJobPerformance
                                    info={this.state.performanceInfo}
                                    loadError={this.state.performanceLoadError}
                                    open={this.state.performanceOpen}
                                    values={this.state.performanceValues}
                                    preset={this.state.performancePreset}
                                    errors={this.state.performanceErrors}
                                    onToggle={open => this.setState({performanceOpen: open})}
                                    onChange={(name, value) => this.handlePerformanceChange(name, value)}
                                    onPreset={id => this.handlePreset(id)}
                                />

                            </div>
                        </Modal.Body>
                        <Modal.Footer>
                            <DialogError message={this.state.submitError} />
                            <Button variant="secondary" onClick={() => this.handleClose()}>
                                Close
                            </Button>
                            <Button variant="primary" type='submit' disabled={isLoading}>
                                { isLoading ? "Submitting..." : "Submit Copy Job" }
                            </Button>
                        </Modal.Footer>
                    </form>
                </Modal>
            </div>
        );
    }

    handleClose() {
        this.props.onClose();
    }

    handlePerformanceChange(name, value) {
        const info = this.state.performanceInfo;
        const values = {...this.state.performanceValues, [name]: value};
        this.setState({
            performanceValues: values,
            performancePreset: matchPreset(values, info.presets, info.fields),
            performanceErrors: validatePerformance(values, info.fields),
            submitError: null,
        });
    }

    handlePreset(id) {
        const info = this.state.performanceInfo;
        if (id === CUSTOM) {
            this.setState({performancePreset: CUSTOM});
            return;
        }
        const preset = info.presets.find(p => p.id === id);
        this.setState({
            performancePreset: id,
            performanceValues: presetValues(preset, info.fields),
            performanceErrors: {},
            submitError: null,
        });
    }

    /** The `performance` of the request; null for the defaults; false (and shows why) if invalid */
    performanceRequest() {
        const info = this.state.performanceInfo;
        if (!info) {
            return null;
        }
        const values = this.state.performanceValues;
        const errors = validatePerformance(values, info.fields);
        const memory = estimateMemory(values, info);
        let message = null;
        if (Object.keys(errors).length) {
            message = Object.values(errors)[0];
        } else if (memory !== null && memory > info.memory_budget) {
            message = `These settings need about ${describeSize(memory)} of memory, more than the ` +
                `${describeSize(info.memory_budget)} allowed per job. Use fewer transfers, streams or a smaller chunk size.`;
        }
        if (message) {
            this.setState({performanceErrors: errors, performanceOpen: true, submitError: message});
            return false;
        }
        return toRequest(values, info.fields);
    }

    async handleSubmit(event) {
        event.preventDefault();

        const propsData = this.props.data;
        const formData = serializeForm(event.target)
        const performance = this.performanceRequest();
        if (performance === false) {
            return;
        }
        this.setState({submitError: null});

        for (let i in propsData.source_paths) {
            const src_resource_path = propsData.source_paths[i]
            const dst_resource_path = propsData.destination_paths[i]
            const data = {
                "description": formData['description'] || '',
                "copy_links": formData['copy_links'] || false,
                "src_cloud_id": propsData['source_cloud'].id,
                "src_resource_path": src_resource_path,
                "dst_cloud_id": propsData['destination_cloud'].id,
                "dst_resource_path": dst_resource_path,
                "notification_email": formData['notification_email'],
            }
            if (performance) {
                data['performance'] = performance;
            }

            if (data['src_cloud_id'] === 0) {
                delete data['src_cloud_id'];
            }
            if (data['dst_cloud_id'] === 0) {
                delete data['dst_cloud_id'];
            }

            const result = await this.props.onSubmit(data);
            if (result && result.error) {
                if (!this.unmounted) {
                    this.setState({submitError: apiErrorMessage(result.payload)});
                }
                return;
            }
        }
    }

    componentDidMount() {
        this.inputRef.current.focus()
        this.loadPerformance();
    }

    componentWillUnmount() {
        this.unmounted = true;
    }

    async loadPerformance() {
        const {data} = this.props;
        const dstCloudId = data.destination_cloud ? data.destination_cloud.id : 0;
        const action = await this.props.fetchPerformance(dstCloudId || 0);
        if (this.unmounted) {
            return;
        }
        const info = action && !action.error ? action.payload : null;
        if (!info || !Array.isArray(info.fields) || !Array.isArray(info.presets)) {
            this.setState({performanceLoadError: true});
            return;
        }
        // Retry of a job: its own settings
        const values = presetValues({values: data.performance || {}}, info.fields);
        this.setState({
            performanceInfo: info,
            performanceValues: values,
            performancePreset: matchPreset(values, info.presets, info.fields),
            performanceErrors: validatePerformance(values, info.fields),
            performanceOpen: this.state.performanceOpen || Boolean(toRequest(values, info.fields)),
        });
    }
}

NewCopyJobDialog.defaultProps = {
    data: {},
    username: 'ERROR',

    isLoading: false,

    followSymlinksDefault: false,
    emailNotificationsDefault: false,
    emailAddressDefault: "",

    onClose: () => {},
    onSubmit: async (data) => {},
    fetchPerformance: async (dstCloudId) => null,
}

import {connect} from 'react-redux';
import {hideNewCopyJobDialog} from 'actions/dialogActions.jsx'
import {createCopyJob, retrieveCopyJobPerformance} from 'actions/apiActions.jsx'
import { getCurrentUser } from 'reducers/authReducer.jsx';

const mapStateToProps = state => ({
    data: state.dialog.newCopyJobDialogData,
    username: getCurrentUser(state.auth),

    isLoading: state.loaders.createCopyJobLoading,

    followSymlinksDefault: state.settings.followSymlinks,
    emailNotificationsDefault: state.settings.emailNotifications,
    emailAddressDefault: state.settings.emailAddress,
});

const mapDispatchToProps = dispatch => ({
    onClose: () => dispatch(hideNewCopyJobDialog()),
    onSubmit: data => dispatch(createCopyJob(data)),
    fetchPerformance: dstCloudId => dispatch(retrieveCopyJobPerformance(dstCloudId)),
});

export default connect(mapStateToProps, mapDispatchToProps)(NewCopyJobDialog);
