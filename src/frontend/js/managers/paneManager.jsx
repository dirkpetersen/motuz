import upath from 'upath'


export function getSide(state) {
    if (state.focusPaneLeft) {
        return 'left';
    } else {
        return 'right';
    }
}

export function getOtherSide(side) {
    if (side === 'left') {
        return 'right'
    } else if (side === 'right') {
        return 'left'
    } else {
        console.error("Unknown side", side)
        return 'left';
    }
}

/**
 * Whether a pane row can be copied. The parent directory (..) and the
 * placeholder rows (Loading..., ERROR), which have no type, cannot.
 */
export function isCopyableFile(file) {
    return Boolean(file && file.type && file.name !== '..');
}

export function getCurrentPane(state, side=null) {
    if (!side) {
        side = getSide(state);
    }
    const index = state.indexes[side]
    return state.panes[side][index]
}

export function setCurrentPane(state, payload, side=null) {
    if (!side) {
        side = getSide(state);
    }
    const index = state.indexes[side]
    const panes = state.panes[side].slice()
    panes[index] = payload
    return {
        ...state.panes,
        [side]: panes,
    }
}

export function getCurrentFiles(state, side=null) {
    if (!side) {
        side = getSide(state);
    }
    return state.files[side]
}

export function setCurrentFiles(state, payload, side=null) {
    if (!side) {
        side = getSide(state);
    }
    return {
        ...state.files,
        [side]: payload,
    }
}

export function fileExists(state, hostId, dirname, basename) {
    const side = ['left', 'right'].find(side => {
        const pane = getCurrentPane(state, side);
        return (pane.host.id || 0) === (hostId || 0) && pane.path === dirname;
    });
    if (!side) {
        console.error(`Neither left nor right pane shows '${dirname}' on host ${hostId || 0}`)
        return false; // Fail safe
    }

    return getCurrentFiles(state, side).some(d => d.name === basename);
}
