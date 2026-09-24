import React from 'react'
import { createRoot } from 'react-dom/client';

import { Provider } from 'react-redux';
import { PersistGate } from 'redux-persist/es/integration/react'

import Root from 'Root.jsx'
import configureStore from 'store.jsx';

const {store, persistor} = configureStore();

createRoot(document.getElementById("container")).render(
    <Provider store={store}>
        <PersistGate loading={null} persistor={persistor}>
            <Root />
        </PersistGate>
    </Provider>
);
