import React from 'react';
import classnames from 'classnames';
import upath from 'upath';

import PaneFile from 'views/App/Pane/PaneFile.jsx'
import {isCopyableFile} from 'managers/paneManager.jsx'
import {parentDirectory} from 'utils/parentDirectory.js'

// Drag and drop between panes. The payload ({side}) is only readable on drop,
// so the source side is also encoded in a second type, which dragover can see.
// Types are lowercased by the browser.
const DRAG_MIME = 'application/x-motuz-files';
const DRAG_SIDE_MIME_PREFIX = 'application/x-motuz-side-';
const DROP_ON_PANE = -1; // dropTarget value for the pane itself (its current path)


class Pane extends React.Component {
    constructor(props) {
        super(props);
        this.state = {
            dropTarget: null, // null, DROP_ON_PANE or the index of a folder row
        };
        this.dropTarget = null;
        this.pendingSelectIndex = null;
        this.onDocumentDragEnd = () => this.setDropTarget(null);
    }

    render() {
        const {dropTarget} = this.state;

        const paneFiles = this.props.files.map((file, i) => (
            <PaneFile
                key={i}
                index={i}
                type={file.type}
                name={file.name}
                size={file.size}
                useSiUnits={this.props.useSiUnits}
                active={this.props.active && this.props.pane.fileMultiFocusIndexes[i]}
                draggable={isCopyableFile(file)}
                dropTarget={dropTarget === i}
                onMouseDown={(event) => this.onFileMouseDown(event, this.props.side, i)}
                onClick={(event) => this.onFileClick(event, this.props.side, i)}
                onDoubleClick={() => this.onFileDoubleClick(this.props.side, i)}
                onDragStart={(event) => this.onFileDragStart(event, this.props.side, i)}
            />
        ))

        return (
            <div
                className={classnames({
                    'grid-files': true,
                    'drop-target': dropTarget !== null,
                })}
                onDragOver={(event) => this.onDragOver(event)}
                onDragLeave={(event) => this.onDragLeave(event)}
                onDrop={(event) => this.onDrop(event)}
            >
                {paneFiles}
            </div>
        );
    }

    componentDidMount() {
        // dragend bubbles from the source row, so this also clears the highlight
        // of the other pane, including when the drag is cancelled with Esc
        document.addEventListener('dragend', this.onDocumentDragEnd);
        document.addEventListener('drop', this.onDocumentDragEnd);
    }

    componentWillUnmount() {
        document.removeEventListener('dragend', this.onDocumentDragEnd);
        document.removeEventListener('drop', this.onDocumentDragEnd);
    }

    onFileMouseDown(event, side, index) {
        const isRangeSelection = event.shiftKey
        const isMultiSelection = event.metaKey || event.ctrlKey
        this.pendingSelectIndex = null

        if (isMultiSelection) {
            this.props.onMultiSelect(side, index)
        } else if (isRangeSelection) {
            this.props.onRangeSelect(side, index)
        } else if (this.isSelected(index) && this.getSelectedIndexes().length > 1) {
            // Keep the multi-selection so it can be dragged; a plain click
            // (mouseup without a drag) still selects only this file.
            this.pendingSelectIndex = index
        } else {
            this.props.onSelect(side, index)
        }
    }

    onFileClick(event, side, index) {
        if (this.pendingSelectIndex === index) {
            this.pendingSelectIndex = null
            this.props.onSelect(side, index)
        }
    }

    onFileDoubleClick(side, index) {
        const currPath = this.props.pane.path;
        const directoryToEnter = this.props.files[index]
        if (directoryToEnter.type !== 'dir') {
            return; // We do not open files
        }

        const path = upath.join(currPath, directoryToEnter.name)
        this.props.onDirectoryChange(side, path)
    }

    onFileDragStart(event, side, index) {
        this.pendingSelectIndex = null

        let indexes = this.getSelectedIndexes()
        if (!this.isSelected(index)) {
            // Dragging an unselected file drags only that file
            this.props.onSelect(side, index)
            indexes = [index]
        }

        const files = indexes
            .map(i => this.props.files[i])
            .filter(file => isCopyableFile(file))
        if (files.length === 0) {
            event.preventDefault(); // Only .. or placeholder rows: nothing to copy
            return;
        }

        const dataTransfer = event.dataTransfer
        dataTransfer.effectAllowed = 'copy'
        dataTransfer.setData(DRAG_MIME, JSON.stringify({side}))
        dataTransfer.setData(DRAG_SIDE_MIME_PREFIX + side, side)

        const label = files.length === 1 ? files[0].name : `${files.length} items`
        this.setDragImage(dataTransfer, label)
    }

    setDragImage(dataTransfer, label) {
        if (!dataTransfer.setDragImage) {
            return;
        }
        const element = document.createElement('div')
        element.className = 'drag-image'
        element.textContent = label
        document.body.appendChild(element)
        dataTransfer.setDragImage(element, 0, 0)
        // The browser takes a snapshot when dragstart returns
        setTimeout(() => element.remove(), 0)
    }

