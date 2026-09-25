export const SHOW_ALERT = '@@alert/SHOW_ALERT';
export const HIDE_ALERT = '@@alert/HIDE_ALERT';

/**
 * Show an informational notice (not an error response) in the alert area
 */
export const showAlert = (heading, text, variant='warning') => ({
    type: SHOW_ALERT,
    payload: {heading, text, variant},
});

export const hideAlert = () => ({
    type: HIDE_ALERT,
});
