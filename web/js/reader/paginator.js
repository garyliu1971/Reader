// Reflowing page engine. One section (chapter) is laid out at a time with CSS multi-column; pages are columns that are
// brought into view with a transform. The reading position is a *text offset* (sum of DOM text-node lengths), never a
// page index, so any viewport/typography change re-derives the page from the offset.
//
// Geometry: a page is one column of stride Pw (Pw = frame width, or half of it in two-page spread); content is
// Tw = Pw - 2*mx wide and Ph high, with column-gap 2*mx, so the column stride equals Pw. Showing page p =
// translateX(-p*Pw); margins are empty gap, so neighbouring columns never peek in. In spread mode (paged, frame wide
// enough) `cols` = 2 and `page` is always the LEFT page of the spread (even), so the view never splits a spread.
// Line length is capped (`maxLineEm`) by growing the side margins, which keeps wide windows readable.

export const SPREAD_MIN_W = 1000;   // frame width (px) from which two pages are shown side by side

const isWs = (c) => c === ' ' || c === '\n' || c === '\t' || c === '　' || c === ' ';

export class Paginator {
  constructor(frame, content) {
    this.frame = frame;
    this.content = content;
    this.mode = 'paged';
    this.page = 0;
    this.pageCount = 1;
    this.pw = 0;              // stride of one page (column)
    this.fw = 0;              // frame width
    this.ph = 0;
    this.mx = 24;
    this.cols = 1;            // pages shown at once (1, or 2 in spread)
    this.spread = true;       // allow two-page spread when the frame is wide enough (paged mode)
    this.maxLineEm = 40;      // text line cap in em (0 = unlimited)
    this.nodes = [];          // [{node, start}]
    this.breaks = new Set();  // block-start offsets (see indexText)
    this.total = 0;           // text length of the section
    this.anchor = 0;          // authoritative reading offset (survives relayout)
    this.cleanup = null;
    this.animate = true;
    this.onChange = null;     // (reason) => void
    this._ro = null;
    this._raf = 0;
  }

  // ------------------------------------------------------------------ content
  async setContent(frag, cleanup) {
    if (this.cleanup) { try { this.cleanup(); } catch { /* ignore */ } }
    this.cleanup = cleanup || null;
    this.content.replaceChildren(frag);
    this.nodes = [];
    this.total = 0;
    this.page = 0;
    this.anchor = 0;
    this.measure();
    this.applyGeometry();
    await this.imagesReady();
    this.indexText();
    this.relayoutNow(0);
  }

  indexText() {
    this.nodes = [];
    this.breaks = new Set();   // offsets where a new block (paragraph, heading, list item...) starts
    let n = 0;
    let prevBlock = null;
    const w = document.createTreeWalker(this.content, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) {
      const node = w.currentNode;
      const block = node.parentElement && node.parentElement.closest('p,div,h1,h2,h3,h4,h5,h6,li,blockquote,pre,tr,figcaption,td,th');
      if (prevBlock !== null && block !== prevBlock && node.nodeValue.trim()) this.breaks.add(n);
      if (node.nodeValue.trim()) prevBlock = block;
      this.nodes.push({ node, start: n });
      n += node.nodeValue.length;
    }
    this.total = n;
  }

  imagesReady(timeout = 4000) {
    const imgs = Array.from(this.content.querySelectorAll('img')).filter((i) => !i.complete);
    if (!imgs.length) return Promise.resolve();
    const done = Promise.all(imgs.map((i) => new Promise((r) => { i.addEventListener('load', r, { once: true }); i.addEventListener('error', r, { once: true }); })));
    // late images (slow decode) re-trigger layout while keeping the anchor
    imgs.forEach((i) => i.addEventListener('load', () => this.requestRelayout(), { once: true }));
    return Promise.race([done, new Promise((r) => setTimeout(r, timeout))]);
  }

  // ------------------------------------------------------------------ geometry
  measure() {
    this.fw = Math.max(1, Math.floor(this.frame.clientWidth));
    this.ph = Math.max(1, Math.floor(this.frame.clientHeight));
    this.cols = this.mode === 'paged' && this.spread && this.fw >= SPREAD_MIN_W ? 2 : 1;
    this.pw = Math.max(1, Math.floor(this.fw / this.cols));
    const minText = 120;
    const cs = getComputedStyle(this.frame);
    const want = parseFloat(cs.getPropertyValue('--mx')) || 24;
    let mx = Math.max(6, Math.min(want, Math.floor((this.pw - minText) / 2)));
    if (this.maxLineEm > 0) {
      const maxTw = Math.round(this.maxLineEm * (parseFloat(cs.getPropertyValue('--fs')) || 19));
      if (this.pw - 2 * mx > maxTw) mx = Math.floor((this.pw - maxTw) / 2);
    }
    this.mx = mx;
  }

  /** first page of the spread that contains page p */
  alignPage(p) { return Math.floor(p / this.cols) * this.cols; }

