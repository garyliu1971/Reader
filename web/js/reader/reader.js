// Reader view: toolbar, drawers (TOC / bookmarks / settings), search bar (find.js), gestures, progress saving.
// The text layout itself lives in paginator.js; positions are {s, o, f, c} anchors (see core/anchor.js).
import { h, openPanel, toast, debounce } from '../ui/dom.js';
import { icon } from '../ui/icons.js';
import { Paginator } from './paginator.js';
import { TtsReader, pickVoice } from './tts.js';
import { CloudEngine, CloudApi } from './cloudengine.js';
import { getCloudCfg, loadCloudCfg } from '../store/cloudcfg.js';
import { Translate } from './translate.js';
import { FindBar } from './find.js';
import { kvGet, kvSet } from '../store/db.js';
import { openStored, touchRead, getProgress, saveProgress, getBookmarks, addBookmark, deleteBookmark } from '../library.js';
import { positionToPercent, percentToPosition, makeAnchor, resolveOffset } from '../core/anchor.js';
import { stampNow } from '../core/progress.js';
import { aliasIds, localBookId } from '../core/bookid.js';
import { normalizeSelection } from '../parse/pdftext.js';
import { DWELL_MS } from '../core/recent.js';
import { navDir } from '../core/search.js';
import { makeTimer, isDue, timerLabel, activeChoice, SLEEP_MINUTES } from '../core/sleeptimer.js';
import { loadSettings, saveSettings, normalize, applyTheme, applyTypography, LIMITS } from '../store/settings.js';

const TAP_ZONE = 0.3;
const DBLCLICK_MS = 230;   // a mouse tap waits this long so a double-click (select a word) can cancel the page turn
const LANG_NAME = { en: '英语', zh: '中文', ja: '日语', ko: '韩语', fr: '法语', de: '德语', es: '西班牙语', ru: '俄语', it: '意大利语', pt: '葡萄牙语' };
const THEMES = [['auto', '跟随系统'], ['light', '明亮'], ['dark', '夜间'], ['sepia', '护眼黄'], ['eye', '护眼绿']];
const FONTS = [['serif', '宋体'], ['sans', '黑体'], ['kai', '楷体'], ['system', '系统']];

const isDarkNow = () => {
  const t = document.documentElement.getAttribute('data-theme');
  return t === 'dark' || (!t && matchMedia('(prefers-color-scheme: dark)').matches);
};

