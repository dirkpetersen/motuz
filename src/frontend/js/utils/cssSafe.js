/**
 * CSS from previewed documents (docx-preview turns a DOCX's styles into style sheets
 * and style attributes) must not load anything: every url() except the blob: URLs the
 * viewer made itself becomes `none`, and @import rules and image-set() are removed.
 * The page's Content-Security-Policy (img-src/font-src 'self' blob: data:) blocks
 * other sites as well; this keeps even the attempt from happening.
 * Plain functions for node --test.
 */

function allowedUrl(raw, blobPrefix) {
    let value = raw.trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1).trim();
    }
    return !!blobPrefix && value.startsWith(blobPrefix) && !/[\s"'()\\]/.test(value.slice(blobPrefix.length));
}

/**
 * The CSS text with external references removed. `blobPrefix` is 'blob:<origin>/'
 * (the page's own blob URLs are kept), or null to remove all url()s.
 */
export function stripCssUrls(css, blobPrefix = null) {
    if (typeof css !== 'string' || !css) {
        return '';
    }
    let out = css;
    // CSS escapes could spell url( or @import in another way: refuse escaped text
    // around those words by decoding hex escapes of letters first
    out = out.replace(/\\([0-9a-fA-F]{1,6})\s?/g, (match, hex) => {
        const code = parseInt(hex, 16);
        return code > 0x20 && code < 0x7f ? String.fromCharCode(code) : match;
    });
    out = out.replace(/@import[^;]*;?/gi, '');
    out = out.replace(/(?:-webkit-)?image-set\s*\([^)]*\)/gi, 'none');
    out = out.replace(/url\s*\(\s*("[^"]*"|'[^']*'|[^)]*)\s*\)/gi,
        (match, inner) => (allowedUrl(inner, blobPrefix) ? match : 'none'));
    // Anything left that still calls url( (unbalanced quotes, comments inside) goes
    if (/url\s*\(/i.test(out.replace(/url\s*\(\s*(["']?)blob:[^)"'\s]*\1\s*\)/gi, ''))) {
        out = out.replace(/url\s*\(/gi, 'none(');
    }
    return out;
}
