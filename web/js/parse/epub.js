// EPUB reader on top of JSZip (own parser instead of epub.js: we need our own pagination/anchoring, TOC and offsets,
// and epub.js's iframe-per-chapter rendition does not give stable text offsets across reflows).
import { sanitizeChildren, parseMarkup } from './sanitize.js';
import { decodeText } from '../core/textenc.js';

let zipLibPromise = null;
export function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('无法加载 ' + src));
    document.head.append(s);
  });
}
export async function getJSZip() {
  if (!zipLibPromise) {
    zipLibPromise = (globalThis.JSZip ? Promise.resolve() : loadScript(new URL('../../vendor/jszip.min.js', import.meta.url).href))
      .then(() => globalThis.JSZip);
  }
  return zipLibPromise;
}

const MAX_ENTRY_BYTES = 64 * 1024 * 1024;   // one file inside the zip (zip-bomb guard)
const NS_OPS = 'http://www.idpf.org/2007/ops';

const dirname = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
function resolvePath(baseDir, ref) {
  let path = ref.split('#')[0].split('?')[0];
  try { path = decodeURIComponent(path); } catch { /* keep raw */ }
  const parts = (path.startsWith('/') ? path.slice(1) : (baseDir ? baseDir + '/' : '') + path).split('/');
  const out = [];
  for (const p of parts) {
    if (p === '..') out.pop();
    else if (p && p !== '.') out.push(p);
  }
  return out.join('/');
}

const MIME = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml', bmp: 'image/bmp', avif: 'image/avif' };
const extOf = (p) => (p.split('.').pop() || '').toLowerCase();

function xml(text) {
  const d = new DOMParser().parseFromString(text, 'application/xml');
  if (d.getElementsByTagName('parsererror').length) throw new Error('XML 解析失败');
  return d;
}
const byTag = (root, name) => Array.from(root.getElementsByTagNameNS('*', name));
const first = (root, name) => root.getElementsByTagNameNS('*', name)[0] || null;

/**
 * plain-text length + first heading of a spine document, without building a DOM (cheap enough for 1000s of files).
 * Also what the comic detector (core/comic.js) needs: image count (<img>, <svg><image>) and visible characters without whitespace.
 */
export function scanMarkup(raw) {
  // comments first (like pyreader/comic.py): an <img> or text inside <!-- --> is neither an image nor visible
  let s = raw.replace(/<!--[\s\S]*?-->/g, '').replace(/<head[\s\S]*?<\/head>/i, '').replace(/<(script|style)[\s\S]*?<\/\1\s*>/gi, '');
  const hm = /<h[1-3][^>]*>([\s\S]*?)<\/h[1-3]\s*>/i.exec(s);
  const strip = (x) => unescapeEntities(x.replace(/<[^>]*>/g, '')).replace(/\s+/g, ' ').trim();
  const title = hm ? strip(hm[1]).slice(0, 80) : '';
  const imgs = (s.match(/<img\b/gi) || []).length + (s.match(/<image\b/gi) || []).length;
  s = strip(s.replace(/([　-鿿])\s*\n\s*(?=[　-鿿])/g, '$1'));
  return { len: s.length, title, imgs, chars: s.replace(/\s/g, '').length };
}

const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
/** one pass (so "&amp;lt;" stays "&lt;"): numeric decimal/hex references and the common named ones; anything else is left as is */
function unescapeEntities(x) {
  return x.replace(/&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|([a-zA-Z]+));/g, (m, dec, hex, name) => {
    if (name) return Object.prototype.hasOwnProperty.call(NAMED_ENTITIES, name) ? NAMED_ENTITIES[name] : m;
    const cp = dec ? parseInt(dec, 10) : parseInt(hex, 16);
    return !cp || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff) ? '�' : String.fromCodePoint(cp);
  });
}

