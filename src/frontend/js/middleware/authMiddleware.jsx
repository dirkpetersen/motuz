import { isRSAA, createMiddleware } from 'redux-api-middleware';

import { REFRESH_TOKEN_REQUEST, REFRESH_TOKEN_SUCCESS, REFRESH_TOKEN_FAILURE, refreshAccessToken } from 'actions/authActions.jsx';
import { refreshToken, isAccessTokenExpired, isRefreshTokenExpired } from 'reducers/reducers.jsx';


function createAuthMiddleware() {
    let postponedRSAAs = [];
    let refreshing = false;

    return ({ dispatch, getState }) => {
        const rsaaMiddleware = createMiddleware()({dispatch, getState});

        return (next) => (action) => {
            const nextCheckPostponed = (nextAction) => {
                // Run postponed actions after token refresh
                if (nextAction.type === REFRESH_TOKEN_SUCCESS) {
                    refreshing = false;
                    next(nextAction);
                    const postponed = postponedRSAAs;
                    postponedRSAAs = [];
                    postponed.forEach((postponedAction) => {
                        rsaaMiddleware(next)(postponedAction);
                    });
                } else if (
                    nextAction.type === REFRESH_TOKEN_FAILURE ||
                    (nextAction.type === REFRESH_TOKEN_REQUEST && nextAction.error) // Network error
                ) {
                    // Drop the queue so that the next expiry triggers a fresh refresh
                    refreshing = false;
                    postponedRSAAs = [];
                    next(nextAction);
                } else {
                    next(nextAction);
                }
            };

            if (isRSAA(action)) {
                const state = getState()
                const token = refreshToken(state);

                if (token && isAccessTokenExpired(state) && !isRefreshTokenExpired(state)) {
                    postponedRSAAs.push(action);
                    if (!refreshing) {
                        refreshing = true;
                        return rsaaMiddleware(nextCheckPostponed)(refreshAccessToken(token));
                    }
                    return;
                }

                return rsaaMiddleware(next)(action);
            }
            return next(action);
        };
    };
}

export default createAuthMiddleware();
