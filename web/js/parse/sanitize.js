// Allowlist HTML sanitizer for book content (EPUB/HTML/Markdown). Builds a *new* DOM from an untrusted parsed one:
// only known tags and attributes are copied; scripts, styles, event handlers, forms, iframes... never make it across.
// Whitespace is normalised so the DOM text (what reading offsets refer to) is stable and close to the plain text length.

const BLOCK = new Set(['p', 'div', 'section', 'article', 'aside', 'header', 'footer', 'main', 'nav', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'blockquote', 'pre', 'figure',
  'figcaption', 'hr', 'br', 'address', 'body']);
const KEEP = new Set(['p', 'div', 'span', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'em', 'i', 'strong', 'b', 'u', 's', 'del', 'ins',
  'sub', 'sup', 'small', 'blockquote', 'q', 'cite', 'pre', 'code', 'kbd', 'samp', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'table', 'thead',
  'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'figure', 'figcaption', 'ruby', 'rt', 'rp', 'rb', 'mark', 'abbr', 'address', 'a', 'img',
  'section', 'article', 'aside', 'header', 'footer', 'main', 'nav']);
const RENAME = { center: 'div', tt: 'code', strike: 's', big: 'span', font: 'span', dir: 'ul', menu: 'ul' };
const DROP = new Set(['script', 'style', 'head', 'title', 'meta', 'link', 'iframe', 'object', 'embed', 'applet', 'form', 'input', 'button',
  'select', 'textarea', 'audio', 'video', 'source', 'track', 'canvas', 'noscript', 'template', 'base', 'frame', 'frameset', 'math', 'svg']);
const ATTRS = {
  a: ['href', 'id', 'title', 'lang'], img: ['src', 'alt', 'title', 'width', 'height'], td: ['colspan', 'rowspan'], th: ['colspan', 'rowspan'],
  ol: ['start'], '*': ['id', 'lang', 'dir'],
};
const SAFE_URL = /^(https?:|mailto:|#|[^:]*$)/i;   // relative refs, anchors, http(s), mailto. Never javascript:/data:/vbscript:

function localName(n) { return (n.localName || n.nodeName || '').toLowerCase(); }

function alignOf(style) {
  const m = /text-align\s*:\s*(center|right)/i.exec(style || '');
  return m ? m[1].toLowerCase() : null;
}

/**
 * @param {Node} root parsed element (usually body)
 * @param {{doc?: Document}} opts
 * @returns {DocumentFragment}
 */
export function sanitizeChildren(root, { doc = document } = {}) {
  const frag = doc.createDocumentFragment();
  const walk = (src, dst, inPre) => {
    for (let n = src.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) {
        let t = n.nodeValue;
        if (!inPre) {
          // a source line break between two CJK characters is not a space (CSS segment-break rule)
          t = t.replace(/([　-鿿＀-￯])[ \t]*[\r\n]\s*(?=[　-鿿＀-￯])/g, '$1');
          t = t.replace(/[ \t\r\n\f]+/g, ' ');
        }
        if (t) dst.append(doc.createTextNode(t));
        continue;
      }
      if (n.nodeType !== 1) continue;
      let tag = localName(n);
      if (DROP.has(tag)) {
        if (tag === 'svg') {   // cover pages are often <svg><image xlink:href=...>
          const im = n.querySelector && (n.querySelector('image'));
          const href = im && (im.getAttribute('xlink:href') || im.getAttribute('href'));
          if (href) { const img = doc.createElement('img'); img.setAttribute('data-src', href); img.alt = ''; dst.append(img); }
        }
        continue;
      }
      tag = RENAME[tag] || tag;
      if (!KEEP.has(tag)) { walk(n, dst, inPre); continue; }   // unknown element: keep its content, drop the wrapper
      const el = doc.createElement(tag);
      const allowed = (ATTRS[tag] || []).concat(ATTRS['*']);
      for (const a of Array.from(n.attributes || [])) {
        const name = a.name.toLowerCase();
        if (!allowed.includes(name)) continue;
        let v = a.value;
        if (name === 'href') {
          if (!SAFE_URL.test(v.trim())) continue;
          el.setAttribute('data-href', v.trim());
          continue;
        }
        if (name === 'src') { el.setAttribute('data-src', v.trim()); continue; }   // resolved later against the book (blob: URL)
        if (name === 'id') v = v.slice(0, 200);
        el.setAttribute(name, v);
      }
      const al = alignOf(n.getAttribute && n.getAttribute('style'));
      if (al) el.setAttribute('data-align', al);
      if (/^h[1-6]$/.test(tag) && n.getAttribute && /text-align\s*:\s*center/i.test(n.getAttribute('style') || '')) el.setAttribute('data-align', 'center');
      dst.append(el);
      if (tag === 'img' || tag === 'br' || tag === 'hr') continue;
      walk(n, el, inPre || tag === 'pre');
    }
  };
  walk(root, frag, false);
  tidyWhitespace(frag);
  return frag;
}

/** drop whitespace-only text between blocks, trim block edges, drop empty wrappers without content */
function tidyWhitespace(root) {
  const isBlock = (n) => n && n.nodeType === 1 && BLOCK.has(n.localName);
  const visit = (el) => {
    if (el.localName === 'pre') return;
    for (let n = el.firstChild; n;) {
      const next = n.nextSibling;
      if (n.nodeType === 3) {
        const prev = n.previousSibling;
        const edgeBefore = !prev || isBlock(prev);
        const edgeAfter = !next || isBlock(next);
        if (edgeBefore) n.nodeValue = n.nodeValue.replace(/^\s+/, '');
        if (edgeAfter) n.nodeValue = n.nodeValue.replace(/\s+$/, '');
        if (!n.nodeValue) n.remove();
      } else if (n.nodeType === 1) visit(n);
      n = next;
    }
  };
  if (root.nodeType === 11) { for (const c of Array.from(root.childNodes)) if (c.nodeType === 1) visit(c); else if (c.nodeType === 3 && !c.nodeValue.trim()) c.remove(); } else visit(root);
}

/** Parse markup (XHTML first, HTML as fallback) and return its body/root element, or null. */
export function parseMarkup(text, { prefer = 'xml' } = {}) {
  if (prefer === 'xml') {
    const d = new DOMParser().parseFromString(text, 'application/xhtml+xml');
    if (!d.getElementsByTagName('parsererror').length && d.documentElement) {
      const body = Array.from(d.documentElement.children).find((c) => localName(c) === 'body');
      return { doc: d, body: body || d.documentElement };
    }
  }
  const d = new DOMParser().parseFromString(text, 'text/html');
  return { doc: d, body: d.body };
}

export function textOf(frag) {
  let s = '';
  const w = document.createTreeWalker(frag, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) s += w.currentNode.nodeValue;
  return s;
}
