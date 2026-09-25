import React from 'react';
import { Button } from 'react-bootstrap'

import Icon from 'components/Icon.jsx'


class VerifyStatusButton extends React.PureComponent {
    render() {
        const {loading, success, message} = this.props;

        if (loading) {
            return (
                <Button variant='outline-warning' className='ml-2' disabled>
                    <span> Verifying... </span>
                </Button>
            )
        }

        if (success == null) {
            return <div></div>
        }

        if (success) {
            return (
                <Button variant='outline-success' className='ml-2' disabled>
                    <Icon name='check'/>
                    <span> Correct </span>
                </Button>
            )
        }

        if (!success) {
            return (
                <React.Fragment>
                    <Button variant='outline-danger' className='ml-2' disabled title={message || ''}>
                        <Icon name='x'/>
                        <span> Incorrect </span>
                    </Button>
                    {message &&
                        <div className='text-danger small mt-1 verify-message' style={{maxWidth: '28em', wordBreak: 'break-word'}}>
                            {message.length > 300 ? '...' + message.slice(-300) : message}
                        </div>
                    }
                </React.Fragment>
            )
        }
    }
}

VerifyStatusButton.defaultProps = {
    loading: false,
    success: null,
    message: null,
}

export default VerifyStatusButton;
