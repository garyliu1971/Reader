// Read-aloud on top of the browser's speechSynthesis. The engine speaks ONE sentence per utterance (long utterances
// stall in several browsers), highlights it and turns pages through the `host`, and continues into the next chapter.
// Everything browser-specific is injected (synth, Utter, cloud engine), so the loop is unit-tested with fakes.
// Two engines behind one contract: BrowserEngine (speechSynthesis, default) and CloudEngine (reader/cloudengine.js, opt-in).
import { cloudReady, cloudHint, PREFETCH_AHEAD } from '../core/cloudtts.js';

const END_MARKS = new Set('。！？!?；;…');
const CLOSERS = new Set('”’」』）)"\'】》>〉]');
const SOFT_BREAKS = new Set('，、,：:；; 　');
const MAX_CHUNK = 160;

/**
 * Split `text` into speakable sentences. Returns [{a, b}] with absolute offsets (a inclusive, b exclusive).
 * `breaks` = offsets where a new block (paragraph/heading) starts: a sentence never crosses one.
 */
export function splitSentences(text, breaks = null) {
  const out = [];
  const isBreak = breaks ? (i) => breaks.has(i) : () => false;
  const push = (a, b) => {
    while (a < b && /\s/.test(text[a])) a++;
    while (b > a && /\s/.test(text[b - 1])) b--;
    if (b <= a || !/[\p{L}\p{N}]/u.test(text.slice(a, b))) return;   // nothing to say (only punctuation/space)
    // very long runs without terminators: cut at a soft break (comma, space) so every utterance stays short
    while (b - a > MAX_CHUNK) {
      let cut = -1;
      for (let i = a + MAX_CHUNK; i > a + MAX_CHUNK / 2; i--) if (SOFT_BREAKS.has(text[i - 1])) { cut = i; break; }
      if (cut < 0) cut = a + MAX_CHUNK;
      out.push({ a, b: cut });
      a = cut;
      while (a < b && /\s/.test(text[a])) a++;
    }
    if (b > a) out.push({ a, b });
  };
  let start = 0;
  const n = text.length;
  for (let i = 0; i < n; i++) {
    if (i > start && isBreak(i)) { push(start, i); start = i; }
    const c = text[i];
    if (c === '\n') { push(start, i); start = i + 1; continue; }
    let end = false;
    if (END_MARKS.has(c)) end = true;
    else if (c === '.' && (i + 1 >= n || /\s/.test(text[i + 1])) && !/\d/.test(text[i - 1] || '')) end = true;
    if (!end) continue;
    let j = i + 1;
    while (j < n && (END_MARKS.has(text[j]) || CLOSERS.has(text[j]) || text[j] === '.')) j++;
    push(start, j);
    start = j;
    i = j - 1;
  }
  push(start, n);
  return out;
}

const hasCjk = (s) => /[㐀-鿿豈-﫿]/.test(s);

export function pickVoice(voices, lang, uri = '') {
  if (!voices || !voices.length) return null;
  const pre = lang.slice(0, 2).toLowerCase();
  if (uri) {
    const v = voices.find((x) => x.voiceURI === uri);
    if (v && v.lang.slice(0, 2).toLowerCase() === pre) return v;
  }
  const cands = voices.filter((x) => x.lang.toLowerCase().replace('_', '-').startsWith(pre));
  if (!cands.length) return null;
  const score = (v) => (/natural|neural|online/i.test(v.name) ? 4 : 0) + (v.lang.toLowerCase().replace('_', '-') === lang.toLowerCase() ? 2 : 0) + (v.localService ? 0 : 1);
  return [...cands].sort((p, q) => score(q) - score(p))[0];
}

