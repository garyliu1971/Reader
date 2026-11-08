// Online book source (Project Gutenberg through the app's own /books/v1/ proxy): pure logic, no DOM.
// Contract (proxy built separately, same origin, Authorization: Bearer <token>, the SAME token as online read-aloud):
//   GET /books/v1/search?q=&lang=en&limit=25&offset=0 -> {total, items:[{id,title,authors,lang}]}
//       503 {error:'catalog_loading', retryAfter}   429 {error, retryAfter}   401/403 bad token
//   GET /books/v1/download/<id>?variant=noimages|epub3 -> epub bytes
// Covers are plain <img> tags on gutenberg.org (fetch would be CORS-blocked, <img> is not).

export const PAGE_SIZE = 25;
export const ORIGIN_PREFIX = 'gutenberg:';
export const LANGS = [['en', 'English'], ['fr', 'Français'], ['de', 'Deutsch'], ['es', 'Español'], ['zh', '中文']];
export const VARIANTS = { noimages: 'noimages', images: 'epub3' };   // UI choice -> server query value
export const RETRY_MIN_S = 2;
export const RETRY_MAX_S = 30;
export const MAX_AUTO_RETRIES = 20;

/** absolute URL of a /books/v1/ endpoint (the API sits next to the app dir, like /tts/) */
export function endpoint(path, baseHref) { return new URL('../books/v1/' + path, baseHref).href; }

export const originOf = (id) => ORIGIN_PREFIX + id;
export const gutenbergIdOf = (origin) => { const m = /^gutenberg:(\d{1,9})$/.exec(String(origin || '')); return m ? m[1] : null; };
/** Set of Gutenberg ids already on the shelf (books rows carry meta.origin) */
export const addedIds = (books) => new Set((books || []).map((b) => gutenbergIdOf(b && b.origin)).filter(Boolean));

export const coverUrl = (id) => `https://www.gutenberg.org/cache/epub/${id}/pg${id}.cover.medium.jpg`;
const validId = (id) => /^\d{1,9}$/.test(String(id));

export function searchUrl({ q = '', lang = 'en', limit = PAGE_SIZE, offset = 0 }, baseHref) {
  const u = new URL(endpoint('search', baseHref));
  u.searchParams.set('q', String(q).trim());
  u.searchParams.set('lang', LANGS.some((l) => l[0] === lang) ? lang : 'en');
  u.searchParams.set('limit', String(Math.max(1, Math.min(50, Math.floor(limit) || PAGE_SIZE))));
  u.searchParams.set('offset', String(Math.max(0, Math.floor(offset) || 0)));
  return u.href;
}

export function downloadUrl(id, images, baseHref) {
  if (!validId(id)) throw new Error('无效的书籍编号');
  const u = new URL(endpoint('download/' + id, baseHref));
  u.searchParams.set('variant', images ? VARIANTS.images : VARIANTS.noimages);
  return u.href;
}

