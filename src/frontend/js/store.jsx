import storage from 'redux-persist/es/storage';
import thunkMiddleware from 'redux-thunk';
import { applyMiddleware, compose, createStore } from 'redux';
import { createFilter } from 'redux-persist-transform-filter';
import { persistReducer, persistStore } from 'redux-persist';

import getRootReducer from 'reducers/reducers.jsx';
import authMiddleware from 'middleware/authMiddleware.jsx';

export default () => {
    const rootReducer = getRootReducer()

    const authFilter = createFilter('auth', ['access', 'refresh']);

    const persistedReducer = persistReducer({
        key: 'polls',
        storage: storage,
        whitelist: ['auth', 'settings'],
        transforms: [authFilter],
    }, rootReducer);

    // const persistedReducer = rootReducer;


    let devTools = a => a;
    // Never in production: dispatched actions include request bodies with cloud credentials
    if (process.env.NODE_ENV !== 'production' && window.__REDUX_DEVTOOLS_EXTENSION__) {
        devTools = window.__REDUX_DEVTOOLS_EXTENSION__();
    }

    const store = createStore(
        persistedReducer,
        {},
        compose(
            applyMiddleware(
                authMiddleware,
                thunkMiddleware,
            ),
            devTools,
        )
    );

    const persistor = persistStore(store);

    return {store, persistor};
};