  applyGeometry() {
    const c = this.content;
    const tw = Math.max(1, this.pw - 2 * this.mx);
    c.style.setProperty('--page-h', this.ph + 'px');
    const st = c.style;
    if (this.mode === 'paged') {
      this.frame.classList.remove('scroll');
      Object.assign(st, {
        left: this.mx + 'px', top: '0', width: tw + 'px', height: this.ph + 'px', columnWidth: tw + 'px',
        columnGap: 2 * this.mx + 'px', columnFill: 'auto', padding: '0', boxSizing: 'content-box',
      });
    } else {
      this.frame.classList.add('scroll');
      Object.assign(st, {
        left: '0', top: '0', width: this.pw + 'px', height: 'auto', columnWidth: 'auto', columnGap: 'normal',
        padding: `0 ${this.mx}px`, boxSizing: 'border-box', transform: 'none', transition: 'none',
      });
    }
  }

  /** recompute columns from the current geometry; position is restored from `this.anchor` */
  relayoutNow(anchor = this.anchor) {
    this.anchor = Math.max(0, Math.min(anchor, this.total));
    this.measure();
    this.applyGeometry();
    if (this.mode === 'paged') {
      const sw = this.content.scrollWidth;
      this.pageCount = Math.max(1, Math.round((sw + 2 * this.mx) / this.pw));
      this.page = this.alignPage(Math.min(this.pageCount - 1, Math.max(0, this.pageOfOffset(this.anchor))));
      this.showPage(false);
    } else {
      this.pageCount = 1;
      this.page = 0;
      this.scrollToOffset(this.anchor);
    }
    this.emit('layout');
  }