export async function openEpub(blob) {
  const JSZip = await getJSZip();
  let zip;
  try { zip = await JSZip.loadAsync(blob); } catch { throw new Error('不是有效的 EPUB 文件（无法解压）'); }
  const names = new Map();
  zip.forEach((p) => names.set(p.normalize('NFC').toLowerCase(), p));
  const entry = (p) => zip.file(p) || (names.has(p.normalize('NFC').toLowerCase()) ? zip.file(names.get(p.normalize('NFC').toLowerCase())) : null);
  async function bytes(p) {
    const e = entry(p);
    if (!e) return null;
    const size = e._data && e._data.uncompressedSize;
    if (size && size > MAX_ENTRY_BYTES) throw new Error('EPUB 内含过大的文件');
    return e.async('uint8array');
  }
  const text = async (p) => { const b = await bytes(p); return b ? decodeText(b).text : null; };

  const container = await text('META-INF/container.xml');
  if (!container) throw new Error('EPUB 缺少 container.xml');
  const rootfile = byTag(xml(container), 'rootfile')[0];
  const opfPath = rootfile && rootfile.getAttribute('full-path');
  if (!opfPath) throw new Error('EPUB 缺少 OPF');
  const opfText = await text(opfPath);
  if (!opfText) throw new Error('EPUB OPF 文件缺失');
  const opf = xml(opfText);
  const opfDir = dirname(opfPath);

  const meta = first(opf, 'metadata');
  const dc = (n) => (meta ? byTag(meta, n).map((e) => e.textContent.trim()).filter(Boolean) : []);
  const title = dc('title')[0] || '';
  const author = dc('creator').join(', ');
  const language = dc('language')[0] || '';

  const manifest = {};
  for (const it of byTag(first(opf, 'manifest') || opf, 'item')) {
    const id = it.getAttribute('id');
    if (id) manifest[id] = { id, href: resolvePath(opfDir, it.getAttribute('href') || ''), type: it.getAttribute('media-type') || '', props: it.getAttribute('properties') || '' };
  }
  const spineEl = first(opf, 'spine');
  const itemrefs = spineEl ? byTag(spineEl, 'itemref') : [];
  let linear = itemrefs.filter((r) => r.getAttribute('linear') !== 'no');
  if (!linear.length) linear = itemrefs;   // every item marked linear="no": read them all rather than refuse the book
  const spineIds = linear.map((r) => r.getAttribute('idref'));
  const bookTypeMeta = meta && byTag(meta, 'meta').find((m) => (m.getAttribute('name') || '').toLowerCase() === 'book-type');
  const bookType = ((bookTypeMeta && bookTypeMeta.getAttribute('content')) || '').trim().toLowerCase();
  const ppdAttr = ((spineEl && spineEl.getAttribute('page-progression-direction')) || '').trim().toLowerCase();
  const ppd = ppdAttr === 'rtl' || ppdAttr === 'ltr' ? ppdAttr : null;
  // `docs` = spine documents. `sections` (public) = reading units: a document, or - for a very large document - several
  // parts of ~40k characters, because multi-column layout of one million-character chapter takes seconds per page turn.
  const docs = spineIds.map((id) => manifest[id]).filter((m) => m && /x?html|xml/i.test(m.type || 'html')).map((m) => ({ id: m.id, path: m.href, heading: '', len: 0 }));
  if (!docs.length) throw new Error('EPUB 没有可阅读的内容');
  const docIndex = new Map(docs.map((d, i) => [d.path.normalize('NFC'), i]));

  // ---- table of contents: EPUB3 nav, else NCX
  const tocRaw = [];
  const pushToc = (title, href, base, level) => {
    const path = resolvePath(dirname(base), href);
    const d = docIndex.get(path.normalize('NFC'));
    if (d === undefined) return;
    const hash = href.includes('#') ? href.slice(href.indexOf('#') + 1) : '';
    tocRaw.push({ title: title.replace(/\s+/g, ' ').trim() || '', d, id: decodeHash(hash), level });
  };
  const navItem = Object.values(manifest).find((m) => /\bnav\b/.test(m.props));
  let tocDone = false;
  if (navItem) {
    try {
      const nd = parseMarkup(await text(navItem.href) || '', { prefer: 'xml' }).doc;
      const navs = Array.from(nd.getElementsByTagName('nav')).concat(byTag(nd, 'nav'));
      const nav = navs.find((n) => (n.getAttributeNS(NS_OPS, 'type') || n.getAttribute('epub:type') || '').split(/\s+/).includes('toc')) || navs[0];
      const walk = (ol, level) => {
        for (const li of Array.from(ol.children).filter((c) => c.localName === 'li')) {
          const a = Array.from(li.children).find((c) => c.localName === 'a');
          const label = Array.from(li.children).find((c) => c.localName === 'a' || c.localName === 'span');
          if (a && a.getAttribute('href')) pushToc(a.textContent, a.getAttribute('href'), navItem.href, level);
          else if (label) { /* heading without link: skip entry but descend */ }
          const sub = Array.from(li.children).find((c) => c.localName === 'ol');
          if (sub) walk(sub, level + 1);
        }
      };
      const ol = nav && (nav.getElementsByTagName('ol')[0] || byTag(nav, 'ol')[0]);
      if (ol) { walk(ol, 0); tocDone = tocRaw.length > 0; }
    } catch { /* fall through to NCX */ }
  }
  if (!tocDone) {
    const ncxId = spineEl && spineEl.getAttribute('toc');
    const ncxItem = (ncxId && manifest[ncxId]) || Object.values(manifest).find((m) => /ncx/.test(m.type));
    if (ncxItem) {
      try {
        const nd = xml(await text(ncxItem.href));
        const walk = (parent, level) => {
          for (const np of Array.from(parent.children).filter((c) => c.localName === 'navPoint')) {
            const label = first(np, 'text');
            const content = Array.from(np.children).find((c) => c.localName === 'content');
            if (content && content.getAttribute('src')) pushToc(label ? label.textContent : '', content.getAttribute('src'), ncxItem.href, level);
            walk(np, level + 1);
          }
        };
        const map = first(nd, 'navMap');
        if (map) walk(map, 0);
      } catch { /* no toc */ }
    }
  }

  const SPLIT_LEN = 60000;   // a document with more text than this is cut into parts
  const PART_LEN = 40000;
  const BLOCK_WRAPPERS = /^(div|section|article|main|body)$/;
  let spec = null;           // per document: null | {ranges:[[a,b)..], lens:[..], ids:{id:partIdx}}
  let docLens = [];
  let sections = [];
  let docFirst = [];
  let viewCache = null;      // {d, blocks, urls}: sanitized document (with images) for part-sections being read
  let textCache = null;      // {d, blocks}: sanitized without images, for search / part building

  /** sanitize one document; images optionally resolved to blob: URLs */
  async function sanitizeDoc(path, images) {
    const src = await text(path);
    if (src === null) throw new Error('章节文件缺失：' + path);
    const { body } = parseMarkup(src, { prefer: 'xml' });
    const frag = sanitizeChildren(body);
    const urls = [];
    await Promise.all(Array.from(frag.querySelectorAll('img[data-src]')).map(async (img) => {
      const ref = img.getAttribute('data-src');
      img.removeAttribute('data-src');
      if (!images || /^(https?:|data:)/i.test(ref)) { if (images) img.remove(); return; }   // never load remote images from a book
      const p = resolvePath(dirname(path), ref);
      const b = await bytes(p);
      if (!b) { img.remove(); return; }
      const url = URL.createObjectURL(new Blob([b], { type: MIME[extOf(p)] || 'application/octet-stream' }));
      urls.push(url);
      img.src = url;
    }));
    return { frag, urls };
  }
  const flatten = (frag) => {
    let nodes = Array.from(frag.childNodes);
    while (nodes.length === 1 && nodes[0].nodeType === 1 && BLOCK_WRAPPERS.test(nodes[0].localName)) nodes = Array.from(nodes[0].childNodes);
    return nodes;
  };
  async function docBlocks(d, images) {
    if (images) {
      if (viewCache && viewCache.d === d) return viewCache;
      if (viewCache) viewCache.urls.forEach((u) => URL.revokeObjectURL(u));
      viewCache = null;
      const { frag, urls } = await sanitizeDoc(docs[d].path, true);
      viewCache = { d, blocks: flatten(frag), urls };
      return viewCache;
    }
    if (textCache && textCache.d === d) return textCache;
    const { frag } = await sanitizeDoc(docs[d].path, false);
    textCache = { d, blocks: flatten(frag) };
    return textCache;
  }
  async function buildParts(d) {
    const { blocks } = await docBlocks(d, false);
    const ranges = [], lens = [];
    let a = 0, acc = 0;
    blocks.forEach((n, i) => {
      if (acc >= PART_LEN && i > a) { ranges.push([a, i]); lens.push(acc); a = i; acc = 0; }
      acc += (n.textContent || '').length;
    });
    ranges.push([a, blocks.length]); lens.push(acc);
    const ids = {};
    let nIds = 0;   // bounded: element ids -> part index, used to resolve TOC / link targets
    ranges.forEach(([x, y], k) => {
      for (let i = x; i < y && nIds < 20000; i++) {
        const n = blocks[i];
        if (n.nodeType !== 1) continue;
        if (n.id) { ids[n.id] = k; nIds++; }
        for (const e of n.querySelectorAll('[id]')) { if (nIds >= 20000) break; ids[e.id] = k; nIds++; }
      }
    });
    return { ranges, lens, ids };
  }

  const book = {
    format: 'epub', title, author, language, docs, sections: [], toc: [], lens: [],
    bookType, ppd,   // OPF <meta name="book-type"> (lower case, '') and <spine page-progression-direction> ('rtl' | 'ltr' | null)
    docStats: null,  // set by scan(): [{imgs, chars}] per spine document, the input of core/comic.js detectComic
    /** (re)build sections/toc/lens from docLens + parts spec */
    layout(docLensIn, headings, specIn) {
      docLens = docLensIn;
      spec = specIn || docs.map(() => null);
      docs.forEach((d, i) => { d.len = docLens[i] || 0; if (headings) d.heading = headings[i] || ''; });
      sections = [];
      const lens = [];
      docs.forEach((doc, d) => {
        docFirst[d] = sections.length;
        const p = spec[d];
        if (p) p.ranges.forEach((r, k) => { sections.push({ doc: d, range: r, part: k, heading: k ? '' : doc.heading, title: '' }); lens.push(p.lens[k]); });
        else { sections.push({ doc: d, range: null, part: 0, heading: doc.heading, title: '' }); lens.push(docLens[d] || 0); }
      });
      this.sections = sections;
      this.lens = lens;
      this.toc = tocRaw.map((t) => {
        const p = spec[t.d];
        const k = p && t.id && Object.hasOwn(p.ids, t.id) ? p.ids[t.id] : 0;
        return { title: t.title, s: docFirst[t.d] + k, id: t.id, level: t.level };
      });
      const byS = new Map();
      for (const t of this.toc) if (!byS.has(t.s) && t.title) byS.set(t.s, t.title);
      sections.forEach((s, i) => {
        const base = byS.get(i) || s.heading || '';
        s.title = s.part ? `${base || `第 ${s.doc + 1} 节`}（续）` : base || `第 ${i + 1} 节`;
      });
      if (!this.toc.length) this.toc = sections.filter((s) => !s.part).map((s) => ({ title: s.title, s: sections.indexOf(s), id: '', level: 0 }));
    },
    /** what must be persisted to open the book again without scanning */
    layoutData() { return { docLens, headings: docs.map((d) => d.heading), parts: spec }; },
    async scan(onProgress) {
      const lensOut = [], parts = [], stats = [];
      for (let i = 0; i < docs.length; i++) {
        const t = await text(docs[i].path);
        const r = scanMarkup(t || '');
        lensOut.push(r.len);
        stats.push({ imgs: r.imgs, chars: r.chars });
        docs[i].heading = r.title;
        parts.push(r.len > SPLIT_LEN ? await buildParts(i) : null);
        if (onProgress && i % 8 === 0) { onProgress((i + 1) / docs.length); await new Promise((res) => setTimeout(res, 0)); }
      }
      textCache = null;
      this.docStats = stats;
      this.layout(lensOut, docs.map((d) => d.heading), parts);
      return this.lens;
    },
    async loadSection(i) {
      const sec = sections[i];
      if (!sec.range) {
        const { frag, urls } = await sanitizeDoc(docs[sec.doc].path, true);
        return { frag, cleanup: () => urls.forEach((u) => URL.revokeObjectURL(u)) };
      }
      const { blocks } = await docBlocks(sec.doc, true);
      const f = document.createDocumentFragment();
      for (const n of blocks.slice(sec.range[0], sec.range[1])) f.append(n.cloneNode(true));
      return { frag: f, cleanup() {} };   // blob URLs live as long as the document cache (replaced when another document is read)
    },
    async sectionText(i) {
      const sec = sections[i];
      let nodes;
      if (sec.range) nodes = (await docBlocks(sec.doc, false)).blocks.slice(sec.range[0], sec.range[1]);
      else nodes = [(await sanitizeDoc(docs[sec.doc].path, false)).frag];
      let s = '';
      for (const n of nodes) {
        const w = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
        if (n.nodeType === 3) s += n.nodeValue;
        while (w.nextNode()) s += w.currentNode.nodeValue;
      }
      return s;
    },
    /** internal link -> {s, id}; null for external/unknown */
    resolveHref(fromS, href) {
      if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href)) return null;
      const hash = href.includes('#') ? decodeHash(href.slice(href.indexOf('#') + 1)) : '';
      const pathPart = href.split('#')[0];
      let d = sections[fromS].doc;
      if (pathPart) {
        d = docIndex.get(resolvePath(dirname(docs[d].path), pathPart).normalize('NFC'));
        if (d === undefined) return null;
      }
      const p = spec && spec[d];
      const k = p && hash && Object.hasOwn(p.ids, hash) ? p.ids[hash] : 0;
      return { s: docFirst[d] + k, id: hash };
    },
    async cover() {
      const cands = [];
      const ci = Object.values(manifest).find((m) => /cover-image/.test(m.props));
      if (ci) cands.push(ci.href);
      const metaCover = meta && byTag(meta, 'meta').find((m) => m.getAttribute('name') === 'cover');
      if (metaCover && manifest[metaCover.getAttribute('content')]) cands.push(manifest[metaCover.getAttribute('content')].href);
      for (const m of Object.values(manifest)) if (/^image\//.test(m.type) && /cover/i.test(m.id + m.href)) cands.push(m.href);
      for (const p of cands) {
        const b = await bytes(p);
        if (b && b.length > 200) return new Blob([b], { type: MIME[extOf(p)] || 'image/jpeg' });
      }
      // last resort: first image of the first section
      try {
        const { frag, cleanup } = await this.loadSection(0);
        const img = frag.querySelector('img');
        if (img && img.src) { const r = await (await fetch(img.src)).blob(); cleanup(); return r; }
        cleanup();
      } catch { /* none */ }
      return null;
    },
    close() { /* blob URLs are revoked per section */ },
  };
  return book;
}

function decodeHash(h) { try { return decodeURIComponent(h); } catch { return h; } }
