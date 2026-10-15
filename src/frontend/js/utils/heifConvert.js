/**
 * When the image viewer asks the server for a JPEG of a HEIC/HEIF image. The server
 * converts on request only (`convert: "jpeg"`, as the logged-in user; a child process,
 * api/utils/heic_convert.py) and only HEIC/HEIF: any other image comes back unchanged.
 * Safari shows HEIC itself, so the plain image is asked for when a probe (utils/heicProbe.js:
 * decoding a tiny HEIC picture) says the browser decodes HEIC; otherwise the JPEG is asked
 * for at once, which saves reading the file twice. Plain functions for node --test.
 */

export const HEIF_TYPES = ['image/heic', 'image/heif'];

/** A name that probably is a HEIC/HEIF image (the server decides by content) */
export function isHeifName(name) {
    return typeof name === 'string' && /\.(heic|heif)$/i.test(name);
}

export function isHeifType(type) {
    return HEIF_TYPES.includes(type);
}

/**
 * Ask for the JPEG straight away? `nativeHeic`: the probe's answer (true = this browser
 * decodes HEIC/HEIF itself, false = it cannot, null = unknown)
 */
export function convertFirst(name, nativeHeic) {
    return nativeHeic === false && isHeifName(name);
}

/** After the plain image arrived: must it be fetched again as a JPEG? */
export function needsConversion(type, decodedNatively) {
    return isHeifType(type) && !decodedNatively;
}

/** The text of the footer for a shown image: "HEIC" or "HEIC, converted to JPEG" */
export function describeType(label, convertedFromLabel) {
    return convertedFromLabel ? `${convertedFromLabel}, converted to ${label}` : label;
}
