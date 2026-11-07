// Online (cloud) read-aloud: pure logic, no DOM, no network. The browser glue is reader/cloudengine.js.
// Contract (server built separately): POST /tts/v1/speak {text<=200, voice, rate} -> audio/mpeg; GET /tts/v1/voices; GET /tts/v1/usage.
// Auth is a static per-user bearer token entered in settings (stored on this device only).

export const MAX_TEXT = 200;
export const PREFETCH_AHEAD = 3;
export const CLOUD_DEFAULTS = { enabled: false, consent: false, token: '', voice: '', voices: [] };

export const CONSENT_TEXT = '开启后，正在朗读的书籍文字（逐句）会发送到本应用的服务器，并由服务器转发给 Azure / edge-tts 合成语音。请勿朗读不希望外传的内容。默认关闭，仅在本设备生效。';

// shown when the list from /voices is unavailable (offline, first run)
export const FALLBACK_VOICES = [
  { id: 'zh-CN-XiaoxiaoNeural', name: '晓晓', lang: 'zh-CN', gender: 'Female' },
  { id: 'zh-CN-YunxiNeural', name: '云希', lang: 'zh-CN', gender: 'Male' },
  { id: 'zh-CN-XiaoyiNeural', name: '晓伊', lang: 'zh-CN', gender: 'Female' },
  { id: 'en-US-AriaNeural', name: 'Aria', lang: 'en-US', gender: 'Female' },
  { id: 'en-US-GuyNeural', name: 'Guy', lang: 'en-US', gender: 'Male' },
];

const str = (v, n) => (typeof v === 'string' ? v.slice(0, n) : '');

export function normalizeCloud(c) {
  const o = { ...CLOUD_DEFAULTS, ...(c || {}) };
  o.enabled = !!o.enabled; o.consent = !!o.consent;
  o.token = str(o.token, 512).trim(); o.voice = str(o.voice, 120);
  o.voices = Array.isArray(o.voices) ? o.voices.filter((v) => v && typeof v.id === 'string').slice(0, 40).map((v) => ({ id: str(v.id, 120), name: str(v.name, 80) || str(v.id, 120), lang: str(v.lang, 20), gender: str(v.gender, 12) })) : [];
  return o;
}

/** cloud reading may be used: switched on, consent given, token present */
export const cloudReady = (c) => !!c && c.enabled && c.consent && !!c.token;

/** absolute URL of a /tts/v1/ endpoint. The API lives next to the app directory (/reader/ + /tts/), same origin. */
export function endpoint(path, baseHref) { return new URL('../tts/v1/' + path, baseHref).href; }

export const hasCjk = (s) => /[㐀-鿿豈-﫿]/.test(s);
export const clampPlaybackRate = (r) => { const n = Number(r); return Number.isFinite(n) ? Math.min(2.5, Math.max(0.5, n)) : 1; };

function fmtWait(sec) {
  if (sec < 90) return `${Math.ceil(sec)} 秒`;
  if (sec < 5400) return `${Math.ceil(sec / 60)} 分钟`;
  return `${Math.ceil(sec / 3600)} 小时`;
}

/** Map an HTTP failure to {kind, message, retryAfter?}. `body` = parsed JSON or null. */
export function classifyStatus(status, body = null) {
  if (status === 401 || status === 403) return { kind: 'auth', message: '在线朗读令牌无效或已过期，请在 设置 → 在线朗读 中重新输入。' };
  if (status === 413) return { kind: 'toolong', message: '这一句太长，已跳过。' };
  if (status === 429) {
    const ra = body && Number(body.retryAfter);
    const ok = Number.isFinite(ra) && ra > 0;
    return { kind: 'quota', retryAfter: ok ? ra : null, message: '在线朗读额度已用完' + (ok ? `（约 ${fmtWait(ra)} 后恢复）` : '') + '。' };
  }
  if (status >= 500) return { kind: 'upstream', message: '在线语音服务暂时不可用（服务器或 Azure 出错）。' };
  return { kind: 'http', message: `在线朗读请求失败（HTTP ${status}）。` };
}

