/**
 * Whether this browser can decode HEIC/HEIF itself (Safari can), found out once by decoding
 * a 551-byte HEIC picture (48 x 64) instead of guessing from the first user file: a corrupt
 * or timed-out file of the user says nothing about the browser. `decode(blob)` is the
 * viewer's decodeCheck ({width, height} or null).
 */
const PROBE_BASE64 =
    'AAAAHGZ0eXBoZWljAAAAAG1pZjFoZWljbWlhZgAAAX1tZXRhAAAAAAAAACFoZGxyAAAAAAAAAABwaWN0AAAAAAAAAAAAAAAAAAAA' +
    'ACJpbG9jAAAAAERAAAEAAQAAAAABoQABAAAAAAAAAIYAAAAjaWluZgAAAAAAAQAAABVpbmZlAgAAAAABAABodmMxAAAAAA5waXRt' +
    'AAAAAAABAAAA/WlwcnAAAADdaXBjbwAAAHZodmNDAQNwAAAAAAAAAAAAHvAA/P34+AAADwNgAAEAGEABDAH//wNwAAADAJAAAAMA' +
    'AAMAHroCQGEAAQAqQgEBA3AAAAMAkAAAAwAAAwAeoCCBBZbq5Ka5uAhoMCAAAAMDIAAAAwAhYgABAAZEAcFzwIkAAAATY29scm5j' +
    'bHgAAQANAAaAAAAAFGlzcGUAAAAAAAAAQAAAAEAAAAAoY2xhcAAAADAAAAABAAAAQAAAAAH////wAAAAAgAAAAAAAAACAAAAEHBp' +
    'eGkAAAAAAwgICAAAABhpcG1hAAAAAAAAAAEAAQWBAgMFhAAAAI5tZGF0AAAAgigBrxOA5U3NOntUtvy4iRzFHT/mYfl1Sr66+0UN' +
    'vp+AfnfNclikXJ9zC8Q/FI1HZtKBIKUn0dFcQs2+v3QKkyECTV7uP+neb+giYr1XmN2538nYVfOlcoTi2FecJhLLEUT1NhHwBAZC' +
    'lldzn+DHAi+tQREoBxdcXtKTZaOEYXfxdsA=';

let probe = null;

export function probeBytes() {
    const binary = atob(PROBE_BASE64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
}

/** A promise of true/false, decoded once per page */
export function probeNativeHeic(decode) {
    if (!probe) {
        probe = Promise.resolve()
            .then(() => decode(new Blob([probeBytes()], {type: 'image/heic'})))
            .then(result => !!result, () => false);
    }
    return probe;
}
