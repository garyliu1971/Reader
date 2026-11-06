// In-book search: pure logic, no DOM. Offsets are text offsets inside ONE section (same model as core/anchor.js).
// `matches` is an array indexed by section; each entry is a flat [offset, length, offset, length, ...] list in document
// order (undefined = no hit / not scanned yet). A position inside the result set is {s: section, k: hit index in it}.

export const MAX_HITS = 5000;   // stop collecting past this many hits in the whole book (the UI shows "5000+")

const escapeRe = (w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Typed text -> RegExp, or null when blank. Literal match (no regex syntax), case-insensitive with Unicode folding,
 * and any run of whitespace in the query matches any run of whitespace in the book (HTML collapses line breaks).
 */
export function compileQuery(q) {
  const words = String(q ?? '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  return new RegExp(words.map(escapeRe).join('\\s+'), 'giu');
}

/** All non-overlapping hits of `re` in `text`, left to right: flat [o, len, ...], at most `limit` hits. */
export function findAll(text, re, limit = MAX_HITS) {
  const out = [];
  if (!re || limit <= 0) return out;
  re.lastIndex = 0;
  let m;
  while (out.length / 2 < limit && (m = re.exec(text))) {
    if (!m[0].length) { re.lastIndex++; continue; }
    out.push(m.index, m[0].length);
  }
  return out;
}

export const countIn = (matches, s) => (matches[s] ? matches[s].length / 2 : 0);

export function totalHits(matches) {
  let n = 0;
  for (let s = 0; s < matches.length; s++) n += countIn(matches, s);
  return n;
}

/** 1-based position of `cur` among all hits known so far */
export function rankOf(matches, cur) {
  let n = 0;
  for (let s = 0; s < cur.s; s++) n += countIn(matches, s);
  return n + cur.k + 1;
}

function firstSection(matches, from, to) { for (let s = from; s < to; s++) if (countIn(matches, s)) return s; return -1; }
function lastSection(matches, from, to) { for (let s = from; s >= to; s--) if (countIn(matches, s)) return s; return -1; }

/** the hit after (dir > 0) or before (dir < 0) `cur`, wrapping around the book; the first/last hit when cur is null */
export function stepHit(matches, cur, dir) {
  const n = matches.length;
  if (dir >= 0) {
    if (cur && cur.k + 1 < countIn(matches, cur.s)) return { s: cur.s, k: cur.k + 1 };
    let s = firstSection(matches, cur ? cur.s + 1 : 0, n);
    if (s < 0) s = firstSection(matches, 0, n);
    return s < 0 ? null : { s, k: 0 };
  }
  if (cur && cur.k > 0) return { s: cur.s, k: cur.k - 1 };
  let s = lastSection(matches, cur ? cur.s - 1 : n - 1, 0);
  if (s < 0) s = lastSection(matches, n - 1, 0);
  return s < 0 ? null : { s, k: countIn(matches, s) - 1 };
}

/** index of the first hit in a flat list whose offset is >= o (the hit count when there is none) */
export function hitAtOrAfter(flat, o) {
  let lo = 0, hi = flat.length / 2;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (flat[mid * 2] >= o) hi = mid; else lo = mid + 1;
  }
  return lo;
}

/**
 * First hit at or after position (s, o) in reading order. With `wrap` it continues from the start of the book, so only
 * null when there is no hit at all.
 */
export function seekHit(matches, s, o, wrap = false) {
  const n = matches.length;
  const here = matches[s];
  if (here) {
    const k = hitAtOrAfter(here, o);
    if (k < here.length / 2) return { s, k };
  }
  const next = firstSection(matches, s + 1, n);
  if (next >= 0) return { s: next, k: 0 };
  if (!wrap) return null;
  const first = firstSection(matches, 0, n);
  return first < 0 ? null : { s: first, k: 0 };
}

/** order in which to scan n sections: start at section s0 so the first hit after the reader is found early, then wrap */
export function sectionOrder(n, s0) {
  const out = [];
  const from = Math.min(Math.max(0, s0 | 0), Math.max(0, n - 1));
  for (let i = 0; i < n; i++) out.push((from + i) % n);
  return out;
}

/** text around a hit for result lists: {lead, before, hit, after}; line breaks and runs of spaces are collapsed */
export function snippet(text, o, len, before = 18, after = 28) {
  const a = Math.max(0, o - before);
  const flat = (t) => t.replace(/\s+/g, ' ');
  return { lead: a > 0, before: flat(text.slice(a, o)), hit: text.slice(o, o + len), after: flat(text.slice(o + len, o + len + after)) };
}
