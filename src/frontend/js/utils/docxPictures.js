/**
 * Pictures of a previewed DOCX. docx-preview makes a blob: URL for every picture with
 * the content type the DOCX itself declares in its [Content_Types].xml, so a document
 * can claim `text/html` for a PNG/HTML polyglot: shown as a picture, but "open image in
 * a new tab" would run it as an HTML page in Motuz's origin. retypeBlobUrl checks the
 * bytes (magic numbers: PNG, JPEG, GIF, WebP, utils/officeFiles.js imageMime) and makes
 * a new URL with the detected type, or none for anything that is not such a picture.
 * Plain functions with their effects passed in, for node --test.
 */
import {imageMime} from './officeFiles.js';

/**
 * {url, mime} with a new blob: URL of the same bytes typed as the detected image, or
 * null if the bytes are not a PNG/JPEG/GIF/WebP picture (or cannot be read). The old
 * URL is revoked either way. `fetchBytes(url)` -> Uint8Array, `makeUrl(bytes, mime)` ->
 * URL, `revoke(url)`.
 */
export async function retypeBlobUrl(url, {fetchBytes, makeUrl, revoke}) {
    let bytes = null;
    try {
        bytes = await fetchBytes(url);
    } catch (e) {
        bytes = null;
    }
    revoke(url);
    const mime = bytes ? imageMime(bytes) : null;
    return mime ? {url: makeUrl(bytes, mime), mime} : null;
}

/** The blob: URLs in a piece of CSS text */
export function blobUrlsIn(text) {
    return [...new Set(String(text || '').match(/blob:[^)"'\s]+/g) || [])];
}
