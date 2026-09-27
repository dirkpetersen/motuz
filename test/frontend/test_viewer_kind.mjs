// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {
    dirnameOf,
    extensionOf,
    fragmentTarget,
    markdownImageSource,
    normalizePath,
    safeLinkUrl,
    viewerKind,
} from '../../src/frontend/js/utils/viewerKind.js';

test('the viewer is chosen by extension, case-insensitive', () => {
    for (const name of ['a.png', 'A.PNG', 'photo.jpg', 'photo.JPeG', 'x.gif', 'y.webp', 'dir.v2/scan.Jpg']) {
        assert.equal(viewerKind(name), 'image', name);
    }
    for (const name of ['README.md', 'notes.MD', 'doc.markdown', 'Doc.MarkDown']) {
        assert.equal(viewerKind(name), 'markdown', name);
    }
    for (const name of ['logo.svg', 'LOGO.SVG', 'a.txt', 'Makefile', '.md', '.png', 'png', 'a.png.txt',
                        'a.md.bak', 'a.', '', 'image.tif', 'page.html', 'archive.mdx']) {
        assert.equal(viewerKind(name), 'text', name);
    }
    for (const name of ['report.PDF', 'a.docx', 'b.xlsx', 'old.xls', 'c.ods', 'talk.pptx', 'legacy.doc', 'legacy.ppt']) {
        assert.equal(viewerKind(name), 'document', name);
    }
    assert.equal(viewerKind(undefined), 'text');
    assert.equal(viewerKind(null), 'text');
});

test('extensionOf', () => {
    assert.equal(extensionOf('a.tar.GZ'), 'gz');
    assert.equal(extensionOf('/x/y.d/z'), '');
    assert.equal(extensionOf('.bashrc'), '');
    assert.equal(extensionOf('trailing.'), '');
});

test('only http, https and mailto links are links', () => {
    for (const url of ['https://example.org/', 'http://example.org/a?b=c#d', 'HTTPS://EXAMPLE.ORG',
                       'mailto:someone@example.org']) {
        assert.equal(safeLinkUrl(url), url, url);
    }
    for (const url of ['javascript:alert(1)', 'JavaScript:alert(1)', 'java\tscript:alert(1)', ' javascript:alert(1)',
                       'data:text/html,<script>alert(1)</script>', 'vbscript:msgbox(1)', 'file:///etc/passwd',
                       'ftp://example.org/', 'blob:https://x/1', 'other.md', './other.md', '/abs/path', '#section',
                       '//evil.example.org/x', 'https:', 'https://', 'mailto:', '', null, undefined, 42,
                       'https://example.org/\u0000', 'https://exa mple.org/', 'https://example.org/\u2028',
                       'https://user:pass@example.org/', 'https://trusted.example@evil.example/']) {
        assert.equal(safeLinkUrl(url), null, String(url));
    }
});

test('fragment links', () => {
    assert.equal(fragmentTarget('#user-content-fn-1'), 'user-content-fn-1');
    assert.equal(fragmentTarget('#caf%C3%A9'), 'café');
    for (const url of ['#', 'section', 'https://x/#a', '#a b', '#%E0%A4%A', null]) {
        assert.equal(fragmentTarget(url), null, String(url));
    }
});

test('paths', () => {
    assert.equal(normalizePath('/a/./b/../c//d'), '/a/c/d');
    assert.equal(normalizePath('/../../etc/x'), '/etc/x');
    assert.equal(normalizePath('a/../../b'), '../b');
    assert.equal(dirnameOf('/a/b/c.md'), '/a/b');
    assert.equal(dirnameOf('/c.md'), '/');
    assert.equal(dirnameOf('c.md'), '');
});

test('images in Markdown: relative paths are resolved against the file, remote ones never loaded', () => {
    const md = '/home/alice/project/README.md';
    assert.deepEqual(markdownImageSource('img/shot.png', md), {kind: 'local', path: '/home/alice/project/img/shot.png'});
    assert.deepEqual(markdownImageSource('./shot.JPG', md), {kind: 'local', path: '/home/alice/project/shot.JPG'});
    assert.deepEqual(markdownImageSource('../logo.webp', md), {kind: 'local', path: '/home/alice/logo.webp'});
    assert.deepEqual(markdownImageSource('../../../../../etc/x.png', md), {kind: 'local', path: '/etc/x.png'});
    assert.deepEqual(markdownImageSource('/data/plot.gif', md), {kind: 'local', path: '/data/plot.gif'});
    assert.deepEqual(markdownImageSource('my%20shot.png?raw=true#x', md), {kind: 'local', path: '/home/alice/project/my shot.png'});
    // cloud paths look the same (bucket or container first)
    assert.deepEqual(markdownImageSource('fig.png', '/bucket/docs/README.md'), {kind: 'local', path: '/bucket/docs/fig.png'});
    assert.deepEqual(markdownImageSource('logo.svg', md), {kind: 'unsupported', path: '/home/alice/project/logo.svg'});
    assert.deepEqual(markdownImageSource('diagram', md), {kind: 'unsupported', path: '/home/alice/project/diagram'});
    for (const src of ['https://tracker.example.org/pixel.png', 'http://x/y.png', 'HTTPS://X/Y.PNG',
                       '//cdn.example.org/a.png', 'ftp://x/a.png', 'javascript:alert(1)', 'file:///etc/a.png']) {
        assert.deepEqual(markdownImageSource(src, md), {kind: 'remote'}, src);
    }
    assert.deepEqual(markdownImageSource('data:image/png;base64,AAAA', md), {kind: 'embedded'});
    for (const src of ['', '   ', null, undefined, '%E0%A4%A.png', 'a\u0000.png', '?x', '#y', '/']) {
        assert.deepEqual(markdownImageSource(src, md), {kind: 'none'}, String(src));
    }
});
