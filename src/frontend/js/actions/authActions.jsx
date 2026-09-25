import { RSAA } from 'redux-api-middleware';

import { withRefresh } from 'reducers/reducers.jsx';

export const LOGIN_REQUEST = '@@auth/LOGIN_REQUEST';
export const LOGIN_SUCCESS = '@@auth/LOGIN_SUCCESS';
export const LOGIN_FAILURE = '@@auth/LOGIN_FAILURE';

export const REFRESH_TOKEN_REQUEST = '@@auth/REFRESH_TOKEN_REQUEST';
export const REFRESH_TOKEN_SUCCESS = '@@auth/REFRESH_TOKEN_SUCCESS';
export const REFRESH_TOKEN_FAILURE = '@@auth/REFRESH_TOKEN_FAILURE';

export const LOGOUT_REQUEST = '@@auth/LOGOUT_REQUEST';
export const LOGOUT_SUCCESS = '@@auth/LOGOUT_SUCCESS';
export const LOGOUT_FAILURE = '@@auth/LOGOUT_FAILURE';

// Tokens written to localStorage by another tab (utils/persistedAuth.jsx)
export const SYNC_TOKENS = '@@auth/SYNC_TOKENS';

export const login = (username, password) => ({
    [RSAA]: {
        endpoint: '/api/auth/login/',
        method: 'POST',
        body: JSON.stringify({username, password}),
        headers: { 'Content-Type': 'application/json' },
        types: [
            LOGIN_REQUEST, LOGIN_SUCCESS, LOGIN_FAILURE
        ]
    }
});

// Revokes the refresh token and its whole session on the server, i.e. every access
// token of this login, in all tabs
export const logout = () => ({
    [RSAA]: {
        endpoint: '/api/auth/logout/',
        method: 'POST',
        headers: withRefresh({ 'Content-Type': 'application/json' }),
        types: [
            LOGOUT_REQUEST, LOGOUT_SUCCESS, LOGOUT_FAILURE
        ]
    }
});


// Rotates the refresh token: the server revokes the one sent here (after a short
// grace window for other tabs), so the pair in the response replaces both tokens
export const refreshAccessToken = (refresh_token) => ({
    [RSAA]: {
        endpoint: '/api/auth/refresh/',
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${refresh_token}` },
        types: [
            REFRESH_TOKEN_REQUEST, REFRESH_TOKEN_SUCCESS, REFRESH_TOKEN_FAILURE
        ]
    }
});

export const syncTokens = ({access, refresh}) => ({
    type: SYNC_TOKENS,
    payload: {access, refresh},
});