/** Local file name: "pg<id> - <title>.epub", no path/reserved characters, at most ~100 chars of title */
export function bookFileName(id, title) {
  let t = String(title || '').normalize('NFC')
    .replace(/[\u0000-\u001f\u007f\\/:*?"<>|]/g, ' ').replace(/\s+/g, ' ').trim().replace(/^[.\s]+|[.\s]+$/g, '');
  if (Array.from(t).length > 100) t = Array.from(t).slice(0, 100).join('').trim();
  return t ? `pg${id} - ${t}.epub` : `pg${id}.epub`;
}

export function fmtWait(sec) {
  if (sec < 90) return `${Math.ceil(sec)} 秒`;
  if (sec < 5400) return `${Math.ceil(sec / 60)} 分钟`;
  return `${Math.ceil(sec / 3600)} 小时`;
}

export class LibraryError extends Error {
  constructor(kind, message, retryAfter = null) { super(message); this.name = 'LibraryError'; this.kind = kind; this.retryAfter = retryAfter; }
}

const retryOf = (body) => { const n = body && Number(body.retryAfter); return Number.isFinite(n) && n > 0 ? n : null; };

/** HTTP failure -> LibraryError. kinds: auth | loading (catalog_loading, auto retry) | quota | http | offline | token */
export function classifyStatus(status, body = null) {
  if (status === 401 || status === 403) return new LibraryError('auth', '令牌缺失或不正确：请到 设置 → 在线朗读 里填写令牌（与在线朗读使用同一个令牌）。');
  if (status === 429) { const ra = retryOf(body); return new LibraryError('quota', '请求太频繁，已被限流' + (ra ? `，约 ${fmtWait(ra)} 后可再试` : '，请稍后再试') + '。', ra); }
  if (status === 503 && body && body.error === 'catalog_loading') { const ra = retryOf(body); return new LibraryError('loading', '服务器正在加载书库目录，请稍候（将自动重试）…', ra); }
  if (status >= 500) return new LibraryError('http', `书库服务暂时不可用（HTTP ${status}）。`);
  if (status === 404) return new LibraryError('http', '没有找到这本书（HTTP 404）。');
  return new LibraryError('http', `请求失败（HTTP ${status}）。`);
}

export const missingTokenError = () => classifyStatus(401);
export const offlineError = () => new LibraryError('offline', '无法连接到书库服务：请检查网络连接后重试。');

/** seconds to wait before an automatic retry of a catalog_loading answer */
export const retryDelay = (err) => Math.min(RETRY_MAX_S, Math.max(RETRY_MIN_S, Math.ceil((err && err.retryAfter) || RETRY_MIN_S)));

export function normalizeItems(list) {
  return (Array.isArray(list) ? list : []).filter((it) => it && validId(it.id)).map((it) => ({
    id: String(it.id),
    title: typeof it.title === 'string' && it.title.trim() ? it.title.trim().slice(0, 300) : `Gutenberg #${it.id}`,
    authors: (Array.isArray(it.authors) ? it.authors : typeof it.authors === 'string' ? [it.authors] : [])
      .map((a) => (typeof a === 'string' ? a : a && a.name)).filter((a) => typeof a === 'string' && a).slice(0, 6),
    lang: typeof it.lang === 'string' ? it.lang.slice(0, 12) : '',
  }));
}

async function readJson(res) { try { return await res.json(); } catch { return null; } }

function guard(token, fetchFn) {
  if (!token) throw missingTokenError();
  if (typeof navigator !== 'undefined' && navigator.onLine === false) throw offlineError();
  return fetchFn;
}

async function request(fetchFn, url, token, signal) {
  let res;
  try { res = await fetchFn(url, { headers: { Authorization: 'Bearer ' + token }, signal, cache: 'no-store' }); }
  catch (e) { if (e && e.name === 'AbortError') throw e; throw offlineError(); }
  if (!res.ok) throw classifyStatus(res.status, await readJson(res));
  return res;
}

/** @returns {{total:number, items:Array}} ; throws LibraryError (or AbortError) */
export async function searchBooks({ q, lang, offset = 0, limit = PAGE_SIZE, token, baseHref, fetch: f = globalThis.fetch, signal }) {
  guard(token, f);
  const res = await request(f, searchUrl({ q, lang, offset, limit }, baseHref), token, signal);
  const body = await readJson(res);
  const items = normalizeItems(body && body.items);
  const total = body && Number.isFinite(Number(body.total)) ? Number(body.total) : offset + items.length;
  return { total, items };
}

/** Download the epub; onProgress(received, totalOrZero). @returns {Uint8Array} */
export async function downloadBook({ id, images = false, token, baseHref, fetch: f = globalThis.fetch, signal, onProgress = () => {}, maxBytes = 300 * 1048576 }) {
  guard(token, f);
  const res = await request(f, downloadUrl(id, images, baseHref), token, signal);
  const total = Number(res.headers.get('content-length')) || 0;
  if (total > maxBytes) throw new LibraryError('http', '文件过大，已取消。');
  if (!res.body || !res.body.getReader) { const b = new Uint8Array(await res.arrayBuffer()); onProgress(b.length, b.length); return b; }
  const reader = res.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    let r;
    try { r = await reader.read(); } catch (e) { if (e && e.name === 'AbortError') throw e; throw offlineError(); }
    if (r.done) break;
    chunks.push(r.value); got += r.value.length;
    if (got > maxBytes) { try { await reader.cancel(); } catch { /* ignore */ } throw new LibraryError('http', '文件过大，已取消。'); }
    onProgress(got, total);
  }
  const out = new Uint8Array(got);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  if (!out.length) throw new LibraryError('http', '下载到的文件是空的。');
  return out;
}

// ---------------------------------------------------------------- search state machine
// status: idle (nothing searched) | loading | ready | empty | error ; `more` = a load-more request is running;
// error.kind 'loading' = catalog_loading (the UI schedules an automatic retry, `retries` counts them).
export const initialState = (lang = 'en') => ({ seq: 0, q: '', lang, status: 'idle', items: [], total: 0, more: false, error: null, retries: 0 });

/** events: {type:'search', q, lang} | {type:'more'} | {type:'loaded', seq, total, items, append} | {type:'failed', seq, error, more} | {type:'retry'} */
export function reduce(s, ev) {
  switch (ev.type) {
    case 'search': {
      const q = String(ev.q || '').trim();
      if (!q) return { ...initialState(ev.lang || s.lang), seq: s.seq + 1 };
      return { ...s, seq: s.seq + 1, q, lang: ev.lang || s.lang, status: 'loading', items: [], total: 0, more: false, error: null, retries: 0 };
    }
    case 'retry':   // automatic retry of the same query after catalog_loading
      return s.status === 'error' && s.error && s.error.kind === 'loading' ? { ...s, seq: s.seq + 1, status: 'loading', error: null, retries: s.retries + 1 } : s;
    case 'more':
      return s.status === 'ready' && !s.more && hasMore(s) ? { ...s, seq: s.seq + 1, more: true, error: null } : s;
    case 'loaded': {
      if (ev.seq !== s.seq) return s;   // stale answer of a superseded request
      const items = ev.append ? dedupe([...s.items, ...ev.items]) : dedupe(ev.items);
      return { ...s, status: items.length ? 'ready' : 'empty', items, total: ev.total, more: false, error: null, retries: 0 };
    }
    case 'failed': {
      if (ev.seq !== s.seq) return s;
      if (ev.more) return { ...s, more: false, error: ev.error };   // keep the list; the error shows under it
      return { ...s, status: 'error', more: false, error: ev.error };
    }
    default: return s;
  }
}

function dedupe(items) { const seen = new Set(); return items.filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true))); }
export const hasMore = (s) => s.items.length > 0 && s.items.length < s.total;
export const canAutoRetry = (s) => s.status === 'error' && !!s.error && s.error.kind === 'loading' && s.retries < MAX_AUTO_RETRIES;
