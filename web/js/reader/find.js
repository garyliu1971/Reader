// In-book search bar: type, Enter / Shift+Enter (or the arrow buttons) step through every hit in the book, the active hit
// is highlighted (CSS Custom Highlight API) and brought on screen. The matching itself is core/search.js (pure).
// The reader provides the book/layout side through `host`:
//   count() sections, text(i) -> Promise<string>, title(i), start() -> {s, o} reading position, here() current offset,
//   visible(s, o) is that offset on the page being read,
//   section() section whose text is laid out right now, goto(s, o) -> Promise (make offset o of section s visible),
//   range(a, b) -> Range for the laid-out section, onToggle(open).
import { h, openPanel, debounce, sleep } from '../ui/dom.js';
import { icon } from '../ui/icons.js';
import { compileQuery, findAll, totalHits, rankOf, stepHit, seekHit, hitAtOrAfter, sectionOrder, snippet, MAX_HITS } from '../core/search.js';

const LIST_MAX = 500;
const PAINT_BEFORE = 100, PAINT_AFTER = 200;   // highlight only the hits around the reading position: a common word has thousands

export class FindBar {
  constructor(host) {
    this.host = host;
    this.matches = [];      // per section flat [o, len, ...] (core/search.js)
    this.cur = null;        // active hit {s, k}
    this.q = '';            // the query `matches` belongs to
    this.done = true;       // scan finished
    this.capped = false;
    this.scanned = 0;
    this.placed = false;    // an active hit has been chosen for this query
    this.token = 0;         // scan generation
    this.navSeq = 0;
    this.compEnd = 0;       // time of the last IME compositionend (a Safari quirk delivers the commit Enter after it)
    this.selSet = false;
    this.input = h('input', { type: 'search', class: 'find-input', placeholder: '在全文中搜索…', 'aria-label': '搜索关键词', enterKeyHint: 'search', autocomplete: 'off', autocapitalize: 'off', spellcheck: false });
    this.count = h('span', { class: 'find-count', role: 'status', 'aria-live': 'polite' });
    this.btnPrev = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '上一个（Shift+Enter）', title: '上一个（Shift+Enter）', onclick: () => this.step(-1) }, icon('up'));
    this.btnNext = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '下一个（Enter）', title: '下一个（Enter）', onclick: () => this.step(1) }, icon('down'));
    this.btnList = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '结果列表', title: '结果列表', onclick: () => this.openList() }, icon('list'));
    this.btnClose = h('button', { class: 'icon-btn find-close', type: 'button', 'aria-label': '关闭搜索', title: '关闭（Esc）', onclick: () => this.close() }, icon('close'));
    this.el = h('div', { class: 'findbar', role: 'search', 'aria-label': '全文搜索', hidden: true }, this.input, this.btnClose, this.count, this.btnPrev, this.btnNext, this.btnList);
    this.go = debounce(() => this.run(), 220);
    this.repaintSoon = debounce(() => this.paint(), 150);
    this.input.addEventListener('input', (e) => { if (!e.isComposing) this.go(); });
    this.input.addEventListener('compositionend', () => { this.compEnd = performance.now(); this.go(); });
    this.input.addEventListener('keydown', (e) => this.onKey(e));
    this.showCount();
  }

  get isOpen() { return !this.el.hidden; }

  onKey(e) {
    if (e.isComposing) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      if (performance.now() - this.compEnd < 40) return;   // that Enter only committed the IME text
      if (this.input.value !== this.q) { this.go.cancel(); this.run(); } else this.step(e.shiftKey ? -1 : 1);
    } else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.close(); }
    else if (e.key === 'F3') { e.preventDefault(); this.step(e.shiftKey ? -1 : 1); }
    else if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) { e.preventDefault(); this.input.select(); }
  }

  open() {
    this.el.hidden = false;
    this.host.onToggle(true);
    this.input.focus({ preventScroll: true });
    this.input.select();
    if (this.input.value.trim() && (this.input.value !== this.q || !this.done)) this.run(); else this.paint();
  }

  close() {
    if (!this.isOpen) return;
    this.el.hidden = true;
    this.token++;
    this.navSeq++;
    this.go.cancel();
    this.repaintSoon.cancel();
    this.clearPaint();
    this.host.onToggle(false);
  }

  /** a different book is opened: forget everything */
  reset() {
    const was = this.isOpen;
    this.token++;
    this.navSeq++;
    this.go.cancel();
    this.repaintSoon.cancel();
    this.clearPaint();
    this.el.hidden = true;
    this.input.value = '';
    this.matches = [];
    this.cur = null;
    this.q = '';
    this.done = true;
    this.capped = false;
    this.showCount();
    if (was) this.host.onToggle(false);
  }

  // ------------------------------------------------------------------ scanning
  async run() {
    const my = ++this.token;
    const q = this.input.value;
    const re = compileQuery(q);
    // an extended query keeps its place: start from the active hit while it is still the page being read, else from the page
    const at = this.cur && this.matches[this.cur.s] ? { s: this.cur.s, o: this.matches[this.cur.s][this.cur.k * 2] } : null;
    const start = at && this.host.visible(at.s, at.o) ? at : this.host.start();
    const matches = [];
    matches.length = this.host.count();
    this.matches = matches;
    this.q = q;
    this.cur = null;
    this.placed = false;
    this.capped = false;
    this.scanned = 0;
    this.done = !re;
    this.navSeq++;
    this.clearPaint();
    this.showCount();
    if (!re) return;
    const order = sectionOrder(matches.length, start.s);
    let found = 0;
    let t0 = performance.now();
    for (let idx = 0; idx < order.length && found < MAX_HITS; idx++) {
      const i = order[idx];
      let text = '';
      try { text = await this.host.text(i); } catch (e) { console.warn('[search] section unreadable', i, e); }
      if (my !== this.token) return;
      const flat = findAll(text, re, MAX_HITS - found);
      if (flat.length) { matches[i] = flat; found += flat.length / 2; }
      this.scanned = idx + 1;
      if (!this.placed && flat.length && i >= start.s) {   // still before the wrap-around: the first hit after the reader
        const c = seekHit(matches, start.s, start.o);
        if (c) this.activate(c);
      }
      this.showCount();
      if (performance.now() - t0 > 12) { await sleep(0); if (my !== this.token) return; t0 = performance.now(); }
    }
    this.capped = found >= MAX_HITS;
    this.done = true;
    if (!this.placed) { const c = seekHit(matches, start.s, start.o, true); if (c) this.activate(c); }
    this.showCount();
  }

  showCount() {
    const total = totalHits(this.matches);
    let t = '';
    if (this.q.trim()) {
      if (!this.done) t = total ? `已找到 ${total} 处 · 搜索 ${this.scanned}/${this.matches.length}` : `搜索中 ${this.scanned}/${this.matches.length}`;
      else if (!total) t = '没有找到';
      else t = this.cur ? `${rankOf(this.matches, this.cur)} / ${total}${this.capped ? '+' : ''}` : `共 ${total} 处`;
    }
    this.count.textContent = t;
    this.count.classList.toggle('none', this.done && !!this.q.trim() && !total);
    this.btnPrev.disabled = this.btnNext.disabled = this.btnList.disabled = !total;
  }

  // ------------------------------------------------------------------ navigation
  step(dir) {
    const c = stepHit(this.matches, this.cur, dir);
    if (c) this.activate(c);
  }

  async activate(c) {
    const seq = ++this.navSeq;
    this.cur = c;
    this.placed = true;
    this.showCount();
    try { await this.host.goto(c.s, this.matches[c.s][c.k * 2]); } catch (e) { console.warn('[search] jump failed', e); }
    if (seq !== this.navSeq || !this.isOpen) return;
    this.paint();
  }

  // ------------------------------------------------------------------ highlight
  /** called when the reader turns a page or lays out a new section */
  onPage() { if (this.isOpen) this.repaintSoon(); }

  paint() {
    const s = this.host.section();
    const flat = this.isOpen ? this.matches[s] : null;
    if (!flat || !flat.length) { this.clearPaint(); return; }
    const n = flat.length / 2;
    const act = this.cur && this.cur.s === s ? this.cur.k : -1;
    const range = (k) => { try { return this.host.range(flat[k * 2], flat[k * 2] + flat[k * 2 + 1]); } catch { return null; } };
    const reg = globalThis.CSS && CSS.highlights, HL = globalThis.Highlight;
    if (!reg || !HL) {   // no Custom Highlight API: select the active hit instead
      const r = act >= 0 ? range(act) : null;
      const sel = globalThis.getSelection && getSelection();
      if (r && sel) { sel.removeAllRanges(); sel.addRange(r); this.selSet = true; }
      return;
    }
    const mid = Math.min(n - 1, hitAtOrAfter(flat, this.host.here()));
    const all = new HL();
    for (let k = Math.max(0, mid - PAINT_BEFORE); k < Math.min(n, mid + PAINT_AFTER); k++) {
      if (k === act) continue;
      const r = range(k);
      if (r) all.add(r);
    }
    reg.set('search', all);
    const r = act >= 0 ? range(act) : null;
    if (r) { const a = new HL(r); a.priority = 1; reg.set('search-active', a); } else reg.delete('search-active');
  }

  clearPaint() {
    if (globalThis.CSS && CSS.highlights) { CSS.highlights.delete('search'); CSS.highlights.delete('search-active'); }
    if (this.selSet) { this.selSet = false; const sel = globalThis.getSelection && getSelection(); if (sel) sel.removeAllRanges(); }
  }

  // ------------------------------------------------------------------ result list
  async openList() {
    if (!totalHits(this.matches)) return;
    const my = this.token;
    const status = h('p', { class: 'hint' });
    const ul = h('ul', { class: 'list' });
    let closed = false;
    const p = openPanel({ title: '搜索结果', side: 'left', content: [status, ul], onClose: () => { closed = true; } });
    let n = 0;
    for (let s = 0; s < this.matches.length && n < LIST_MAX; s++) {
      const flat = this.matches[s];
      if (!flat) continue;
      let text = '';
      try { text = await this.host.text(s); } catch { continue; }
      if (closed || my !== this.token) return;
      const title = (this.host.title(s) || '').slice(0, 14);
      const frag = document.createDocumentFragment();
      for (let k = 0; k < flat.length / 2 && n < LIST_MAX; k++, n++) {
        const sn = snippet(text, flat[k * 2], flat[k * 2 + 1]);
        const on = !!this.cur && this.cur.s === s && this.cur.k === k;
        frag.append(h('li', { 'aria-current': on ? 'true' : null },
          h('button', { type: 'button', onclick: () => { p.close(); this.activate({ s, k }); } },
            h('span', { class: 'snip' }, (sn.lead ? '…' : '') + sn.before, h('mark', null, sn.hit), sn.after),
            h('span', { class: 'sub' }, title))));
      }
      ul.append(frag);
      status.textContent = `已列出 ${n} 条`;
    }
    const total = totalHits(this.matches);
    status.textContent = n < total || this.capped ? `共 ${total}${this.capped ? '+' : ''} 处，仅列出前 ${n} 条；用上一个 / 下一个浏览全部` : `共 ${total} 处`;
    if (!this.done) status.textContent += '（仍在搜索，结果可能不全）';
  }
}
