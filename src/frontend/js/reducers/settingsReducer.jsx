import * as settings from 'actions/settingsActions.jsx';
import * as pane from 'actions/paneActions.jsx';
import { DEFAULT_SORT } from 'utils/fileSort.js';

const initialState = {
    showHiddenFiles: false,
    useSiUnits: false,
    followSymlinks: false,
    emailNotifications: false,
    emailAddress: "",
    // Sort of each pane ({column: name|age|size, asc}), persisted with the settings
    paneSort: {
        left: DEFAULT_SORT,
        right: DEFAULT_SORT,
    },
};


export default (state=initialState, action) => {
    switch(action.type) {
    case settings.UPDATE_SETTINGS_REQUEST: {
        return {
            ...state,
            ...action.payload.data,
        }
    }

    case pane.SORT_CHANGE: {
        const {side, sort} = action.payload;
        return {
            ...state,
            paneSort: {
                ...initialState.paneSort,
                ...state.paneSort,
                [side]: sort,
            },
        }
    }

    default:
        return state;
    }
};
