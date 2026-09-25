// Login tokens as redux-persist keeps them in localStorage, shared by all tabs.
// Each tab has its own in-memory store, so a tab re-reads them (see
// middleware/authMiddleware.jsx) instead of refreshing with a token that another tab
// has already rotated (the server revokes rotated refresh tokens).

export const PERSIST_CONFIG_KEY = 'polls';
export const PERSIST_STORAGE_KEY = `persist:${PERSIST_CONFIG_KEY}`;

/**
 * {access, refresh} token strings from a persisted value (both undefined when logged
 * out), or null if it cannot be read.
 */
export function parsePersistedAuth(raw) {
    if (!raw) {
        return null;
    }
    try {
        const auth = JSON.parse(JSON.parse(raw).auth);
        return {
            access: auth.access && auth.access.token,
            refresh: auth.refresh && auth.refresh.token,
        };
    } catch (error) {
        return null;
    }
}

export function readPersistedAuth() {
    try {
        return parsePersistedAuth(window.localStorage.getItem(PERSIST_STORAGE_KEY));
    } catch (error) { // Storage disabled
        return null;
    }
}
