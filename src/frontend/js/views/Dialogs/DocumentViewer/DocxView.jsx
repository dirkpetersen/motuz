import React from 'react';
import { renderAsync } from 'docx-preview';
import createDOMPurify from 'dompurify';

import { SAFE_LINK_REL, fragmentTarget, safeLinkUrl } from 'utils/viewerKind.js';
import { stripCssUrls } from 'utils/cssSafe.js';

const RENDER_TIMEOUT_MS = 30000;

// docx-preview's options: no alternative HTML chunks (they would be HTML documents
// in iframes), no embedded fonts, no comments; headers, footers and notes are shown
const DOCX_OPTIONS = {
    className: 'docx',
    inWrapper: true,
    ignoreWidth: false,
    ignoreHeight: false,
    ignoreFonts: true,
    breakPages: true,
    ignoreLastRenderedPageBreak: true,
    experimental: false,
    trimXmlDeclaration: true,
    useBase64URL: false,
    renderChanges: false,
    renderHeaders: true,
    renderFooters: true,
    renderFootnotes: true,
    renderEndnotes: true,
    renderComments: false,
    renderAltChunks: false,
    debug: false,
};

// Elements a document preview never needs (active content, forms, frames, metadata)
const FORBID_TAGS = ['script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'form', 'input',
    'button', 'textarea', 'select', 'option', 'link', 'meta', 'base', 'template', 'audio', 'video', 'source',
    'track', 'portal', 'dialog', 'foreignObject', 'use', 'animate', 'animateMotion', 'animateTransform', 'set'];

// Base styles inside the shadow root (the document's own CSS stays in there too)
const HOST_CSS = `
:host { display: block; }
.docx-wrapper { padding: 20px !important; }
.docx-link-removed { text-decoration: underline dotted; cursor: not-allowed; }
a[href] { cursor: pointer; color: #0563c1; text-decoration: underline; }
img.docx-image-removed { outline: 1px dashed #adb5bd; color: #6c757d; font: 12px sans-serif; }
`;

function blobPrefix() {
    return `blob:${window.location.origin}/`;
}

/**
 * Makes a link of the document safe: bookmarks ('#name') stay internal links (the
 * viewer scrolls to them), http/https/mailto open in a new tab without opener or
 * referrer, anything else (javascript:, file:, relative, ...) loses its href and is
 * marked as a removed link.
 */
export function processLink(node, value) {
    // docx-preview writes bookmark links as `a.href = '' + '#name'`, which the browser
    // resolves against the page: that is an internal link too
    const page = window.location.href.split('#')[0];
    const local = typeof value === 'string' && value.startsWith(`${page}#`) ? value.slice(page.length) : value;
    const anchor = fragmentTarget(local);
    const url = anchor ? null : safeLinkUrl(value);
    node.removeAttribute('xlink:href');
    if (anchor) {
        node.setAttribute('href', `#${anchor}`);
        node.setAttribute('data-motuz-anchor', anchor);
        node.removeAttribute('target');
    } else if (url) {
        node.setAttribute('href', url);
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', SAFE_LINK_REL);
        node.setAttribute('title', url);
    } else {
        node.removeAttribute('href');
        node.removeAttribute('target');
        if (value) {
            node.classList.add('docx-link-removed');
            node.setAttribute('title', 'Link removed: only http, https and mailto links are shown');
        }
    }
}

/**
 * A DOMPurify instance for docx-preview's output: the elements and attributes DOMPurify
 * allows, minus FORBID_TAGS; links only to http(s)/mailto (new tab, no opener or
 * referrer) or to bookmarks in the document; images only from blob: URLs this page
 * made from the file (docx-preview never loads external pictures, this makes sure);
 * url() in style attributes removed.
 */
