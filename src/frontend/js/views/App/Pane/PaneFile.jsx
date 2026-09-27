import React from 'react';
import classnames from 'classnames';

import Icon from 'components/Icon.jsx'
import formatBytes from 'utils/formatBytes.jsx'
import {formatAge, formatLocalDateTime, UNKNOWN} from 'utils/fileAge.js'


class PaneFile extends React.Component {
    constructor(props) {
        super(props);
    }

    render() {
        const {type, name, size, modified, now, useSiUnits} = this.props;

        // All grid cells of the row share the handlers, so the row can be
        // clicked, dragged and dropped onto from any cell.
        const rowProps = {
            'data-file-index': this.props.index,
            draggable: this.props.draggable,
            onClick: event => this.props.onClick(event),
            onDoubleClick: event => this.props.onDoubleClick(event),
            onMouseDown: event => this.props.onMouseDown(event),
            onDragStart: event => this.props.onDragStart(event),
        }
        const cellClasses = extra => classnames({
            'grid-file-row': true,
            'active': this.props.active,
            'drop-target': this.props.dropTarget,
            ...extra,
        })

        // Placeholder rows (Loading..., ERROR) have no type, '..' no time
        const isPlaceholder = !type
        let sizeText = ''
        if (type === 'dir') {
            sizeText = 'Folder'
        } else if (!isPlaceholder) {
            sizeText = typeof size === 'number' ? formatBytes(size, useSiUnits) : UNKNOWN
        }
        const hasAge = !isPlaceholder && name !== '..'

        return (
            <React.Fragment>
                <div
                    className={cellClasses({'grid-file-name': true})}
                    title={this.props.title || undefined}
                    {...rowProps}
                >
                    {!isPlaceholder && <Icon
                        name={type === 'dir' ? 'file-directory' : 'file'}
                        className='me-2'
                    />}
                    <span>{name}</span>
                </div>
                <div
                    className={cellClasses({'grid-file-age': true})}
                    title={hasAge ? formatLocalDateTime(modified) || 'Modification time unknown' : undefined}
                    {...rowProps}
                >
                    {hasAge ? formatAge(modified, now) : ''}
                </div>
                <div
                    className={cellClasses({'grid-file-size': true})}
                    {...rowProps}
                >
                    <em>{sizeText}</em>
                </div>
            </React.Fragment>
        );
    }

    componentDidMount() {

    }
}

PaneFile.defaultProps = {
    index: 0,
    type: '', // none for the placeholder rows
    name: '',
    size: 0,
    modified: null,
    now: 0,
    title: '',
    useSiUnits: false,
    draggable: false,
    dropTarget: false,
    onClick: event => {},
    onDoubleClick: event => {},
    onMouseDown: event => {},
    onDragStart: event => {},
}

export default PaneFile;