export class ReaderView {
  /**
   * @param root container element
   * @param ctx {goShelf(), onProgress(info), fetchRemoteProgress(bid), now()}
   */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.meta = null;
    this.book = null;
    this.s = 0;
    this.seq = 0;
    this.seq2 = 0;
    this.quiet = false;
    this.active = false;
    this.dirty = false;
    this.settings = null;
    this.lastSaved = null;
    this.searchCache = new Map();
    this.build();
    this.saveSoon = debounce(() => this.saveNow(), 700);
  }

  // ------------------------------------------------------------------ DOM
  build() {
    const r = this.root;
    r.replaceChildren();
    this.titleEl = h('div', { class: 'title' });
    this.btnToc = h('button', { class: 'icon-btn labelled', type: 'button', 'aria-label': '目录和书签', onclick: () => this.openToc('toc') }, icon('menu'), h('span', { class: 'lbl', 'aria-hidden': 'true' }, '目录'));
    this.btnTts = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '朗读', 'aria-pressed': 'false', onclick: () => this.toggleTts() }, icon('speaker'));
    this.btnSearch = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '搜索全文', title: '搜索全文（Ctrl+F 或 /）', onclick: () => this.openSearch() }, icon('search'));
    this.btnMark = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '添加书签', 'aria-pressed': 'false', onclick: () => this.toggleBookmark() }, icon('bookmark'));
    this.btnSet = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '阅读设置', onclick: () => this.openSettings() }, icon('font'));
    this.topbar = h('div', { class: 'topbar', role: 'toolbar', 'aria-label': '阅读工具栏' },
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '返回书架', onclick: () => this.ctx.goShelf() }, icon('back')),
      this.titleEl, this.btnToc, this.btnSearch, this.btnTts, this.btnMark, this.btnSet);
    this.content = h('div', { class: 'content', lang: 'zh-CN' });
    this.scrollNav = h('div', { class: 'scroll-nav row' },
      h('button', { class: 'btn', type: 'button', onclick: () => this.chapter(-1) }, '上一章'),
      h('button', { class: 'btn', type: 'button', onclick: () => this.chapter(1) }, '下一章'));
    this.frame = h('div', { class: 'frame', tabIndex: -1, 'aria-label': '正文' }, this.content, this.scrollNav);
    this.stage = h('div', { class: 'stage' }, this.frame);
    this.chEl = h('span', { class: 'ch' });
    this.posEl = h('span', { class: 'pos' });
    this.status = h('div', { class: 'statusline', 'aria-hidden': 'true' }, this.chEl, this.posEl);
    this.slider = h('input', { type: 'range', min: '0', max: '1000', step: '1', value: '0', 'aria-label': '跳转到阅读百分比' });
    this.pctLabel = h('span', { class: 'pctlabel', 'aria-live': 'off' }, '0%');
    this.bottom = h('div', { class: 'bottombar', role: 'toolbar', 'aria-label': '阅读进度' }, h('div', { class: 'line' },
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '上一章', onclick: () => this.chapter(-1) }, icon('prev')),
      this.slider, this.pctLabel,
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '下一章', onclick: () => this.chapter(1) }, icon('next'))));
    this.ttsState = h('span', { class: 'state', 'aria-live': 'polite' }, '朗读中');
    this.ttsPlay = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '暂停朗读', onclick: () => this.ttsPauseResume() }, icon('pause'));
    this.ttsRateEl = h('span', { class: 'rate' }, '1.0x');
    this.sleep = null;       // sleep timer (core/sleeptimer.js); only lives while reading aloud
    this.sleepTick = 0;
    this.ttsSleep = h('button', { class: 'sleep-btn', type: 'button', 'aria-label': '睡眠定时', onclick: () => this.openSleepMenu() }, '定时');
    this.ttsBar = h('div', { class: 'tts-bar', role: 'toolbar', 'aria-label': '朗读控制' },
      this.ttsState,
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '减慢语速', onclick: () => this.ttsStep(-0.1) }, '−'),
      this.ttsRateEl,
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '加快语速', onclick: () => this.ttsStep(0.1) }, '+'),
      this.ttsPlay,
      this.ttsSleep,
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '停止朗读', onclick: () => this.tts && this.tts.stop() }, icon('stop')));
    this.loading = h('div', { class: 'loading', role: 'status' }, '正在打开…');
    this.contentS = -1;   // section whose text is laid out in the paginator (this.s is already the new one while it loads)
    this.find = new FindBar({
      count: () => (this.book ? this.book.sections.length : 0),
      text: (i) => this.sectionText(i),
      title: (i) => (this.book && this.book.sections[i] && this.book.sections[i].title) || '',
      start: () => ({ s: this.s, o: this.s === this.contentS ? this.pg.pageRange()[0] : 0 }),   // this.s is ahead of the laid-out text while a section loads
      here: () => this.pg.currentOffset(),
      visible: (s, o) => { if (s !== this.contentS) return false; const [a, b] = this.pg.pageRange(); return o >= a && o < Math.max(b, a + 1); },
      section: () => this.contentS,
      goto: (s, o) => this.gotoTarget(s, { offset: o }),
      range: (a, b) => this.pg.rangeFor(a, b),
      onToggle: (on) => {
        this.root.classList.toggle('finding', on);
        if (on) { this.setBars(false); this.hideTr(); } else if (this.pg.mode === 'paged') this.frame.focus({ preventScroll: true });
      },
    });
    r.append(this.stage, this.status, this.topbar, this.bottom, this.ttsBar, this.find.el, this.loading);
    // select text -> small 翻译 button -> popover with the on-device translation (Chrome/Edge Translator API)
    this.tr = Translate.supported() ? new Translate({ T: globalThis.Translator, LD: globalThis.LanguageDetector }) : null;
    this.trSeq = 0;
    this.trSel = null;
    this.trLast = '';
    this.trBtn = h('button', { class: 'tr-btn', type: 'button', hidden: true, 'aria-label': '翻译选中的文字', onmousedown: (e) => e.preventDefault(), onclick: () => this.runTranslate() }, '翻译');
    this.trHead = h('div', { class: 'tr-head' });
    this.trBody = h('div', { class: 'tr-body', role: 'status', 'aria-live': 'polite' });
    this.trPop = h('div', { class: 'tr-pop', hidden: true, role: 'region', 'aria-label': '译文', onmousedown: (e) => { if (!e.target.closest('.tr-body')) e.preventDefault(); } },
      this.trHead, this.trBody,
      h('div', { class: 'tr-actions' },
        h('button', { class: 'btn small', type: 'button', onclick: () => this.copyTranslation() }, '复制译文'),
        h('button', { class: 'btn small', type: 'button', onclick: () => this.hideTr() }, '关闭')));
    r.append(this.trBtn, this.trPop);
    this._ttsMoving = 0;
    this.tts = null;
    // online voice engine: opt-in per device (settings), needs <audio>; the browser engine stays the default
    let cloud = null;
    if (typeof globalThis.Audio === 'function' && typeof globalThis.fetch === 'function') {
      cloud = new CloudEngine({ audio: new Audio(), api: new CloudApi({ fetchFn: (...a) => fetch(...a), getCfg: getCloudCfg, baseHref: location.href }) });
    }
    if (TtsReader.supported() || cloud) {
      this.tts = new TtsReader({
        synth: globalThis.speechSynthesis, Utter: globalThis.SpeechSynthesisUtterance, cloud,
        host: {
          text: () => this.pg.fullText(),
          breaks: () => this.pg.breaks,
          reveal: (a, b) => this.ttsReveal(a, b),
          nextSection: () => this.ttsNextSection(),
          changed: (st) => this.ttsChanged(st),
          finished: () => { if (!this.sleepFired) toast('朗读完毕', { ms: 1800 }); this.sleepFired = false; },
          failed: (m) => { if (m) toast(m, { ms: 6000 }); },
          notice: (m) => { if (m) toast(m, { ms: 7000 }); },
          settings: () => ({ ...(this.settings || {}), cloud: getCloudCfg() }),
        },
      });
    } else this.btnTts.hidden = true;

    this.pg = new Paginator(this.frame, this.content);
    this.pg.onChange = (reason) => this.onPaginator(reason);
    this.slider.addEventListener('input', () => { this.pctLabel.textContent = (this.slider.value / 10).toFixed(1) + '%'; });
    this.slider.addEventListener('change', () => this.seekPercent(this.slider.value / 10));
    this.bindInput();
  }

  // ------------------------------------------------------------------ input
  bindInput() {
    const f = this.frame;
    const ptrs = new Map();
    let down = null;
    let pinch = null;
    f.addEventListener('pointerdown', (e) => {
      if (this._tapT) { clearTimeout(this._tapT); this._tapT = 0; this._tapPrev = this._tapAct; }   // second half of a double-click?
      ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (ptrs.size === 2) {
        const [a, b] = [...ptrs.values()];
        pinch = { d0: Math.hypot(a.x - b.x, a.y - b.y), size0: this.settings.fontSize, ratio: 1 };
        down = null;
      } else if (ptrs.size === 1) down = { x: e.clientX, y: e.clientY, t: performance.now(), type: e.pointerType, target: e.target };
    });
    f.addEventListener('pointermove', (e) => {
      if (!ptrs.has(e.pointerId)) return;
      ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && ptrs.size >= 2) {
        const [a, b] = [...ptrs.values()];
        pinch.ratio = Math.hypot(a.x - b.x, a.y - b.y) / (pinch.d0 || 1);
      }
    });
    const end = (e) => {
      if (!ptrs.has(e.pointerId)) return;
      ptrs.delete(e.pointerId);
      if (pinch) {
        if (ptrs.size === 0) {   // apply once when the gesture ends: live re-layout during a pinch would thrash
          if (Math.abs(pinch.ratio - 1) > 0.06) this.setSetting('fontSize', Math.round(pinch.size0 * pinch.ratio));
          pinch = null;
        }
        return;
      }
      if (!down || e.type === 'pointercancel') { down = null; return; }
      const d = down;
      down = null;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      const sel = window.getSelection && window.getSelection().toString();
      if (sel) { this._tapPrev = null; return; }   // text is being selected/copied: never treat it as a tap
      if (d.target && d.target.closest && d.target.closest('a')) return;
      if (d.type !== 'mouse' && Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.5 && this.pg.mode === 'paged') {
        dx < 0 ? this.next() : this.prev();   // swipe (touch/pen only: a mouse drag is a text selection)
        return;
      }
      if (Math.abs(dx) > 10 || Math.abs(dy) > 10) return;
      const rect = f.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      if (this._tapPrev) { const p = this._tapPrev; this._tapPrev = null; p(); }   // double-click that selected nothing: both clicks count
      const act = () => {
        this._tapT = 0;
        if (this.barsOn()) { this.setBars(false); return; }
        if (x < TAP_ZONE) this.prev();
        else if (x > 1 - TAP_ZONE) this.next();
        else this.setBars(true);
      };
      // a mouse click may be the first half of a double-click: wait a moment so the second click can cancel the turn.
      // Touch and pen act at once.
      if (d.type === 'mouse') { this._tapAct = act; this._tapT = setTimeout(act, DBLCLICK_MS); } else act();
    };
    f.addEventListener('pointerup', end);
    f.addEventListener('pointercancel', end);
    f.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[data-href]');
      if (a) { e.preventDefault(); this.followLink(a.getAttribute('data-href')); }
    });
    let wheelAt = 0;
    f.addEventListener('wheel', (e) => {
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); this.setSetting('fontSize', this.settings.fontSize + (e.deltaY < 0 ? 1 : -1)); return; }
      if (this.pg.mode !== 'paged' && !this.pdf) return;
      if (Math.abs(e.deltaY) < 4 && Math.abs(e.deltaX) < 4) return;
      e.preventDefault();
      const now = performance.now();
      if (now - wheelAt < 280) return;
      wheelAt = now;
      (e.deltaY + e.deltaX > 0) ? this.next() : this.prev();
    }, { passive: false });
    f.addEventListener('scroll', () => { if (this.pg.mode === 'scroll') this.onScroll(); }, { passive: true });

    this.keyHandler = (e) => {
      if (!this.active) return;
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      if (t && t.closest && t.closest('.tr-pop, .tr-btn, .findbar') && (e.key === ' ' || e.key === 'Enter')) return;   // activate the focused button, do not turn the page
      const mod = e.ctrlKey || e.metaKey;
      if (mod && ['+', '=', '-', '_', '0'].includes(e.key)) {
        e.preventDefault();
        if (e.key === '0') this.setSetting('fontSize', 19);
        else this.setSetting('fontSize', this.settings.fontSize + (e.key === '-' || e.key === '_' ? -1 : 1));
        return;
      }
      const overlay = document.querySelector('.overlay-root:not(.side-sheet)');
      if (mod && !e.altKey && !e.shiftKey && (e.key === 'f' || e.key === 'F') && !overlay && this.book && !this.pdf) { e.preventDefault(); this.openSearch(); return; }   // a PDF has no text: leave the browser's own find alone
      const nav = !overlay && this.find.isOpen ? navDir(e) : 0;   // F3 / Ctrl+G: next hit, Shift = previous
      if (nav) { e.preventDefault(); this.find.step(nav); return; }
      if (overlay || mod || e.altKey) return;
      switch (e.key) {
        case 'ArrowRight': case 'ArrowDown': case 'PageDown': this.next(); break;
        case 'ArrowLeft': case 'ArrowUp': case 'PageUp': this.prev(); break;
        case ' ': e.shiftKey ? this.prev() : this.next(); break;
        case 'Home': this.seekPercent(0); break;
        case 'End': this.seekPercent(100); break;
        case 'Escape': if (!this.trPop.hidden || !this.trBtn.hidden) this.hideTr(); else if (this.find.isOpen) this.find.close(); else if (this.barsOn()) this.setBars(false); else this.ctx.goShelf(); break;
        case 't': case 'T': this.openToc('toc'); break;
        case 'b': case 'B': this.toggleBookmark(); break;
        case '/': e.preventDefault(); this.openSearch(); break;
        case 'm': case 'M': this.setBars(!this.barsOn()); break;
        case 'r': case 'R': this.toggleTts(); break;
        default: return;
      }
      e.preventDefault();
    };
    document.addEventListener('keydown', this.keyHandler);
    this.selHandler = debounce(() => this.onSelection(), 250);
    document.addEventListener('selectionchange', this.selHandler);
    // do not offer 翻译 while the mouse is still dragging the selection; re-check when it is released
    this._ptrDown = false;
    document.addEventListener('pointerdown', (e) => { this._ptrDown = !(e.target.closest && e.target.closest('.tr-btn, .tr-pop')); }, true);
    const released = () => { if (this._ptrDown) { this._ptrDown = false; this.selHandler(); } };
    document.addEventListener('pointerup', released, true);
    document.addEventListener('pointercancel', released, true);
    this.vis = () => { if (document.visibilityState === 'hidden' && this.active) this.saveSoon.flush(); };
    document.addEventListener('visibilitychange', this.vis);
    window.addEventListener('pagehide', this.vis);
  }

  barsOn() { return this.root.classList.contains('bars-on'); }
  setBars(on) { this.root.classList.toggle('bars-on', on); }

  // ------------------------------------------------------------------ opening
  async open(id) {
    const seq = ++this.seq;
    this.active = true;
    this.loading.hidden = false;
    this.loading.textContent = '正在打开…';
    this.setBars(false);
    this.closeBook();
    try {
      this.settings = await loadSettings();
      await loadCloudCfg();
      this.applySettings(false);
      const { meta, book } = await openStored(id);
      if (seq !== this.seq) { book.close(); return; }
      this.meta = meta;
      this.book = book;
      this.pdf = book.format === 'pdf';
      this.titleEl.textContent = meta.title;
      document.title = `${meta.title} - PyReader Web`;
      this.frame.classList.toggle('pdf', this.pdf);
      this.scrollNav.hidden = this.pdf;
      if (!this.pdf && this.pg.mode !== this.settings.mode) this.pg.setMode(this.settings.mode);
      this.content.hidden = this.pdf;
      this.btnSearch.setAttribute('aria-disabled', String(this.pdf));   // not `disabled`: a tap on a phone must still explain why
      this.btnSearch.classList.toggle('is-disabled', this.pdf);
      this.btnSearch.title = this.pdf ? 'PDF 暂不支持全文搜索' : '搜索全文（Ctrl+F 或 /）';
      this.btnTts.disabled = this.pdf;
      this.btnTts.title = this.pdf ? 'PDF 暂不支持朗读' : '朗读（R）';
      this.searchCache.clear();
      const saved = await getProgress(id);
      this.lastSaved = saved || null;
      this.dirty = false;
      this.pg.observe();
      this.quiet = true;
      if (this.pdf) await this.openPdfAt(saved && saved.anchor ? saved.anchor.s : 0);
      else {
        const a = saved && saved.anchor;
        const s = a && a.s < book.sections.length ? a.s : 0;
        await this.showSection(s, a && a.s === s ? { anchor: a } : { start: true });
      }
      this.quiet = false;
      this._restoredTop = this.pg.mode === 'scroll' ? this.frame.scrollTop : null;   // the scroll event the restore above queued is not reading
      if (seq !== this.seq) return;
      this.loading.hidden = true;
      this.pg.mode === 'paged' && this.frame.focus({ preventScroll: true });
      this.armDwell(meta.id);
      this.checkRemote(saved);
      this.refreshMarkButton();
      this.maybeShowHint();
    } catch (e) {
      console.error(e);
      this.loading.textContent = '打开失败：' + (e && e.message ? e.message : e);
      this.loading.append(h('div', { class: 'row', style: 'margin-left:12px' }, h('button', { class: 'btn', type: 'button', onclick: () => this.ctx.goShelf() }, '返回书架')));
    }
  }

  /** Opening alone is not reading: the book only becomes "continue reading" once progress moves or it stays open (and visible) for DWELL_MS */
  armDwell(id) {
    clearTimeout(this._dwellT);
    this._dwellT = setTimeout(() => {
      if (!this.active || !this.meta || this.meta.id !== id || document.visibilityState === 'hidden') return;
      touchRead(id, Math.round(stampNow(this.ctx.now(), this.ctx.skewMs ? this.ctx.skewMs() : 0) * 1000)).catch(() => {});
    }, DWELL_MS);
  }

  closeBook() {
    clearTimeout(this._dwellT);
    this.hideTr();
    this.find.reset();
    this.contentS = -1;
    if (this.tts) this.tts.stop();
    if (this.book) { try { this.book.close(); } catch { /* ignore */ } }
    this.book = null;
    this.content.replaceChildren();
    if (this.pdfWrap) { this.pdfWrap.remove(); this.pdfWrap = null; this.pdfCanvas = null; this.pdfText = null; }
  }

  close() {
    this.saveSoon.flush();
    this.active = false;
    this.seq++;
    this.pg.disconnect();
    this.closeBook();
    document.title = 'PyReader Web';
  }

  // ------------------------------------------------------------------ settings
  applySettings(relayout = true) {
    const s = this.settings;
    applyTheme(s.theme);
    applyTypography(this.root, s);
    this.pg.animate = true;
    this.pg.spread = s.spread;
    this.pg.maxLineEm = s.lineWidth;
    if (this.book && !this.pdf && this.pg.mode !== s.mode) this.pg.setMode(s.mode);
    if (this.pdfCanvas) this.pdfCanvas.classList.toggle('invert', isDarkNow());
    if (relayout && this.book) { if (this.pdf) this.renderPdf(); else this.pg.relayoutNow(); }
  }

  setSetting(key, value) {
    if (key in LIMITS) value = Math.min(LIMITS[key][1], Math.max(LIMITS[key][0], value));
    this.settings = normalize({ ...this.settings, [key]: value });
    saveSettings(this.settings).catch(() => {});
    if (key === 'fontSize') toast(`字号 ${this.settings.fontSize}`, { ms: 900 });
    if (!key.startsWith('tts')) this.applySettings(true);
    if (this.settingsSync) this.settingsSync();
  }

  // ------------------------------------------------------------------ translate selection (Translator API)
  onSelection() {
    if (!this.tr || !this.active || !this.book) { this.hideTr(); return; }
    if (this._ptrDown) return;
    const sel = globalThis.getSelection && getSelection();
    if (!sel || !sel.rangeCount) { this.hideTr(); return; }
    const range = sel.getRangeAt(0);
    if (this.trPop.contains(range.commonAncestorContainer)) return;   // selecting inside the translation itself
    const text = this.pdf ? normalizeSelection(sel.toString()) : sel.toString();
    if (sel.isCollapsed || !text.trim() || !(this.pdf ? this.pdfText : this.content).contains(range.commonAncestorContainer)) { this.hideTr(); return; }
    const rects = range.getClientRects();
    const last = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
    const first = rects.length ? rects[0] : last;
    if (!this.trPop.hidden && this.trSel && this.trSel.text === text) return;   // same selection, translation already showing
    this.trSel = { text, rect: { left: first.left, right: last.right, top: first.top, bottom: last.bottom } };
    this.trSeq++;
    this.trPop.hidden = true;
    this.trBtn.hidden = false;
    const w = this.trBtn.offsetWidth || 60;
    const below = last.bottom + 8 + 36 <= innerHeight;
    this.trBtn.style.left = Math.max(8, Math.min(last.right - w / 2, innerWidth - w - 8)) + 'px';
    this.trBtn.style.top = (below ? last.bottom + 8 : Math.max(8, first.top - 44)) + 'px';
  }

  hideTr() {
    this.trSeq++;
    this.trBtn.hidden = true;
    this.trPop.hidden = true;
    this.trSel = null;
  }

  showTr(head, body) {
    const s = this.trSel;
    this.trHead.textContent = head;
    this.trBody.textContent = body;
    this.trPop.hidden = false;
    if (!s) return;
    const w = Math.min(380, innerWidth - 24);
    this.trPop.style.width = w + 'px';
    this.trPop.style.left = Math.max(12, Math.min(s.rect.left, innerWidth - w - 12)) + 'px';
    const ph = this.trPop.offsetHeight;
    const room = innerHeight - s.rect.bottom - 16;
    const top = room >= Math.min(ph, 180) ? s.rect.bottom + 10 : s.rect.top - ph - 10;
    this.trPop.style.top = Math.max(8, Math.min(top, innerHeight - ph - 8)) + 'px';   // never push the buttons off screen
  }

  async runTranslate() {
    const s = this.trSel;
    if (!s || !this.tr) return;
    this.trBtn.hidden = true;
    const seq = ++this.trSeq;
    this.showTr('翻译', '翻译中…');
    const live = globalThis.getSelection ? getSelection() : null;
    const text = live && live.toString().trim() && this.content.contains(live.anchorNode) ? live.toString() : s.text;   // the debounce may lag behind the real selection
    try {
      const r = await this.tr.translate(text, {
        target: this.settings.trTarget,
        onProgress: (p) => { if (seq === this.trSeq) this.showTr('首次使用', `正在下载语言模型 ${p}%（只下载一次）…`); },
      });
      if (seq !== this.trSeq) return;
      this.trLast = r.text;
      this.showTr(`${LANG_NAME[r.source] || r.source} → ${LANG_NAME[r.target] || r.target}`, r.text);
    } catch (e) {
      if (seq !== this.trSeq) return;
      this.trLast = '';
      this.showTr('无法翻译', e && e.message ? e.message : String(e));
      if (e && e.code === 'activation') this.retryTr();   // the message says "click again": the button must be there
    }
  }

  // put the 翻译 button next to the (error) popover so the user can click again
  retryTr() {
    const pop = this.trPop.getBoundingClientRect();
    this.trBtn.hidden = false;
    const h = this.trBtn.offsetHeight || 36, w = this.trBtn.offsetWidth || 60;
    this.trBtn.style.left = Math.max(8, Math.min(pop.left, innerWidth - w - 8)) + 'px';
    this.trBtn.style.top = Math.max(8, Math.min(pop.bottom + 8 + h <= innerHeight ? pop.bottom + 8 : pop.top - h - 8, innerHeight - h - 8)) + 'px';
  }

  async copyTranslation() {
    if (!this.trLast) return;
    try { await navigator.clipboard.writeText(this.trLast); toast('已复制译文', { ms: 1200 }); } catch { toast('复制失败，请手动选中复制', { ms: 2000 }); }
  }

  // ------------------------------------------------------------------ read aloud (speechSynthesis)
  toggleTts() {
    if (!this.tts || !this.book || this.pdf) return;
    if (this.tts.state === 'idle') {
      this.setBars(false);
      if (this._hintToast) { this._hintToast.remove(); this._hintToast = null; } // the one-time tip would sit on top of the control bar
      this.tts.start(this.pg.pageRange()[0]);
    } else this.tts.stop();
  }

  ttsPauseResume() {
    if (!this.tts) return;
    if (this.tts.state === 'playing') this.tts.pause(); else this.tts.resume();
  }

  ttsStep(d) {
    const v = Math.round((this.settings.ttsRate + d) * 10) / 10;
    this.setSetting('ttsRate', v);
    this.ttsRateEl.textContent = this.settings.ttsRate.toFixed(1) + 'x';
  }

  ttsChanged(st) {
    this.mediaSession(st);
    this.root.classList.toggle('tts-on', st !== 'idle');
    this.btnTts.setAttribute('aria-pressed', String(st !== 'idle'));
    this.ttsState.textContent = st === 'paused' ? '已暂停' : '朗读中';
    this.ttsPlay.replaceChildren(icon(st === 'paused' ? 'play' : 'pause'));
    this.ttsPlay.setAttribute('aria-label', st === 'paused' ? '继续朗读' : '暂停朗读');
    if (this.settings) this.ttsRateEl.textContent = this.settings.ttsRate.toFixed(1) + 'x';
    if (st === 'idle' && globalThis.CSS && CSS.highlights) CSS.highlights.delete('tts');
    if (st === 'idle') this.setSleep(null);
  }

  /** lock-screen / headset controls while reading (helps the OS treat cloud playback as media) */
  mediaSession(st) {
    const ms = globalThis.navigator && navigator.mediaSession;
    if (!ms) return;
    try {
      if (st === 'idle') { ms.playbackState = 'none'; return; }
      ms.playbackState = st === 'paused' ? 'paused' : 'playing';
      if (!this._msBound) {
        this._msBound = true;
        if (globalThis.MediaMetadata) ms.metadata = new MediaMetadata({ title: (this.meta && this.meta.title) || 'PyReader', artist: 'PyReader' });
        ms.setActionHandler('play', () => this.tts && this.tts.resume());
        ms.setActionHandler('pause', () => this.tts && this.tts.pause());
        ms.setActionHandler('stop', () => this.tts && this.tts.stop());
      }
    } catch { /* unsupported action */ }
  }

  // ------------------------------------------------------------------ sleep timer
  /** arm (or with null, cancel) the timer; the countdown is polled against the clock so a throttled background tab still stops on time */
  setSleep(t) {
    clearInterval(this.sleepTick);
    this.sleepTick = 0;
    this.sleep = t;
    this.ttsSleep.classList.toggle('on', !!t);
    this.ttsSleep.textContent = timerLabel(t, Date.now());
    this.ttsSleep.setAttribute('aria-label', t ? `睡眠定时：${this.ttsSleep.textContent}，点按更改或取消` : '睡眠定时');
    if (t && t.kind === 'time') this.sleepTick = setInterval(() => this.sleepCheck(), 500);
  }

  sleepCheck() {
    if (!this.sleep) return;
    if (isDue(this.sleep, Date.now())) this.sleepStop();
    else this.ttsSleep.textContent = timerLabel(this.sleep, Date.now());
  }

  sleepStop() {
    if (this.tts) this.tts.stop();   // goes idle -> clears the timer
    toast('睡眠定时已到，已停止朗读', { ms: 3000 });
  }

  openSleepMenu() {
    if (!this.tts || this.tts.state === 'idle') return;
    const on = activeChoice(this.sleep);
    const pick = (choice) => { p.close(); this.setSleep(makeTimer(choice, Date.now())); };
    const item = (label, choice) => h('li', { 'aria-current': on === choice ? 'true' : null }, h('button', { type: 'button', onclick: () => pick(choice) }, label));
    const p = openPanel({
      title: '睡眠定时', side: 'dialog', label: '睡眠定时',
      content: h('ul', { class: 'list' },
        ...SLEEP_MINUTES.map((m) => item(`${m} 分钟后停止`, m)),
        item('读完本章后停止', 'chapter'),
        ...(this.sleep ? [h('li', null, h('button', { type: 'button', class: 'danger', onclick: () => { p.close(); this.setSleep(null); } }, '取消定时'))] : [])),
    });
  }

  /** called before each sentence is spoken: bring it into view (turning the page if needed) and highlight it */
  ttsReveal(a, b) {
    const [s, e] = this.pg.pageRange();
    if (a < s || a >= e) {
      this._ttsMoving++;
      try { this.pg.gotoOffset(a); } finally { this._ttsMoving--; }
    }
    const range = this.pg.rangeFor(a, b);
    if (range && globalThis.CSS && CSS.highlights && globalThis.Highlight) CSS.highlights.set('tts', new Highlight(range));
  }

  async ttsNextSection() {
    if (this.sleep && this.sleep.kind === 'chapter') {   // chapter read to its end: stop here instead of rolling on (the tts loop then goes idle)
      this.sleepFired = true;
      this.setSleep(null);
      toast('睡眠定时已到，已停止朗读', { ms: 3000 });
      return false;
    }
    if (!this.book || this.s >= this.book.sections.length - 1) return false;
    this._ttsMoving++;
    try { await this.showSection(this.s + 1, { start: true }); } finally { this._ttsMoving--; }
    this.markDirty();
    return true;
  }

  /** one-time tip: the toolbars are hidden until the middle of the page is tapped */
  async maybeShowHint() {
    try {
      if (await kvGet('hint.reader.v1', false)) return;
      await kvSet('hint.reader.v1', true);
    } catch { return; }
    this._hintToast = toast('点击页面中间呼出菜单（目录、搜索、朗读、设置），点击左右两侧翻页。快捷键：T 目录，R 朗读。', { ms: 8000 });
  }

  openSettings() {
    const s = () => this.settings;
    const seg = (items, key) => {
      const wrap = h('div', { class: 'seg', role: 'group' });
      const upd = () => wrap.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(String(s()[key]) === b.dataset.v)));
      items.forEach(([v, label]) => wrap.append(h('button', { type: 'button', dataset: { v: String(v) }, onclick: () => { this.setSetting(key, v); upd(); } }, label)));
      upd();
      return { wrap, upd };
    };
    const range = (label, key, step, fmt = (v) => v) => {
      const out = h('span', { class: 'pct' });
      const inp = h('input', { type: 'range', min: LIMITS[key][0], max: LIMITS[key][1], step, value: s()[key], 'aria-label': label });
      const upd = () => { inp.value = s()[key]; out.textContent = fmt(s()[key]); };
      inp.addEventListener('input', () => { this.setSetting(key, parseFloat(inp.value)); upd(); });
      upd();
      return { el: h('label', { class: 'field' }, h('span', null, label, ' ', out), inp), upd };
    };
    const toggle = (label, key) => {
      const b = h('button', { class: 'chip', type: 'button', 'aria-pressed': String(!!s()[key]), onclick: () => { this.setSetting(key, !s()[key]); b.setAttribute('aria-pressed', String(!!s()[key])); } }, label);
      return b;
    };
    const theme = seg(THEMES, 'theme');
    const fam = seg(FONTS, 'fontFamily');
    const fs = range('字号', 'fontSize', 1, (v) => v + 'px');
    const lh = range('行距', 'lineHeight', 0.05, (v) => v.toFixed(2));
    const mg = range('页边距', 'margin', 2, (v) => v + 'px');
    const ps = range('段间距', 'paraSpace', 0.05, (v) => v.toFixed(2) + 'em');
    const mode = seg([['paged', '分页'], ['scroll', '连续滚动']], 'mode');
    const lw = seg([[30, '窄'], [40, '标准'], [55, '宽'], [0, '不限']], 'lineWidth');
    const rate = range('朗读语速', 'ttsRate', 0.1, (v) => v.toFixed(1) + 'x');
    let offVoices = null;
    const ttsBlock = [];
    if (this.tts) {
      const synth = globalThis.speechSynthesis;
      const sel = h('select', { 'aria-label': '朗读音色', onchange: () => this.setSetting('ttsVoice', sel.value) });
      const fill = () => {
        const vs = (synth.getVoices() || []).slice().sort((a, b) => (Number(b.lang.startsWith('zh')) - Number(a.lang.startsWith('zh'))) || a.name.localeCompare(b.name));
        sel.replaceChildren(h('option', { value: '', text: '自动（优先中文自然音色）' }), ...vs.map((v) => h('option', { value: v.voiceURI, text: `${v.name} (${v.lang})` })));
        sel.value = s().ttsVoice;
      };
      fill();
      synth.addEventListener('voiceschanged', fill);
      offVoices = () => synth.removeEventListener('voiceschanged', fill);
      const sample = () => {
        if (this.tts.state !== 'idle') { toast('正在朗读，先停止后再试听', { ms: 1500 }); return; }
        const u = new SpeechSynthesisUtterance('这是朗读示例：天地玄黄，宇宙洪荒。');
        const v = pickVoice(synth.getVoices(), 'zh-CN', s().ttsVoice);
        u.lang = v ? v.lang : 'zh-CN';
        if (v) u.voice = v;
        u.rate = s().ttsRate;
        synth.cancel();
        synth.speak(u);
      };
      ttsBlock.push(h('label', { class: 'field' }, h('span', null, '朗读音色'), sel), rate.el,
        h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', onclick: sample }, '试听')),
        h('p', { class: 'hint', text: '朗读用浏览器自带的语音：Edge 带“Natural”的在线中文音色效果最好（需联网）；手机锁屏后系统可能暂停朗读。快捷键 R 开始/停止。' }));
    }
    const trSeg = seg([['zh', '中文'], ['en', 'English']], 'trTarget');
    const trBlock = this.tr
      ? [h('label', { class: 'field' }, h('span', null, '划词翻译：翻译成'), trSeg.wrap),
        h('p', { class: 'hint', text: '选中文字后点“翻译”。使用浏览器内置的本地翻译，不上传文字；首次使用某个语言需下载一次语言模型。中文原文会翻成英文。' })]
      : [h('p', { class: 'hint', text: '划词翻译需要桌面版 Chrome 或 Edge（浏览器内置翻译）。当前浏览器不支持；Edge 可用右键“翻译所选内容”。' })];
    const typo = [
      h('label', { class: 'field' }, h('span', null, '字体'), fam.wrap), fs.el, lh.el, mg.el, ps.el,
      h('label', { class: 'field' }, h('span', null, '行宽上限（宽屏时居中）'), lw.wrap),
      h('div', { class: 'row' }, toggle('首行缩进', 'indent'), toggle('两端对齐', 'justify'), toggle('宽屏双页', 'spread')),
      h('label', { class: 'field' }, h('span', null, '翻页方式'), mode.wrap),
      ...ttsBlock,
      ...trBlock,
    ];
    const body = [h('label', { class: 'field' }, h('span', null, '主题'), theme.wrap),
      ...(this.pdf ? [h('p', { class: 'hint', text: 'PDF 为固定版式，暂不支持字体/行距/边距设置（仅主题：夜间模式会反色显示）。' })] : typo),
      h('p', { class: 'hint', text: '快捷键：Ctrl/⌘ + 加号/减号/0 调字号；左右方向键、PageUp/PageDown、空格翻页；T 目录；B 书签；Ctrl+F 或 / 搜索（Enter 下一个，Shift+Enter 上一个）；双指捏合也可调字号。' })];
    this.settingsSync = () => { theme.upd(); fam.upd(); fs.upd(); lh.upd(); mg.upd(); ps.upd(); mode.upd(); if (!this.pdf) { lw.upd(); rate.upd(); trSeg.upd(); } };
    const p = openPanel({ title: '阅读设置', side: 'sheet', content: body, onClose: () => { this.settingsSync = null; if (offVoices) offVoices(); } });
    return p;
  }

  // ------------------------------------------------------------------ text navigation
  async showSection(i, where = { start: true }) {
    const seq = ++this.seq2;
    const { frag, cleanup } = await this.book.loadSection(i);
    if (seq !== this.seq2) { cleanup(); return; }
    this.s = i;
    this.find.clearPaint();
    await this.pg.setContent(frag, cleanup);
    if (seq !== this.seq2) return;
    this.contentS = i;
    if (where.anchor) this.pg.gotoOffset(resolveOffset(where.anchor, this.pg.fullText()));
    else if (where.offset !== undefined) this.pg.gotoOffset(where.offset);
    else if (where.fraction !== undefined) this.pg.gotoOffset(this.pg.skipBlank(Math.round(where.fraction * this.pg.total)));
    else if (where.id) { if (!this.pg.gotoId(where.id)) this.pg.gotoOffset(0); }
    else if (where.end) this.pg.gotoEnd();
    else this.pg.gotoOffset(0);
    this.updateUi();
    this.find.onPage();
  }

  async next() {
    if (!this.book) return;
    if (this.pdf) { if (this.pdfPage < this.book.pageCount - 1) await this.setPdfPage(this.pdfPage + 1, true); return; }
    if (this.pg.next() === 'end') {
      if (this.s < this.book.sections.length - 1) { await this.showSection(this.s + 1, { start: true }); this.markDirty(); }
      else toast('已是最后一页', { ms: 900 });
    }
  }

  async prev() {
    if (!this.book) return;
    if (this.pdf) { if (this.pdfPage > 0) await this.setPdfPage(this.pdfPage - 1, true); return; }
    if (this.pg.prev() === 'start') {
      if (this.s > 0) { await this.showSection(this.s - 1, { end: true }); this.markDirty(); }
      else toast('已是第一页', { ms: 900 });
    }
  }

  tocSections() {
    const set = new Set(this.book.toc.map((t) => t.s));
    return set.size ? [...set].sort((a, b) => a - b) : this.book.sections.map((_, i) => i);
  }

  async chapter(dir) {
    if (!this.book) return;
    if (this.pdf) {
      const t = this.book.toc.length ? [...new Set(this.book.toc.map((x) => x.s))].sort((a, b) => a - b) : null;
      const cur = this.pdfPage;
      const target = t ? (dir > 0 ? t.find((x) => x > cur) : [...t].reverse().find((x) => x < cur)) : cur + dir * 10;
      if (target !== undefined) await this.setPdfPage(Math.max(0, Math.min(this.book.pageCount - 1, target)), true);
      return;
    }
    const list = this.tocSections();
    let target;
    if (dir > 0) target = list.find((x) => x > this.s);
    else target = this.pg.page > 0 || this.pg.anchor > 0 ? [...list].reverse().find((x) => x <= this.s) : [...list].reverse().find((x) => x < this.s);
    if (target === undefined) { toast(dir > 0 ? '已是最后一章' : '已是第一章', { ms: 900 }); return; }
    await this.showSection(target, { start: true });
    this.markDirty();
  }

  async seekPercent(p) {
    if (!this.book) return;
    if (this.pdf) { await this.setPdfPage(Math.min(this.book.pageCount - 1, Math.max(0, Math.round((p / 100) * this.book.pageCount - 0.5))), true); return; }
    const { s, f } = percentToPosition(this.book.lens, p);
    if (s !== this.s) await this.showSection(s, { fraction: f });
    else this.pg.gotoOffset(this.pg.skipBlank(Math.round(f * this.pg.total)));
    this.markDirty();
    this.updateUi();
  }

  async gotoTarget(s, where) {
    if (this.pdf) { await this.setPdfPage(s, true); return; }
    if (s === this.s && s === this.contentS) {   // not while that section is still loading: the paginator holds the old text
      if (where.anchor) this.pg.gotoOffset(resolveOffset(where.anchor, this.pg.fullText()));
      else if (where.id) { if (!this.pg.gotoId(where.id)) this.pg.gotoOffset(0); }
      else if (where.offset !== undefined) this.pg.gotoOffset(where.offset);
      else this.pg.gotoOffset(0);
      this.updateUi();
    } else await this.showSection(s, where);
    this.markDirty();
  }

  async followLink(href) {
    if (!href) return;
    const target = await this.book.resolveHref(this.s, href);   // synchronous for most formats; KF8 (azw3) links resolve asynchronously
    if (target) { await this.gotoTarget(target.s, target.id ? { id: target.id } : { start: true }); return; }
    if (/^(https?:|mailto:)/i.test(href)) {
      const a = document.createElement('a');
      a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.click();
    }
  }

  onPaginator(reason) {
    if (reason === 'layout') this.hideTr();   // resize / font change: the fixed-position popover would point at the wrong place
    if (reason === 'page') {
      this.find.onPage();
      this.hideTr();
      this.markDirty();
      // the reader turned the page by hand while reading aloud: continue from the top of the new page
      if (this.tts && this.tts.state !== 'idle' && !this._ttsMoving && this.pg.mode === 'paged') this.tts.restart(this.pg.pageRange()[0]);
    }
    this.updateUi();
  }

  onScroll() {
    if (this._scrollT) clearTimeout(this._scrollT);
    this._scrollT = setTimeout(() => {
      if (this._restoredTop != null) {
        if (Math.abs(this.frame.scrollTop - this._restoredTop) < 1) return;   // still where open() put it
        this._restoredTop = null;
      }
      this.pg.syncAnchorFromScroll();
    }, 120);
  }

  markDirty() { if (this.quiet) return; this.dirty = true; this.saveSoon(); }

  // ------------------------------------------------------------------ position / percent
  percent() {
    if (!this.book) return 0;
    if (this.pdf) return ((this.pdfPage + 1) / this.book.pageCount) * 100;
    const pg = this.pg;
    const last = this.s === this.book.sections.length - 1;
    const atEnd = pg.mode === 'paged' ? pg.page + pg.cols >= pg.pageCount : pg.frame.scrollTop + pg.frame.clientHeight >= pg.frame.scrollHeight - 2;
    const frac = last && atEnd ? 1 : pg.total ? pg.currentOffset() / pg.total : 0;
    return positionToPercent(this.book.lens, this.s, frac);
  }

  anchor() {
    if (this.pdf) return { s: this.pdfPage, o: 0, f: 0, c: '' };
    const o = this.pg.currentOffset();
    return { s: this.s, o, f: this.pg.total ? o / this.pg.total : 0, c: this.pg.textAt(o, 12) };
  }

  sectionTitle(s = this.s) {
    if (!this.book) return '';
    if (this.pdf) {
      const t = [...this.book.toc].reverse().find((x) => x.s <= s);
      return t ? t.title : '';
    }
    const t = this.curTocEntry();
    return (t && t.title) || (this.book.sections[s] && this.book.sections[s].title) || '';
  }

  curTocEntry() {
    let best = null;
    for (const t of this.book.toc) if (t.s <= this.s) best = t; else break;
    return best;
  }

  /** "3/12", or "3-4/12" when two pages are shown side by side */
  pageLabel() {
    const { page, cols, pageCount } = this.pg;
    const last = Math.min(page + cols, pageCount);
    return last > page + 1 ? `${page + 1}-${last}/${pageCount}` : `${page + 1}/${pageCount}`;
  }

  updateUi() {
    if (!this.book) return;
    const pct = this.percent();
    const shown = Math.round(pct * 10) / 10;
    this.slider.value = String(Math.round(pct * 10));
    this.pctLabel.textContent = shown.toFixed(1) + '%';
    this.chEl.textContent = this.sectionTitle();
    this.posEl.textContent = this.pdf ? `第 ${this.pdfPage + 1}/${this.book.pageCount} 页 · ${shown.toFixed(1)}%`
      : this.pg.mode === 'paged' ? `${this.pageLabel()} · ${shown.toFixed(1)}%` : `${shown.toFixed(1)}%`;
    this.refreshMarkButton();
  }

  // ------------------------------------------------------------------ progress persistence
  async saveNow() {
    if (!this.book || !this.meta || !this.dirty) return;
    this.dirty = false;
    const pct = this.percent();
    const rec = { bid: this.meta.id, anchor: this.anchor(), percent: Math.round(pct * 1000) / 1000, updated: stampNow(this.ctx.now(), this.ctx.skewMs ? this.ctx.skewMs() : 0), chapter: this.sectionTitle() };
    this.lastSaved = rec;
    try { await saveProgress(rec); } catch (e) { console.warn('[progress] save failed', e); }
    this.ctx.onProgress({ bid: rec.bid, aliases: aliasIds(this.meta), percent: rec.percent, chapter: rec.chapter, updated: rec.updated });
  }

  async checkRemote(saved) {
    try {
      const remote = await this.ctx.fetchRemoteProgress(this.meta.id, aliasIds(this.meta));
      if (!remote || !this.active || !this.meta) return;
      const localTs = saved ? saved.updated : 0;
      if (remote.updated <= localTs || Math.abs(remote.percent - this.percent()) < 0.5) return;
      const jump = async () => {
        banner.remove();
        await this.seekPercent(remote.percent);
        const rec = { bid: this.meta.id, anchor: this.anchor(), percent: remote.percent, updated: remote.updated, chapter: this.sectionTitle() };
        this.lastSaved = rec;
        await saveProgress(rec);   // adopt the remote timestamp: opening must not look like a newer local read
        this.dirty = false;
      };
      const banner = h('div', { class: 'banner', role: 'alert' },
        h('span', null, `「${remote.device || '其他设备'}」读到了 ${remote.percent.toFixed(1)}%`),
        h('button', { class: 'btn small primary', type: 'button', onclick: jump }, '跳转'),
        h('button', { class: 'btn small', type: 'button', onclick: () => banner.remove() }, '忽略'));
      if (!saved) { await jump(); toast(`已同步进度（来自 ${remote.device || '其他设备'}，${remote.percent.toFixed(1)}%）`); return; }
      this.root.append(banner);
      setTimeout(() => banner.remove(), 15000);
    } catch (e) { console.warn('[sync] remote progress check failed', e); }
  }

  // ------------------------------------------------------------------ TOC / bookmarks
  async openToc(tab = 'toc') {
    const book = this.book;
    if (!book) return;
    let panelRef = null;
    const body = h('div');
    const render = async (which) => {
      body.replaceChildren(h('div', { class: 'tabs', role: 'tablist' },
        h('button', { type: 'button', role: 'tab', 'aria-selected': String(which === 'toc'), onclick: () => render('toc') }, '目录'),
        h('button', { type: 'button', role: 'tab', 'aria-selected': String(which === 'mark'), onclick: () => render('mark') }, '书签')));
      if (which === 'toc') {
        const cur = this.pdf ? null : this.curTocEntry();
        const ul = h('ul', { class: 'list' });
        if (!book.toc.length) ul.append(h('li', null, h('p', { class: 'hint', text: this.pdf ? '这个 PDF 没有目录。' : '没有目录。' })));
        let curIdx = -1;
        if (this.pdf) { book.toc.forEach((t, i) => { if (t.s <= this.pdfPage) curIdx = i; }); } else if (cur) curIdx = book.toc.indexOf(cur);
        book.toc.forEach((t, i) => {
          const li = h('li', { 'aria-current': i === curIdx ? 'true' : null },
            h('button', { type: 'button', style: `padding-left:${6 + Math.min(t.level, 4) * 16}px`, onclick: async () => { panelRef.close(); await this.gotoTarget(t.s, t.id ? { id: t.id } : { start: true }); } },
              h('span', { text: t.title || `第 ${t.s + 1} 节` })));
          ul.append(li);
        });
        body.append(ul);
        setTimeout(() => { const c = ul.querySelector('[aria-current=true]'); if (c) c.scrollIntoView({ block: 'center' }); }, 40);
      } else {
        const marks = (await getBookmarks(this.meta.id)).sort((a, b) => a.percent - b.percent);
        const ul = h('ul', { class: 'list' });
        if (!marks.length) ul.append(h('li', null, h('p', { class: 'hint', text: '还没有书签。点顶栏的书签图标（或按 B）收藏当前页。' })));
        for (const m of marks) {
          ul.append(h('li', null, h('div', { class: 'li' },
            h('button', { type: 'button', style: 'flex:1;padding:4px 0', onclick: async () => { panelRef.close(); await this.gotoTarget(m.anchor.s, this.pdf ? {} : { anchor: m.anchor }); } },
              h('span', { class: 'snip' }, m.label || '（空白页）'),
              h('span', { class: 'sub' }, `${m.chapter ? m.chapter + ' · ' : ''}${m.percent.toFixed(1)}% · ${new Date(m.ts).toLocaleString()}`)),
            h('button', { class: 'icon-btn', type: 'button', 'aria-label': '删除书签', onclick: async () => { await deleteBookmark(m.id); await render('mark'); this.refreshMarkButton(); } }, icon('trash')))));
        }
        body.append(ul);
      }
    };
    panelRef = openPanel({ title: book.title || this.meta.title, side: 'left', content: body, label: '目录和书签' });
    await render(tab);
  }

  async curPageBookmark() {
    if (!this.meta || !this.book) return null;
    const marks = await getBookmarks(this.meta.id);
    if (!marks.length) return null;   // skip the (layout-querying) page range when there is nothing to match
    if (this.pdf) return marks.find((m) => m.anchor.s === this.pdfPage) || null;
    const [a, b] = this.pg.pageRange();
    return marks.find((m) => m.anchor.s === this.s && m.anchor.o >= a && m.anchor.o < Math.max(b, a + 1)) || null;
  }

  async refreshMarkButton() {
    if (!this.book) return;
    const seq = (this._markSeq = (this._markSeq || 0) + 1);
    const cur = await this.curPageBookmark();
    if (seq !== this._markSeq) return;
    const on = !!cur;
    this.btnMark.setAttribute('aria-pressed', String(on));
    this.btnMark.setAttribute('aria-label', on ? '移除此页书签' : '添加书签');
    this.btnMark.replaceChildren(icon(on ? 'bookmarkFill' : 'bookmark'));
  }

  async toggleBookmark() {
    if (!this.book) return;
    const cur = await this.curPageBookmark();
    if (cur) { await deleteBookmark(cur.id); toast('已移除书签', { ms: 1200 }); } else {
      const anchor = this.anchor();
      const label = this.pdf ? `第 ${this.pdfPage + 1} 页` : this.pg.textAt(anchor.o, 36).trim();
      await addBookmark({ bid: this.meta.id, anchor, label, chapter: this.sectionTitle(), percent: this.percent(), ts: Date.now() });
      toast('已添加书签', { ms: 1200 });
    }
    this.refreshMarkButton();
  }

  // ------------------------------------------------------------------ search (the bar itself is reader/find.js)
  openSearch() {
    if (!this.book) return;
    if (this.pdf) { toast('PDF 是图片页面，没有文字层，暂不支持全文搜索', { ms: 3000 }); return; }
    this.find.open();
  }

  /** text of one section for searching; cached per book (a few hundred sections at most) */
  async sectionText(i) {
    const book = this.book;
    let t = this.searchCache.get(i);
    if (t === undefined) {
      t = await book.sectionText(i);
      if (book === this.book && this.searchCache.size < 400) this.searchCache.set(i, t);
    }
    return t;
  }

  // ------------------------------------------------------------------ PDF (render-only)
  async openPdfAt(page) {
    this.pdfPage = Math.max(0, Math.min(this.book.pageCount - 1, page || 0));
    this.pdfCanvas = h('canvas', { 'aria-label': 'PDF 页面', role: 'img' });
    this.pdfText = h('div', { class: 'pdf-text' });   // invisible selectable text over the canvas
    this.pdfWrap = h('div', { class: 'pdf-page' }, this.pdfCanvas, this.pdfText);
    this.frame.append(this.pdfWrap);
    this.frame.classList.remove('scroll');
    await this.renderPdf();
    this.updateUi();
    this._pdfRo = this._pdfRo || new ResizeObserver(() => { if (this.pdf && this.active) this.renderPdfSoon(); });
    this._pdfRo.observe(this.frame);
  }
  renderPdfSoon() { clearTimeout(this._pdfT); this._pdfT = setTimeout(() => this.renderPdf(), 120); }
  async renderPdf() {
    if (!this.pdf || !this.pdfCanvas) return;
    this.pdfCanvas.classList.toggle('invert', isDarkNow());
    const w = this.frame.clientWidth - 8, hgt = this.frame.clientHeight - 8;
    try { await this.book.render(this.pdfPage, this.pdfCanvas, Math.max(50, w), Math.max(50, hgt), this.pdfText); } catch (e) { toast('渲染失败：' + e.message); }
  }
  async setPdfPage(p, user) {
    this.pdfPage = p;
    await this.renderPdf();
    if (user) this.markDirty();
    this.updateUi();
  }
}