  requestRelayout() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.relayoutNow(); });
  }

  observe() {
    if (this._ro) return;
    this._ro = new ResizeObserver(() => this.requestRelayout());
    this._ro.observe(this.frame);
  }

  disconnect() { if (this._ro) { this._ro.disconnect(); this._ro = null; } if (this.cleanup) { try { this.cleanup(); } catch { /* ignore */ } this.cleanup = null; } }

  setMode(mode) {
    if (mode === this.mode) return;
    const keep = this.anchor;
    this.mode = mode;
    this.content.style.transform = 'none';
    this.frame.scrollTop = 0;
    this.relayoutNow(keep);
  }

  emit(reason) { if (this.onChange) this.onChange(reason); }

  // ------------------------------------------------------------------ char <-> geometry
  nodeAt(offset) {
    let lo = 0, hi = this.nodes.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.nodes[mid].start <= offset) lo = mid; else hi = mid - 1;
    }
    return this.nodes[lo];
  }

  /** client rect of the character at `offset` (null if the section has no text) */
  charRect(offset) {
    if (!this.nodes.length) return null;
    let o = Math.min(Math.max(0, offset), Math.max(0, this.total - 1));
    for (let tries = 0; tries < 40; tries++) {
      const { node, start } = this.nodeAt(o);
      const i = o - start;
      const len = node.nodeValue.length;
      if (len === 0) { o++; continue; }
      const r = document.createRange();
      r.setStart(node, Math.min(i, len - 1));
      r.setEnd(node, Math.min(i, len - 1) + 1);
      const rects = r.getClientRects();
      const rect = rects.length ? rects[0] : r.getBoundingClientRect();
      if ((rect.width > 0 || rect.height > 0) && !(rect.left === 0 && rect.top === 0 && rect.width === 0)) return rect;
      if (o + 1 >= this.total) break;
      o++;   // collapsed whitespace has no box: look at the next character
    }
    return null;
  }

  pageOfOffset(offset) {
    const r = this.charRect(offset);
    if (!r) return 0;
    const rel = r.left - this.content.getBoundingClientRect().left;
    return Math.max(0, Math.floor((rel + 0.5) / this.pw));
  }

  /** smallest offset in [0,total] such that pred(offset) is true (pred monotone); total if none */
  lowerBound(pred) {
    let lo = 0, hi = this.total;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pred(mid)) hi = mid; else lo = mid + 1;
    }
    return lo;
  }

  /** first text offset that is laid out on page p (paged mode) */
  offsetAtPage(p) {
    if (p <= 0) return 0;
    return this.lowerBound((o) => this.pageOfOffset(o) >= p);
  }

  // scroll mode: y of a char's bottom edge relative to the content top
  yOfOffset(offset) {
    const r = this.charRect(offset);
    return r ? r.bottom - this.content.getBoundingClientRect().top : 0;
  }
  offsetAtScroll() {
    const top = this.frame.scrollTop;
    return this.lowerBound((o) => this.yOfOffset(o) > top + 1);
  }
  scrollToOffset(offset) {
    const r = this.charRect(offset);
    if (!r) { this.frame.scrollTop = 0; return; }
    const y = r.top - this.content.getBoundingClientRect().top;
    this.frame.scrollTop = Math.max(0, y - 4);
  }

  // ------------------------------------------------------------------ navigation
  showPage(animate = this.animate) {
    const c = this.content;
    c.style.transition = animate && !matchMedia('(prefers-reduced-motion: reduce)').matches ? 'transform .16s ease-out' : 'none';
    c.style.transform = `translateX(${-this.page * this.pw}px)`;
  }

  setPage(p, { animate = true, notify = true } = {}) {
    this.page = this.alignPage(Math.max(0, Math.min(this.pageCount - 1, p)));
    this.showPage(animate);
    this.anchor = this.mode === 'paged' ? this.offsetAtPage(this.page) : this.anchor;
    if (notify) this.emit('page');
  }

  /** @returns 'moved' | 'end' (need next section) */
  next() {
    if (this.mode === 'scroll') {
      const f = this.frame;
      if (f.scrollTop + f.clientHeight >= f.scrollHeight - 2) return 'end';
      f.scrollBy({ top: f.clientHeight * 0.9, behavior: 'smooth' });
      return 'moved';
    }
    if (this.page + this.cols >= this.pageCount) return 'end';
    this.setPage(this.page + this.cols);
    return 'moved';
  }

  /** @returns 'moved' | 'start' (need previous section) */
  prev() {
    if (this.mode === 'scroll') {
      const f = this.frame;
      if (f.scrollTop <= 1) return 'start';
      f.scrollBy({ top: -f.clientHeight * 0.9, behavior: 'smooth' });
      return 'moved';
    }
    if (this.page <= 0) return 'start';
    this.setPage(this.page - this.cols);
    return 'moved';
  }

  gotoOffset(offset, { animate = false } = {}) {
    this.anchor = Math.max(0, Math.min(offset, this.total));
    if (this.mode === 'paged') {
      this.page = this.alignPage(Math.min(this.pageCount - 1, this.pageOfOffset(this.anchor)));
      this.showPage(animate);
    } else this.scrollToOffset(this.anchor);
    this.emit('page');
  }

  gotoEnd() {
    if (this.mode === 'paged') this.setPage(this.pageCount - 1, { animate: false });
    else { this.frame.scrollTop = this.frame.scrollHeight; this.syncAnchorFromScroll(); }
  }

  gotoId(id) {
    const el = this.content.querySelector(`[id="${CSS.escape(id)}"]`);
    if (!el) return false;
    // offset of the first text node inside (or after) the element
    const w = document.createTreeWalker(this.content, NodeFilter.SHOW_TEXT);
    let found = null;
    while (w.nextNode()) {
      const pos = el.compareDocumentPosition(w.currentNode);
      if (el === w.currentNode.parentNode || (pos & Node.DOCUMENT_POSITION_CONTAINED_BY) || (pos & Node.DOCUMENT_POSITION_FOLLOWING)) { found = w.currentNode; break; }
    }
    const n = found && this.nodes.find((x) => x.node === found);
    if (!n) return false;
    this.gotoOffset(n.start);
    return true;
  }

  syncAnchorFromScroll() {
    if (this.mode === 'scroll') { this.anchor = this.offsetAtScroll(); this.emit('page'); }
  }

  /** current reading offset (first character on screen) */
  currentOffset() { return this.anchor; }

  /** [start, end) offsets of the text on the current page */
  pageRange() {
    if (this.mode !== 'paged') {
      const s = this.anchor;
      const bottom = this.frame.scrollTop + this.frame.clientHeight;
      const e = this.lowerBound((o) => this.yOfOffset(o) > bottom);
      return [s, Math.max(s, e)];
    }
    const s = this.offsetAtPage(this.page);
    const e = this.page + this.cols >= this.pageCount ? this.total : this.offsetAtPage(this.page + this.cols);
    return [s, e];
  }

  textAt(offset, n) {
    let { node, start } = this.nodeAt(offset) || {};
    if (!node) return '';
    let out = '';
    let i = this.nodes.findIndex((x) => x.node === node);
    let from = offset - start;
    while (out.length < n && i < this.nodes.length) {
      out += this.nodes[i].node.nodeValue.slice(from, from + n - out.length);
      from = 0;
      i++;
    }
    return out;
  }

  fullText() { return this.nodes.map((x) => x.node.nodeValue).join(''); }

  /** Range spanning text offsets [a,b) for highlighting */
  rangeFor(a, b) {
    if (!this.nodes.length) return null;
    const s = this.nodeAt(a);
    const e = this.nodeAt(Math.max(a, b - 1));
    const r = document.createRange();
    r.setStart(s.node, Math.min(a - s.start, s.node.nodeValue.length));
    r.setEnd(e.node, Math.min(b - e.start, e.node.nodeValue.length));
    return r;
  }

  skipBlank(offset) {
    const t = this.fullText();
    let o = offset;
    while (o < t.length && isWs(t[o])) o++;
    return o;
  }
}
