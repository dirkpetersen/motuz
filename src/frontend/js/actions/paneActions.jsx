import * as api from 'actions/apiActions.jsx';
import { getCurrentPane } from 'managers/paneManager.jsx'
import { nextSort } from 'utils/fileSort.js'

export const SIDE_FOCUS = '@@pane/SIDE_FOCUS';
export const FILE_FOCUS_INDEX = '@@pane/FILE_FOCUS_INDEX';
export const FILE_MULTI_FOCUS_INDEX = '@@pane/FILE_MULTI_FOCUS_INDEX';
export const FILE_RANGE_FOCUS_INDEX = '@@pane/FILE_RANGE_FOCUS_INDEX';
export const DIRECTORY_CHANGE = '@@pane/DIRECTORY_CHANGE';
export const HOST_CHANGE = '@@pane/HOST_CHANGE';
export const SORT_CHANGE = '@@pane/SORT_CHANGE';


export const fileFocusIndex = (side, index) => ({
    type: FILE_FOCUS_INDEX,
    payload: {side, index},
});

export const fileMultiFocusIndexes = (side, index) => ({
    type: FILE_MULTI_FOCUS_INDEX,
    payload: {side, index},
});

export const fileRangeFocusIndex = (side, index) => ({
    type: FILE_RANGE_FOCUS_INDEX,
    payload: {side, index},
});


/**
 * Click on a column header of a pane: sorts by that column, or toggles the direction.
 * Handled by the pane reducer (re-sorts, keeps the selection) and the settings
 * reducer (persisted per pane).
 */
export const sortChange = (side, column) => {
    return (dispatch, getState) => {
        const current = getState().pane.sort[side];
        dispatch({
            type: SORT_CHANGE,
            payload: {side, sort: nextSort(current, column)},
        });
    }
}


export const sideFocus = (side) => ({
    type: SIDE_FOCUS,
    payload: {side},
});


export const hostChange = (side=null, host) => {
    return async (dispatch, getState) => {
        await dispatch({
            type: HOST_CHANGE,
            payload: {side, host},
        })

        const state = getState();
        const pane = getCurrentPane(state.pane, side)

        // TODO: Change this
        return await dispatch(api.listFiles(side, {
            connection_id: host.id,
            path: pane.path,
            settings: state.settings,
        }))
    }
}

export const initPanes = () => {
    return async (dispatch, getState) => {
        const state = getState()

        return await dispatch(api.listHomeFiles({
            settings: state.settings,
        }))
    }
}

export const directoryChange = (side=null, path) => {
    return async (dispatch, getState) => {
        const state = getState();
        const pane = getCurrentPane(state.pane, side);
        const host = pane.host;

        if (path === '.' || path === '') {
            path = '/'
        }

        dispatch({
            type: DIRECTORY_CHANGE,
            payload: {side, path}
        })
        return await dispatch(api.listFiles(side, {
            connection_id: host.id,
            path,
            settings: state.settings,
        }));
    }
}

export const refreshPane = (side='left') => {
    return async (dispatch, getState) => {
        const state = getState();

        const path = state.pane.panes[side][0].path;

        await dispatch(directoryChange(side, path))
    }
}

export const refreshPanes = (path=null) => {
    return async (dispatch, getState) => {
        const state = getState();

        const promises = []

        const pathLeft = state.pane.panes.left[0].path;
        const pathRight = state.pane.panes.right[0].path;

        if (path == null || path === pathLeft) {
            promises.push(dispatch(directoryChange('left', pathLeft)))
        }

        if (path == null || path === pathRight) {
            promises.push(dispatch(directoryChange('right', pathRight)))
        }

        await Promise.all(promises);
    }
}
