import React from 'react';

import {
    ROW_HEIGHT, columnName, columnOffsets, visibleRange,
} from 'utils/sheetGrid.js';

const ROW_HEADER_WIDTH = 56;

/**
 * Read-only spreadsheet grid (XLSX, XLSM, XLSB, XLS, ODS). The document worker read
 * the workbook with SheetJS (values as stored in the file, formatted; formulas are
 * never evaluated, macros never loaded) into plain text rows; this renders only the
 * cells in view (utils/sheetGrid.js visibleRange), with the column letters and row
 * numbers in headers that stay in place while the cells scroll. Cell text is React
 * text, never HTML.
 */
export default class SheetView extends React.Component {
    constructor(props) {
        super(props);
        const sheets = props.sheets || [];
        const firstVisible = sheets.findIndex(sheet => !sheet.hidden);
        this.state = {
            active: firstVisible >= 0 ? firstVisible : 0,
            scrollTop: 0,
            scrollLeft: 0,
            width: 800,
            height: 400,
        };
        this.body = null;
        this.frame = null;
        this.onResize = () => this.measure();
    }

    componentDidMount() {
        this.measure();
        window.addEventListener('resize', this.onResize);
        if (this.props.onInfo) {
            this.props.onInfo({sheets: this.props.sheetCount});
        }
    }

    componentDidUpdate() {
        // The grid appears later (another sheet) or the dialog changed size
        if (this.body && (this.body.clientWidth !== this.state.width || this.body.clientHeight !== this.state.height)) {
            this.measure();
        }
    }

    componentWillUnmount() {
        window.removeEventListener('resize', this.onResize);
        cancelAnimationFrame(this.frame);
    }

    measure() {
        if (this.body) {
            this.setState({width: this.body.clientWidth, height: this.body.clientHeight});
        }
    }

    selectSheet(index) {
        this.setState({active: index, scrollTop: 0, scrollLeft: 0}, () => {
            if (this.body) {
                this.body.scrollTop = 0;
                this.body.scrollLeft = 0;
            }
        });
    }

    onScroll() {
        cancelAnimationFrame(this.frame);
        this.frame = requestAnimationFrame(() => {
            if (this.body) {
                this.setState({scrollTop: this.body.scrollTop, scrollLeft: this.body.scrollLeft});
            }
        });
    }

    render() {
        const sheets = this.props.sheets || [];
        const sheet = sheets[this.state.active];
        return (
            <div className='document-viewer sheet-viewer'>
                <div className='sheet-viewer-tabs' role='tablist' aria-label='Sheets'>
                    {sheets.map((s, index) => (
                        <button
                            key={index}
                            type='button'
                            role='tab'
                            aria-selected={index === this.state.active}
                            className={`sheet-viewer-tab${index === this.state.active ? ' active' : ''}${s.hidden ? ' hidden-sheet' : ''}`}
                            title={s.hidden ? `${s.name} (hidden sheet)` : s.name}
                            onClick={() => this.selectSheet(index)}
                        >
                            {s.name}
                        </button>
                    ))}
                </div>
                {sheet && this.renderNotice(sheet)}
                {sheet ? this.renderGrid(sheet) : <div className='text-muted p-3'>The workbook has no sheets.</div>}
            </div>
        );
    }

    renderNotice(sheet) {
        const notes = [];
        if (sheet.totalRows > sheet.rowCount) {
            notes.push(`the first ${sheet.rowCount.toLocaleString()} of ${sheet.totalRows.toLocaleString()} rows`);
        }
        if (sheet.totalCols > sheet.colCount) {
            notes.push(`the first ${sheet.colCount} of ${sheet.totalCols.toLocaleString()} columns`);
        }
        const extraSheets = this.props.sheetCount > (this.props.sheets || []).length;
        if (!notes.length && !extraSheets) return null;
        return (
            <div className='document-viewer-notice sheet-viewer-truncated small' role='status'>
                {notes.length > 0 && `Showing ${notes.join(' and ')}.`}
                {extraSheets && ` Only the first ${(this.props.sheets || []).length} sheets are shown.`}
            </div>
        );
    }

    renderGrid(sheet) {
        const {scrollTop, scrollLeft, width, height} = this.state;
        if (!sheet.rowCount || !sheet.colCount) {
            return <div className='sheet-viewer-empty text-muted'>This sheet is empty.</div>;
        }
        const offsets = columnOffsets(sheet.widths);
        const totalWidth = offsets[offsets.length - 1];
        const totalHeight = sheet.rowCount * ROW_HEIGHT;
        const range = visibleRange({scrollTop, scrollLeft, height, width, rowCount: sheet.rowCount, offsets});

        const columnHeaders = [];
        const rowHeaders = [];
        const cells = [];
        for (let c = range.firstCol; c <= range.lastCol; c++) {
            columnHeaders.push(
                <div key={c} className='sheet-viewer-colhead' style={{left: offsets[c], width: sheet.widths[c]}}>
                    {columnName(c)}
                </div>
            );
        }
        for (let r = range.firstRow; r <= range.lastRow; r++) {
            const top = r * ROW_HEIGHT;
            rowHeaders.push(<div key={r} className='sheet-viewer-rowhead' style={{top}}>{r + 1}</div>);
            const row = sheet.rows[r];
            for (let c = range.firstCol; c <= range.lastCol; c++) {
                const text = row ? row[c] : undefined;
                cells.push(
                    <div key={`${r}:${c}`} className='sheet-viewer-cell'
                         style={{top, left: offsets[c], width: sheet.widths[c]}}
                         title={text && text.length > 12 ? text : undefined}
                         data-cell={`${columnName(c)}${r + 1}`}>
                        {text}
                    </div>
                );
            }
        }

        return (
            <div className='sheet-viewer-grid' role='grid' aria-rowcount={sheet.rowCount} aria-colcount={sheet.colCount}
                 style={{'--sheet-row-header': `${ROW_HEADER_WIDTH}px`, '--sheet-row-height': `${ROW_HEIGHT}px`}}>
                <div className='sheet-viewer-corner' />
                <div className='sheet-viewer-colheads'>
                    <div style={{transform: `translateX(${-scrollLeft}px)`, width: totalWidth}}>{columnHeaders}</div>
                </div>
                <div className='sheet-viewer-rowheads'>
                    <div style={{transform: `translateY(${-scrollTop}px)`, height: totalHeight}}>{rowHeaders}</div>
                </div>
                <div className='sheet-viewer-body' tabIndex={0} ref={el => { this.body = el; }} onScroll={() => this.onScroll()}>
                    <div className='sheet-viewer-canvas' style={{width: totalWidth, height: totalHeight}}>
                        {cells}
                    </div>
                </div>
            </div>
        );
    }
}

SheetView.defaultProps = {
    sheets: [],
    sheetCount: 0,
    onInfo: null,
};