export function makeSanitizer() {
    const purify = createDOMPurify(window);
    const prefix = blobPrefix();
    purify.addHook('afterSanitizeAttributes', (node) => {
        const tag = node.nodeName.toLowerCase();
        for (const name of ['href', 'xlink:href']) {
            if (!node.hasAttribute(name)) continue;
            const value = node.getAttribute(name);
            if (tag === 'a') {
                processLink(node, value);
            } else if (!(typeof value === 'string' && value.startsWith(prefix))) {
                node.removeAttribute(name); // e.g. SVG <image href>: only the file's own pictures
            }
        }
        for (const name of ['src', 'srcset', 'poster', 'background', 'action', 'formaction', 'ping', 'lowsrc', 'dynsrc']) {
            if (!node.hasAttribute(name)) continue;
            const value = node.getAttribute(name);
            if (!(name === 'src' && typeof value === 'string' && value.startsWith(prefix))) {
                node.removeAttribute(name);
            }
        }
        if (node.hasAttribute('style')) {
            const style = node.getAttribute('style');
            if (/url\s*\(|image-set|@import|\\/i.test(style)) {
                node.setAttribute('style', stripCssUrls(style, prefix));
            }
        }
        if (node.hasAttribute('target') && tag !== 'a') {
            node.removeAttribute('target');
        }
    });
    return purify;
}

/**
 * DOCX preview with docx-preview. The document worker has checked the ZIP (limits,
 * [Content_Types].xml) and rebuilt it from the entries it inflated; docx-preview
 * renders it into detached elements, DOMPurify cleans them in place (makeSanitizer),
 * the document's CSS loses every url(), and only then are they attached, inside a
 * shadow root so the document's styles and the app's do not mix.
 */
export default class DocxView extends React.Component {
    constructor(props) {
        super(props);
        this.state = {status: 'rendering', error: null, removedLinks: 0};
        this.host = null;
        this.unmounted = false;
        this.onClick = (event) => this.handleClick(event);
    }

    componentDidMount() {
        this.renderDocument();
    }

    componentWillUnmount() {
        this.unmounted = true;
        clearTimeout(this.timer);
        if (this.shadow) {
            this.shadow.removeEventListener('click', this.onClick);
            // docx-preview's blob: URLs (pictures)
            for (const img of this.shadow.querySelectorAll('img[src^="blob:"]')) {
                URL.revokeObjectURL(img.getAttribute('src'));
            }
        }
    }

    async renderDocument() {
        const body = document.createElement('div');
        const styles = document.createElement('div');
        this.timer = setTimeout(() => {
            if (this.state.status === 'rendering' && !this.unmounted) {
                this.setState({status: 'error', error: 'Rendering the document took too long; it may be too large or complex to preview'});
            }
        }, RENDER_TIMEOUT_MS);
        try {
            await renderAsync(this.props.zip, body, styles, DOCX_OPTIONS);
        } catch (e) {
            clearTimeout(this.timer);
            if (!this.unmounted) {
                this.setState({status: 'error', error: `The document could not be rendered: ${e && e.message ? e.message : e}`});
            }
            return;
        }
        clearTimeout(this.timer);
        if (this.unmounted || this.state.status !== 'rendering') return;

        // Links first (DOMPurify would drop a javascript: href silently), then the rest
        for (const link of body.querySelectorAll('a')) {
            processLink(link, link.getAttribute('href'));
        }
        const purify = makeSanitizer();
        // URLs: DOMPurify's default list has no blob: (the pictures); the hook narrows
        // this to the page's own blob: URLs, http(s)/mailto links and bookmarks
        purify.sanitize(body, {IN_PLACE: true, FORBID_TAGS, ALLOW_DATA_ATTR: true,
                               ALLOWED_URI_REGEXP: /^(?:blob:|https?:|mailto:|#)/i});
        const prefix = blobPrefix();
        // Pictures without a source: linked (external) ones, never loaded
        for (const img of body.querySelectorAll('img:not([src])')) {
            img.classList.add('docx-image-removed');
            img.setAttribute('alt', 'Linked picture (not loaded)');
            img.setAttribute('title', 'Pictures linked from other sites are not loaded');
        }
        const css = [...styles.querySelectorAll('style')].map(style => stripCssUrls(style.textContent, prefix));

        const shadow = this.host.shadowRoot || this.host.attachShadow({mode: 'open'});
        const base = document.createElement('style');
        base.textContent = HOST_CSS;
        const sheets = css.map((text) => {
            const style = document.createElement('style');
            style.textContent = text;
            return style;
        });
        shadow.replaceChildren(base, ...sheets, ...body.childNodes);
        shadow.addEventListener('click', this.onClick);
        this.shadow = shadow;
        const removedLinks = shadow.querySelectorAll('.docx-link-removed').length;
        this.setState({status: 'ready', removedLinks});
        if (this.props.onInfo) {
            this.props.onInfo({pages: shadow.querySelectorAll('section.docx').length || null});
        }
    }

    handleClick(event) {
        const link = event.target.closest && event.target.closest('a');
        if (!link) return;
        const anchor = link.getAttribute('data-motuz-anchor');
        if (anchor) {
            event.preventDefault();
            const target = this.shadow.getElementById(anchor)
                || this.shadow.querySelector(`[name="${CSS.escape(anchor)}"]`);
            if (target) target.scrollIntoView({block: 'start'});
        } else if (!link.hasAttribute('href')) {
            event.preventDefault();
        }
    }

    render() {
        const {status, error, removedLinks} = this.state;
        return (
            <div className='document-viewer docx-viewer'>
                {status === 'rendering' && <div className='file-viewer-loading text-muted'>Rendering the document...</div>}
                {status === 'error' && (
                    <div className='alert alert-warning file-viewer-error document-viewer-error' role='alert'>{error}</div>
                )}
                {removedLinks > 0 && (
                    <div className='document-viewer-notice small text-muted'>
                        {removedLinks === 1 ? '1 link was' : `${removedLinks} links were`} removed:
                        only http, https and mailto links are shown.
                    </div>
                )}
                <div className='docx-viewer-stage' tabIndex={0} hidden={status === 'error'}>
                    <div className='docx-viewer-host' ref={el => { this.host = el; }} />
                </div>
            </div>
        );
    }
}

DocxView.defaultProps = {
    zip: null,
    onInfo: null,
};
