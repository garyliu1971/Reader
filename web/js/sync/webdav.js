// Minimal WebDAV client on top of fetch(). Mirrors pyreader/webdav.py behaviour: everything the server returns is
// untrusted (size caps, defensive XML), no DELETE method exists here at all, and credentials are only sent over
// https (or to private/local hosts over http).

import { safeSegment, isHiddenName } from '../core/names.js';
import { DavError, parseMultistatus, hostIsPrivate, PROPFIND_MAX_BYTES } from './propfind.js';

export { DavError };
export const JSON_MAX_BYTES = 1024 * 1024;
export const MAX_DIRS = 500;
export const MAX_DEPTH = 8;
export const MAX_FILES = 10000;

const PROPFIND_BODY = '<?xml version="1.0" encoding="utf-8"?>'
  + '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getetag/></d:prop></d:propfind>';

function basicAuth(user, password) {
  const bytes = new TextEncoder().encode(`${user}:${password}`);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return 'Basic ' + btoa(bin);
}

function concat(chunks, total) {
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

export class DavClient {
  /**
   * @param {{baseUrl:string,user?:string,password?:string,timeoutMs?:number,fetchImpl?:Function}} o
   */
  constructor({ baseUrl, user = '', password = '', timeoutMs = 20000, fetchImpl = null }) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.user = user;
    this.password = password;
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl || ((...a) => fetch(...a));
    this.knownDirs = new Set();
    this.skewMs = null;        // server Date minus local clock (needs Access-Control-Expose-Headers: Date)
    const u = new URL(this.base);
    if (u.protocol === 'http:' && !hostIsPrivate(u.hostname) && (user || password)) {
      throw new DavError('refusing to send credentials over plain http to a public host');
    }
  }

  url(path = '') {
    const parts = String(path).replace(/\\/g, '/').split('/').filter(Boolean);
    if (parts.some((p) => p === '.' || p === '..')) throw new DavError(`illegal path segment in ${path}`);
    return this.base + (parts.length ? '/' + parts.map((p) => encodeURIComponent(p)).join('/') : '');
  }

  /** decoded URL path of a remote path (what PROPFIND hrefs are compared against) */
  urlPath(path = '') {
    return decodeURIComponent(new URL(this.url(path)).pathname);
  }

  headers(extra = {}) {
    const h = { ...extra };
    if (this.user || this.password) h.Authorization = basicAuth(this.user, this.password);
    return h;
  }

  /**
   * Low-level request. Returns {status, headers(lower-case), body: Uint8Array}. HTTP errors outside `okCodes` throw DavError.
   * The body is read with a hard cap (maxBytes) and checked against Content-Length (truncation).
   */
  async request(method, path, { body = null, headers = {}, okCodes = [], maxBytes = null, onProgress = null, signal = null, idleMs = null } = {}) {
    const what = `${method} ${path || '/'}`;
    const ctl = new AbortController();
    let timer = null;
    const arm = () => { clearTimeout(timer); timer = setTimeout(() => ctl.abort(new DOMException('timeout', 'TimeoutError')), idleMs || this.timeoutMs); };
    if (signal) {
      if (signal.aborted) throw new DavError(`${what}: cancelled`);
      signal.addEventListener('abort', () => ctl.abort(new DOMException('cancelled', 'AbortError')), { once: true });
    }
    arm();
    let resp;
    try {
      resp = await this.fetch(this.url(path), {
        method, headers: this.headers(headers), body, signal: ctl.signal,
        credentials: 'omit', cache: 'no-store', mode: 'cors',
      });
    } catch (e) {
      clearTimeout(timer);
      if (e instanceof DavError) throw e;
      const cancelled = signal && signal.aborted;
      throw new DavError(`${what} ${cancelled ? 'cancelled' : 'failed'}: ${e && e.message ? e.message : e}`
        + (cancelled ? '' : ' (网络/CORS：请检查服务器的 CORS 配置，见 web/README.md)'));
    }
    try {
      const hdrs = {};
      resp.headers.forEach((v, k) => { hdrs[k.toLowerCase()] = v; });
      if (hdrs.date) {
        const t = Date.parse(hdrs.date);
        if (Number.isFinite(t)) this.skewMs = t - Date.now();
      }
      const ok = (resp.status >= 200 && resp.status < 300) || okCodes.includes(resp.status);
      // With Content-Encoding (gzip from Caddy/SFTPGo) Content-Length counts the compressed bytes while the browser hands
      // us the decoded stream, so it cannot be compared with what we read. A truncated compressed stream errors in fetch
      // itself; the maxBytes cap below still applies to the decoded size.
      const encoded = !['', 'identity'].includes((hdrs['content-encoding'] || '').trim().toLowerCase());
      const cl = !encoded && /^\d+$/.test((hdrs['content-length'] || '').trim()) ? parseInt(hdrs['content-length'], 10) : null;
      if (maxBytes !== null && cl !== null && cl > maxBytes) { ctl.abort(); throw new DavError(`${what}: response too large (${cl} > ${maxBytes})`); }
      if (!ok) {
        try { await resp.body?.cancel(); } catch { /* ignore */ }
        throw new DavError(`${what} -> HTTP ${resp.status}`, resp.status);
      }
      let data = new Uint8Array(0);
      if (resp.body && method !== 'HEAD') {
        const reader = resp.body.getReader();
        const chunks = [];
        let total = 0;
        for (;;) {
          arm();
          const { done, value } = await reader.read();
          if (done) break;
          total += value.length;
          if (maxBytes !== null && total > maxBytes) { ctl.abort(); throw new DavError(`${what}: response too large (> ${maxBytes})`); }
          chunks.push(value);
          if (onProgress) { try { onProgress(total, cl || 0); } catch { /* callback errors never break the transfer */ } }
        }
        data = concat(chunks, total);
        if (cl !== null && total !== cl) throw new DavError(`${what}: truncated response (${total} of ${cl} bytes)`);
      }
      return { status: resp.status, headers: hdrs, body: data };
    } catch (e) {
      if (e instanceof DavError) throw e;
      throw new DavError(`${what} failed: ${e && e.message ? e.message : e}`);
    } finally {
      clearTimeout(timer);
    }
  }

  async propfind(path = '', depth = 1) {
    const r = await this.request('PROPFIND', path, {
      body: PROPFIND_BODY, headers: { Depth: String(depth), 'Content-Type': 'application/xml; charset=utf-8' },
      maxBytes: PROPFIND_MAX_BYTES,
    });
    const text = new TextDecoder('utf-8').decode(r.body);
    return { entries: parseMultistatus(text, this.urlPath(path)), headers: r.headers };
  }

  async exists(path = '') {
    const r = await this.request('PROPFIND', path, {
      body: PROPFIND_BODY, headers: { Depth: '0', 'Content-Type': 'application/xml' }, okCodes: [404], maxBytes: PROPFIND_MAX_BYTES,
    });
    return r.status !== 404;
  }

  get(path, opts = {}) { return this.request('GET', path, opts); }

  put(path, data, headers = {}, okCodes = []) {
    return this.request('PUT', path, { body: data, headers: { 'Content-Type': 'application/octet-stream', ...headers }, okCodes })
      .catch((e) => { if (e.status === 404 || e.status === 409) this.knownDirs.clear(); throw e; });
  }

  /** WebDAV MOVE (used only to finish an upload: .part -> final name). */
  move(src, dst, { overwrite = false, okCodes = [] } = {}) {
    return this.request('MOVE', src, { headers: { Destination: this.url(dst), Overwrite: overwrite ? 'T' : 'F' }, okCodes });
  }

  async mkcol(path) {
    const r = await this.request('MKCOL', path, { okCodes: [403, 405] });
    let st = r.status;
    if (st === 403) {   // some servers answer 403 for an existing mount point: confirm by PROPFIND
      if (!(await this.exists(path))) throw new DavError(`MKCOL ${path}: 403 Forbidden`, 403);
      st = 405;
    }
    this.knownDirs.add(path);
    return st !== 405;
  }

  async mkdirs(path) {
    let cur = '';
    for (const p of String(path).split('/').filter(Boolean)) {
      cur = cur ? `${cur}/${p}` : p;
      if (!this.knownDirs.has(cur)) await this.mkcol(cur);
    }
  }

  /**
   * Recursive listing with the same caps as the Qt client. Names are filtered; failures in a sub-folder make the
   * listing incomplete but do not throw (the root failing does).
   */
  async listTree(path, { maxFiles = MAX_FILES, maxDirs = MAX_DIRS, maxDepth = MAX_DEPTH, accept = null, concurrency = 4 } = {}) {
    const res = { files: [], complete: true, truncated: false, errors: [], skipped: 0 };
    const seenFiles = new Set();
    const visited = new Set(['']);
    const queue = [['', 0]];
    let listed = 0;
    let active = 0;
    let rootError = null;
    const join = (a, b) => (a ? `${a}/${b}` : b);
    const work = async () => {
      while (queue.length || active) {
        if (!queue.length) { await new Promise((r) => setTimeout(r, 5)); continue; }
        if (listed >= maxDirs) { res.truncated = true; queue.length = 0; return; }
        const [rel, depth] = queue.shift();
        listed++;
        active++;
        try {
          let entries;
          try {
            entries = (await this.propfind(join(path, rel), 1)).entries;
          } catch (e) {
            if (rel === '') { rootError = e; queue.length = 0; return; }
            res.errors.push(rel);
            continue;
          }
          for (const e of entries) {
            if (safeSegment(e.name) === null || isHiddenName(e.name)) { res.skipped++; continue; }
            const r = join(rel, e.name);
            const key = r.normalize('NFC');
            if (e.isDir) {
              if (depth + 1 > maxDepth) { res.truncated = true; continue; }
              if (visited.has(key)) continue;
              visited.add(key);
              queue.push([r, depth + 1]);
            } else {
              if (seenFiles.has(key)) continue;
              if (accept && !accept(r)) { res.skipped++; continue; }
              if (res.files.length >= maxFiles) { res.truncated = true; queue.length = 0; break; }
              seenFiles.add(key);
              res.files.push({ rel: r, entry: e });
            }
          }
        } finally { active--; }
      }
    };
    await Promise.all(Array.from({ length: concurrency }, work));
    if (rootError) throw rootError;
    res.complete = !res.errors.length && !res.truncated;
    return res;
  }
}
