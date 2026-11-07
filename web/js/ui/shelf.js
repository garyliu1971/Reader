// Shelf: grid of local books + (when sync is on) books that only exist in the WebDAV library.
import { h, toast, openPanel, confirmDialog, fmtSize } from './dom.js';
import { icon } from './icons.js';
import { allBooks, importBlob, removeBook, coverUrl, ACCEPT, formatOf } from '../library.js';
import { syncMgr } from '../sync/manager.js';
import { fieldValue } from '../core/shelfmerge.js';
import { storageEstimate, persistRequest } from '../store/db.js';
import { remoteBookId, aliasIds } from '../core/bookid.js';
import { continueCandidate, touchedAt, ago, DONE_PERCENT } from '../core/recent.js';
import { comicPrompt } from './comic.js';
import { openOnlineLibrary, ONLINE_LIB_LABEL } from './onlinelib.js';

const SORTS = [['recent', '最近阅读'], ['title', '书名'], ['added', '加入时间'], ['progress', '阅读进度']];
const FILTERS = [['all', '全部'], ['recent', '最近'], ['reading', '在读'], ['unread', '未读'], ['done', '读完'], ['fav', '收藏'], ['cloud', '云端'], ['hidden', '已隐藏']];
const DONE = DONE_PERCENT;
const coll = new Intl.Collator('zh-Hans-CN', { numeric: true, sensitivity: 'base' });

function hue(str) { let x = 0; for (const c of str) x = (x * 31 + c.codePointAt(0)) >>> 0; return x % 360; }
const stemOf = (name) => name.replace(/\.[^.]+$/, '');
const baseName = (rel) => rel.slice(rel.lastIndexOf('/') + 1);

