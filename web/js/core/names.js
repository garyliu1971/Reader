// Name validation ported 1:1 from pyreader/store.py + shelf.py (the Python sources are authoritative).
// Everything that comes from the network or from file names must pass through here before being
// used in a URL path or as a book id.

const SAFE_RE = /^[A-Za-z0-9._-]{1,64}$/;
const RESERVED = new Set(['CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`)]);
// bidi control characters used to disguise names (RLO etc.)
const BIDI_RE = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/u;
const CONTROL_RE = /\p{Cc}/u;
export const MAX_SEGMENT = 255;
export const MAX_REL = 1024;

function isReserved(name) {
  const stem = name.split('.', 1)[0].normalize('NFKC').replace(/ +$/, '').toUpperCase();
  return RESERVED.has(stem);
}

const cpLen = (s) => Array.from(s).length;

/** device_id / sync user name etc.: [A-Za-z0-9._-]{1,64}, no leading/trailing '.', no Windows reserved names. */
export function safeComponent(name) {
  if (typeof name !== 'string' || !SAFE_RE.test(name)) return null;
  if (name.startsWith('.') || name.endsWith('.')) return null;
  if (isReserved(name)) return null;
  return name;
}

/** One path segment of a remote relative path (non-ASCII allowed). */
export function safeSegment(name) {
  if (typeof name !== 'string' || !name || cpLen(name) > MAX_SEGMENT) return null;
  if (name === '.' || name === '..') return null;
  if (/[/\\:]/.test(name)) return null;
  if (CONTROL_RE.test(name) || BIDI_RE.test(name)) return null;
  const last = name[name.length - 1];
  if (last === '.' || last === ' ') return null;
  if (isReserved(name)) return null;
  return name;
}

/** '/'-separated relative path; returned verbatim (book_id depends on the raw rel) or null. */
export function normalizeRel(rel) {
  if (typeof rel !== 'string' || !rel || cpLen(rel) > MAX_REL) return null;
  for (const seg of rel.split('/')) if (safeSegment(seg) === null) return null;
  return rel;
}

/** Names the library listing ignores: dot files, .part uploads, Office lock files. */
export function isHiddenName(name) {
  return name.startsWith('.') || name.startsWith('~$');
}

export const BOOK_EXTS_READABLE = ['.txt', '.md', '.markdown', '.epub', '.mobi', '.azw', '.azw3', '.prc', '.html', '.htm', '.pdf'];

/** Lower-cased extension including the dot ('' if none). */
export function extOf(name) {
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i).toLowerCase();
}

const HEX40 = /^[0-9a-f]{40}$/;

/** book_id is remote:<valid rel> or local:<40 lowercase hex>. */
export function validBookId(bid) {
  if (typeof bid !== 'string' || bid.length > 1100) return false;
  if (bid.startsWith('remote:')) {
    const rel = bid.slice(7);
    return normalizeRel(rel) !== null && !rel.split('/').some(isHiddenName);
  }
  if (bid.startsWith('local:')) return HEX40.test(bid.slice(6));
  return false;
}

/**
 * Remote library entry we are willing to show: valid rel, no hidden segment, and an extension in `exts`.
 * (Qt lists more formats; the web reader only offers the ones it can open.)
 */
export function validRemoteRel(rel, exts = BOOK_EXTS_READABLE) {
  if (normalizeRel(rel) === null) return false;
  const segs = rel.split('/');
  if (segs.some(isHiddenName)) return false;
  return exts.includes(extOf(segs[segs.length - 1]));
}
