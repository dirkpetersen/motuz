import React from 'react';

import Icon from 'components/Icon.jsx'
import { VERIFY_ATTEMPTS } from 'actions/apiActions.jsx'


// Result of "Test connection", next to the button. Why a test failed is shown by
// DialogError above the dialog's buttons.
class VerifyStatusButton extends React.PureComponent {
    render() {
        const {loading, success, attempt, willRetry} = this.props;

        if (loading) {
            return (
                <span className='ms-2 text-muted verify-status'>
                    {attempt > 1 ? `Testing... (attempt ${attempt} of ${VERIFY_ATTEMPTS})` : 'Testing...'}
                </span>
            );
        }
        if (success == null) {
            return null;
        }
        if (!success && willRetry) {
            return (
                <span className='ms-2 text-warning verify-status'>
                    <Icon name='x'/> Attempt {attempt} of {VERIFY_ATTEMPTS} failed, trying again...
                </span>
            );
        }
        if (success) {
            return (
                <span className='ms-2 text-success verify-status'>
                    <Icon name='check'/> Connection works
                </span>
            );
        }
        return (
            <span className='ms-2 text-danger verify-status'>
                <Icon name='x'/> Test failed
            </span>
        );
    }
}

VerifyStatusButton.defaultProps = {
    loading: false,
    success: null,
}


// The one place of a connection dialog for errors: sign-in, create, save and test
export class DialogError extends React.PureComponent {
    render() {
        const {message} = this.props;
        if (!message) {
            return null;
        }
        return (
            <div className='alert alert-danger w-100 mb-2 dialog-error' role='alert' style={{wordBreak: 'break-word'}}>
                {message.length > 400 ? '...' + message.slice(-400) : message}
            </div>
        );
    }
}

// The error to show for the dialog's own error (sign-in, connect), the last failed
// save (state.api.cloudErrorMessage) or a failed test
export const dialogErrorMessage = (ownError, cloudErrorMessage, verification) => {
    if (ownError) {
        return ownError;
    }
    if (cloudErrorMessage) {
        return cloudErrorMessage;
    }
    if (verification && verification.message && verification.success !== true) {
        if (verification.willRetry || verification.loading) {
            // shown at once; the test goes on (VerifyStatusButton shows the attempt)
            return 'Attempt failed: ' + verification.message;
        }
        return 'The test failed: ' + verification.message;
    }
    return null;
};

export default VerifyStatusButton;
