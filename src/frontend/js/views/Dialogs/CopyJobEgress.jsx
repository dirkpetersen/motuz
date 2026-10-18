import React from 'react';
import { Spinner } from 'react-bootstrap'

import { confirmationPayload, describeEgress, needsConfirmation } from 'utils/egressCost.js'


function errorMessage(payload) {
    const response = (payload && payload.response) || {};
    if (typeof response.message === 'string' && response.message) {
        return response.message;
    }
    return (payload && payload.message) || 'the request failed';
}


/**
 * The AWS egress cost note of the New Copy Job and New Hashsum Job dialogs: fetches the
 * estimate (GET /api/<jobType>/egress-estimate/) when the dialog opens, shows it and a
 * required checkbox for a job out of AWS. Tells the dialog through
 * onChange({ready, payload}): `ready` is false while the size is measured and until the
 * cost is confirmed, `payload` is the request's `egress_estimate` (null: none needed).
 * The server refuses a job out of AWS without it.
 */
class CopyJobEgress extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            loading: false,
            estimate: null,
            error: null,
            confirmed: false,
        };
    }

    componentDidMount() {
        this.update();
    }

    componentDidUpdate(prevProps) {
        if (prevProps.enabled !== this.props.enabled) {
            this.update();
        }
    }

    componentWillUnmount() {
        this.unmounted = true;
    }

    update() {
        if (!this.props.enabled) {
            this.report({loading: false, estimate: null, confirmed: false});
            return;
        }
        if (this.state.estimate || this.state.loading) {
            this.report(this.state);
            return;
        }
        this.load();
    }

    async load() {
        const {jobType, srcCloudId, srcPaths, dstCloudId, dstPaths} = this.props;
        this.setState({loading: true, error: null});
        this.report({loading: true, estimate: null, confirmed: false});
        const action = await this.props.fetchEstimate(jobType, {srcCloudId, srcPaths, dstCloudId, dstPaths});
        if (this.unmounted) {
            return;
        }
        const failed = !action || action.error || !action.payload || typeof action.payload.applies !== 'boolean';
        const state = {
            loading: false,
            estimate: failed ? null : action.payload,
            error: failed ? errorMessage(action && action.payload) : null,
            confirmed: false,
        };
        this.setState(state);
        this.report(state);
    }

    report({loading, estimate, confirmed}) {
        const enabled = this.props.enabled;
        this.props.onChange({
            ready: !enabled || (!loading && (!needsConfirmation(estimate) || confirmed)),
            payload: enabled ? confirmationPayload(estimate, confirmed) : null,
        });
    }

    handleConfirm(confirmed) {
        this.setState({confirmed});
        this.report({...this.state, confirmed});
    }

    render() {
        if (!this.props.enabled) {
            return null;
        }
        const {loading, estimate, error, confirmed} = this.state;
        if (loading) {
            return (
                <div className='egress-estimate text-muted mb-3'>
                    <Spinner animation='border' size='sm' className='me-2' role='status' />
                    Measuring the size…
                </div>
            );
        }
        if (error) {
            return (
                <div className='egress-estimate text-muted mb-3'>
                    Could not check for AWS data transfer fees: {error}
                </div>
            );
        }
        const description = describeEgress(estimate, this.props.action);
        if (!description) {
            return null;
        }
        if (!description.confirmLabel) {
            return <div className='egress-estimate text-muted mb-3'>{description.text}</div>;
        }
        return (
            <div className='egress-estimate alert alert-warning mb-3'>
                <div className='egress-estimate-text mb-2'>{description.text}</div>
                <div className='form-check'>
                    <input
                        id='egress-confirm'
                        name='egress_confirm'
                        type='checkbox'
                        className='form-check-input'
                        required={true}
                        checked={confirmed}
                        onChange={event => this.handleConfirm(event.target.checked)}
                    />
                    <label className='form-check-label fw-bold' htmlFor='egress-confirm'>
                        {description.confirmLabel}
                    </label>
                </div>
            </div>
        );
    }
}

CopyJobEgress.defaultProps = {
    jobType: 'copy-jobs',
    action: 'copy',
    enabled: true,
    srcCloudId: 0,
    srcPaths: [],
    dstCloudId: 0,
    dstPaths: [],
    onChange: () => {},
    fetchEstimate: async () => null,
}

import {connect} from 'react-redux';
import {retrieveEgressEstimate} from 'actions/apiActions.jsx'

const mapDispatchToProps = dispatch => ({
    fetchEstimate: (jobType, params) => dispatch(retrieveEgressEstimate(jobType, params)),
});

export default connect(null, mapDispatchToProps)(CopyJobEgress);
