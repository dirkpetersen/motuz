/**
 * Links inside previewed documents (PDF link annotations, DOCX hyperlinks) are data
 * from the file. Only http, https and mailto links are kept, as absolute URLs; they
 * open in a new tab with rel="noopener noreferrer". Everything else (javascript:,
 * data:, file:, vbscript:, blob:, relative URLs, ...) is dropped.
 * Plain functions for node --test.
 */

export const SAFE_LINK_PROTOCOLS = Object.freeze(['http:', 'https:', 'mailto:']);
export const SAFE_LINK_REL = 'noopener noreferrer';

// Control characters and whitespace that browsers strip or ignore inside a scheme
// ("java\tscript:" is javascript:)
const IGNORED = new RegExp('[\\u0000-\\u0020\\u007f-\\u009f\\u00ad\\u200b-\\u200f\\u2028\\u2029\\ufeff]', 'g');

/**
 * The URL to link to, or null: `href` must be an absolute http(s) URL with a host or a
 * mailto: URL, without user name or password. Returned in the URL parser's form.
 */
export function safeLinkUrl(href) {
    if (typeof href !== 'string') {
        return null;
    }
    const trimmed = href.trim();
    if (!trimmed || trimmed.length > 8192) {
        return null;
    }
    // Decide on the scheme with the characters browsers ignore removed, so that
    // obfuscations like " java\nscript:" cannot pass as something else
    const compact = trimmed.replace(IGNORED, '');
    const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(compact);
    if (!scheme || !SAFE_LINK_PROTOCOLS.includes(`${scheme[1].toLowerCase()}:`)) {
        return null;
    }
    let url;
    try {
        url = new URL(trimmed);
    } catch (e) {
        return null;
    }
    if (!SAFE_LINK_PROTOCOLS.includes(url.protocol)) {
        return null;
    }
    if (url.protocol !== 'mailto:' && !url.hostname) {
        return null;
    }
    if (url.username || url.password) {
        return null; // http://trusted.example@evil.example/ misleads
    }
    return url.href;
}

/**
 * For internal links of a document: the fragment of '#name' links (bookmarks), or
 * null. Used to scroll within the preview instead of changing the app's URL.
 */
export function internalAnchor(href) {
    if (typeof href !== 'string' || !href.startsWith('#') || href.length < 2 || href.length > 512) {
        return null;
    }
    return href.slice(1);
}
