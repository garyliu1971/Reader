// Hand-off of an image-only book (comic epub) to the ImageViewer web app served from the same origin at /iv/.
// Pure: only builds the URL. The URL carries the library path, never credentials: /iv/ asks for its own (read-only) login.
//   /iv/#open=<rel, each segment percent-encoded>&src=dav&bid=remote:<same>&dir=<rtl|ltr>&back=/reader/
// `dir` is only sent when the book itself declares a reading direction (OPF page-progression-direction).

export const IV_PATH = '/iv/';
export const BACK_PATH = '/reader/';

/** Percent-encode a library path segment by segment, exactly like DavClient.url (sync/webdav.js) does for the request path. */
export function encodeRel(rel) {
  const parts = String(rel).replace(/\\/g, '/').split('/').filter(Boolean);
  if (!parts.length || parts.some((p) => p === '.' || p === '..')) throw new Error(`illegal library path: ${rel}`);
  return parts.map((p) => encodeURIComponent(p)).join('/');
}

/**
 * A library path that is safe to hand over: every segment non-empty and not "." / "..", so what /iv/ opens is exactly the path
 * the book was synced under (encodeRel alone would silently collapse "a//b" and a leading/trailing "/").
 */
export function isSafeRel(rel) {
  if (typeof rel !== 'string' || !rel) return false;
  return rel.replace(/\\/g, '/').split('/').every((p) => p !== '' && p !== '.' && p !== '..');
}

/**
 * @param rel path of the book below <base>/library (the part after "remote:" in its book id)
 * @param opts {ppd: 'rtl'|'ltr'|null|undefined, bid: book id, default "remote:" + rel}
 * @returns {string|null} same-origin URL; '' when there is no library path to hand over; null when the path (or the "remote:" part of
 *   the book id) is unsafe or malformed ("..", ".", empty segments such as "a//b"): never throws, callers fall back to reading here
 */
export function ivUrl(rel, { ppd = null, bid = null } = {}) {
  if (!rel) return '';
  if (!isSafeRel(rel)) return null;
  const id = bid || 'remote:' + rel;
  if (id.startsWith('remote:') && !isSafeRel(id.slice(7))) return null;
  const enc = encodeRel(rel);
  const encId = id.startsWith('remote:') ? 'remote:' + encodeRel(id.slice(7)) : encodeURIComponent(id);
  const q = [`open=${enc}`, 'src=dav', `bid=${encId}`];
  if (ppd === 'rtl' || ppd === 'ltr') q.push(`dir=${ppd}`);
  q.push(`back=${BACK_PATH}`);
  return `${IV_PATH}#${q.join('&')}`;
}
