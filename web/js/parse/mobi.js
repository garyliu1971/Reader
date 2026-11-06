// MOBI / AZW / AZW3 / PRC reader on top of the vendored foliate-js mobi.js (vendor/foliate-mobi.js, see vendor/README.md).
// Returns the same book object as parse/epub.js (sections, toc, lens, layout/layoutData/scan, loadSection, sectionText,
// resolveHref, cover, close) so reader.js / library.js treat all three alike.
//
// What is ours (the vendored parser does none of it): DRM / truncation / KFX / Print Replica detection (mobicheck.js, run BEFORE
// the vendored parser sees the file), the sanitizing pipeline, link rewriting, TOC fallback, part splitting and cover sniffing.
//
// Security: book HTML is only ever used through section.createDocument() (a detached, inert DOMParser document) and goes through
// sanitize.js. section.load() (serialized HTML in a blob: URL) is not used for display, there is no iframe. Images: only blob: URLs
// that the vendored parser handed back for a record of THIS book are accepted; every other src (remote, data:, javascript:) is dropped.
//
// MOBI6 (the old format, header version < 8) is verified on a real book. KF8 / AZW3 (version >= 8, incl. combined MOBI+KF8 files)
// is implemented by reading mobi.js and is NOT verified on a real file: it lives in the `kf8` branches below.
import { sanitizeChildren } from './sanitize.js';
import {
  MobiError, MOBI_FORMAT, PDB_HEADER_LEN, PEEK_RECORD0, pdbRecordCount, recordOffsets, inspectMobi, encryptionOf, sniffImageType,
  parseFilepos, parseKindlePos, fileposHref, kindlePosHref,
} from './mobicheck.js';

let vendorPromise = null;
const loadVendor = () => (vendorPromise ||= import('../../vendor/foliate-mobi.js'));

const SPLIT_LEN = 60000;   // a section with more text than this is cut into parts (same numbers as epub.js)
const PART_LEN = 40000;
const HEADING_MAX = 40;    // a "short first line" used as section title may have at most this many characters
const MAX_TOC = 5000;
const BLOCK_WRAPPERS = /^(div|section|article|main|body)$/;
const BREAKS = new Set(['p', 'div', 'section', 'article', 'aside', 'header', 'footer', 'main', 'nav', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
  'dl', 'dt', 'dd', 'table', 'tr', 'blockquote', 'pre', 'figure', 'figcaption', 'hr', 'br', 'address', 'caption']);
const KINDLE_EMBED = /^kindle:embed:\w+(?:\?mime=[\w/+.-]+)?$/;

const tidy = (x, max = 300) => String(x == null ? '' : x).replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, max);

/** read the file head and judge it (DRM, truncation, KFX, Print Replica...); throws MobiError, returns inspectMobi's ok-result */
async function preflight(blob) {
  const fileSize = blob.size;
  const first = new Uint8Array(await blob.slice(0, Math.min(fileSize, PDB_HEADER_LEN)).arrayBuffer());
  const n = pdbRecordCount(first) || 0;
  const tableEnd = PDB_HEADER_LEN + n * 8;
  const table = new Uint8Array(await blob.slice(0, Math.min(fileSize, tableEnd)).arrayBuffer());
  const offs = recordOffsets(table, n);
  const o0 = offs && offs[0] >= tableEnd && offs[0] < fileSize ? Math.min(offs[0], tableEnd + 65536) : tableEnd;
  const head = new Uint8Array(await blob.slice(0, Math.min(fileSize, o0 + PEEK_RECORD0)).arrayBuffer());
  let rec1 = null;
  if (offs && n > 1 && offs[1] < fileSize) rec1 = new Uint8Array(await blob.slice(offs[1], Math.min(fileSize, offs[1] + 8)).arrayBuffer());
  const r = inspectMobi({ head, fileSize, rec1 });
  if (!r.ok) throw new MobiError(r.code);
  const b = r.exth.boundary;
  if (b > 0 && b < r.numRecords) {   // combined MOBI6 + KF8 file: the KF8 half has its own PalmDOC header (and its own encryption field)
    const rec = new Uint8Array(await blob.slice(r.offsets[b], Math.min(fileSize, r.offsets[b] + 64)).arrayBuffer());
    if (encryptionOf(rec) !== 0) throw new MobiError('drm');
  }
  return r;
}

