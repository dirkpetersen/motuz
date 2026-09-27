import React from 'react';

const UNSUPPORTED_LABELS = {
    chart: 'Chart (not shown)',
    diagram: 'SmartArt diagram (not shown)',
    object: 'Embedded object (not shown)',
    'linked picture': 'Linked picture (never loaded)',
    'picture in an unsupported format': 'Picture in a format the preview does not show (e.g. EMF, SVG, TIFF)',
    'missing slide': 'This slide is missing from the file',
};

/**
 * PPTX outline: slide titles, text (with list levels), tables, pictures and speaker
 * notes, one card per slide, with a slide list to jump to a slide. Slides are not
 * drawn: no maintained, safe browser renderer for PPTX exists (see CLAUDE.md), so the
 * document worker extracts the content itself (utils/officeFiles.js pptxOutline).
 * Everything is React text; pictures are PNG, JPEG, GIF or WebP from inside the file
 * (checked by magic number), shown through blob: URLs revoked on close.
 */
export default class PptxView extends React.Component {
    constructor(props) {
        super(props);
        this.urls = {};
        for (const [part, {mime, bytes}] of Object.entries(props.pictures || {})) {
            this.urls[part] = URL.createObjectURL(new Blob([bytes], {type: mime}));
        }
        this.cards = {};
        this.stage = null;
        this.state = {current: 1};
    }

    componentDidMount() {
        if (this.props.onInfo) {
            this.props.onInfo({slides: (this.props.slides || []).length});
        }
    }

    componentWillUnmount() {
        for (const url of Object.values(this.urls)) {
            URL.revokeObjectURL(url);
        }
    }

    goTo(number) {
        const card = this.cards[number];
        if (card && this.stage) {
            this.stage.scrollTop = card.offsetTop - 8; // the stage is the cards' offsetParent
        }
        this.setState({current: number});
    }

    onScroll() {
        if (!this.stage) return;
        const top = this.stage.scrollTop + 40;
        let current = 1;
        for (const [number, card] of Object.entries(this.cards)) {
            if (card && card.offsetTop <= top) current = Math.max(current, Number(number));
        }
        if (current !== this.state.current) this.setState({current});
    }

    render() {
        const slides = this.props.slides || [];
        if (!slides.length) {
            return <div className='document-viewer pptx-viewer text-muted p-3'>The presentation has no slides.</div>;
        }
        return (
            <div className='document-viewer pptx-viewer'>
                <div className='document-viewer-notice small text-muted'>
                    Outline view: the text, tables and pictures of each slide, not the slide layout.
                </div>
                <div className='pptx-viewer-layout'>
                    <nav className='pptx-viewer-list' aria-label='Slides'>
                        <ol>
                            {slides.map(slide => (
                                <li key={slide.number}>
                                    <button type='button'
                                            className={`pptx-viewer-list-item${slide.number === this.state.current ? ' active' : ''}`}
                                            onClick={() => this.goTo(slide.number)}>
                                        <span className='pptx-viewer-list-number'>{slide.number}</span>
                                        <span className='pptx-viewer-list-title'>{slide.title || `Slide ${slide.number}`}</span>
                                    </button>
                                </li>
                            ))}
                        </ol>
                    </nav>
                    <div className='pptx-viewer-slides' tabIndex={0} ref={el => { this.stage = el; }} onScroll={() => this.onScroll()}>
                        {slides.map(slide => this.renderSlide(slide))}
                    </div>
                </div>
            </div>
        );
    }

    renderSlide(slide) {
        return (
            <section key={slide.number} className='pptx-viewer-slide' ref={el => { this.cards[slide.number] = el; }}
                     aria-label={`Slide ${slide.number}`} data-slide={slide.number}>
                <header className='pptx-viewer-slide-header'>
                    <span className='pptx-viewer-slide-number'>Slide {slide.number}</span>
                    {slide.hidden && <span className='badge bg-secondary ms-2'>Hidden</span>}
                </header>
                {slide.title && <h5 className='pptx-viewer-slide-title'>{slide.title}</h5>}
                {slide.items.map((item, index) => this.renderItem(item, index))}
                {!slide.title && !slide.items.length && <div className='text-muted small'>No text on this slide.</div>}
                {slide.notes.length > 0 && (
                    <div className='pptx-viewer-notes'>
                        <div className='pptx-viewer-notes-label'>Notes</div>
                        {slide.notes.map((note, index) => <p key={index}>{note}</p>)}
                    </div>
                )}
            </section>
        );
    }

    renderItem(item, index) {
        if (item.type === 'text') {
            return (
                <ul key={index} className='pptx-viewer-text'>
                    {item.paragraphs.map((p, i) => (
                        <li key={i} style={{marginLeft: `${p.level * 1.5}em`}}>{p.text}</li>
                    ))}
                </ul>
            );
        }
        if (item.type === 'table') {
            return (
                <div key={index} className='pptx-viewer-table-wrap'>
                    <table className='table table-sm table-bordered pptx-viewer-table'>
                        <tbody>
                            {item.rows.map((row, r) => (
                                <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            );
        }
        if (item.type === 'image' && this.urls[item.part]) {
            return (
                <figure key={index} className='pptx-viewer-picture'>
                    <img src={this.urls[item.part]} alt={item.part.split('/').pop()} draggable={false} />
                </figure>
            );
        }
        return (
            <div key={index} className='pptx-viewer-unsupported small text-muted'>
                [{UNSUPPORTED_LABELS[item.what] || 'Content not shown'}]
            </div>
        );
    }
}

PptxView.defaultProps = {
    slides: [],
    pictures: {},
    onInfo: null,
};