/** speechSynthesis engine. speak(str, live, cfg) -> 'end' | 'cancel' | 'error' | 'nostart' (live() false = superseded) */
export class BrowserEngine {
  constructor({ synth, Utter, startTimeoutMs = 6000 }) { this.id = 'browser'; this.synth = synth; this.Utter = Utter; this.startTimeoutMs = startTimeoutMs; }
  available() { return !!(this.synth && this.Utter); }
  voiceCount() { try { return (this.synth.getVoices() || []).length; } catch { return 0; } }
  prime() {}
  prefetch() {}
  cancel() { try { this.synth.cancel(); } catch { /* ignore */ } }
  pause() { this.synth.pause(); }
  resume() { this.synth.resume(); }

  speak(str, live, cfg) {
    return new Promise((resolve) => {
      const u = new this.Utter(str);
      const lang = hasCjk(str) ? 'zh-CN' : 'en-US';
      let voices = [];
      try { voices = this.synth.getVoices(); } catch { /* ignore */ }
      const v = pickVoice(voices, lang, cfg.ttsVoice);
      u.lang = v ? v.lang : lang;
      if (v) u.voice = v;
      u.rate = cfg.ttsRate || 1;
      let timer = null;
      const done = (r) => { clearTimeout(timer); resolve(r); };
      u.onstart = () => clearTimeout(timer);
      u.onend = () => done('end');
      u.onerror = (e) => done(e && (e.error === 'canceled' || e.error === 'interrupted') ? 'cancel' : 'error');
      if (!live()) { resolve('cancel'); return; }
      if (this.startTimeoutMs > 0) timer = setTimeout(() => {
        this.cancel();
        resolve('nostart');
      }, this.startTimeoutMs);
      this.synth.speak(u);
    });
  }
}

export class TtsReader {
  /**
   * @param synth  window.speechSynthesis (may be absent when only the cloud engine exists)
   * @param Utter  SpeechSynthesisUtterance constructor
   * @param cloud  optional CloudEngine; used when host.settings().cloud is ready (enabled + consent + token)
   * @param host   { text(): string, breaks(): Set<number>|null, reveal(a,b), nextSection(): Promise<boolean>,
   *                 changed(state), finished(), failed(msg), settings(): {ttsRate, ttsVoice, cloud?},
   *                 notice?(msg)  non-blocking message (fallback / suggestion); defaults to failed }
   */
  constructor({ synth, Utter, host, cloud = null, startTimeoutMs = 6000 }) {
    this.synth = synth;
    this.Utter = Utter;
    this.host = host;
    this.browser = new BrowserEngine({ synth, Utter, startTimeoutMs });
    this.cloud = cloud;
    this.engine = this.browser;
    this.state = 'idle';      // idle | playing | paused
    this.token = 0;
    this._hinted = false;
  }

  get startTimeoutMs() { return this.browser.startTimeoutMs; }
  set startTimeoutMs(v) { this.browser.startTimeoutMs = v; }

  static supported(w = globalThis) { return !!(w.speechSynthesis && w.SpeechSynthesisUtterance); }

  _set(state) { if (this.state !== state) { this.state = state; this.host.changed(state); } }
  _notice(msg) { (this.host.notice || this.host.failed).call(this.host, msg); }
  _cloudOn() { const c = (this.host.settings() || {}).cloud; return !!(this.cloud && cloudReady(c)); }
  _cancelEngines() { this.browser.cancel(); if (this.cloud) this.cloud.cancel(); }

  /** (re)start reading from the sentence that contains/follows `fromOffset` of the current section */
  start(fromOffset = 0) {
    const wasIdle = this.state === 'idle';
    const token = ++this.token;
    this._cancelEngines();
    this.engine = this._cloudOn() ? this.cloud : this.browser;
    // a cloud <audio> can only be started by a user gesture: this runs inside the Play tap, so unlock it now (once per session)
    if (wasIdle) this.engine.prime();
    if (this.engine === this.browser) {
      if (!this.browser.available()) { this._set('idle'); this.host.failed('此浏览器不支持朗读。可在 书架 → 设置 → 在线朗读 中启用在线语音。'); return; }
      if (this.browser.voiceCount() === 0) this._hint('novoices');
    }
    this._set('playing');
    this._run(token, fromOffset).catch((e) => { if (token === this.token) { this.stop(); this.host.failed(e && e.message ? e.message : String(e)); } });
  }

