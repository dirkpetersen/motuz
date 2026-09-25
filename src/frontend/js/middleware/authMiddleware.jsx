import { isRSAA, createMiddleware } from 'redux-api-middleware';
import { jwtDecode } from 'jwt-decode';

import { REFRESH_TOKEN_REQUEST, REFRESH_TOKEN_SUCCESS, REFRESH_TOKEN_FAILURE, refreshAccessToken, syncTokens } from 'actions/authActions.jsx';
import { refreshToken, isAccessTokenExpired, isRefreshTokenExpired } from 'reducers/reducers.jsx';
import { PERSIST_STORAGE_KEY, parsePersistedAuth, readPersistedAuth } from 'utils/persistedAuth.jsx';

/*
 * Access tokens are short lived (15 minutes); API calls made with an expired one wait
 * for a refresh and then run. The server rotates refresh tokens: /auth/refresh/
 * revokes the refresh token it was called with (after a grace window of a minute), and
 * using it later revokes the whole login. All tabs share the tokens in localStorage
 * (redux-persist) but each has its own store, so a tab never refreshes with a token
 * another tab has already rotated:
 * - syncTokensAcrossTabs: a tab adopts tokens (and logouts) other tabs write
 * - before refreshing, and when a refresh fails, a tab re-reads localStorage
 * - two tabs refreshing at the same moment both succeed thanks to the grace window
 */

function expiresSoon(token) {
    try {
        return 1000 * jwtDecode(token).exp - Date.now() < 5000;
    } catch (error) {
        return true;
    }
}

/** Adopts the tokens in localStorage if another tab wrote newer ones */
function adoptPersistedTokens(dispatch, getState) {
    const persisted = readPersistedAuth();
    if (!persisted || !persisted.access || !persisted.refresh) {
        return false;
    }
    if (persisted.refresh === refreshToken(getState()) || expiresSoon(persisted.refresh)) {
        return false;
    }
    dispatch(syncTokens(persisted));
    return true;
}

export function syncTokensAcrossTabs(store) {
    window.addEventListener('storage', event => {
        if (event.key !== PERSIST_STORAGE_KEY) {
            return;
        }
        const persisted = parsePersistedAuth(event.newValue);
        if (!persisted || persisted.refresh === refreshToken(store.getState())) {
            return;
        }
        store.dispatch(syncTokens(persisted)); // Also logs this tab out
    });
}


function createAuthMiddleware() {
    let postponed = []; // {action, resolve}: API calls waiting for a refresh
    let refreshing = false;

    return ({ dispatch, getState }) => {
        const rsaaMiddleware = createMiddleware()({dispatch, getState});

        const takePostponed = () => {
            const queue = postponed;
            postponed = [];
            refreshing = false;
            return queue;
        };

        return (next) => {
            // Dispatches the actions of the refresh request
            const onRefreshAction = usedToken => (refreshAction) => {
                if (refreshAction.type === REFRESH_TOKEN_SUCCESS) {
                    next(refreshAction);
                    takePostponed().forEach(({action, resolve}) => resolve(dispatch(action)));
                } else if (refreshAction.type === REFRESH_TOKEN_FAILURE) {
                    const queue = takePostponed();
                    // Rejected because another tab rotated it first: use that tab's tokens
                    adoptPersistedTokens(dispatch, getState);
                    const state = getState();
                    const current = refreshToken(state);
                    if (current && current !== usedToken && !isRefreshTokenExpired(state)) {
                        queue.forEach(({action, resolve}) => resolve(dispatch(action)));
                        return;
                    }
                    next(refreshAction); // Logged out
                    queue.forEach(({resolve}) => resolve(undefined));
                } else if (refreshAction.type === REFRESH_TOKEN_REQUEST && refreshAction.error) {
                    // Network error: drop the queue so that the next call refreshes again
                    takePostponed().forEach(({resolve}) => resolve(undefined));
                    next(refreshAction);
                } else {
                    next(refreshAction);
                }
            };

            return (action) => {
                if (!isRSAA(action)) {
                    return next(action);
                }

                if (refreshToken(getState()) && isAccessTokenExpired(getState())) {
                    adoptPersistedTokens(dispatch, getState); // Another tab may have refreshed
                }

                const state = getState();
                const token = refreshToken(state);
                if (token && isAccessTokenExpired(state) && !isRefreshTokenExpired(state)) {
                    // Resolves with the result of the API call once the tokens are refreshed
                    return new Promise(resolve => {
                        postponed.push({action, resolve});
                        if (!refreshing) {
                            refreshing = true;
                            rsaaMiddleware(onRefreshAction(token))(refreshAccessToken(token));
                        }
                    });
                }

                return rsaaMiddleware(next)(action);
            };
        };
    };
}

export default createAuthMiddleware();