export class ShelfView {
  /** ctx: {open(id), openSettings(), installPrompt()} */
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.q = '';
    this.sort = 'recent';
    this.filter = 'all';
    this.urls = [];
    this.entries = [];
    this.build();
    this.bindDrop();
    syncMgr.on(() => this.updateSyncLine());
    syncMgr.shelfChanged = () => { if (!this.root.hidden) this.render(); };
  }

  build() {
    this.search = h('input', { type: 'search', placeholder: '搜索书名 / 作者', 'aria-label': '搜索书架', oninput: () => { this.q = this.search.value.trim(); this.render(); } });
    this.sortSel = h('select', { 'aria-label': '排序', onchange: () => { this.sort = this.sortSel.value; localStorageSafe('set', 'shelf.sort', this.sort); this.render(); } },
      SORTS.map(([v, l]) => h('option', { value: v, text: l })));
    try { const s = localStorage.getItem('shelf.sort'); if (SORTS.some((x) => x[0] === s)) { this.sort = s; this.sortSel.value = s; } } catch { /* ignore */ }
    this.fileInput = h('input', { type: 'file', multiple: true, accept: ACCEPT, hidden: true, onchange: async () => { const fs = [...this.fileInput.files]; this.fileInput.value = ''; await this.importFiles(fs); } });
    this.chips = h('div', { class: 'chips', role: 'group', 'aria-label': '筛选' });
    this.syncLine = h('div', { class: 'sync-line', role: 'status' });
    this.grid = h('div', { class: 'grid', role: 'list' });
    this.contBox = h('div', { class: 'continue-box' });
    this.syncBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '立即同步', onclick: () => this.syncNow() }, icon('sync'));
    this.installBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '安装应用', hidden: true, onclick: () => this.ctx.installPrompt() }, icon('install'));
    const head = h('header', { class: 'shelf-head' },
      h('div', { class: 'shelf-title' }, h('h1', { text: 'PyReader Web' }),
        this.installBtn, this.syncBtn,
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': '设置', onclick: () => this.ctx.openSettings() }, icon('gear')),
        h('button', { class: 'btn primary', type: 'button', onclick: () => this.fileInput.click() }, icon('plus', 18), '导入')),
      h('div', { class: 'shelf-tools' }, this.search, this.sortSel),
      h('div', { class: 'shelf-add' }, this.onlineButton()),
      this.chips, this.syncLine);
    this.root.replaceChildren(head, this.contBox, this.grid, this.fileInput);
    this.renderChips();
  }

  /** entry to the online library (Gutenberg); one node per place */
  onlineButton(kind = '') {
    return h('button', { class: `btn small online-lib-btn ${kind}`.trim(), type: 'button', onclick: () => this.openOnline() }, icon('cloud', 16), ONLINE_LIB_LABEL);
  }

  openOnline() {
    return openOnlineLibrary({
      getBooks: () => allBooks(),
      importFile: (file, { origin, onStatus }) => this.importOnline(file, { origin, onStatus }),
      openBook: (id) => { const e = this.entries.find((x) => x.id === id); if (e) this.openLocal(e); else this.ctx.open(id); },
      openSettings: () => this.ctx.openSettings(),
    });
  }

  /** a downloaded online-library epub goes through the same importBlob path as a picked file; origin marks it for 已添加 */
  async importOnline(file, { origin, onStatus }) {
    this.importing = (this.importing || 0) + 1;   // the update prompt asks before reloading mid-import
    try {
      persistRequest().catch(() => {});
      const r = await importBlob(file, { name: file.name, origin, onStatus });
      await this.render();
      return r.meta;
    } finally { this.importing--; }
  }

  renderChips() {
    this.chips.replaceChildren(...FILTERS.map(([v, l]) => h('button', { class: 'chip', type: 'button', 'aria-pressed': String(this.filter === v), onclick: () => { this.filter = v; this.renderChips(); this.render(); } }, l)));
  }

  setInstallable(on) { this.installBtn.hidden = !on; }

  updateSyncLine() {
    const st = syncMgr.status;
    const lib = syncMgr.lib;
    this.syncBtn.classList.toggle('spin', st.state === 'syncing');
    this.syncBtn.disabled = !syncMgr.enabled();
    this.syncBtn.title = syncMgr.enabled() ? '立即同步' : '在设置里开启 WebDAV 同步';
    this.syncLine.textContent = syncMgr.enabled()
      ? `${st.state === 'error' ? '⚠ ' : ''}${st.msg}${lib.problem ? ` · ${lib.problem}` : ''}` : '';
  }

  // ------------------------------------------------------------------ data
  async collect() {
    const books = await allBooks();
    const eng = syncMgr.engine;
    const merged = eng ? eng.mergedView() : { entries: {}, progress: {} };
    const digest = eng ? eng.digest() : {};
    const out = [];
    const have = new Set();
    for (const b of books) {
      have.add(b.id);
      const d = [digest[b.id], ...aliasIds(b).map((a) => digest[a])].filter(Boolean).sort((x, y) => y.updated - x.updated)[0];
      let percent = b.percent || 0;
      let remoteNote = '';
      if (d && d.updated * 1000 > (b.lastRead || 0) && Math.abs(d.percent - percent) >= 0.5) { percent = d.percent; remoteNote = `${d.device} 读到 ${d.percent.toFixed(1)}%`; }
      out.push({
        id: b.id, title: b.title, author: b.author, format: b.format, size: b.size, percent, remoteNote, cloud: false,
        lastRead: Math.max(b.lastRead || 0, d ? d.updated * 1000 : 0), touched: touchedAt(b, d ? d.updated * 1000 : 0), addedAt: b.addedAt, rel: b.rel, source: b.source, comic: !!b.comic, ppd: b.ppd || null,
        fav: !!fieldValue(merged, b.id, 'fav', false), hidden: !!fieldValue(merged, b.id, 'hidden', false),
      });
    }
    if (syncMgr.enabled()) {
      const unstable = new Set(syncMgr.lib.unstable || []);
      for (const f of syncMgr.lib.files || []) {
        const id = remoteBookId(f.rel);
        if (have.has(id)) continue;
        const d = digest[id];
        out.push({
          id, title: stemOf(baseName(f.rel)), author: '', format: formatOf(f.rel) || (f.rel.split('.').pop() || '').toLowerCase(), size: f.size,
          percent: d ? d.percent : 0, remoteNote: d ? `${d.device} 读到 ${d.percent.toFixed(1)}%` : '', cloud: true, uploading: unstable.has(f.rel),
          lastRead: d ? d.updated * 1000 : 0, touched: d ? d.updated * 1000 : 0, addedAt: (f.mtime || 0) * 1000, rel: f.rel, source: 'remote', mtime: f.mtime,
          fav: !!fieldValue(merged, id, 'fav', false), hidden: !!fieldValue(merged, id, 'hidden', false),
        });
      }
    }
    return out;
  }

  view(entries) {
    let list = entries.filter((e) => {
      if (this.filter === 'hidden') return e.hidden;
      if (e.hidden) return false;
      switch (this.filter) {
        case 'recent': return e.touched > 0;
        case 'reading': return e.percent > 0 && e.percent < DONE;
        case 'unread': return e.percent <= 0;
        case 'done': return e.percent >= DONE;
        case 'fav': return e.fav;
        case 'cloud': return e.cloud;
        default: return true;
      }
    });
    if (this.q) {
      const q = this.q.normalize('NFC').toLowerCase();
      list = list.filter((e) => `${e.title} ${e.author}`.normalize('NFC').toLowerCase().includes(q));
    }
    let by = {
      recent: (a, b) => (b.touched || b.addedAt) - (a.touched || a.addedAt),
      title: (a, b) => coll.compare(a.title, b.title),
      added: (a, b) => b.addedAt - a.addedAt,
      progress: (a, b) => b.percent - a.percent,
    }[this.sort];
    if (this.filter === 'recent') by = (a, b) => b.touched - a.touched;   // the 最近 chip is always newest-first
    return list.sort((a, b) => by(a, b) || coll.compare(a.title, b.title));
  }

  async render() {
    const seq = (this._seq = (this._seq || 0) + 1);
    const entries = await this.collect();
    if (seq !== this._seq) return;
    this.entries = entries;
    this.updateSyncLine();
    for (const u of this.urls) URL.revokeObjectURL(u);
    this.urls = [];
    const list = this.view(entries);
    this.grid.replaceChildren();
    this.grid.toggleAttribute('role', false);   // role=list only while it holds cards (an empty/message-only list fails axe aria-required-children)
    if (list.length) this.grid.setAttribute('role', 'list');
    this.renderContinue(entries);
    if (!entries.length) {
      this.grid.replaceChildren(h('div', { class: 'empty', style: 'grid-column:1/-1' },
        h('p', null, '书架是空的。'), h('p', null, '点右上角“导入”，或把 EPUB / MOBI / AZW3 / TXT / Markdown / HTML / PDF 文件拖到这个页面。'),
        h('p', null, this.onlineButton('primary')),
        h('p', { class: 'hint', text: '也可以在设置里连接 WebDAV，与桌面版 PyReader 共享书库和阅读进度。' })));
      return;
    }
    if (!list.length) { this.grid.replaceChildren(h('div', { class: 'empty', style: 'grid-column:1/-1' }, '没有符合条件的书。')); return; }
    for (const e of list) this.grid.append(this.card(e));
  }

  /** cover (generated tile, replaced by the real cover when the book has one) with badge/format decorations */
  makeCover(e) {
    const cover = h('div', { class: 'cover' });
    const letter = Array.from(e.title.trim())[0] || '书';
    const tile = h('div', { class: 'tile', 'aria-hidden': 'true', style: `background:linear-gradient(145deg,hsl(${hue(e.title)} 45% 38%),hsl(${(hue(e.title) + 40) % 360} 50% 28%))` },
      h('b', { text: letter }), h('span', { text: e.title }));
    cover.append(tile);
    if (!e.cloud) coverUrl(e.id).then((u) => { if (u) { this.urls.push(u); cover.replaceChildren(h('img', { src: u, alt: '', loading: 'lazy', decoding: 'async' })); this.decorate(cover, e); } }).catch(() => {});
    this.decorate(cover, e);
    return cover;
  }

  /** "继续阅读": the book you were last in, one tap from the top of the shelf */
  renderContinue(entries) {
    const e = !this.q && ['all', 'recent'].includes(this.filter) ? continueCandidate(entries) : null;
    if (!e) { this.contBox.replaceChildren(); return; }
    const pct = Math.max(0, Math.min(100, e.percent));
    // floor, not round: 96.6% must not read as 97% (finished) while the book is still offered
    const where = pct > 0 ? `读到 ${pct < 10 ? pct.toFixed(1) : Math.floor(pct)}%` : '刚开始';
    // remoteNote ("设备 读到 N%") is set exactly when the percentage comes from another device: it replaces `where`
    const sub = [e.remoteNote || where, ago(e.touched)].filter(Boolean).join(' · ');
    this.contBox.replaceChildren(h('section', { class: 'continue', 'aria-label': '继续阅读' },
      h('button', { class: 'continue-open', type: 'button', onclick: () => this.openEntry(e) },
        h('div', { class: 'mini-cover' }, this.makeCover(e)),
        h('div', { class: 'continue-info' },
          h('div', { class: 'kicker', text: e.cloud ? '继续阅读（云端）' : '继续阅读' }),
          h('div', { class: 'ct', text: e.title }),
          h('div', { class: 'bar', 'aria-hidden': 'true' }, h('i', { style: `width:${pct}%` })),
          h('div', { class: 'cs', text: sub })),
        h('span', { class: 'go', 'aria-hidden': 'true' }, icon('chevron', 22)))));
  }

  card(e) {
    const cover = this.makeCover(e);
    const pct = Math.max(0, Math.min(100, e.percent));
    const more = h('button', { class: 'more icon-btn', type: 'button', 'aria-label': `${e.title} 的更多操作`, onclick: (ev) => { ev.stopPropagation(); this.menu(e); } }, icon('more', 20));
    if (e.fav) cover.append(h('span', { class: 'favmark', 'aria-label': '已收藏' }, icon('starFill', 18)));
    const meta = h('div', { class: 'meta' },
      h('div', { class: 't', text: e.title }),
      e.author ? h('div', { class: 'a', text: e.author }) : null,
      h('div', { class: 'bar', 'aria-hidden': 'true' }, h('i', { style: `width:${pct}%` })),
      h('div', { class: 'pct', text: e.cloud ? (e.uploading ? '上传中…' : `云端 · ${fmtSize(e.size)}${e.remoteNote ? ` · ${e.remoteNote}` : ''}`) : `${pct.toFixed(pct && pct < 10 ? 1 : 0)}%${e.remoteNote ? ` · ${e.remoteNote}` : ''}`, title: e.remoteNote }));
    const btn = h('div', { class: `book${e.cloud ? ' cloud' : ''}`, role: 'listitem' },
      h('button', { class: 'book-open', type: 'button', disabled: !!e.uploading, onclick: () => this.openEntry(e) }, cover, meta),
    );
    // the "more" button is a sibling of the open button (nested buttons are invalid), positioned over the cover by CSS
    btn.append(more);
    return btn;
  }

  decorate(cover, e) {
    if (e.cloud) cover.append(h('span', { class: 'badge' }, icon('cloud', 12), '云端'));
    cover.append(h('span', { class: 'fmt', 'aria-hidden': 'true', text: e.format }));
  }

  async openEntry(e) {
    if (!e.cloud) { this.openLocal(e); return; }
    await this.downloadAndOpen(e);
  }

  /** an image-only book (comic epub) is offered to ImageViewer first; nothing is pushed to the history until the reader is chosen */
  openLocal(e) {
    if (!e.comic) { this.ctx.open(e.id); return; }
    comicPrompt({
      title: e.title, rel: e.rel, ppd: e.ppd, bid: e.id,
      onUpload: !e.rel && syncMgr.enabled() && e.source === 'local' ? () => this.uploadDialog(e) : null,
      onAnyway: () => this.ctx.open(e.id, { anyway: true }),
    });
  }

  async downloadAndOpen(e, { open = true } = {}) {
    if (!syncMgr.enabled()) return;
    const t = toast(`正在下载 ${e.title}…`, { ms: 600000 });
    const label = t.firstChild;
    try {
      const bytes = await syncMgr.engine.downloadBook(e.rel, e.size, {
        onProgress: (n, total) => { label.textContent = `正在下载 ${e.title}… ${total ? Math.round((n / total) * 100) + '%' : fmtSize(n)}`; },
      });
      label.textContent = `正在处理 ${e.title}…`;
      const file = new File([bytes], baseName(e.rel), { type: 'application/octet-stream' });
      const { meta } = await importBlob(file, { name: baseName(e.rel), remote: { rel: e.rel, size: e.size, mtime: e.mtime || 0 }, onStatus: (m) => { label.textContent = m; } });
      t.remove();
      await this.render();
      if (open) this.openLocal({ id: meta.id, title: meta.title, rel: meta.rel, ppd: meta.ppd || null, comic: !!meta.comic, source: meta.source, format: meta.format });
    } catch (err) {
      t.remove();
      toast(`下载失败：${err && err.message ? err.message : err}`, { ms: 6000 });
    }
  }

  menu(e) {
    const item = (label, ic, fn, cls = '') => h('button', { class: `menu-item ${cls}`, type: 'button', onclick: async () => { p.close(); await fn(); } }, icon(ic, 20), label);
    const items = [
      item(e.cloud ? '下载并打开' : '打开', e.cloud ? 'cloud' : 'eye', () => this.openEntry(e)),
      item(e.fav ? '取消收藏' : '收藏', e.fav ? 'star' : 'starFill', () => { syncMgr.setFlag(e.id, 'fav', !e.fav); return this.render(); }),
      item(e.hidden ? '取消隐藏' : '隐藏（可在“已隐藏”里找回）', 'eye', () => { syncMgr.setFlag(e.id, 'hidden', !e.hidden); return this.render(); }),
    ];
    if (!e.cloud && syncMgr.enabled() && e.source === 'local') items.push(item('上传到云端书库…', 'upload', () => this.uploadDialog(e)));
    if (!e.cloud) items.push(item('删除本地副本', 'trash', () => this.removeLocal(e), 'danger'));
    const p = openPanel({ title: e.title, side: 'dialog', content: [h('div', null, ...items), h('p', { class: 'hint', text: '删除只会清除本浏览器里的副本和书签，永远不会删除云端的任何文件。' })] });
  }

  async removeLocal(e) {
    if (!(await confirmDialog(`删除本地副本「${e.title}」？\n（本浏览器里的书、书签和进度会被清除；云端和其他设备不受影响。）`, { ok: '删除', danger: true }))) return;
    await removeBook(e.id);
    toast('已删除本地副本');
    await this.render();
  }

  uploadDialog(e) {
    const folder = h('input', { type: 'text', placeholder: '留空 = 放在 library 根目录', 'aria-label': '云端文件夹' });
    const name = h('input', { type: 'text', value: e.title.replace(/[\\/:]/g, '_') + '.' + e.format.replace('md', 'md'), 'aria-label': '云端文件名' });
    const msg = h('p', { class: 'hint' });
    const go = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
      go.disabled = true; msg.textContent = '上传中…';
      const { db } = await import('../store/db.js');
      const row = await db.get('files', e.id);
      const r = await syncMgr.engine.uploadBook({ name: name.value.trim(), folder: folder.value.trim(), data: row.blob, size: row.blob.size });
      msg.textContent = r.message;
      go.disabled = false;
      if (r.status === 'ok') { toast(r.message); p.close(); syncMgr.syncNow({ quiet: true }).then(() => this.render()); }
    } }, '上传');
    const p = openPanel({ title: '上传到云端书库', side: 'dialog', content: [
      h('p', { class: 'hint', text: '只会新增文件：先传到隐藏的 .part，再用 MOVE（Overwrite: F）改名；云端已有同名文件时拒绝上传，绝不覆盖，也不会删除任何东西。' }),
      h('label', { class: 'field' }, h('span', null, '文件夹'), folder), h('label', { class: 'field' }, h('span', null, '文件名'), name), msg,
      h('div', { class: 'row end' }, go)] });
  }

  // ------------------------------------------------------------------ import
  bindDrop() {
    let depth = 0;
    let overlay = null;
    const has = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
    window.addEventListener('dragenter', (e) => { if (this.root.hidden || !has(e)) return; e.preventDefault(); if (++depth === 1) { overlay = h('div', { class: 'drop-overlay', text: '松开以导入书籍' }); document.body.append(overlay); } });
    window.addEventListener('dragover', (e) => { if (this.root.hidden || !has(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
    window.addEventListener('dragleave', (e) => { if (!has(e)) return; if (--depth <= 0) { depth = 0; if (overlay) overlay.remove(); overlay = null; } });
    window.addEventListener('drop', async (e) => {
      if (this.root.hidden || !has(e)) return;
      e.preventDefault(); depth = 0; if (overlay) overlay.remove(); overlay = null;
      await this.importFiles([...e.dataTransfer.files]);
    });
  }

  async importFiles(files) {
    this.importing = (this.importing || 0) + 1;   // the update prompt asks before reloading mid-import
    try { await this._importFiles(files); } finally { this.importing--; }
  }

  async _importFiles(files) {
    if (!files.length) return;
    persistRequest().catch(() => {});
    let ok = 0, dup = 0;
    const t = toast('正在导入…', { ms: 600000 });
    const label = t.firstChild;
    for (const f of files) {
      if (!formatOf(f.name)) { toast(`跳过 ${f.name}：暂不支持该格式（支持 EPUB、MOBI/AZW3、TXT、MD、HTML、PDF）`, { ms: 5000 }); continue; }
      try {
        const r = await importBlob(f, { onStatus: (m) => { label.textContent = m; } });
        if (r.duplicate) dup++; else ok++;
        await this.render();
      } catch (err) {
        const quota = err && err.name === 'QuotaExceededError';
        toast(`导入 ${f.name} 失败：${quota ? '浏览器存储空间不足' : (err && err.message ? err.message : err)}`, { ms: 7000 });
      }
    }
    t.remove();
    if (ok || dup) toast(`导入完成：${ok} 本${dup ? `，${dup} 本已在书架` : ''}`);
    const est = await storageEstimate();
    if (est && est.quota && est.usage / est.quota > 0.85) toast('浏览器存储接近上限，建议删除不需要的本地副本', { ms: 6000 });
  }

  async syncNow() {
    if (!syncMgr.enabled()) { this.ctx.openSettings(); return; }
    await syncMgr.syncNow({ force: true });
    await this.render();
  }
}

function localStorageSafe(op, k, v) { try { if (op === 'set') localStorage.setItem(k, v); } catch { /* storage may be blocked */ } }
