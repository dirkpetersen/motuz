import React from 'react';
import {
    CheckIcon,
    ChevronDownIcon,
    ChevronLeftIcon,
    ChevronUpIcon,
    CopyIcon,
    FileDirectoryFillIcon,
    FileIcon,
    FileSubmoduleIcon,
    GearIcon,
    HistoryIcon,
    QuestionIcon,
    SignOutIcon,
    SyncIcon,
    XIcon,
} from '@primer/octicons-react'

/**
 * Documentation:
 * https://primer.style/octicons/
 *
 * Usage:
 * <Icon name='check' className='mt-2'/>
 *
 * Props other than `name` go to the octicon (size, verticalAlign, className, ...).
 * Only the icons below are bundled; add new ones here.
 */

const ICONS = {
    'check': CheckIcon,
    'chevron-down': ChevronDownIcon,
    'chevron-left': ChevronLeftIcon,
    'chevron-up': ChevronUpIcon,
    'clippy': CopyIcon,
    'file': FileIcon,
    'file-directory': FileDirectoryFillIcon,
    'file-submodule': FileSubmoduleIcon,
    'gear': GearIcon,
    'history': HistoryIcon,
    'question': QuestionIcon,
    'sign-out': SignOutIcon,
    'sync': SyncIcon,
    'x': XIcon,
};

class Icon extends React.PureComponent {
    render() {
        const {name, verticalAlign, ...props} = this.props;
        const Octicon = ICONS[name];
        if (!Octicon) {
            return null;
        }
        // 'top' used to mean the top of the text
        return <Octicon verticalAlign={verticalAlign === 'top' ? 'text-top' : verticalAlign} {...props} />
    }
}

Icon.defaultProps = {
    name: '',
    size: 16,
    verticalAlign: 'text-bottom',
};

export default Icon;
