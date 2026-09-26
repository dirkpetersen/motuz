import React from 'react';

class StatusBar extends React.Component {
    constructor(props) {
        super(props);
    }

    render() {
        return (
            <div className="status-bar">
                <span>&copy; Fred Hutchinson Cancer Research Center</span>
                {/* Server-side pages (views/legal_views.py): a new tab keeps the app's state */}
                <nav className="status-bar-links">
                    <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a>
                    <a href="/terms" target="_blank" rel="noopener">Terms of Service</a>
                </nav>
            </div>
        );
    }

    componentDidMount() {

    }
}

StatusBar.defaultProps = {

}

import {connect} from 'react-redux';

const mapStateToProps = state => ({
});

const mapDispatchToProps = dispatch => ({
});

export default connect(mapStateToProps, mapDispatchToProps)(StatusBar);
