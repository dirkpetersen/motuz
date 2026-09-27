/**
 * The spreadsheet viewer's data and window math. The document worker turns a SheetJS
 * workbook into plain rows of formatted cell text (workbookToSheets: values as the
 * file stores them, no formula evaluation, at most SHEET_LIMITS rows and columns per
 * sheet), and the grid renders only the cells in view (visibleRange). Plain
 * functions for node --test; SheetJS is passed in, not imported.
 */

export const SHEET_LIMITS = Object.freeze({maxRows: 5000, maxCols: 200, maxSheets: 200});

export const ROW_HEIGHT = 24; // px, every row
export const DEFAULT_COL_WIDTH = 96; // px
export const MIN_COL_WIDTH = 32;
export const MAX_COL_WIDTH = 480;

/** Spreadsheet column name of a 0-based index: 0 → A, 25 → Z, 26 → AA */
export function columnName(index) {
    let name = '';
    let n = index + 1;
    while (n > 0) {
        const rest = (n - 1) % 26;
        name = String.fromCharCode(65 + rest) + name;
        n = Math.floor((n - 1) / 26);
    }
    return name;
}

function clampWidth(px) {
    if (!Number.isFinite(px) || px <= 0) return DEFAULT_COL_WIDTH;
    return Math.round(Math.max(MIN_COL_WIDTH, Math.min(MAX_COL_WIDTH, px)));
}

/** Column widths in px from SheetJS's `!cols` (wpx, or wch characters) */
export function columnWidths(cols, count) {
    const widths = [];
    for (let c = 0; c < count; c++) {
        const col = cols && cols[c];
        let px = DEFAULT_COL_WIDTH;
        if (col && col.hidden) {
            px = MIN_COL_WIDTH;
        } else if (col && Number.isFinite(col.wpx)) {
            px = col.wpx;
        } else if (col && Number.isFinite(col.wch)) {
            px = col.wch * 7 + 5;
        }
        widths.push(clampWidth(px));
    }
    return widths;
}

function cellText(XLSX, cell) {
    if (!cell) return '';
    if (typeof cell.w === 'string') return cell.w;
    if (cell.v === undefined || cell.v === null) return '';
    try {
        return XLSX.utils.format_cell(cell);
    } catch (e) {
        return String(cell.v);
    }
}

/**
 * Plain data of every sheet: [{name, hidden, rows, rowCount, colCount, totalRows,
 * totalCols, widths}], where rows[r][c] is the cell's formatted text (rows and cells
 * may be missing: empty), counted from A1 like the spreadsheet shows them, and
 * rowCount/colCount are what is shown (at most `limits`), totalRows/totalCols what
 * the sheet has (rowsKnown: false when the file does not say how many rows there are
 * beyond the ones read). Workbooks are read with `dense: true` and `sheetRows: maxRows
 * + 1`, which keeps only the first rows; `!fullref` tells the real size when the file
 * has a <dimension>.
 */
export function workbookToSheets(XLSX, workbook, limits = SHEET_LIMITS) {
    const names = (workbook.SheetNames || []).slice(0, limits.maxSheets);
    const meta = (workbook.Workbook && workbook.Workbook.Sheets) || [];
    return names.map((name, index) => {
        const sheet = workbook.Sheets[name];
        const hidden = !!(meta[index] && meta[index].Hidden);
        const empty = {name, hidden, rows: [], rowCount: 0, colCount: 0, totalRows: 0, totalCols: 0, rowsKnown: true, widths: []};
        if (!sheet || !sheet['!ref']) {
            return empty;
        }
        const ref = XLSX.utils.decode_range(sheet['!ref']);
        const full = XLSX.utils.decode_range(sheet['!fullref'] || sheet['!ref']);
        // Read with sheetRows = maxRows + 1: one row more than shown means "truncated",
        // also for files without a <dimension> (then the real count is unknown)
        const parsedRows = ref.e.r + 1;
        const totalRows = Math.max(full.e.r + 1, parsedRows);
        const totalCols = Math.max(full.e.c + 1, ref.e.c + 1);
        const rowCount = Math.min(parsedRows, limits.maxRows);
        const colCount = Math.min(ref.e.c + 1, limits.maxCols);
        const rowsKnown = parsedRows <= limits.maxRows || !!sheet['!fullref'];
        const dense = sheet['!data'];
        const rows = [];
        for (let r = ref.s.r; r < rowCount; r++) {
            const source = dense ? dense[r] : null;
            if (dense && !source) continue;
            let row = null;
            for (let c = ref.s.c; c < colCount; c++) {
                const cell = dense ? source[c] : sheet[XLSX.utils.encode_cell({r, c})];
                const text = cellText(XLSX, cell);
                if (text !== '') {
                    if (!row) row = [];
                    row[c] = text;
                }
            }
            if (row) {
                rows[r] = row;
            }
        }
        return {
            name,
            hidden,
            rows,
            rowCount,
            colCount,
            totalRows,
            totalCols,
            rowsKnown,
            widths: columnWidths(sheet['!cols'], colCount),
        };
    });
}

/** Offsets of the columns (prefix sums of the widths, one more than widths) */
export function columnOffsets(widths) {
    const offsets = [0];
    for (const width of widths) {
        offsets.push(offsets[offsets.length - 1] + width);
    }
    return offsets;
}

/** Index of the item that contains position `x`, from prefix sums */
function indexAt(offsets, x) {
    let lo = 0;
    let hi = offsets.length - 2;
    if (hi < 0) return 0;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (offsets[mid] <= x) lo = mid; else hi = mid - 1;
    }
    return lo;
}

/**
 * The cells to render for a scroll position: {firstRow, lastRow, firstCol, lastCol}
 * (inclusive; lastRow < firstRow for an empty sheet), `overscan` extra rows and
 * columns around the viewport.
 */
export function visibleRange({scrollTop, scrollLeft, height, width, rowCount, offsets, rowHeight = ROW_HEIGHT, overscan = 4}) {
    const colCount = offsets.length - 1;
    if (!rowCount || !colCount) {
        return {firstRow: 0, lastRow: -1, firstCol: 0, lastCol: -1};
    }
    const firstRow = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
    const lastRow = Math.min(rowCount - 1, Math.ceil((scrollTop + height) / rowHeight) + overscan);
    const firstCol = Math.max(0, indexAt(offsets, scrollLeft) - overscan);
    const lastCol = Math.min(colCount - 1, indexAt(offsets, scrollLeft + width) + overscan);
    return {firstRow, lastRow, firstCol, lastCol};
}