/** curated voice list: Chinese first, then English; at most 10 + 4. Entries without an id/lang are dropped. */
export function curateVoices(list) {
  const ok = (Array.isArray(list) ? list : []).filter((v) => v && v.id && typeof v.lang === 'string');
  const zh = ok.filter((v) => /^zh-(CN|HK|TW)/i.test(v.lang)).sort((a, b) => Number(/^zh-CN/i.test(b.lang)) - Number(/^zh-CN/i.test(a.lang)));
  const en = ok.filter((v) => /^en-(US|GB)/i.test(v.lang));
  return [...zh.slice(0, 10), ...en.slice(0, 4)];
}

/** voice id for `text`: the preferred voice when its language matches the text, else the first of that language */
export function pickCloudVoice(voices, text, pref = '') {
  const want = hasCjk(text) ? 'zh' : 'en';
  const list = voices && voices.length ? voices : FALLBACK_VOICES;
  const isLang = (v) => v.lang.slice(0, 2).toLowerCase() === want;
  const p = pref && list.find((v) => v.id === pref);
  if (p && isLang(p)) return p.id;
  const c = list.find(isLang) || FALLBACK_VOICES.find(isLang);
  return c ? c.id : FALLBACK_VOICES[0].id;
}

export function formatUsage(u) {
  if (!u || typeof u !== 'object') return '';
  const part = (used, lim, label) => (Number.isFinite(used) ? `${label} ${used}${Number.isFinite(lim) && lim > 0 ? ' / ' + lim : ''} 字` : '');
  const s = [part(u.usedToday, u.limitToday, '今日'), part(u.usedMonth, u.limitMonth, '本月')].filter(Boolean).join('；');
  return s + (u.backend ? `（${u.backend}）` : '');
}

/** non-blocking suggestion to try cloud reading; null when none is due. kind: 'novoices' | 'nostart' */
export function cloudHint({ kind, cloudOn, alreadyShown }) {
  if (cloudOn || alreadyShown) return null;
  if (kind === 'novoices') return '此浏览器没有可用的朗读语音。可在 书架 → 设置 → 在线朗读 中启用在线语音（需联网和令牌）。';
  if (kind === 'nostart') return '也可以在 书架 → 设置 → 在线朗读 中启用在线语音。';
  return null;
}

/**
 * Look-ahead window of synthesized sentences. `fetchBlob(text, signal)` does the network call. `want(items)` starts what is
 * missing and aborts whatever fell out of the window, so seeking or stopping never burns quota on unused sentences.
 */
export class PrefetchQueue {
  constructor(fetchBlob) { this.fetchBlob = fetchBlob; this.map = new Map(); }

  _start(key, text) {
    const ctrl = new AbortController();
    const entry = { ctrl };
    entry.p = this.fetchBlob(text, ctrl.signal).catch((e) => { if (this.map.get(key) === entry) this.map.delete(key); throw e; });
    entry.p.catch(() => {});   // a failed look-ahead is retried when it becomes the current sentence
    this.map.set(key, entry);
    return entry;
  }

  /** promise of the blob for `key` (starts the fetch when not already running) */
  get(key, text) { return (this.map.get(key) || this._start(key, text)).p; }

  /** items = [{key, text}] in reading order; the first is the current sentence */
  want(items) {
    const keep = new Set(items.map((x) => x.key));
    for (const [k, e] of this.map) if (!keep.has(k)) { e.ctrl.abort(); this.map.delete(k); }
    for (const x of items) if (!this.map.has(x.key)) this._start(x.key, x.text);
  }

  clear() { for (const e of this.map.values()) e.ctrl.abort(); this.map.clear(); }
  get size() { return this.map.size; }
}
