import React from 'react';
import classnames from 'classnames';

import Icon from 'components/Icon.jsx'
import formatBytes from 'utils/formatBytes.jsx'


class PaneFile extends React.Component {
    constructor(props) {
        super(props);
    }

    render() {
        const {type, name, size, useSiUnits} = this.props;

        // Both grid cells of the row share the handlers, so the row can be
        // clicked, dragged and dropped onto from either cell.
        const rowProps = {
            'data-file-index': this.props.index,
            draggable: this.props.draggable,
            onClick: event => this.props.onClick(event),
            onDoubleClick: event => this.props.onDoubleClick(event),
            onMouseDown: event => this.props.onMouseDown(event),
            onDragStart: event => this.props.onDragStart(event),
        }

        return (
            <React.Fragment>
                <div
                    className={classnames({
                        'grid-file-row': true,
                        'active': this.props.active,
                        'drop-target': this.props.dropTarget,
                    })}
                    style={{paddingLeft: "10px"}}
                    {...rowProps}
                >
                    <Icon
                        name={type === 'dir' ? 'file-directory' : 'file'}
                        className='mr-2 octicon'
                    />
                    <span>{name}</span>
                </div>
                <div
                    className={classnames({
                        'text-right': true,
                        'grid-file-row': true,
                        'active': this.props.active,
                        'drop-target': this.props.dropTarget,
                        'pr-2': true,
                    })}
                    {...rowProps}
                >
                    <em>
                        {type === 'dir' ? 'Folder' : formatBytes(size, useSiUnits)}
                    </em>
                </div>
            </React.Fragment>
        );
    }

    componentDidMount() {

    }
}

PaneFile.defaultProps = {
    index: 0,
    type: 'dir',
    name: '',
    size: 0,
    useSiUnits: false,
    draggable: false,
    dropTarget: false,
    onClick: event => {},
    onDoubleClick: event => {},
    onMouseDown: event => {},
    onDragStart: event => {},
}

export default PaneFile;
