// Read-aloud on top of the browser's speechSynthesis. The engine speaks ONE sentence per utterance (long utterances
// stall in several browsers), highlights it and turns pages through the `host`, and continues into the next chapter.
// Everything browser-specific is injected (synth, Utter), so the loop is unit-tested with a fake synthesiser.

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

export class TtsReader {
  /**
   * @param synth  window.speechSynthesis
   * @param Utter  SpeechSynthesisUtterance constructor
   * @param host   { text(): string, breaks(): Set<number>|null, reveal(a,b), nextSection(): Promise<boolean>,
   *                 changed(state), finished(), failed(msg), settings(): {ttsRate, ttsVoice} }
   */
  constructor({ synth, Utter, host, startTimeoutMs = 6000 }) {
    this.startTimeoutMs = startTimeoutMs;
    this.synth = synth;
    this.Utter = Utter;
    this.host = host;
    this.state = 'idle';      // idle | playing | paused
    this.token = 0;
  }

  static supported(w = globalThis) { return !!(w.speechSynthesis && w.SpeechSynthesisUtterance); }

  _set(state) { if (this.state !== state) { this.state = state; this.host.changed(state); } }

  /** (re)start reading from the sentence that contains/follows `fromOffset` of the current section */
  start(fromOffset = 0) {
    const token = ++this.token;
    try { this.synth.cancel(); } catch { /* ignore */ }
    this._set('playing');
    this._run(token, fromOffset).catch((e) => { if (token === this.token) { this.stop(); this.host.failed(e && e.message ? e.message : String(e)); } });
  }

  restart(fromOffset) { if (this.state !== 'idle') this.start(fromOffset); }

  stop() {
    this.token++;
    try { this.synth.cancel(); } catch { /* ignore */ }
    this._set('idle');
  }

  pause() { if (this.state === 'playing') { this.synth.pause(); this._set('paused'); } }
  resume() { if (this.state === 'paused') { this.synth.resume(); this._set('playing'); } }

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
      const r = await this._say(text.slice(a, b), token);
      if (token !== this.token) return;
      if (r === 'nostart') {
        // speak() was accepted but the engine never began: e.g. Android Edge (speechSynthesis unusable) or a device without a TTS engine
        this.stop();
        this.host.failed('朗读没有声音输出：此浏览器的朗读功能不可用（例如手机版 Edge）。请换用系统自带浏览器（如华为浏览器）或电脑浏览器；若仍无声，请检查系统“文字转语音”引擎。');
        return;
      }
      if (r === 'error') {
        if (++failures >= 3) { this.stop(); this.host.failed('朗读失败：浏览器没有可用的语音，或语音服务无响应。'); return; }
      } else failures = 0;
      i++;
    }
  }

  _say(str, token) {
    return new Promise((resolve) => {
      const u = new this.Utter(str);
      const cfg = this.host.settings();
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
      if (token !== this.token) { resolve('cancel'); return; }
      if (this.startTimeoutMs > 0) timer = setTimeout(() => {
        try { this.synth.cancel(); } catch { /* ignore */ }
        resolve('nostart');
      }, this.startTimeoutMs);
      this.synth.speak(u);
    });
  }
}
