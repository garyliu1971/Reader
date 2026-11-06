// Sync engine: PyReader-compatible shelf/progress sync over WebDAV. Wire format and rules are taken from
// pyreader/shelfsync.py, sync.py, shelf.py (authoritative). Layout on the server:
//   <base>/library/<rel>                                  books (the web app only reads them; uploading is a manual action)
//   <base>/users/<user>/progress/<sha1(book_id)[:20]>.json  per-book progress
//   <base>/users/<user>/shelf/<device_id>.json              one file per device; each device writes ONLY its own
// There is intentionally no code path that issues DELETE, and nothing overwrites an existing book.

import { safeComponent, safeSegment, normalizeRel, isHiddenName, validBookId, validRemoteRel, extOf } from '../core/names.js';
import { progressPath } from '../core/bookid.js';
import { parseProgressBody, makeRecord, stampNow, FUTURE_LIMIT_S } from '../core/progress.js';
import {
  validateOverlay, merge, newOverlay, setProgress, setField, pruneProgress, wireText, contentKey, reconcileOwn, hasContent,
  MAX_WIRE_BYTES, cleanStr,
} from '../core/shelfmerge.js';
import { DavClient, DavError, JSON_MAX_BYTES, MAX_FILES } from './webdav.js';
import { checkServerUrl } from './propfind.js';

export const LIBRARY = 'library';
export const STABLE_SECONDS = 60;
export const CLOCK_SLACK = 300;
export const RACY_SECONDS = 2;
export const FULL_EVERY = 10;
export const MAX_BOOK_BYTES = 300 * 1024 * 1024;   // browser memory: far below the Qt 1 GB limit on purpose
export const STALE_PART_SECONDS = 900;
const MOVE_UNSUPPORTED = [400, 403, 405, 415, 501];

