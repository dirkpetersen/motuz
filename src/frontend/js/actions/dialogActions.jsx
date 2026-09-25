import upath from 'upath'

import {
    getSide,
    getOtherSide,
    getCurrentPane,
    setCurrentPane,
    getCurrentFiles,
    setCurrentFiles,
    isCopyableFile,
} from 'managers/paneManager.jsx'

export const SHOW_NEW_COPY_JOB_DIALOG = '@@dialog/SHOW_NEW_COPY_JOB_DIALOG';
export const HIDE_NEW_COPY_JOB_DIALOG = '@@dialog/HIDE_NEW_COPY_JOB_DIALOG';

export const SHOW_EDIT_COPY_JOB_DIALOG = '@@dialog/SHOW_EDIT_COPY_JOB_DIALOG';
export const HIDE_EDIT_COPY_JOB_DIALOG = '@@dialog/HIDE_EDIT_COPY_JOB_DIALOG';

export const SHOW_NEW_HASHSUM_JOB_DIALOG = '@@dialog/SHOW_NEW_HASHSUM_JOB_DIALOG';
export const HIDE_NEW_HASHSUM_JOB_DIALOG = '@@dialog/HIDE_NEW_HASHSUM_JOB_DIALOG';

export const SHOW_EDIT_HASHSUM_JOB_DIALOG = '@@dialog/SHOW_EDIT_HASHSUM_JOB_DIALOG';
export const HIDE_EDIT_HASHSUM_JOB_DIALOG = '@@dialog/HIDE_EDIT_HASHSUM_JOB_DIALOG';

export const SHOW_NEW_CLOUD_CONNECTION_DIALOG = '@@dialog/SHOW_NEW_CLOUD_CONNECTION_DIALOG';
export const HIDE_NEW_CLOUD_CONNECTION_DIALOG = '@@dialog/HIDE_NEW_CLOUD_CONNECTION_DIALOG';

export const SHOW_EDIT_CLOUD_CONNECTION_DIALOG = '@@dialog/SHOW_EDIT_CLOUD_CONNECTION_DIALOG';
export const HIDE_EDIT_CLOUD_CONNECTION_DIALOG = '@@dialog/HIDE_EDIT_CLOUD_CONNECTION_DIALOG';

export const SHOW_MKDIR_DIALOG = '@@dialog/SHOW_MKDIR_DIALOG';
export const HIDE_MKDIR_DIALOG = '@@dialog/HIDE_MKDIR_DIALOG';

export const SHOW_SETTINGS_DIALOG = '@@dialog/SHOW_SETTINGS_DIALOG';
export const HIDE_SETTINGS_DIALOG = '@@dialog/HIDE_SETTINGS_DIALOG';


export const showNewCopyJobDialog = (data) => {
    if (data != null) {
        return _showNewCopyJobDialog(data)
    }

    return async (dispatch, getState) => {
        const state = getState();

        const srcSide = getSide(state.pane);
        const dstSide = getOtherSide(srcSide);

        const data = buildCopyJobData(state.pane, srcSide, dstSide);
        if (data === null) {
            return; // Nothing copyable selected
        }

        dispatch(_showNewCopyJobDialog(data))
    }
};

/**
 * Open the New Copy Job dialog for a drag and drop of the selection of `srcSide`
 * onto the pane `dstSide`, or onto the folder `dstSubdir` in that pane.
 *
 * Dropping onto the pane it came from is only allowed onto a folder that is not
 * itself part of the dragged selection.
 */
export const showDropCopyJobDialog = (srcSide, dstSide, dstSubdir=null) => {
    return async (dispatch, getState) => {
        const state = getState();

        if (srcSide === dstSide) {
            if (!dstSubdir) {
                return; // Copying a selection onto itself
            }
            const srcPane = getCurrentPane(state.pane, srcSide);
            const srcFiles = getCurrentFiles(state.pane, srcSide);
            const isSubdirDragged = Object.keys(srcPane.fileMultiFocusIndexes).some(key => {
                const srcFile = srcFiles[Number(key)];
                return srcFile && srcFile.name === dstSubdir;
            });
            if (isSubdirDragged) {
                return; // Copying a folder into itself
            }
        }

        const data = buildCopyJobData(state.pane, srcSide, dstSide, dstSubdir);
        if (data === null) {
            return; // Nothing copyable selected
        }

        dispatch(_showNewCopyJobDialog(data))
    }
};

