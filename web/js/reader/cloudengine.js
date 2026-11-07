// Cloud read-aloud engine: one <audio> element (unlocked by the Play tap via prime()), sentences fetched from the
// server's /tts/v1/speak a few ahead of the one being played. Speed = audio.playbackRate (pitch preserved), so the
// server cache is shared across speeds. Everything browser-specific is injected, so it is unit-tested with fakes.
import { PrefetchQueue, PREFETCH_AHEAD, MAX_TEXT, classifyStatus, endpoint, pickCloudVoice, clampPlaybackRate } from '../core/cloudtts.js';

// 0.1 s of silence: playing it inside the user's tap unlocks the element for later programmatic play() calls
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';

export class CloudError extends Error {
  constructor(kind, message, extra = {}) { super(message); this.kind = kind; Object.assign(this, extra); }
}

/** thin client for the three endpoints; `getCfg()` -> {token}; `baseHref` = the app's URL (endpoints resolve relative to it) */
export class CloudApi {
  constructor({ fetchFn, getCfg, baseHref }) { this.fetchFn = fetchFn; this.getCfg = getCfg; this.baseHref = baseHref; }

  async _req(path, init, signal) {
    const cfg = this.getCfg();
    let res;
    try {
      res = await this.fetchFn(endpoint(path, this.baseHref), { ...init, signal, headers: { ...(init.headers || {}), Authorization: 'Bearer ' + cfg.token } });
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      throw new CloudError('offline', '无法连接在线朗读服务器（网络错误）。');
    }
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch { /* not JSON */ }
      const c = classifyStatus(res.status, body);
      throw new CloudError(c.kind, c.message, { status: res.status, retryAfter: c.retryAfter });
    }
    return res;
  }

  async speak(text, voice, signal) {
    const res = await this._req('speak', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text.slice(0, MAX_TEXT), voice, rate: 0 }) }, signal);
    const blob = await res.blob();
    if (!blob.size) throw new CloudError('upstream', '在线语音服务返回了空音频。');
    return blob;
  }
  async voices(signal) { return (await this._req('voices', { method: 'GET' }, signal)).json(); }
  async usage(signal) { return (await this._req('usage', { method: 'GET' }, signal)).json(); }
}

/**
 * Engine contract (shared with BrowserEngine in tts.js):
 *   speak(str, token, cfg) -> 'end' | 'cancel' | 'error' | 'nostart' | 'auth' | 'quota' | 'offline' | 'toolong'
 *   cancel(), pause(), resume(), prime(), prefetch(texts, cfg); `lastError` = the CloudError behind the last failure
 */
export class CloudEngine {
  constructor({ audio, api, urls = globalThis.URL, startTimeoutMs = 8000, online = () => globalThis.navigator ? navigator.onLine !== false : true }) {
    this.audio = audio; this.api = api; this.urls = urls; this.startTimeoutMs = startTimeoutMs; this.online = online;
    this.id = 'cloud';
    this.lastError = null;
    this._gen = 0; this._cur = null; this._paused = false; this._onResume = null; this._cfg = null;
    this.q = new PrefetchQueue((text, signal) => this.api.speak(text, pickCloudVoice(this._cfg.cloud.voices, text, this._cfg.cloud.voice), signal));
  }

  /** call synchronously from the Play tap: unlocks the element for autoplay on mobile browsers */
  prime() {
    try { this.audio.src = SILENT_WAV; const p = this.audio.play(); if (p && p.catch) p.catch(() => {}); } catch { /* ignore */ }
  }

  prefetch(texts, cfg) {
    this._cfg = cfg;
    this.q.want(texts.slice(0, 1 + PREFETCH_AHEAD).map((t) => ({ key: this._key(t), text: t })));
  }

  _key(t) { return pickCloudVoice(this._cfg.cloud.voices, t, this._cfg.cloud.voice) + '|' + t; }

  async speak(text, token, cfg) {
    this._cfg = cfg;
    const gen = this._gen;
    this.lastError = null;
    if (!this.online()) { this.lastError = new CloudError('offline', '当前离线，无法使用在线朗读。'); return 'offline'; }
    let blob;
    try { blob = await this.q.get(this._key(text), text); } catch (e) {
      if (gen !== this._gen || (e && e.name === 'AbortError')) return 'cancel';
      this.lastError = e instanceof CloudError ? e : new CloudError('offline', String(e && e.message || e));
      return { auth: 'auth', quota: 'quota', offline: 'offline', toolong: 'toolong' }[this.lastError.kind] || 'error';
    }
    if (gen !== this._gen) return 'cancel';
    if (this._paused) { await new Promise((r) => { this._onResume = r; }); if (gen !== this._gen) return 'cancel'; }
    return this._play(blob, clampPlaybackRate(cfg.ttsRate), gen);
  }

  _play(blob, rate, gen) {
    return new Promise((resolve) => {
      const a = this.audio;
      const url = this.urls.createObjectURL(blob);
      let timer = null;
      const arm = () => { clearTimeout(timer); if (this.startTimeoutMs > 0) timer = setTimeout(() => { try { a.pause(); } catch { /* ignore */ } done('nostart'); }, this.startTimeoutMs); };
      const done = (r) => {
        clearTimeout(timer);
        a.onended = a.onerror = a.onplaying = null;
        try { this.urls.revokeObjectURL(url); } catch { /* ignore */ }
        if (this._cur === cur) this._cur = null;
        resolve(r);
      };
      const cur = { done, arm, clear: () => clearTimeout(timer) };
      this._cur = cur;
      a.onended = () => done('end');
      a.onerror = () => done('error');
      a.onplaying = () => clearTimeout(timer);
      a.src = url;                       // setting src resets playbackRate to defaultPlaybackRate, so set both afterwards
      a.defaultPlaybackRate = a.playbackRate = rate;
      try { a.preservesPitch = true; } catch { /* ignore */ }
      arm();
      let p;
      try { p = a.play(); } catch { p = null; }
      if (p && p.catch) p.catch((e) => {
        if (e && e.name === 'NotAllowedError') done('nostart');
        else if (gen !== this._gen) done('cancel');   // AbortError from our own pause()/new src is ignored (resume plays again)
        else if (e && e.name !== 'AbortError') done('error');
      });
    });
  }

  cancel() {
    this._gen++;
    this.q.clear();
    this._paused = false;
    try { this.audio.pause(); } catch { /* ignore */ }
    if (this._onResume) { const r = this._onResume; this._onResume = null; r(); }
    if (this._cur) this._cur.done('cancel');
  }

  pause() {
    this._paused = true;
    if (this._cur) this._cur.clear();
    try { this.audio.pause(); } catch { /* ignore */ }
  }

  resume() {
    this._paused = false;
    if (this._onResume) { const r = this._onResume; this._onResume = null; r(); return; }
    if (this._cur) {
      this._cur.arm();
      try { const p = this.audio.play(); if (p && p.catch) p.catch(() => {}); } catch { /* ignore */ }
    }
  }
}
