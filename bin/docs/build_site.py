#!/usr/bin/env python3
"""Build the Zensical documentation source (.docs/) from README.md.

README.md stays the only source of the text. Every "## " section becomes one page,
"Configuration and features" becomes one page per "### " section. Headings keep the
anchors GitHub gives them ({#slug}), so the README's "#anchor" links work across pages.
The hand-written landing page and style are in docs/site/; the nav is in zensical.toml,
and this script fails if a page is missing from it.

    python3 bin/docs/build_site.py && zensical build -f zensical.toml
"""
import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.docs'
REPO_BLOB = 'https://github.com/dirkpetersen/motuz/blob/dirk/'
SKIP = {'Contents'}  # the site has its own navigation
SPLIT = {'Configuration and features': 'features'}  # H2 -> folder of one page per H3

FENCE = re.compile(r'^\s*(```|~~~)')
HEADING = re.compile(r'^(#{1,6}) +(.*?) *$')
LINK = re.compile(r'(\])\(([^)\s]+)\)')  # only the end of a link: its text can span lines


def github_slug(text, seen):
    """The anchor GitHub gives a heading, with -1, -2, ... for repeated ones"""
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)  # links: keep their text
    slug = re.sub(r'[^\w\- ]', '', text.replace('`', '').lower()).replace(' ', '-')
    n = seen.get(slug, 0)
    seen[slug] = n + 1
    return slug if n == 0 else '{}-{}'.format(slug, n)


def parse(lines):
    """[(level, title, slug, line index)] of the headings outside code blocks"""
    headings, seen, fenced = [], {}, False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := HEADING.match(line)):
            title = m.group(2)
            headings.append((len(m.group(1)), title, github_slug(title, seen), i))
    return headings


def pages(lines, headings):
    """[(page path, first line, last line + 1, heading level that becomes H1)]"""
    h2 = [h for h in headings if h[0] == 2]
    result = []
    for n, (_, title, slug, start) in enumerate(h2):
        end = h2[n + 1][3] if n + 1 < len(h2) else len(lines)
        if title in SKIP:
            continue
        if title not in SPLIT:
            result.append((slug + '.md', start, end, 2))
            continue
        folder = SPLIT[title]
        h3 = [h for h in headings if h[0] == 3 and start < h[3] < end]
        result.append((folder + '/index.md', start, h3[0][3] if h3 else end, 2))
        for m, (_, _, sub, sub_start) in enumerate(h3):
            sub_end = h3[m + 1][3] if m + 1 < len(h3) else end
            result.append(('{}/{}.md'.format(folder, sub), sub_start, sub_end, 3))
    return result


def render(lines, start, end, top, path, anchors, broken):
    """The page text: headings shifted up and given their GitHub anchor, links fixed"""
    up = '../' * path.count('/')
    slugs = {line: slug for slug, (_, line) in anchors.items()}

    def link(m):
        text, target = m.groups()
        if target.startswith('#'):
            if target[1:] not in anchors:
                broken.append('{}: {}'.format(path, target))
                return m.group(0)
            page = anchors[target[1:]][0]
            return '{}({}{})'.format(text, '' if page == path else up + page, target)
        if target.startswith('docs/img/'):
            return '{}({}{})'.format(text, up, target[len('docs/'):])
        if re.match(r'^[a-z]+:', target):  # https:, mailto:
            return m.group(0)
        return '{}({}{})'.format(text, REPO_BLOB, target.lstrip('/'))

    out, fenced = [], False
    for i in range(start, end):
        line = lines[i]
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            if i in slugs:
                level, title = HEADING.match(line).groups()
                line = '{} {} {{#{}}}'.format('#' * (len(level) - top + 1), title, slugs[i])
            else:
                line = LINK.sub(link, line)
        out.append(line)
    return '\n'.join(out).strip() + '\n'


def nav_pages(nav):
    for item in nav:
        for value in (item.values() if isinstance(item, dict) else [item]):
            if isinstance(value, list):
                yield from nav_pages(value)
            else:
                yield value


def main():
    lines = (ROOT / 'README.md').read_text().splitlines()
    headings = parse(lines)
    plan = pages(lines, headings)
    # Each anchor points to the page of the last page start at or before its heading.
    anchors = {}
    for _, _, slug, i in headings:
        page = next((p for p, s, e, _ in plan if s <= i < e), None)
        if page:
            anchors[slug] = (page, i)

    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / 'docs' / 'site', OUT)
    shutil.copytree(ROOT / 'docs' / 'img', OUT / 'img')
    (OUT / 'assets').mkdir(exist_ok=True)
    for name in ('logo.png', 'favicon.ico'):
        shutil.copy(ROOT / 'src' / 'frontend' / 'img' / name, OUT / 'assets' / name)

    broken = []
    for path, start, end, top in plan:
        target = OUT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(lines, start, end, top, path, anchors, broken))

    config = tomllib.loads((ROOT / 'zensical.toml').read_text())
    in_nav = set(nav_pages(config['project']['nav']))
    built = {p.relative_to(OUT).as_posix() for p in OUT.rglob('*.md')}
    problems = ['not in the nav of zensical.toml: ' + p for p in sorted(built - in_nav)]
    problems += ['in the nav but not built: ' + p for p in sorted(in_nav - built)]
    problems += ['link to a missing anchor in ' + b for b in broken]
    for problem in problems:
        print(problem, file=sys.stderr)
    print('{} pages in {}'.format(len(built), OUT.relative_to(ROOT)))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