  restart(fromOffset) { if (this.state !== 'idle') this.start(fromOffset); }

  stop() {
    this.token++;
    this._cancelEngines();
    this._set('idle');
  }

  pause() { if (this.state === 'playing') { this.engine.pause(); this._set('paused'); } }
  resume() { if (this.state === 'paused') { this.engine.resume(); this._set('playing'); } }

  _hint(kind) {
    const m = cloudHint({ kind, cloudOn: this._cloudOn() || !this.cloud, alreadyShown: this._hinted });
    if (m) { this._hinted = true; this._notice(m); }
  }

  /** cloud failed for this session: carry on with the browser voice from the same sentence (false = nothing to fall back to) */
  _fallback(msg) {
    if (!this.browser.available()) return false;
    this.engine = this.browser;
    this._notice(msg);
    return true;
  }

  async _run(token, fromOffset) {
    let text = this.host.text();
    let sents = splitSentences(text, this.host.breaks());
    let i = sents.findIndex((x) => x.b > fromOffset);
    let failures = 0;
    for (;;) {
      if (token !== this.token) return;
      if (i < 0 || i >= sents.length) {
        const more = await this.host.nextSection();
        if (token !== this.token) return;
        if (!more) { this.stop(); this.host.finished(); return; }
        text = this.host.text();
        sents = splitSentences(text, this.host.breaks());
        i = 0;
        continue;
      }
      const { a, b } = sents[i];
      this.host.reveal(a, b);
      const cfg = this.host.settings() || {};
      if (this.engine !== this.browser) this.engine.prefetch(sents.slice(i, i + 1 + PREFETCH_AHEAD).map((x) => text.slice(x.a, x.b)), cfg);
      const r = await this.engine.speak(text.slice(a, b), () => token === this.token, cfg);
      if (token !== this.token) return;
      const cloud = this.engine !== this.browser;
      const why = cloud && this.cloud.lastError ? this.cloud.lastError.message : '';
      if (r === 'toolong') { i++; continue; }
      if (r === 'auth') { this.stop(); this.host.failed(why); return; }
      if (cloud && (r === 'quota' || r === 'offline')) {
        if (!this._fallback(r === 'offline' ? '网络不可用，已切换回浏览器朗读。' : why + ' 已切换回浏览器朗读。')) { this.stop(); this.host.failed(why); return; }
        failures = 0;
        continue;
      }
      if (r === 'nostart') {
        if (cloud) {
          const m = '在线朗读无法播放音频（浏览器可能阻止了自动播放，或网络太慢）。';
          if (this._fallback(m + '已切换回浏览器朗读。')) { failures = 0; continue; }
          this.stop(); this.host.failed(m); return;
        }
        // speak() was accepted but the engine never began: e.g. Android Edge (speechSynthesis unusable) or a device without a TTS engine
        this.stop();
        this.host.failed('朗读没有声音输出：此浏览器的朗读功能不可用（例如手机版 Edge）。请换用系统自带浏览器（如华为浏览器）或电脑浏览器；若仍无声，请检查系统“文字转语音”引擎。');
        this._hint('nostart');
        return;
      }
      if (r === 'error') {
        if (++failures >= 3) {
          if (cloud && this._fallback((why || '在线朗读连续失败。') + ' 已切换回浏览器朗读。')) { failures = 0; continue; }
          this.stop(); this.host.failed(cloud ? (why || '在线朗读失败。') : '朗读失败：浏览器没有可用的语音，或语音服务无响应。'); return;
        }
      } else failures = 0;
      i++;
    }
  }
}
