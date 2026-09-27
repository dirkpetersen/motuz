export default {
    convertLocalFilesToMotuz,
    convertRcloneFilesToMotuz,
    filterFiles,
}

export function convertLocalFilesToMotuz(files) {
    return files;
}


export function convertRcloneFilesToMotuz(files) {
    return files.map(d => ({
        name: d.Name,
        type: d.IsDir ? 'dir' : 'file',
        size: d.Size,
        modified: d.modified, // ISO 8601 UTC from the server (rclone's ModTime), or null
    }))
    return files;
}

export function filterFiles(files, options) {
    const { showHiddenFiles } = options

    if (!showHiddenFiles) {
        files = files.filter(d => !d.name.startsWith('.'))
    }
    return files;
}
