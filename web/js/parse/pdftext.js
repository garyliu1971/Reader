// Pure helpers for the PDF text layer (no DOM, no pdf.js import) so they can be unit-tested under node.

/** scale that fits a page of (pw,ph) PDF units into (w,h) CSS pixels; never 0/NaN */
export function fitScale(pw, ph, w, h) {
  const s = Math.min(w / pw, h / ph);
  return Number.isFinite(s) && s > 0 ? s : 1;
}

/** geometry of the text-layer box for a page: CSS size (rounded like the canvas) + the `--scale-factor` pdf.js text spans use */
export function layerBox(pw, ph, scale) {
  return { width: Math.floor(pw * scale), height: Math.floor(ph * scale), scaleFactor: scale };
}

const CJK = /[　-鿿豈-﫿＀-￯]/;

/**
 * Plain text of one page from pdf.js `getTextContent().items` (items without `str`, i.e. marked-content markers, are skipped).
 * Items on one line are concatenated as-is (pdf.js emits explicit " " items where the PDF has spaces); `hasEOL` ends a line.
 * A line ending in a hyphen glued to a lowercase letter on the next line is joined ("exam-\nple" -> "example").
 * Lines are joined with "\n"; Latin lines that wrap inside a paragraph are NOT merged (we cannot tell paragraphs apart).
 */
export function itemsToText(items) {
  const lines = [];
  let cur = '';
  for (const it of items || []) {
    if (!it || typeof it.str !== 'string') continue;
    cur += it.str;
    if (it.hasEOL) { lines.push(cur); cur = ''; }
  }
  if (cur) lines.push(cur);
  const out = [];
  for (const ln of lines) {
    const t = ln.replace(/[ \t]+$/g, '');
    const prev = out.length ? out[out.length - 1] : null;
    if (prev !== null && /[A-Za-z]-$/.test(prev) && /^[a-z]/.test(t)) out[out.length - 1] = prev.slice(0, -1) + t;
    else out.push(t);
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

/** true when the page has no usable text (scanned image PDF): the reader then tells the user once instead of silently doing nothing */
export function isTextless(text) {
  return !text || !/[\p{L}\p{N}]/u.test(text);
}

/** collapse selection text from the layer: spans are separated by "\n" by the browser; CJK lines should not get a space */
export function normalizeSelection(text) {
  const parts = String(text || '').trim().split(/\s*\n\s*/).filter(Boolean);
  let out = '';
  for (const p of parts) {
    if (!out) { out = p; continue; }
    const a = out.slice(-1), b = p[0];
    out += (CJK.test(a) || CJK.test(b)) ? p : ' ' + p;
  }
  return out;
}