export async function openMobi(blob, opts = {}) {
  await preflight(blob);
  let mod;
  try { mod = opts.module || (await loadVendor()); } catch { throw new MobiError('parse', '无法加载 MOBI 解析模块'); }
  let book;
  try {
    book = await new mod.MOBI({ unzlib: async (d) => d }).open(blob);   // unzlib only matters for compressed fonts, which we never use
  } catch (e) {
    console.warn('[mobi] open failed', e);
    throw new MobiError('parse');
  }
  const destroy = () => { try { book.destroy(); } catch { /* ignore */ } };
  try {
    return await build(book);
  } catch (e) {
    destroy();
    if (e instanceof MobiError) throw e;
    console.warn('[mobi] build failed', e);
    throw new MobiError('parse');
  }
}

async function build(book) {
  const hdr = (book.mobi && book.mobi.headers) || {};
  if (hdr.palmdoc && hdr.palmdoc.encryption) throw new MobiError('drm');   // second look, after open(): covers the KF8 half of combined files
  const kf8 = !!(hdr.mobi && hdr.mobi.version >= 8);
  if (kf8 && book.rendition && book.rendition.layout === 'pre-paginated') throw new MobiError('fixed-layout');

  // `raw` = readable sections of the vendored object (KF8 may contain {linear:'no'} placeholders without createDocument)
  const raw = (book.sections || []).map((s, i) => ({ s, i })).filter(({ s }) => s && typeof s.createDocument === 'function');
  if (!raw.length) throw new MobiError('no-text');
  const rawToDoc = new Map(raw.map(({ i }, d) => [i, d]));

  const md = book.metadata || {};
  const title = tidy(md.title);
  const author = (Array.isArray(md.author) ? md.author : md.author ? [md.author] : []).map((a) => tidy(a)).filter(Boolean).join(', ');
  const language = tidy(Array.isArray(md.language) ? md.language[0] : md.language, 40);

  const docs = raw.map(({ i }, d) => ({ id: i, path: '', heading: '', len: 0, d }));
  const issued = new Set();   // blob: URLs this book's parser handed out: the only image sources we accept

  // ---------------------------------------------------------------- one section -> sanitized fragment
  const cleanDoc = async (d) => {
    const sec = raw[d].s;
    let doc = await sec.createDocument();
    if (kf8 && doc.getElementsByTagName('parsererror').length) {
      // KF8 sections are parsed as XHTML first; some books are not well-formed. Re-read the library's own serialization as HTML
      // (still only to be sanitized below, never displayed as is).
      const url = await sec.load();
      const txt = await (await fetch(url)).text();
      doc = new DOMParser().parseFromString(txt, 'text/html');
      for (const img of doc.querySelectorAll('img[src^="blob:"]')) issued.add(img.getAttribute('src'));
    }
    if (kf8) {
      // kindle:pos fragment targets are found by id / name / aid; make all three look like an id so the sanitizer keeps them
      for (const el of doc.querySelectorAll('[aid],[name]')) {
        if (el.id) continue;
        const aid = el.getAttribute('aid'), name = el.localName === 'a' ? el.getAttribute('name') : null;
        if (aid) el.id = 'aid-' + aid; else if (name) el.id = name;
      }
      for (const a of doc.querySelectorAll('a[href]')) {
        const p = parseKindlePos(a.getAttribute('href'));
        if (p) a.setAttribute('href', kindlePosHref(p.raw));
      }
    } else {
      for (const a of doc.querySelectorAll('[filepos]')) {
        const p = parseFilepos('filepos:' + (a.getAttribute('filepos') || '').trim());
        if (p) a.setAttribute('href', fileposHref(p.digits));
      }
    }
    return doc;
  };

  /** sanitize one section; `images` = resolve <img> to blob: URLs (reading) or leave them bare (scan / search) */
  async function sanitizeDoc(d, images) {
    const doc = await cleanDoc(d);
    if (!kf8) {
      for (const img of doc.querySelectorAll('img')) {
        const rec = img.getAttribute('recindex');
        img.removeAttribute('src');
        if (!images || !rec || !/^\d{1,6}$/.test(rec)) continue;
        try {
          const url = await book.loadRecindex(rec);
          if (typeof url === 'string' && url.startsWith('blob:')) { issued.add(url); img.setAttribute('src', url); }
        } catch { /* image record missing: the <img> is removed below */ }
      }
    }
    const frag = sanitizeChildren(doc.body || doc.documentElement);
    for (const img of Array.from(frag.querySelectorAll('img[data-src]'))) {
      const ref = img.getAttribute('data-src');
      img.removeAttribute('data-src');
      if (!images) continue;
      let url = null;
      if (issued.has(ref)) url = ref;
      else if (kf8 && KINDLE_EMBED.test(ref)) {   // <img src="kindle:embed:0007?mime=image/jpeg"> (also what the sanitizer makes of svg cover pages)
        try {
          const u = await book.loadResource(ref);
          if (typeof u === 'string' && u.startsWith('blob:')) { issued.add(u); url = u; }
        } catch { /* dropped below */ }
      }
      if (url) img.src = url; else img.remove();
    }
    return frag;
  }
  const flatten = (frag) => {
    let nodes = Array.from(frag.childNodes);
    while (nodes.length === 1 && nodes[0].nodeType === 1 && BLOCK_WRAPPERS.test(nodes[0].localName)) nodes = Array.from(nodes[0].childNodes);
    return nodes;
  };

  let viewCache = null;   // {d, blocks}: sanitized section with images, for part-sections being read
  let textCache = null;   // {d, blocks}: sanitized without images, for search / part building
  async function docBlocks(d, images) {
    const c = images ? viewCache : textCache;
    if (c && c.d === d) return c;
    const next = { d, blocks: flatten(await sanitizeDoc(d, images)) };
    if (images) viewCache = next; else textCache = next;
    return next;
  }
  async function buildParts(d, blocks) {
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

  /** first heading, else the first short line of text (<= HEADING_MAX chars), else '' */
  function headingOf(blocks) {
    for (const n of blocks.slice(0, 30)) {
      if (n.nodeType !== 1) continue;
      const h = /^h[1-4]$/.test(n.localName) ? n : n.querySelector('h1,h2,h3,h4');
      const t = h && tidy(h.textContent, 80);
      if (t) return t;
    }
    let buf = '', done = false;
    const brk = () => { if (buf.trim()) done = true; };
    const visit = (n) => {
      if (done) return;
      if (buf.length > 200) { done = true; return; }
      if (n.nodeType === 3) { buf += n.nodeValue; return; }
      if (n.nodeType !== 1) return;
      const b = BREAKS.has(n.localName);
      if (b) brk();
      if (done) return;
      for (const c of n.childNodes) { visit(c); if (done) return; }
      if (b) brk();
    };
    for (const n of blocks) { visit(n); if (done) break; }
    const t = tidy(buf, 200);
    return t && Array.from(t).length <= HEADING_MAX ? t : '';
  }

  // ---------------------------------------------------------------- TOC (resolved to {title, d, id, level}; persisted in layoutData)
  let kf8DocCache = null;   // {d, doc}: parsed + id-normalised KF8 section, kept for the TOC loop
  async function kf8Resolve(rawHref) {
    const r = await book.resolveHref(rawHref);
    if (!r || typeof r.index !== 'number') return null;
    const d = rawToDoc.get(r.index);
    if (d === undefined) return null;
    let id = '';
    try {
      if (!kf8DocCache || kf8DocCache.d !== d) kf8DocCache = { d, doc: await cleanDoc(d) };
      const el = r.anchor(kf8DocCache.doc);
      id = el ? el.id || '' : '';
    } catch { /* section start */ }
    return { d, id };
  }
  function resolveMobi6(href) {
    const p = parseFilepos(href);
    if (!p) return null;
    const r = book.resolveHref('filepos:' + p.digits);
    const d = r && r.index >= 0 ? rawToDoc.get(r.index) : undefined;
    return d === undefined ? null : { d, id: p.id };
  }
  async function buildToc() {
    const flat = [];
    const walk = (list, level) => {
      for (const it of Array.isArray(list) ? list : []) {
        if (flat.length >= MAX_TOC) return;
        flat.push({ label: tidy(it && it.label, 200), href: it && it.href, level });
        walk(it && it.subitems, level + 1);
      }
    };
    walk(book.toc, 0);
    const out = [];
    for (const it of flat) {
      if (!it.label || typeof it.href !== 'string') continue;
      let t = null;
      try {
        if (kf8) { const p = parseKindlePos(it.href); t = p ? await kf8Resolve(p.raw) : null; } else t = resolveMobi6(it.href);
      } catch { t = null; }
      if (t) out.push({ title: it.label, d: t.d, id: t.id, level: Math.min(it.level, 6) });
    }
    kf8DocCache = null;
    return out;
  }

  // ---------------------------------------------------------------- the book object
  let spec = null;            // per section: null | {ranges, lens, ids}
  let docLens = [];
  let keep = [];              // per section: 1 = shown (has text or an image), 0 = empty page-break leftover
  let tocRaw = [];
  let sections = [];
  const docFirst = [];
  let closed = false;

  const out = {
    format: MOBI_FORMAT, title, author, language, docs, sections: [], toc: [], lens: [],
    layout(docLensIn, headings, specIn, extra = {}) {
      docLens = docLensIn;
      spec = specIn || docs.map(() => null);
      if (Array.isArray(extra.tocRaw)) tocRaw = extra.tocRaw;
      keep = Array.isArray(extra.keep) && extra.keep.length === docs.length ? extra.keep : docs.map(() => 1);
      if (!keep.some(Boolean)) keep = docs.map(() => 1);
      docs.forEach((doc, i) => { doc.len = docLens[i] || 0; if (headings) doc.heading = headings[i] || ''; });
      sections = [];
      const lens = [];
      docs.forEach((doc, d) => {
        docFirst[d] = sections.length;
        if (!keep[d]) return;
        const p = spec[d];
        if (p) p.ranges.forEach((r, k) => { sections.push({ doc: d, range: r, part: k, heading: k ? '' : doc.heading, title: '' }); lens.push(p.lens[k]); });
        else { sections.push({ doc: d, range: null, part: 0, heading: doc.heading, title: '' }); lens.push(docLens[d] || 0); }
      });
      this.sections = sections;
      this.lens = lens;
      const at = (d, id) => {
        const p = spec[d];
        const k = p && id && Object.hasOwn(p.ids, id) ? p.ids[id] : 0;
        return Math.min(docFirst[d] + k, sections.length - 1);
      };
      this.toc = tocRaw.filter((t) => t.d >= 0 && t.d < docs.length).map((t) => ({ title: t.title, s: at(t.d, t.id), id: t.id, level: t.level }));
      const byS = new Map();
      for (const t of this.toc) if (!byS.has(t.s) && t.title) byS.set(t.s, t.title);
      sections.forEach((s, i) => {
        const base = byS.get(i) || s.heading || '';
        s.title = s.part ? `${base || `第 ${s.doc + 1} 节`}（续）` : base || `第 ${i + 1} 节`;
      });
      // no (usable) table of contents in the book: one entry per section, titled by its first heading / short line
      if (!this.toc.length) this.toc = sections.map((s, i) => ({ title: s.title, s: i, id: '', level: 0 })).filter((t, i) => !sections[i].part);
    },
    /** what must be persisted to open the book again without scanning */
    layoutData() { return { docLens, headings: docs.map((d) => d.heading), parts: spec, keep, tocRaw }; },
    async scan(onProgress) {
      const lensOut = [], parts = [], keepOut = [], headings = [];
      let any = false;
      for (let i = 0; i < docs.length; i++) {
        const { blocks } = await docBlocks(i, false);
        let len = 0;
        for (const n of blocks) len += (n.textContent || '').length;
        const hasImg = blocks.some((n) => n.nodeType === 1 && (n.localName === 'img' || n.querySelector('img')));
        lensOut.push(len);
        keepOut.push(len > 0 || hasImg ? 1 : 0);
        if (len > 0 || hasImg) any = true;
        headings.push(headingOf(blocks));
        parts.push(len > SPLIT_LEN ? await buildParts(i, blocks) : null);
        if (onProgress && i % 8 === 0) { onProgress((i + 1) / docs.length); await new Promise((res) => setTimeout(res, 0)); }
      }
      textCache = null;
      if (!any) throw new MobiError('no-text');
      tocRaw = await buildToc();
      docs.forEach((d, i) => { d.heading = headings[i]; });
      this.layout(lensOut, headings, parts, { keep: keepOut, tocRaw });
      return this.lens;
    },
    async loadSection(i) {
      const sec = sections[i];
      if (!sec.range) return { frag: await sanitizeDoc(sec.doc, true), cleanup() {} };   // blob URLs belong to the parser; close() revokes them
      const { blocks } = await docBlocks(sec.doc, true);
      const f = document.createDocumentFragment();
      for (const n of blocks.slice(sec.range[0], sec.range[1])) f.append(n.cloneNode(true));
      return { frag: f, cleanup() {} };
    },
    async sectionText(i) {
      const sec = sections[i];
      let nodes;
      if (sec.range) nodes = (await docBlocks(sec.doc, false)).blocks.slice(sec.range[0], sec.range[1]);
      else nodes = [await sanitizeDoc(sec.doc, false)];
      let s = '';
      for (const n of nodes) {
        if (n.nodeType === 3) { s += n.nodeValue; continue; }
        const w = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
        while (w.nextNode()) s += w.currentNode.nodeValue;
      }
      return s;
    },
    /** internal link -> {s, id}; null for external/unknown. MOBI6 answers synchronously; KF8 (kindle:pos) returns a Promise. */
    resolveHref(fromS, href) {
      if (!href) return null;
      const finish = (t) => {
        if (!t) return null;
        const p = spec && spec[t.d];
        const k = p && t.id && Object.hasOwn(p.ids, t.id) ? p.ids[t.id] : 0;
        return { s: Math.min(docFirst[t.d] + k, sections.length - 1), id: t.id };
      };
      if (!kf8) return finish(resolveMobi6(href));
      const p = parseKindlePos(href);
      return p ? kf8Resolve(p.raw).then(finish, () => null) : null;
    },
    async cover() {
      const typed = async (b) => {
        if (!b || !(b.size > 200)) return null;
        const t = sniffImageType(new Uint8Array(await b.slice(0, 16).arrayBuffer()));
        return t ? new Blob([b], { type: t }) : null;   // getCover() returns an untyped Blob
      };
      try {
        if (typeof book.getCover === 'function') { const c = await typed(await book.getCover()); if (c) return c; }
      } catch { /* try the first image */ }
      try {   // last resort: first image of the first section
        const { frag } = await this.loadSection(0);
        const img = frag.querySelector('img');
        if (img && img.src) { const c = await typed(await (await fetch(img.src)).blob()); if (c) return c; }
      } catch { /* none */ }
      return null;
    },
    close() {
      if (closed) return;
      closed = true;
      viewCache = textCache = null;
      try { book.destroy(); } catch { /* ignore */ }   // revokes every blob: URL the parser created
    },
  };
  return out;
}
