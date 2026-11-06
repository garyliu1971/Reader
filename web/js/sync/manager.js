// App-level sync manager: owns the SyncEngine, persists the config (IndexedDB only), schedules pushes and refreshes.
// Credentials live ONLY in IndexedDB of this browser profile (never in the URL, localStorage or any request but the
// configured WebDAV server).
import { kvGet, kvSet } from '../store/db.js';
import { SyncEngine, validateConfig, newDeviceId } from './sync.js';
import { debounce } from '../ui/dom.js';

const CFG_KEY = 'sync.config';
const LIB_KEY = 'sync.library';

export const syncMgr = {
  cfg: null,           // {enabled,url,user,password,syncUser,deviceId,deviceName}
  engine: null,
  status: { state: 'off', msg: '', at: 0 },
  listeners: new Set(),
  lib: { at: 0, files: [], unstable: [], complete: false, problem: '' },
  _busy: null,
  _bookTimers: new Map(),

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
  emit() { for (const fn of this.listeners) { try { fn(this.status); } catch { /* ignore */ } } },
  setStatus(state, msg = '') {
    this.status = { state, msg, at: Date.now() };
    this.emit();
  },

  now: () => Date.now(),

  async load() {
    let cfg = await kvGet(CFG_KEY, null);
    if (!cfg || typeof cfg !== 'object') {
      cfg = { enabled: false, url: '', user: '', password: '', syncUser: 'default', deviceId: newDeviceId(), deviceName: 'PyReader Web' };
      await kvSet(CFG_KEY, cfg);
    }
    this.cfg = cfg;
    this.lib = (await kvGet(LIB_KEY, null)) || this.lib;
    await this.rebuild();
    this.setStatus(this.enabled() ? 'idle' : 'off', this.enabled() ? '' : '同步未开启');
  },

  enabled() { return !!(this.cfg && this.cfg.enabled && this.cfg.url); },

  /** (re)create the engine from this.cfg; offline-capable even without a URL */
  async rebuild() {
    const v = this.cfg.url ? validateConfig(this.cfg) : null;
    const base = v && v.ok ? v.cfg : { url: '', user: '', password: '', syncUser: 'default', deviceId: this.cfg.deviceId, deviceName: this.cfg.deviceName || 'PyReader Web' };
    this.engine = await new SyncEngine(base, { kv: { get: kvGet, set: kvSet }, now: () => this.now() }).init();
  },

  /** Validate + store a settings form. Returns {ok, errors}. Does not touch the network. */
  async saveConfig(input) {
    const v = validateConfig({ ...input });
    if (!v.ok) return { ok: false, errors: v.errors };
    this.cfg = { enabled: !!input.enabled, ...v.cfg };
    await kvSet(CFG_KEY, this.cfg);
    await this.rebuild();
    this.setStatus(this.enabled() ? 'idle' : 'off', this.enabled() ? '已保存' : '同步未开启');
    return { ok: true, errors: [] };
  },

  async disable({ forgetCredentials = true } = {}) {
    this.cfg = { ...this.cfg, enabled: false, ...(forgetCredentials ? { user: '', password: '' } : {}) };
    await kvSet(CFG_KEY, this.cfg);
    await this.rebuild();
    this.setStatus('off', '同步未开启');
  },

  // ---------------------------------------------------------------- refresh
  /** pull peers + library listing, then push our device file if it changed. Single flight. */
  syncNow({ quiet = false, force = false } = {}) {
    if (!this.enabled()) return Promise.resolve(null);
    if (this._busy) return this._busy;
    this.setStatus('syncing', '同步中…');
    this._busy = (async () => {
      const e = this.engine;
      const out = { peers: null, library: null, pushed: null, errors: [] };
      try {
        out.peers = await e.pullPeers({ force });
        const prevSizes = Object.fromEntries((this.lib.files || []).map((f) => [f.rel, f.size]));
        const l = await e.listLibrary({ prevSizes });
        this.lib = {
          at: Date.now(), files: l.files, unstable: [...l.unstable], complete: l.complete,
          problem: l.rootMissing ? '云端没有 library 文件夹' : l.truncated ? '书库超过读取上限，只显示了一部分' : l.errors.length ? `${l.errors.length} 个子文件夹读取失败` : '',
        };
        await kvSet(LIB_KEY, this.lib);
        out.library = l;
        if (e.dirty() || out.peers.ownBehind) out.pushed = await e.pushDevice({ force: out.peers.ownBehind });
        this.setStatus('idle', `已同步 ${new Date().toLocaleTimeString()}`);
      } catch (err) {
        out.errors.push(err);
        this.setStatus('error', `同步失败：${err && err.message ? err.message : err}`);
        if (!quiet) console.warn('[sync]', err);
      } finally { this._busy = null; }
      return out;
    })();
    return this._busy;
  },

  /** Connection test with the *saved* config. */
  async test() {
    if (!this.cfg || !this.cfg.url) throw new Error('请先填写并保存服务器地址');
    const r = await this.engine.test();
    return r;
  },

  // ---------------------------------------------------------------- reading hooks
  /** called by the reader after each saved position */
  onProgress({ bid, percent, chapter, updated }) {
    const e = this.engine;
    if (!e) return;
    try { e.recordProgress(bid, percent, updated); } catch (err) { console.warn(err); }
    e.persist().catch(() => {});
    this.shelfChanged && this.shelfChanged();
    if (!this.enabled()) return;
    let t = this._bookTimers.get(bid);
    if (!t) { t = debounce(() => this.pushBook(bid), 4000); this._bookTimers.set(bid, t); }
    t.latest = { percent, chapter, updated };
    t();
    this._devicePush();
  },

  _devicePush: debounce(() => syncMgr.pushDeviceNow(), 12000),

  async pushBook(bid) {
    const t = this._bookTimers.get(bid);
    const l = t && t.latest;
    if (!l || !this.enabled()) return null;
    const r = await this.engine.pushBookProgress({ bid, percent: l.percent, chapter: l.chapter, updated: l.updated });
    if (r.status === 'error') this.setStatus('error', `进度上传失败：${r.message}`);
    return r;
  },

  async pushDeviceNow() {
    if (!this.enabled()) return;
    try { await this.engine.pushDevice(); this.setStatus('idle', `已同步 ${new Date().toLocaleTimeString()}`); } catch (err) { this.setStatus('error', `同步失败：${err.message || err}`); }
  },

  /** flush pending debounced pushes (page hidden / reader closed); best effort */
  flush() {
    for (const t of this._bookTimers.values()) t.flush();
    this._devicePush.flush();
  },

  async fetchRemoteProgress(bid) {
    if (!this.enabled()) return null;
    return this.engine.pullBookProgress(bid);
  },

  setFlag(bid, field, value) {
    this.engine.setFlag(bid, field, value);
    this.engine.persist().catch(() => {});
    if (this.enabled()) this._devicePush();
  },
};