export function newDeviceId() {
  const b = new Uint8Array(3);
  (globalThis.crypto || {}).getRandomValues ? crypto.getRandomValues(b) : b.forEach((_, i) => { b[i] = Math.floor(Math.random() * 256); });
  return 'web-' + Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

/** Validate the settings form. Returns {ok, errors[], cfg} with the cleaned config. */
export function validateConfig(input) {
  const errors = [];
  const u = checkServerUrl(input.url || '');
  if (!u.ok) errors.push(u.error);
  const syncUser = safeComponent(((input.syncUser || '').trim()) || 'default');
  if (!syncUser) errors.push('同步用户名不合法（只允许字母、数字、点、下划线、连字符，最长 64，不能以点开头/结尾）');
  const deviceId = safeComponent((input.deviceId || '').trim() || newDeviceId());
  if (!deviceId) errors.push('设备 ID 不合法（同上规则）');
  const deviceName = cleanStr((input.deviceName || '').trim() || 'PyReader Web', 64);
  return {
    ok: errors.length === 0, errors,
    cfg: { url: u.url, user: input.user || '', password: input.password || '', syncUser, deviceId, deviceName },
  };
}

const deviceDir = (user) => `users/${user}/shelf`;
const deviceFile = (user, dev) => `${deviceDir(user)}/${dev}.json`;

function needsFetch(cur, ent) {
  if (!cur || typeof cur !== 'object') return true;
  if (!ent.mtime && !ent.etag) return true;
  if ((ent.etag || cur.etag) && (cur.etag || '') !== (ent.etag || '')) return true;
  if (Number(cur.mtime || 0) !== ent.mtime || Number(cur.size ?? -1) !== ent.size) return true;
  if (typeof cur.fetchedAt !== 'number' || cur.fetchedAt - Number(cur.mtime || 0) < RACY_SECONDS) return true;
  return false;
}

export class SyncEngine {
  /**
   * @param cfg cleaned config (see validateConfig)
   * @param kv {get(key,default), set(key,value)} persistence (IndexedDB in the app, a Map in tests)
   */
  constructor(cfg, { kv, fetchImpl = null, now = () => Date.now() } = {}) {
    this.cfg = cfg;
    this.kv = kv;
    this.now = now;
    // no URL configured = local-only mode: favourites/hidden/progress digest still work offline, nothing touches the network
    this._client = cfg.url ? new DavClient({ baseUrl: cfg.url, user: cfg.user, password: cfg.password, fetchImpl }) : null;
    this.own = null;        // own overlay (view form)
    this.peers = {};        // dev -> {view, cursor}
    this.state = { pushedKey: null, ownStamp: null, rounds: 0 };
    this.ready = false;
  }

  get client() {
    if (!this._client) throw new DavError('未配置同步服务器');
    return this._client;
  }

  get user() { return this.cfg.syncUser; }
  get deviceId() { return this.cfg.deviceId; }
  skewMs() { return (this._client && this._client.skewMs) || 0; }

  async init() {
    const ns = `sync.${this.deviceId}.`;
    let own = await this.kv.get(ns + 'own', null);
    const [clean] = own ? validateOverlay(own, this.now(), { view: true }) : [null];
    if (clean && clean.device_id === this.deviceId) { own = { ...clean, _rev: own._rev || 0 }; } else { own = newOverlay(this.deviceId, this.cfg.deviceName, this.user); }
    own.hostname = this.cfg.deviceName;
    own.user = this.user;
    this.own = own;
    this.peers = await this.kv.get(ns + 'peers', {}) || {};
    this.state = { ...this.state, ...(await this.kv.get(ns + 'state', {}) || {}) };
    this.ready = true;
    return this;
  }

  async persist() {
    const ns = `sync.${this.deviceId}.`;
    await this.kv.set(ns + 'own', this.own);
    await this.kv.set(ns + 'peers', this.peers);
    await this.kv.set(ns + 'state', this.state);
  }

  // ------------------------------------------------------------ connection / library
  /** Cheap connectivity + CORS + auth check. */
  async test() {
    await this.client.propfind('', 0);
    const lib = await this.client.exists(LIBRARY);
    return { ok: true, libraryExists: lib, skewMs: this.client.skewMs };
  }

  /**
   * List <base>/library (recursive, capped). Only books the web reader can open are returned; entries whose mtime is
   * recent or whose size changed since `prevSizes` are marked unstable (still being uploaded).
   */
  async listLibrary({ prevSizes = {}, trusted = {} } = {}) {
    const nowS = this.now() / 1000;
    let tree;
    try {
      tree = await this.client.listTree(LIBRARY, { maxFiles: MAX_FILES, accept: (rel) => validRemoteRel(rel) });
    } catch (e) {
      if (e.status === 404) return { files: [], unstable: new Set(), complete: false, truncated: false, errors: [], skipped: 0, rootMissing: true };
      throw e;
    }
    const files = tree.files
      .map(({ rel, entry }) => ({ rel, size: entry.size, mtime: entry.mtime }))
      .sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
    const unstable = new Set();
    for (const f of files) {
      if (trusted[f.rel] === f.size) continue;
      if (f.mtime > 0) {
        const age = nowS - f.mtime;
        if (age > -CLOCK_SLACK && age < STABLE_SECONDS) { unstable.add(f.rel); continue; }
      }
      const prev = prevSizes[f.rel];
      if (prev !== undefined && prev !== f.size) unstable.add(f.rel);
    }
    return { files, unstable, complete: tree.complete, truncated: tree.truncated, errors: tree.errors, skipped: tree.skipped, rootMissing: false };
  }

  /** Download a library book into memory. The size must match the listing (when known). */
  async downloadBook(rel, size, { onProgress = null, signal = null, maxBytes = MAX_BOOK_BYTES } = {}) {
    if (normalizeRel(rel) === null || rel.split('/').some(isHiddenName)) throw new DavError('illegal path');
    if (size > maxBytes) throw new DavError(`文件过大（${Math.round(size / 1048576)} MB，上限 ${Math.round(maxBytes / 1048576)} MB）`);
    const r = await this.client.get(`${LIBRARY}/${rel}`, { maxBytes, onProgress, signal, idleMs: 60000 });
    if (size > 0 && r.body.length !== size) throw new DavError(`size mismatch for ${rel}: got ${r.body.length}, expected ${size}`);
    return r.body;
  }

  // ------------------------------------------------------------ device files (shelf overlay)
  /** Pull the other devices' files (cursor-aware) and check our own file. */
  async pullPeers({ force = false } = {}) {
    const dir = deviceDir(this.user);
    let entries = [];
    let headers = {};
    try {
      ({ entries, headers } = await this.client.propfind(dir, 1));
    } catch (e) {
      if (e.status !== 404) throw e;
    }
    const nowS = this.now() / 1000;
    const dateH = headers.date ? Date.parse(headers.date) / 1000 : NaN;
    const fetchedAt = Number.isFinite(dateH) ? dateH : nowS;
    this.state.rounds = (this.state.rounds || 0) + 1;
    const full = force || this.state.rounds >= FULL_EVERY;
    if (full) this.state.rounds = 0;
    const listed = {};
    let ownEnt = null;
    let skipped = 0;
    for (const e of entries) {
      if (e.isDir || !e.name.endsWith('.json')) continue;
      const dev = safeComponent(e.name.slice(0, -5));
      if (dev === null) { skipped++; continue; }
      if (dev === this.deviceId) ownEnt = e; else listed[dev] = e;
    }
    const res = { fetched: [], unchanged: [], failed: [], corrupt: [], dropped: [], skipped, ownAdopted: false, ownBehind: false };
    for (const dev of Object.keys(listed).sort()) {
      const ent = listed[dev];
      const cached = this.peers[dev];
      if (!full && cached && cached.view && !needsFetch(cached.cursor, ent)) { res.unchanged.push(dev); continue; }
      const cursor = { mtime: ent.mtime, size: ent.size, etag: ent.etag, fetchedAt };
      let body;
      try {
        body = await this.client.get(deviceFile(this.user, dev), { maxBytes: JSON_MAX_BYTES });
      } catch (e) { res.failed.push(dev); continue; }
      let raw = null;
      try { raw = JSON.parse(new TextDecoder().decode(body.body).replace(/^﻿/, '')); } catch { /* corrupt */ }
      const [view] = raw && typeof raw === 'object' && !Array.isArray(raw) ? validateOverlay(raw, this.now(), { deviceId: dev }) : [null];
      if (!view) { res.corrupt.push(dev); this.peers[dev] = { view: cached ? cached.view : null, cursor }; continue; }
      this.peers[dev] = { view, cursor };
      res.fetched.push(dev);
    }
    for (const dev of Object.keys(this.peers)) {
      if (!(dev in listed)) { delete this.peers[dev]; res.dropped.push(dev); }
    }
    // our own file: self-heal (server rolled back / device id cloned / file deleted)
    if (ownEnt === null) {
      res.ownBehind = hasContent(this.own);
    } else if (full || needsFetch(this.state.ownStamp, ownEnt)) {
      try {
        const r = await this.client.get(deviceFile(this.user, this.deviceId), { maxBytes: JSON_MAX_BYTES });
        const raw = JSON.parse(new TextDecoder().decode(r.body).replace(/^﻿/, ''));
        const [view] = validateOverlay(raw, this.now(), { deviceId: this.deviceId });
        if (!view) res.ownBehind = true;
        else {
          const { adopted, behind } = reconcileOwn(this.own, view, this.now());
          if (adopted) { this.own = { ...this.own, entries: adopted.entries, progress: adopted.progress, _clock: adopted._clock, _rev: (this.own._rev || 0) + 1 }; res.ownAdopted = true; }
          res.ownBehind = behind;
        }
        this.state.ownStamp = { mtime: ownEnt.mtime, size: ownEnt.size, etag: ownEnt.etag, fetchedAt };
      } catch { /* leave for the next round */ }
    }
    await this.persist();
    return res;
  }

  /** Merged view of this device + every peer. */
  mergedView() {
    return merge([this.own, ...Object.values(this.peers).map((p) => p.view).filter(Boolean)], this.now());
  }

  /** {bid: {percent, updated, device}} best progress digest across peers (and this device). */
  digest() {
    const best = {};
    const consider = (view, label) => {
      for (const [bid, rec] of Object.entries(view.progress || {})) {
        const cur = best[bid];
        if (!cur || rec.updated > cur.updated) best[bid] = { percent: rec.percent, updated: rec.updated, device: label };
      }
    };
    for (const [dev, p] of Object.entries(this.peers)) if (p.view) consider(p.view, cleanStr(p.view.hostname, 64) || dev);
    return best;
  }

  /** Record local reading progress in our own device digest (pushed later by pushDevice). */
  recordProgress(bid, percent, updatedSec) {
    if (!validBookId(bid)) return;
    setProgress(this.own, bid, percent, updatedSec);
    pruneProgress(this.own);
  }

  setFlag(bid, field, value, meta = null) {
    if (!validBookId(bid)) return;
    setField(this.own, bid, field, value, { merged: this.mergedView(), nowMs: this.now(), skewMs: this.skewMs(), meta });
  }

  /** Is there anything new to upload? */
  dirty() {
    return hasContent(this.own) && contentKey(this.own, this.deviceId) !== this.state.pushedKey;
  }

  /** PUT our device file (verified by reading it back). Returns 'pushed' | 'unchanged'. */
  async pushDevice({ force = false } = {}) {
    if (!force && !this.dirty()) return 'unchanged';
    let text = wireText(this.own, this.deviceId);
    for (let k = 0; new TextEncoder().encode(text).length > MAX_WIRE_BYTES && k < 8; k++) {
      pruneProgress(this.own, Math.max(10, Math.floor(Object.keys(this.own.progress).length / 2)));
      text = wireText(this.own, this.deviceId);
    }
    const bytes = new TextEncoder().encode(text);
    if (bytes.length > MAX_WIRE_BYTES) throw new DavError('设备文件超过 1 MiB，已放弃上传');
    await this.client.mkdirs(deviceDir(this.user));
    const path = deviceFile(this.user, this.deviceId);
    await this.client.put(path, bytes);
    const back = await this.client.get(path, { maxBytes: JSON_MAX_BYTES });
    if (new TextDecoder().decode(back.body) !== text) {
      throw new DavError('推送后回读校验失败：服务器上的内容与刚写入的不一致（可能有别的写者），稍后重试');
    }
    this.state.pushedKey = contentKey(this.own, this.deviceId);
    this.state.ownStamp = null;   // next pullPeers re-reads our file once and confirms
    await this.persist();
    return 'pushed';
  }

  // ------------------------------------------------------------ per-book progress
  async pullBookProgress(bid) {
    if (!validBookId(bid)) return null;
    let r;
    try { r = await this.client.get(progressPath(this.user, bid), { maxBytes: JSON_MAX_BYTES, okCodes: [404] }); } catch { return null; }
    if (r.status === 404) return null;
    return parseProgressBody(new TextDecoder().decode(r.body), bid, this.now() / 1000);
  }

  /**
   * Write a per-book progress file. Reads the remote one first and never regresses a newer record.
   * @returns {{status:'written'|'remote-newer'|'error', remote?:object, message?:string}}
   */
  async pushBookProgress({ bid, percent, offset = undefined, chapter = '', updated = null }) {
    if (!validBookId(bid)) return { status: 'error', message: 'bad book id' };
    const stamp = updated ?? stampNow(this.now(), this.skewMs());
    try {
      const remote = await this.pullBookProgress(bid);
      if (remote && remote.updated > stamp) return { status: 'remote-newer', remote };
      await this.client.mkdirs(`users/${this.user}/progress`);
      const rec = makeRecord({ bid, offset, percent, chapter, updated: stamp, device: this.cfg.deviceName || this.deviceId });
      await this.client.put(progressPath(this.user, bid), JSON.stringify(rec), { 'Content-Type': 'application/octet-stream' });
      return { status: 'written', updated: stamp };
    } catch (e) {
      return { status: 'error', message: String(e.message || e) };
    }
  }

  // ------------------------------------------------------------ manual upload (never overwrites)
  /**
   * Upload `data` (Uint8Array|Blob) as library/<folder>/<name>.
   * Flow (same as Qt upload_book_safe): refuse if the name exists -> PUT .<name>.part (If-None-Match: *) -> verify
   * size -> MOVE with Overwrite: F (and If-None-Match: *) -> verify. Falls back to a conditional PUT if MOVE is
   * unsupported. Returns {status:'ok'|'exists'|'busy'|'error', rel, message}.
   */
  async uploadBook({ name, folder = '', data, size }) {
    const nk = (s) => s.normalize('NFC').toLowerCase();
    const f = (folder || '').trim().replace(/^\/+|\/+$/g, '');
    const segs = f ? f.split('/') : [];
    if (segs.length > 8 || segs.some((s) => safeSegment(s) === null || isHiddenName(s) || s !== s.trim())) {
      return { status: 'error', rel: '', message: '文件夹名不合法' };
    }
    const rel = f ? `${f}/${name}` : name;
    if (typeof name !== 'string' || name.length > 240 || name !== name.trim() || safeSegment(name) === null || !validRemoteRel(rel)) {
      return { status: 'error', rel: '', message: `名字不合法或格式不受支持（${extOf(name || '')}）` };
    }
    if (!(size > 0)) return { status: 'error', rel, message: '文件是空的' };
    if (size > MAX_BOOK_BYTES) return { status: 'error', rel, message: '文件超过上传上限' };
    const ldir = f ? `${LIBRARY}/${f}` : LIBRARY;
    const partName = `.${name}.part`;
    const clash = (ents) => ents.some((e) => !e.isDir && nk(e.name) === nk(name));
    try {
      await this.client.mkdirs(ldir);
      let ents = (await this.client.propfind(ldir, 1)).entries;
      if (clash(ents)) return { status: 'exists', rel, message: `云端已有同名文件：${rel}` };
      const part = `${ldir}/${partName}`;
      const headers = {};
      const old = ents.find((e) => e.name === partName);
      if (!old) headers['If-None-Match'] = '*';
      else if (!(old.size === 0 || (old.mtime && this.now() / 1000 - old.mtime > STALE_PART_SECONDS))) {
        return { status: 'busy', rel, message: `另一台设备正在上传同名文件：${rel}（稍后再试）` };
      }
      const r = await this.client.put(part, data, headers, [412]);
      if (r.status === 412) return { status: 'busy', rel, message: `另一台设备正在上传同名文件：${rel}（稍后再试）` };
      ents = (await this.client.propfind(ldir, 1)).entries;
      const pe = ents.find((e) => e.name === partName);
      if (!pe || pe.size !== size) return { status: 'error', rel, message: '上传不完整（服务器上的大小与本机文件不符），请重试' };
      if (clash(ents)) return { status: 'exists', rel, message: `云端已有同名文件：${rel}` };
      const target = `${ldir}/${name}`;
      let method = 'move';
      try {
        const mr = await this.client.request('MOVE', part, {
          headers: { Destination: this.client.url(target), Overwrite: 'F', 'If-None-Match': '*' }, okCodes: [412],
        });
        if (mr.status === 412) return { status: 'exists', rel, message: `云端已有同名文件：${rel}` };
      } catch (e) {
        if (!MOVE_UNSUPPORTED.includes(e.status)) throw e;
        method = 'put';
        const pr = await this.client.put(target, data, { 'If-None-Match': '*' }, [412]);
        if (pr.status === 412) return { status: 'exists', rel, message: `云端已有同名文件：${rel}` };
        // the leftover .part is hidden (starts with '.') and ignored by every listing; nothing is deleted remotely
      }
      const got = (await this.client.propfind(ldir, 1)).entries.find((e) => e.name === name);
      if (!got || got.isDir || got.size !== size) return { status: 'error', rel, message: '上传后校验失败（云端文件的大小与本机不符），请重试' };
      return { status: 'ok', rel, method, message: `已上传：${rel}` };
    } catch (e) {
      return { status: 'error', rel, message: `上传失败：${String(e.message || e).slice(0, 160)}` };
    }
  }
}

export { FUTURE_LIMIT_S };
