import React from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { fragmentTarget, markdownImageSource, safeLinkUrl } from 'utils/viewerKind.js'

/*
 * The Markdown renderer of MarkdownViewerDialog, a separate bundle (dynamic import).
 * react-markdown builds React elements from the syntax tree, never HTML strings, and
 * without rehype-raw raw HTML in the file is not parsed: react-markdown shows it as
 * text. URLs go through urlTransform: a link is an http(s) or mailto link (new tab,
 * rel="noopener noreferrer") or a footnote/fragment inside the document, else plain
 * text; an image's src only reaches MarkdownImage, which decides what to load.
 */

function urlTransform(url, key) {
    if (key === 'href') {
        return safeLinkUrl(url) || (fragmentTarget(url) != null ? url : '');
    }
    if (key === 'src') {
        return url; // MarkdownImage never puts it in the page, only a blob: URL it made
    }
    return '';
}

const NOT_LOADED = {
    remote: 'remote image not loaded',
    embedded: 'embedded image not loaded',
    unsupported: 'not shown: only PNG, JPEG, GIF and WebP images are',
    none: 'image not loaded',
};

function ImagePlaceholder({kind, alt, hint, title}) {
    return (
        <span className='md-image-placeholder' data-md-image={kind} title={title || undefined}>
            <span className='md-image-placeholder-alt'>{alt || 'image'}</span>
            <span className='md-image-placeholder-hint'>{hint}</span>
        </span>
    );
}

function MarkdownImage({src, alt, title, markdownPath, loadImage}) {
    const source = React.useMemo(() => markdownImageSource(src, markdownPath), [src, markdownPath]);
    const [state, setState] = React.useState({url: null, error: null});
    React.useEffect(() => {
        if (source.kind !== 'local') {
            return undefined;
        }
        let active = true;
        loadImage(source.path).then(result => {
            if (active) {
                setState(result && result.url ? {url: result.url, error: null}
                                              : {url: null, error: (result && result.error) || 'could not be loaded'});
            }
        });
        return () => { active = false; };
    }, [source.kind, source.path]);

    if (source.kind !== 'local') {
        return <ImagePlaceholder kind={source.kind} alt={alt} hint={NOT_LOADED[source.kind]} title={source.kind === 'remote' ? src : null} />;
    }
    if (state.error) {
        return <ImagePlaceholder kind='error' alt={alt} hint={`image not shown: ${state.error}`} title={source.path} />;
    }
    if (!state.url) {
        return <ImagePlaceholder kind='loading' alt={alt} hint='loading image…' title={source.path} />;
    }
    return <img className='md-image' src={state.url} alt={alt || ''} title={title || undefined} data-md-image='local' />;
}

function MarkdownLink({href, title, children, container}) {
    const target = href ? fragmentTarget(href) : null;
    if (target != null) {
        const onClick = event => {
            event.preventDefault();
            const root = container.current;
            const el = root && Array.from(root.querySelectorAll('[id]')).find(e => e.id === target);
            if (el) {
                el.scrollIntoView({block: 'start'});
            }
        };
        return <a href={href} title={title || undefined} onClick={onClick}>{children}</a>;
    }
    if (!href) {
        return (
            <span className='md-link-disabled' title='Not a link: only http, https and mailto links are opened'>
                {children}
            </span>
        );
    }
    return (
        <a href={href} title={title || undefined} target='_blank' rel='noopener noreferrer'>
            {children}
        </a>
    );
}

export default function MarkdownContent({text, markdownPath, loadImage}) {
    const container = React.useRef(null);
    const components = React.useMemo(() => ({
        a: ({node, href, title, children}) => (
            <MarkdownLink href={href} title={title} container={container}>{children}</MarkdownLink>
        ),
        img: ({node, src, alt, title}) => (
            <MarkdownImage src={src} alt={alt} title={title} markdownPath={markdownPath} loadImage={loadImage} />
        ),
        table: ({node, children}) => (
            <div className='table-responsive'>
                <table className='table table-sm table-bordered table-striped md-table'>{children}</table>
            </div>
        ),
    }), [markdownPath, loadImage]);

    if (!text.trim()) {
        return <div className='text-muted file-viewer-empty'>The file is empty.</div>;
    }
    return (
        <div className='markdown-body' ref={container}>
            <Markdown remarkPlugins={[remarkGfm]} urlTransform={urlTransform} components={components}>
                {text}
            </Markdown>
        </div>
    );
}
