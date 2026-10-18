/**
 * Files and folders dropped from the desktop onto a pane (actions/uploadActions.jsx):
 * which files they are, with paths relative to the drop, and the names the server
 * accepts (managers/upload_manager.py checks the same).
 */

export const MAX_UPLOAD_FILES = 100000;
// How the job table shows the source of an upload (job.upload)
export const UPLOAD_PROTOCOL = 'desktop';
const MAX_PATH = 4096;
const MAX_NAME_BYTES = 255;
const CONTROL = /[\x00-\x1f\x7f]/;

/**
 * What was dropped: {entries} (files and folders) or, in browsers without
 * webkitGetAsEntry, {files} (files only). Must be called in the drop event handler:
 * the browser empties the DataTransfer when the handler returns.
 */
export function droppedItems(dataTransfer) {
    const items = Array.from(dataTransfer.items || []).filter(item => item.kind === 'file');
    const entries = items.map(item => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null));
    if (entries.length > 0 && entries.every(Boolean)) {
        return {entries};
    }
    return {files: Array.from(dataTransfer.files || [])};
}

/**
 * [{path, file}] of all files of droppedItems(), folders walked; empty folders are
 * not included. Rejects with an Error (message for the user) if a file cannot be read
 * or there are more than `limit` files.
 */
export async function collectFiles(dropped, limit=MAX_UPLOAD_FILES) {
    if (dropped.files) {
        return dropped.files.map(file => ({path: file.name, file}));
    }
    const result = [];
    const walk = async (entry, prefix) => {
        const path = prefix ? `${prefix}/${entry.name}` : entry.name;
        if (entry.isFile) {
            if (result.length >= limit) {
                throw new Error(`The drop has more than ${limit} files. Upload fewer files at once.`);
            }
            result.push({path, file: await fileOf(entry, path)});
        } else if (entry.isDirectory) {
            for (const child of await readAllEntries(entry.createReader(), path)) {
                await walk(child, path);
            }
        }
    };
    for (const entry of dropped.entries) {
        await walk(entry, '');
    }
    return result;
}

function fileOf(entry, path) {
    return new Promise((resolve, reject) => entry.file(resolve, () => reject(new Error(`Could not read "${path}".`))));
}

/** All entries of a folder: readEntries returns them in batches until an empty one */
async function readAllEntries(reader, path) {
    const all = [];
    for (;;) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(
            resolve, () => reject(new Error(`Could not read the folder "${path}".`))));
        if (batch.length === 0) {
            return all;
        }
        all.push(...batch);
    }
}

/** Why the server refuses the relative path `path`, or null if it is accepted */
export function uploadPathError(path) {
    if (typeof path !== 'string' || path === '' || path.length > MAX_PATH || CONTROL.test(path)) {
        return `"${path}" is not a valid file name.`;
    }
    const encoder = new TextEncoder();
    for (const part of path.split('/')) {
        if (part === '' || part === '.' || part === '..' || encoder.encode(part).length > MAX_NAME_BYTES) {
            return `"${path}" is not a valid file name.`;
        }
    }
    return null;
}

/** The distinct first parts of the paths (the dropped files and folders), sorted */
export function topLevelNames(paths) {
    return Array.from(new Set(paths.map(path => path.split('/')[0]))).sort();
}
