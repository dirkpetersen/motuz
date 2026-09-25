import React from 'react';
import { Alert } from 'react-bootstrap'

class Alerts extends React.Component {
    constructor(props) {
        super(props);
    }

    render() {
        if (!this.props.show) {
            return <React.Fragment />
        }

        return (
            <div style={{
                position: 'absolute',
                top: '5rem',
                padding: 30,
                width: '100%',
            }}>
                <Alert
                    variant={this.props.variant}
                    onClose={() => this.props.onDismiss()}
                    dismissible
                >
                    {this.props.notice ? (
                        <React.Fragment>
                            <Alert.Heading>{this.props.heading}</Alert.Heading>
                            <p className='mb-0'>{this.props.text}</p>
                        </React.Fragment>
                    ) : (
                        <React.Fragment>
                            <Alert.Heading>Something went wrong!</Alert.Heading>
                            <pre>
                                {JSON.stringify(this.props.text)}
                            </pre>
                        </React.Fragment>
                    )}
                </Alert>
            </div>
        );
    }

    componentDidMount() {

    }
}

Alerts.defaultProps = {
    show: true,
    text: 'Foo',
    notice: false,
    heading: '',
    variant: 'danger',
    onDismiss: () => {},
}

import {connect} from 'react-redux';
import {hideAlert} from 'actions/alertActions.jsx'

const mapStateToProps = state => ({
    show: state.alert.show,
    text: state.alert.text,
    notice: state.alert.notice,
    heading: state.alert.heading,
    variant: state.alert.variant,
});

const mapDispatchToProps = dispatch => ({
    onDismiss: () => dispatch(hideAlert())
});

export default connect(mapStateToProps, mapDispatchToProps)(Alerts);
