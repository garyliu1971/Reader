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

/** plain-text length + first heading of a spine document, without building a DOM (cheap enough for 1000s of files) */
export function scanMarkup(raw) {
  let s = raw.replace(/<head[\s\S]*?<\/head>/i, '').replace(/<(script|style)[\s\S]*?<\/\1\s*>/gi, '');
  const hm = /<h[1-3][^>]*>([\s\S]*?)<\/h[1-3]\s*>/i.exec(s);
  const strip = (x) => x.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Math.min(+d, 0x10ffff) || 32)).replace(/\s+/g, ' ').trim();
  const title = hm ? strip(hm[1]).slice(0, 80) : '';
  s = strip(s.replace(/([　-鿿])\s*\n\s*(?=[　-鿿])/g, '$1'));
  return { len: s.length, title };
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
  const spineIds = spineEl ? byTag(spineEl, 'itemref').filter((r) => r.getAttribute('linear') !== 'no').map((r) => r.getAttribute('idref')) : [];
  const sections = spineIds.map((id) => manifest[id]).filter((m) => m && /x?html|xml/i.test(m.type || 'html')).map((m) => ({ id: m.id, path: m.href, title: '' }));
  if (!sections.length) throw new Error('EPUB 没有可阅读的内容');
  const sectionIndex = new Map(sections.map((s, i) => [s.path.normalize('NFC'), i]));

  // ---- table of contents: EPUB3 nav, else NCX
  const tocEntries = [];
  const pushToc = (title, href, base, level) => {
    const path = resolvePath(dirname(base), href);
    const s = sectionIndex.get(path.normalize('NFC'));
    if (s === undefined) return;
    const hash = href.includes('#') ? href.slice(href.indexOf('#') + 1) : '';
    tocEntries.push({ title: title.replace(/\s+/g, ' ').trim() || '', s, id: decodeHash(hash), level });
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
      if (ol) { walk(ol, 0); tocDone = tocEntries.length > 0; }
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

  const raw = {};   // lazily read sources for searching/scanning
  const book = {
    format: 'epub', title, author, language, sections, toc: tocEntries, lens: null,
    async scan(onProgress) {
      const lens = [];
      for (let i = 0; i < sections.length; i++) {
        const t = await text(sections[i].path);
        const r = scanMarkup(t || '');
        lens.push(r.len);
        if (!sections[i].heading) sections[i].heading = r.title;
        if (onProgress && i % 8 === 0) { onProgress((i + 1) / sections.length); await new Promise((res) => setTimeout(res, 0)); }
      }
      this.lens = lens;
      this.finishTitles();
      return lens;
    },
    finishTitles() {
      const byS = new Map();
      for (const t of tocEntries) if (!byS.has(t.s) && t.title) byS.set(t.s, t.title);
      sections.forEach((s, i) => { s.title = byS.get(i) || s.heading || `第 ${i + 1} 节`; });
      if (!tocEntries.length) sections.forEach((s, i) => tocEntries.push({ title: s.title, s: i, id: '', level: 0 }));
    },
    async loadSection(i) {
      const sec = sections[i];
      const src = await text(sec.path);
      if (src === null) throw new Error('章节文件缺失：' + sec.path);
      const { body } = parseMarkup(src, { prefer: 'xml' });
      const frag = sanitizeChildren(body);
      const urls = [];
      await Promise.all(Array.from(frag.querySelectorAll('img[data-src]')).map(async (img) => {
        const ref = img.getAttribute('data-src');
        img.removeAttribute('data-src');
        if (/^(https?:|data:)/i.test(ref)) { img.remove(); return; }   // no remote loads from a book (tracking, offline)
        const p = resolvePath(dirname(sec.path), ref);
        const b = await bytes(p);
        if (!b) { img.remove(); return; }
        const url = URL.createObjectURL(new Blob([b], { type: MIME[extOf(p)] || 'application/octet-stream' }));
        urls.push(url);
        img.src = url;
      }));
      return { frag, cleanup: () => urls.forEach((u) => URL.revokeObjectURL(u)) };
    },
    async sectionText(i) {
      const { frag, cleanup } = await this.loadSection(i);
      const w = document.createTreeWalker(frag, NodeFilter.SHOW_TEXT);
      let s = '';
      while (w.nextNode()) s += w.currentNode.nodeValue;
      cleanup();
      return s;
    },
    /** internal link -> {s, id}; null for external/unknown */
    resolveHref(fromS, href) {
      if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href)) return null;
      const hash = href.includes('#') ? decodeHash(href.slice(href.indexOf('#') + 1)) : '';
      const pathPart = href.split('#')[0];
      if (!pathPart) return { s: fromS, id: hash };
      const s = sectionIndex.get(resolvePath(dirname(sections[fromS].path), pathPart).normalize('NFC'));
      return s === undefined ? null : { s, id: hash };
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
  void raw;
  return book;
}

function decodeHash(h) { try { return decodeURIComponent(h); } catch { return h; } }
