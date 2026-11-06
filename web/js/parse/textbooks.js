// Book objects for TXT / Markdown / HTML (same interface as epub.js: sections, toc, lens, loadSection, sectionText).
import { parseTxt } from './txtparse.js';
import { sectionParagraphs } from '../core/chapters.js';
import { decodeText } from '../core/textenc.js';
import { sanitizeChildren, parseMarkup } from './sanitize.js';

/** TXT: parse in a module Worker so a 50 MB novel does not freeze the UI; main-thread fallback if Workers are unavailable. */
export async function parseTxtAsync(buf) {
  if (typeof Worker !== 'undefined') {
    try {
      const w = new Worker(new URL('./txt.worker.js', import.meta.url), { type: 'module' });
      const r = await new Promise((resolve, reject) => {
        w.onmessage = (e) => (e.data.ok ? resolve(e.data) : reject(new Error(e.data.error)));
        w.onerror = (e) => reject(new Error(e.message || 'worker error'));
        w.postMessage({ buf });   // structured clone (not transferred) so the main-thread fallback still has the bytes
      });
      w.terminate();
      return r;
    } catch (e) {
      console.warn('[txt] worker failed, parsing on the main thread', e);
    }
  }
  return parseTxt(new Uint8Array(buf));
}

export function txtBook({ text, sections, lens, title }) {
  const toc = [];
  sections.forEach((s, i) => { if (s.toc) toc.push({ title: s.title || `第 ${i + 1} 部分`, s: i, id: '', level: 0 }); });
  return {
    format: 'txt', title, author: '', sections, toc, lens,
    async loadSection(i) {
      const sec = sections[i];
      const frag = document.createDocumentFragment();
      const paras = sectionParagraphs(text, sec);
      paras.forEach((p, k) => {
        const isTitle = k === 0 && sec.toc && sec.title && p === sec.title.trim();
        const el = document.createElement(isTitle ? 'h2' : 'p');
        el.textContent = p;          // one text node per paragraph; no string concatenation over the book
        frag.append(el);
      });
      return { frag, cleanup() {} };
    },
    async sectionText(i) { return sectionParagraphs(text, sections[i]).join(''); },
    resolveHref() { return null; },
    async cover() { return null; },
    close() {},
  };
}

// ---------------------------------------------------------------- Markdown / HTML
let markedMod = null;
async function marked() {
  if (!markedMod) markedMod = await import('../../vendor/marked.esm.js');
  return markedMod.marked;
}

const SPLIT_CHARS = 40000;

/** split top-level blocks into sections at h1/h2 (and at ~40k chars so one section stays bounded) */
function splitBlocks(frag) {
  let nodes = Array.from(frag.childNodes);
  if (nodes.length === 1 && nodes[0].nodeType === 1 && /^(div|section|article|main)$/.test(nodes[0].localName)) nodes = Array.from(nodes[0].childNodes);
  const secs = [];
  let cur = null;
  const start = (title) => { cur = { title, nodes: [], chars: 0 }; secs.push(cur); };
  for (const n of nodes) {
    const isHead = n.nodeType === 1 && /^h[12]$/.test(n.localName);
    if (isHead) { start(n.textContent.trim().slice(0, 80)); }
    else if (!cur || cur.chars > SPLIT_CHARS) start(cur ? `${cur.title}（续）` : '');
    cur.nodes.push(n);
    cur.chars += (n.textContent || '').length;
  }
  return secs.filter((s) => s.nodes.length);
}

export async function richTextBook(bytes, kind, fallbackTitle) {
  const { text } = decodeText(bytes);
  let html;
  if (kind === 'md') html = (await marked()).parse(text, { async: false, gfm: true });
  else html = text;
  const { body, doc } = parseMarkup(html, { prefer: 'html' });
  const frag = sanitizeChildren(body);
  for (const img of Array.from(frag.querySelectorAll('img'))) {   // only inline data: images; nothing is fetched from the network
    const src = img.getAttribute('data-src') || '';
    if (/^data:image\/(png|jpe?g|gif|webp);/i.test(src)) { img.src = src; img.removeAttribute('data-src'); } else img.remove();
  }
  const h1 = frag.querySelector('h1');
  const title = (doc && doc.title && doc.title.trim()) || (h1 && h1.textContent.trim()) || fallbackTitle;
  let secs = splitBlocks(frag);
  if (!secs.length) secs = [{ title: '', nodes: [document.createElement('p')], chars: 0 }];
  const sections = secs.map((s, i) => ({ title: s.title || (i === 0 ? title : `第 ${i + 1} 节`), toc: true }));
  const lens = secs.map((s) => s.nodes.reduce((n, x) => n + (x.textContent || '').length, 0));
  const toc = sections.map((s, i) => ({ title: s.title, s: i, id: '', level: 0 }));
  return {
    format: kind, title, author: '', sections, toc, lens,
    async loadSection(i) {
      const f = document.createDocumentFragment();
      for (const n of secs[i].nodes) f.append(n.cloneNode(true));
      return { frag: f, cleanup() {} };
    },
    async sectionText(i) { return secs[i].nodes.map((n) => n.textContent || '').join(''); },
    resolveHref(fromS, href) {
      if (href && href[0] === '#') {
        const id = decodeURIComponent(href.slice(1));
        const s = secs.findIndex((sec) => sec.nodes.some((n) => n.nodeType === 1 && (n.id === id || n.querySelector(`[id="${CSS.escape(id)}"]`))));
        return s >= 0 ? { s, id } : null;
      }
      return null;
    },
    async cover() { return null; },
    close() {},
  };
}