    onDragOver(event) {
        const types = Array.from(event.dataTransfer.types)

        if (types.includes(DRAG_MIME)) {
            const dropTarget = this.getDropTarget(event, this.getDragSourceSide(types))
            this.setDropTarget(dropTarget)
            if (dropTarget === null) {
                event.dataTransfer.dropEffect = 'none'
                return; // Not a valid drop target: the drop is not allowed
            }
            event.preventDefault()
            event.dataTransfer.dropEffect = 'copy'
        } else if (types.includes('Files')) {
            // Accept the drop so it can explain that uploads are not supported,
            // instead of the browser navigating away to the dropped file
            event.preventDefault()
            event.dataTransfer.dropEffect = 'copy'
        }
    }

    onDragLeave(event) {
        if (!event.currentTarget.contains(event.relatedTarget)) {
            this.setDropTarget(null)
        }
    }

    onDrop(event) {
        const types = Array.from(event.dataTransfer.types)
        this.setDropTarget(null)

        if (types.includes(DRAG_MIME)) {
            event.preventDefault()

            let srcSide = null
            try {
                srcSide = JSON.parse(event.dataTransfer.getData(DRAG_MIME)).side
            } catch (error) {
                return;
            }
            if (srcSide !== 'left' && srcSide !== 'right') {
                return;
            }

            const dropTarget = this.getDropTarget(event, srcSide)
            if (dropTarget === null) {
                return;
            }
            // '..' is resolved to the parent directory by showDropCopyJobDialog
            const dstSubdir = dropTarget === DROP_ON_PANE ? null : this.props.files[dropTarget].name
            this.props.onDropFiles(srcSide, this.props.side, dstSubdir)
        } else if (types.includes('Files')) {
            event.preventDefault()
            this.props.onDropDesktopFiles()
        }
    }

    /**
     * Where a drag from `srcSide` would drop: a folder row index, DROP_ON_PANE,
     * or null if it cannot be dropped here.
     */
    getDropTarget(event, srcSide) {
        const isSameSide = srcSide === this.props.side

        const row = event.target.closest ? event.target.closest('[data-file-index]') : null
        if (row) {
            const index = Number(row.getAttribute('data-file-index'))
            const file = this.props.files[index]
            if (file && file.name === '..' && file.type === 'dir') {
                // The parent folder, also from the same pane (copies one level up).
                // Not a target at "/" or at the root of a bucket.
                return parentDirectory(this.props.pane.path, this.props.pane.host) === null ? null : index;
            }
            const isFolder = isCopyableFile(file) && file.type === 'dir'
            // Within the same pane, a folder that is being dragged is not a target
            if (isFolder && !(isSameSide && this.isSelected(index))) {
                return index;
            }
        }

        if (isSameSide) {
            return null; // A selection cannot be copied onto itself
        }
        return DROP_ON_PANE;
    }

    getDragSourceSide(types) {
        const type = types.find(d => d.startsWith(DRAG_SIDE_MIME_PREFIX))
        return type ? type.slice(DRAG_SIDE_MIME_PREFIX.length) : null
    }

    setDropTarget(dropTarget) {
        // Compare with the last requested value, not this.state: React renders
        // dragover updates asynchronously, so this.state can lag behind.
        if (this.dropTarget !== dropTarget) {
            this.dropTarget = dropTarget
            this.setState({dropTarget})
        }
    }

    isSelected(index) {
        return Boolean(this.props.active && this.props.pane.fileMultiFocusIndexes[index])
    }

    getSelectedIndexes() {
        if (!this.props.active) {
            return []
        }
        return Object.keys(this.props.pane.fileMultiFocusIndexes).map(Number)
    }
}

Pane.defaultProps = {
    side: 'left',
    files: [],
    pane: {},
    active: false,
    useSiUnits: false,
    onSelect: (side, index) => {},
    onMultiSelect: (side, index) => {},
    onRangeSelect: (side, index) => {},
    onDropFiles: (srcSide, dstSide, dstSubdir) => {},
    onDropDesktopFiles: () => {},
}

import {connect} from 'react-redux';
import {
    fileFocusIndex,
    fileMultiFocusIndexes,
    fileRangeFocusIndex,
    directoryChange,
} from 'actions/paneActions.jsx';
import {showDropCopyJobDialog} from 'actions/dialogActions.jsx';
import {showAlert} from 'actions/alertActions.jsx';

const mapStateToProps = state => ({
    useSiUnits: state.settings.useSiUnits,
});

const mapDispatchToProps = dispatch => ({
    onSelect: (side, index) => dispatch(fileFocusIndex(side, index)),
    onMultiSelect: (side, index) => dispatch(fileMultiFocusIndexes(side, index)),
    onRangeSelect: (side, index) => dispatch(fileRangeFocusIndex(side, index)),
    onDirectoryChange: (side, path) => dispatch(directoryChange(side, path)),
    onDropFiles: (srcSide, dstSide, dstSubdir) => dispatch(showDropCopyJobDialog(srcSide, dstSide, dstSubdir)),
    onDropDesktopFiles: () => dispatch(showAlert(
        'Uploading from your computer is not supported',
        'Motuz copies data between servers and cloud storage; it cannot upload files from your computer. '
        + 'Put the files on a filesystem or cloud storage that Motuz can reach, then copy them from '
        + 'there with Motuz. To copy between the two panes, drag files from one pane to the other.',
    )),
});

export default connect(mapStateToProps, mapDispatchToProps)(Pane);
