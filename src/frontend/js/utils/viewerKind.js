/**
 * Which viewer a double-click opens, and the URL rules of the Markdown viewer. Plain
 * JS (node tests in test/frontend/test_viewer_kind.mjs).
 *
 * - images (.png .jpg .jpeg .gif .webp): the image viewer; the server decides the type
 *   by the file's first bytes, the extension only picks the viewer. SVG is not an
 *   image here (it can carry scripts): it opens in the text pager.
 * - Markdown (.md .markdown): rendered, with a switch to the pager (Source)
 * - everything else: the text pager
 */

export const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'gif', 'webp'];
export const MARKDOWN_EXTENSIONS = ['md', 'markdown'];

/** The extension of a file name, lower case, without the dot ('' if none) */
export function extensionOf(name) {
    if (typeof name !== 'string') {
        return '';
    }
    const base = name.replace(/\/+$/, '').split('/').pop();
    const dot = base.lastIndexOf('.');
    if (dot <= 0 || dot === base.length - 1) {
        return ''; // no extension, or a dot file like ".md"
    }
    return base.slice(dot + 1).toLowerCase();
}

/** 'image' | 'markdown' | 'text' for a file name */
export function viewerKind(name) {
    const extension = extensionOf(name);
    if (IMAGE_EXTENSIONS.includes(extension)) {
        return 'image';
    }
    if (MARKDOWN_EXTENSIONS.includes(extension)) {
        return 'markdown';
    }
    return 'text';
}

// Control characters and whitespace: browsers drop some of them inside a scheme
// ("java\tscript:"), so a URL with any of them is never a link
const UNSAFE_CHARS = /[\u0000- \u007f-\u009f\u2028\u2029]/;
const SCHEME = /^([a-zA-Z][a-zA-Z0-9+.-]*):/;

/**
 * The href of a link in rendered Markdown, or null if it must not be a link. Only
 * http:, https: and mailto: URLs are links; relative URLs, fragments and every other
 * scheme (javascript:, data:, vbscript:, file:, ...) are not.
 */
export function safeLinkUrl(url) {
    if (typeof url !== 'string' || url === '' || UNSAFE_CHARS.test(url)) {
        return null;
    }
    const match = SCHEME.exec(url);
    if (!match) {
        return null;
    }
    const scheme = match[1].toLowerCase();
    if (scheme === 'mailto') {
        return url.length > 'mailto:'.length ? url : null;
    }
    if (scheme !== 'http' && scheme !== 'https') {
        return null;
    }
    let parsed;
    try {
        parsed = new URL(url);
    } catch (e) {
        return null;
    }
    if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') || !parsed.hostname) {
        return null;
    }
    return url;
}

/** A link inside the document ("#section", e.g. footnotes): the id it points to, or null */
export function fragmentTarget(url) {
    if (typeof url !== 'string' || !url.startsWith('#') || url.length < 2 || UNSAFE_CHARS.test(url)) {
        return null;
    }
    try {
        return decodeURIComponent(url.slice(1));
    } catch (e) {
        return null;
    }
}

/** Normalizes a POSIX path: no empty or "." parts, ".." resolved (never above the root) */
export function normalizePath(path) {
    const absolute = path.startsWith('/');
    const parts = [];
    for (const part of path.split('/')) {
        if (part === '' || part === '.') {
            continue;
        }
        if (part === '..') {
            if (parts.length && parts[parts.length - 1] !== '..') {
                parts.pop();
            } else if (!absolute) {
                parts.push('..');
            }
            continue;
        }
        parts.push(part);
    }
    const joined = parts.join('/');
    return absolute ? '/' + joined : joined;
}

/** The folder of a file path ('/a/b/c.md' -> '/a/b', 'c.md' -> '') */
export function dirnameOf(path) {
    const index = path.replace(/\/+$/, '').lastIndexOf('/');
    if (index < 0) {
        return '';
    }
    return index === 0 ? '/' : path.slice(0, index);
}

/**
 * What an image in a Markdown file refers to, with the Markdown file's path:
 *  {kind: 'local', path}  a relative (or absolute) path on the same connection, an image
 *                         type the image viewer shows: loaded through the image endpoint
 *  {kind: 'remote'}       http(s) and any other URL: never loaded (privacy, tracking)
 *  {kind: 'embedded'}     a data: URL: not loaded
 *  {kind: 'unsupported'}  a path that is not a PNG, JPEG, GIF or WebP (e.g. SVG)
 *  {kind: 'none'}         no usable source
 */
export function markdownImageSource(src, markdownPath) {
    if (typeof src !== 'string' || src.trim() === '' || UNSAFE_CHARS.test(src.trim())) {
        return {kind: 'none'};
    }
    src = src.trim();
    if (src.startsWith('//')) {
        return {kind: 'remote'}; // protocol-relative: another host
    }
    const match = SCHEME.exec(src);
    if (match) {
        return {kind: match[1].toLowerCase() === 'data' ? 'embedded' : 'remote'};
    }
    let relative = src.split('#')[0].split('?')[0];
    try {
        relative = decodeURIComponent(relative);
    } catch (e) {
        return {kind: 'none'};
    }
    if (relative === '' || relative.includes('\u0000')) {
        return {kind: 'none'};
    }
    const base = typeof markdownPath === 'string' ? dirnameOf(markdownPath) : '';
    const path = relative.startsWith('/') ? normalizePath(relative) : normalizePath(`${base || '/'}/${relative}`);
    if (!path.startsWith('/') || path === '/') {
        return {kind: 'none'};
    }
    if (viewerKind(path) !== 'image') {
        return {kind: 'unsupported', path};
    }
    return {kind: 'local', path};
}