/**
 * Build the New Copy Job dialog data for the selected files of `srcSide`,
 * copied into the current path of `dstSide` (or its subdirectory `dstSubdir`).
 * Returns null if nothing copyable is selected.
 */
function buildCopyJobData(paneState, srcSide, dstSide, dstSubdir=null) {
    const srcPane = getCurrentPane(paneState, srcSide);
    const srcFiles = getCurrentFiles(paneState, srcSide);
    const dstPane = getCurrentPane(paneState, dstSide);

    const dstDirectory = dstSubdir ? upath.join(dstPane.path, dstSubdir) : dstPane.path;

    const srcResourcePaths = []
    const dstResourcePaths = []

    for (let key in srcPane.fileMultiFocusIndexes) {
        const srcFile = srcFiles[Number(key)];
        if (!isCopyableFile(srcFile)) {
            continue;
        }
        const srcResourceName = srcFile.name;
        const srcResourcePath = upath.join(srcPane.path, srcResourceName)
        const dstResourcePath = upath.join(dstDirectory, srcResourceName)

        srcResourcePaths.push(srcResourcePath)
        dstResourcePaths.push(dstResourcePath)
    }

    if (srcResourcePaths.length === 0) {
        return null;
    }

    return {
        source_cloud: srcPane.host,
        source_paths: srcResourcePaths,
        destination_cloud: dstPane.host,
        destination_paths: dstResourcePaths,
    }
}

export const _showNewCopyJobDialog = (data) => ({
    type: SHOW_NEW_COPY_JOB_DIALOG,
    payload: {data}
});

export const hideNewCopyJobDialog = () => ({
    type: HIDE_NEW_COPY_JOB_DIALOG,
});


export const showEditCopyJobDialog = (copyJob) => ({
    type: SHOW_EDIT_COPY_JOB_DIALOG,
    payload: copyJob,
});

export const hideEditCopyJobDialog = () => ({
    type: HIDE_EDIT_COPY_JOB_DIALOG,
});

export const showNewHashsumJobDialog = (data) => {
    return {
        type: SHOW_NEW_HASHSUM_JOB_DIALOG,
        payload: {data},
    }
};

export const hideNewHashsumJobDialog = () => ({
    type: HIDE_NEW_HASHSUM_JOB_DIALOG,
});

export const showEditHashsumJobDialog = (data) => {
    return {
        type: SHOW_EDIT_HASHSUM_JOB_DIALOG,
        payload: data,
    }
};

export const hideEditHashsumJobDialog = () => ({
    type: HIDE_EDIT_HASHSUM_JOB_DIALOG,
});

export const showNewCloudConnectionDialog = () => ({
    type: SHOW_NEW_CLOUD_CONNECTION_DIALOG,
});

export const hideNewCloudConnectionDialog = () => ({
    type: HIDE_NEW_CLOUD_CONNECTION_DIALOG,
});


export const showEditCloudConnectionDialog = (data) => ({
    type: SHOW_EDIT_CLOUD_CONNECTION_DIALOG,
    payload: data,
});

export const hideEditCloudConnectionDialog = () => ({
    type: HIDE_EDIT_CLOUD_CONNECTION_DIALOG,
});

export const showMkdirDialog = (side) => {
    return async (dispatch, getState) => {
        const state = getState();
        const pane = getCurrentPane(state.pane, side);

        const {host, path} = pane;

        dispatch({
            type: SHOW_MKDIR_DIALOG,
            payload: {
                data: {host, path}
            },
        })
    }
};

export const hideMkdirDialog = () => ({
    type: HIDE_MKDIR_DIALOG,
});

export const showSettingsDialog = () => ({
    type: SHOW_SETTINGS_DIALOG,
});

export const hideSettingsDialog = () => ({
    type: HIDE_SETTINGS_DIALOG,
});
