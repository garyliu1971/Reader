// Translate selected text with the browser's on-device Translator API (Chrome / Edge, desktop). Nothing is sent to a
// server by this app. Everything browser-specific is injected, so the logic is unit-tested with fakes.

export const MAX_CHARS = 3000;
const DETECT_MS = 2500;   // a slow language detector must never hold up the translator (see translate())

function withTimeout(p, ms) {
  let t;
  return Promise.race([p, new Promise((_, rej) => { t = setTimeout(() => rej(new Error('timeout')), ms); })]).finally(() => clearTimeout(t));
}

const KANA = /[぀-ヿ]/;
const HANGUL = /[가-힯]/;
const HAN = /[㐀-鿿豈-﫿]/g;

/** quick script-based guess: 'ja' | 'ko' | 'zh' | null (Latin/other: ask the detector) */
export function guessScript(text) {
  if (KANA.test(text)) return 'ja';
  if (HANGUL.test(text)) return 'ko';
  const han = (text.match(HAN) || []).length;
  const letters = (text.match(/\p{L}/gu) || []).length;
  if (han && han >= letters * 0.3) return 'zh';
  return null;
}

/** 'zh-Hans' / 'zh-TW' -> 'zh'; 'en-US' -> 'en' */
export const baseLang = (tag) => String(tag || '').toLowerCase().split(/[-_]/)[0];

/** never translate a language into itself: Chinese text goes to English, everything else to the target */
export function chooseDirection(source, target) {
  const s = baseLang(source) || 'en';
  const t = baseLang(target) || 'zh';
  return s === t ? { source: s, target: t === 'zh' ? 'en' : 'zh' } : { source: s, target: t };
}

export class TranslateError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

export class Translate {
  /** @param T self.Translator  @param LD self.LanguageDetector (optional) */
  constructor({ T, LD }) {
    this.T = T;
    this.LD = LD || null;
    this.cache = new Map();   // "en>zh" -> translator (creating one is slow; it may download a model)
    this.detector = null;
  }

  static supported(w = globalThis) { return typeof w.Translator === 'function' || (!!w.Translator && typeof w.Translator.create === 'function'); }

  /**
   * Source language of `text`. Browsers only let Translator.create() start a model download while the user's click is
   * still fresh (~5 s), so nothing here may wait on a download: a detector that still has to be downloaded is
   * warmed up in the background and 'en' is assumed for this one translation.
   */
  async detect(text) {
    const g = guessScript(text);
    if (g) return g;
    if (this.LD) {
      try {
        if (!this.detector) {
          const av = typeof this.LD.availability === 'function' ? await this.LD.availability() : 'available';
          if (av === 'available') this.detector = await withTimeout(this.LD.create(), DETECT_MS);
          else {
            if ((av === 'downloadable' || av === 'downloading') && !this._warming) {
              this._warming = this.LD.create().then((d) => { this.detector = d; }).catch(() => { this._warming = null; });
            }
            return 'en';
          }
        }
        const r = await withTimeout(this.detector.detect(text.slice(0, 400)), DETECT_MS);
        if (r && r[0] && r[0].detectedLanguage && r[0].detectedLanguage !== 'und') return baseLang(r[0].detectedLanguage);
      } catch { /* fall through */ }
    }
    return 'en';
  }

  /**
   * @returns {Promise<{text, source, target}>}
   * @throws {TranslateError} code: too-long | empty | unsupported | failed
   */
  async translate(text, { target = 'zh', onProgress = null } = {}) {
    // collapse runs of spaces but keep paragraph breaks: they are part of the meaning
    const clean = String(text || '').replace(/\r\n?/g, '\n').split('\n').map((l) => l.replace(/[ \t\u00a0\u3000]+/g, ' ').trim()).filter(Boolean).join('\n');
    if (!clean) throw new TranslateError('empty', '没有选中文字');
    if (clean.length > MAX_CHARS) throw new TranslateError('too-long', `选中的文字太长（${clean.length} 字，最多 ${MAX_CHARS} 字），请分段选择`);
    const { source, target: tgt } = chooseDirection(await this.detect(clean), target);
    const key = `${source}>${tgt}`;
    let tr = this.cache.get(key);
    if (!tr) {
      let av;
      try { av = await this.T.availability({ sourceLanguage: source, targetLanguage: tgt }); } catch { av = 'unavailable'; }
      if (av === 'unavailable') throw new TranslateError('unsupported', `此浏览器暂不支持 ${source} → ${tgt} 的内置翻译`);
      try {
        tr = await this.T.create({
          sourceLanguage: source, targetLanguage: tgt,
          monitor: onProgress ? (m) => m.addEventListener('downloadprogress', (e) => onProgress(Math.round((e.loaded || 0) * 100))) : undefined,
        });
      } catch (e) {
        if (e && e.name === 'NotAllowedError') throw new TranslateError('activation', '浏览器只允许在你刚点击之后下载语言模型，请再点一次“翻译”。');
        throw new TranslateError('failed', `翻译器启动失败：${e && e.message ? e.message : e}`);
      }
      this.cache.set(key, tr);
    }
    try {
      return { text: await tr.translate(clean), source, target: tgt };
    } catch (e) {
      this.cache.delete(key);
      throw new TranslateError('failed', `翻译失败：${e && e.message ? e.message : e}`);
    }
  }
}
