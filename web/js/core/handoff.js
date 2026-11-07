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
 * @param rel path of the book below <base>/library (the part after "remote:" in its book id)
 * @param opts {ppd: 'rtl'|'ltr'|null|undefined, bid: book id, default "remote:" + rel}
 * @returns {string} same-origin URL, or '' when there is no library path to hand over
 */
export function ivUrl(rel, { ppd = null, bid = null } = {}) {
  if (!rel) return '';
  const enc = encodeRel(rel);
  const id = bid || 'remote:' + rel;
  const encId = id.startsWith('remote:') ? 'remote:' + encodeRel(id.slice(7)) : encodeURIComponent(id);
  const q = [`open=${enc}`, 'src=dav', `bid=${encId}`];
  if (ppd === 'rtl' || ppd === 'ltr') q.push(`dir=${ppd}`);
  q.push(`back=${BACK_PATH}`);
  return `${IV_PATH}#${q.join('&')}`;
}
