/**
 * A small, non-validating XML parser for the parts of Office files the document
 * viewer reads itself (PPTX slides, relationships, [Content_Types].xml). Web Workers
 * and Node have no DOMParser, so this runs in both (documentWorker.js, node --test).
 *
 * Deliberately limited: only the five predefined entities and character references
 * are expanded, a DOCTYPE is refused (no entity definitions, so no "billion laughs"),
 * and the number of nodes and the nesting depth are capped. Elements are
 * {name, local, prefix, attrs, children, parent}; text is {text}. Namespace prefixes
 * are resolved on demand (attrNS), which is all OOXML needs.
 */

export class XmlError extends Error {
    constructor(message) {
        super(message);
        this.name = 'XmlError';
    }
}

export const XML_LIMITS = Object.freeze({maxNodes: 2000000, maxDepth: 256});

const PREDEFINED = {lt: '<', gt: '>', amp: '&', quot: '"', apos: "'"};

export function decodeEntities(text) {
    if (text.indexOf('&') < 0) {
        return text;
    }
    return text.replace(/&(#x[0-9a-fA-F]{1,6}|#[0-9]{1,7}|[a-zA-Z]{2,4});/g, (match, name) => {
        if (name[0] === '#') {
            const code = name[1] === 'x' ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
            if (code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff) || code === 0) {
                return '\uFFFD';
            }
            return String.fromCodePoint(code);
        }
        return Object.prototype.hasOwnProperty.call(PREDEFINED, name) ? PREDEFINED[name] : match;
    });
}

const NAME_END = /[\s/>]/;
const ATTR = /([^\s=/>]+)\s*=\s*("([^"]*)"|'([^']*)')/g;

function splitName(name) {
    const colon = name.indexOf(':');
    return colon < 0 ? {prefix: '', local: name} : {prefix: name.slice(0, colon), local: name.slice(colon + 1)};
}

/** The document node ({name: '#document', children}) of an XML string */
export function parseXml(xml, limits = XML_LIMITS) {
    const root = {name: '#document', local: '#document', prefix: '', attrs: {}, children: [], parent: null};
    let current = root;
    let depth = 0;
    let nodes = 0;
    let pos = 0;
    const length = xml.length;

    const count = () => {
        if (++nodes > limits.maxNodes) {
            throw new XmlError('The document has too many XML nodes');
        }
    };
    const addText = (raw) => {
        if (raw && current !== root) {
            count();
            current.children.push({text: decodeEntities(raw)});
        }
    };

    while (pos < length) {
        const lt = xml.indexOf('<', pos);
        if (lt < 0) {
            addText(xml.slice(pos));
            break;
        }
        if (lt > pos) {
            addText(xml.slice(pos, lt));
        }
        if (xml.startsWith('<!--', lt)) {
            const end = xml.indexOf('-->', lt + 4);
            if (end < 0) throw new XmlError('Unterminated comment');
            pos = end + 3;
        } else if (xml.startsWith('<![CDATA[', lt)) {
            const end = xml.indexOf(']]>', lt + 9);
            if (end < 0) throw new XmlError('Unterminated CDATA section');
            if (current !== root) {
                count();
                current.children.push({text: xml.slice(lt + 9, end)});
            }
            pos = end + 3;
        } else if (xml.startsWith('<?', lt)) {
            const end = xml.indexOf('?>', lt + 2);
            if (end < 0) throw new XmlError('Unterminated processing instruction');
            pos = end + 2;
        } else if (xml.startsWith('<!', lt)) {
            throw new XmlError('Document type declarations are not allowed');
        } else if (xml[lt + 1] === '/') {
            const end = xml.indexOf('>', lt + 2);
            if (end < 0) throw new XmlError('Unterminated end tag');
            const name = xml.slice(lt + 2, end).trim();
            if (current === root || current.name !== name) {
                throw new XmlError(`Mismatched end tag </${name}>`);
            }
            current = current.parent;
            depth--;
            pos = end + 1;
        } else {
            // Start tag: find its end outside quoted attribute values
            let end = lt + 1;
            let quote = null;
            for (; end < length; end++) {
                const c = xml[end];
                if (quote) {
                    if (c === quote) quote = null;
                } else if (c === '"' || c === "'") {
                    quote = c;
                } else if (c === '>') {
                    break;
                }
            }
            if (end >= length) throw new XmlError('Unterminated start tag');
            const selfClosing = xml[end - 1] === '/';
            const inner = xml.slice(lt + 1, selfClosing ? end - 1 : end);
            const nameEnd = inner.search(NAME_END);
            const name = nameEnd < 0 ? inner : inner.slice(0, nameEnd);
            if (!name) throw new XmlError('Empty element name');
            const attrs = Object.create(null);
            if (nameEnd >= 0) {
                ATTR.lastIndex = 0;
                let match;
                const rest = inner.slice(nameEnd);
                while ((match = ATTR.exec(rest)) !== null) {
                    attrs[match[1]] = decodeEntities(match[3] !== undefined ? match[3] : match[4]);
                }
            }
            count();
            const element = {name, ...splitName(name), attrs, children: [], parent: current};
            current.children.push(element);
            if (!selfClosing) {
                if (++depth > limits.maxDepth) {
                    throw new XmlError('The XML is nested too deeply');
                }
                current = element;
            }
            pos = end + 1;
        }
    }
    if (current !== root) {
        throw new XmlError(`Unclosed element <${current.name}>`);
    }
    return root;
}

/** The root element of a parsed document */
export function documentElement(doc) {
    return doc.children.find(child => child.name) || null;
}

/** Child elements with this local name */
export function children(node, local) {
    return node && node.children ? node.children.filter(child => child.name && child.local === local) : [];
}

export function child(node, local) {
    if (!node || !node.children) return null;
    return node.children.find(c => c.name && c.local === local) || null;
}

/** Depth-first descendants with this local name (not below a match) */
export function descendants(node, local, out = []) {
    for (const c of node.children || []) {
        if (!c.name) continue;
        if (c.local === local) {
            out.push(c);
        } else {
            descendants(c, local, out);
        }
    }
    return out;
}

/** The namespace URI a prefix is bound to at `node` */
export function namespaceOf(node, prefix) {
    const key = prefix ? `xmlns:${prefix}` : 'xmlns';
    for (let n = node; n; n = n.parent) {
        if (n.attrs && key in n.attrs) {
            return n.attrs[key];
        }
    }
    return null;
}

/** Value of the attribute with this local name whose prefix is bound to one of `uris` */
export function attrNS(node, local, uris) {
    for (const [name, value] of Object.entries(node.attrs || {})) {
        const {prefix, local: l} = splitName(name);
        if (l === local && prefix && prefix !== 'xmlns' && uris.includes(namespaceOf(node, prefix))) {
            return value;
        }
    }
    return null;
}

/** Value of an unprefixed attribute */
export function attr(node, name) {
    return node && node.attrs && name in node.attrs ? node.attrs[name] : null;
}

/** All text below a node, concatenated */
export function textOf(node) {
    if (node.text !== undefined) return node.text;
    return (node.children || []).map(textOf).join('');
}
