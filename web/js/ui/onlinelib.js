// Online library dialog (Project Gutenberg via the app's /books/v1 proxy). Logic lives in core/onlinelib.js; this file is DOM glue.
import { h, toast, openPanel, fmtSize } from './dom.js';
import { icon } from './icons.js';
import { getCloudCfg } from '../store/cloudcfg.js';
import {
  LANGS, initialState, reduce, hasMore, canAutoRetry, retryDelay, searchBooks, downloadBook, coverUrl, bookFileName, originOf, addedIds,
  LibraryError, offlineError, missingTokenError,
} from '../core/onlinelib.js';

export const ONLINE_LIB_LABEL = '在线书库（Gutenberg）';

const asLibErr = (e) => (e instanceof LibraryError ? e : e && e.name === 'QuotaExceededError' ? new LibraryError('http', '浏览器存储空间不足。') : new LibraryError('http', e && e.message ? e.message : String(e)));

/**
 * ctx: {getBooks(): Promise<meta[]>, importFile(file, {origin, onStatus}): Promise<meta>, openBook(id), openSettings()}
 * @returns the panel handle ({close, ...})
 */
export function openOnlineLibrary(ctx) {
  let s = initialState('en');
  let ctl = null;
  let timer = null;
  let countdown = null;
  let retryAt = 0;
  let adding = null;                 // gutenberg id being downloaded/imported (one at a time)
  const added = new Map();           // gutenberg id -> local book id
  const rowErr = new Map();          // gutenberg id -> message
  let progressBtn = null;
  let focusIdx = -1;

  const input = h('input', { type: 'search', id: 'ol-q', placeholder: '书名或作者，如 Pride and Prejudice', 'aria-label': '搜索书名或作者', autocomplete: 'off', autofocus: true, enterKeyHint: 'search' });
  const langSel = h('select', { 'aria-label': '书籍语言', onchange: () => { if (s.q) submit(); } }, LANGS.map(([v, l]) => h('option', { value: v, text: l })));
  const images = h('input', { type: 'checkbox', id: 'ol-images' });
  const form = h('form', { class: 'ol-form', role: 'search', onsubmit: (ev) => { ev.preventDefault(); submit(); } },
    h('div', { class: 'ol-line' }, input, langSel, h('button', { class: 'btn primary', type: 'submit' }, icon('search', 18), '搜索')),
    h('label', { class: 'ol-opt' }, images, h('span', { text: '含插图（大文件，更耗流量）' })));
  const status = h('div', { class: 'ol-status', role: 'status', 'aria-live': 'polite' });
  const list = h('ul', { class: 'ol-list', 'aria-label': '搜索结果' });
  const moreBox = h('div', { class: 'ol-more' });
  const foot = h('p', { class: 'hint', text: '书籍来自 Project Gutenberg（公版书），经本应用服务器转发；使用与“在线朗读”相同的令牌。' });
  const panel = openPanel({ title: ONLINE_LIB_LABEL, side: 'dialog', wide: true, content: [form, status, list, moreBox, foot], onClose: cleanup });
  panel.panel.classList.add('ol-panel');

  function cleanup() { if (ctl) ctl.abort(); clearTimeout(timer); clearInterval(countdown); }

  async function refreshAdded() {
    try {
      const books = await ctx.getBooks();
      const ids = addedIds(books);
      for (const b of books) { const g = /^gutenberg:(\d+)$/.exec(b.origin || ''); if (g && ids.has(g[1])) added.set(g[1], b.id); }
      for (const k of [...added.keys()]) if (!ids.has(k)) added.delete(k);
    } catch { /* shelf state unknown: buttons simply stay "add" */ }
    render();
  }

  function submit() {
    clearTimeout(timer); clearInterval(countdown);
    dispatch({ type: 'search', q: input.value, lang: langSel.value });
  }

  function dispatch(ev) {
    s = reduce(s, ev);
    if ((ev.type === 'search' || ev.type === 'retry') && s.status === 'loading') fetchPage(false);
    else if (ev.type === 'more' && s.more) fetchPage(true);
    if (ev.type === 'failed' && canAutoRetry(s)) scheduleRetry();
    render();
  }

  function fetchPage(append) {
    if (ctl) ctl.abort();
    ctl = new AbortController();
    const seq = s.seq;
    const token = getCloudCfg().token;
    searchBooks({ q: s.q, lang: s.lang, offset: append ? s.items.length : 0, token, baseHref: location.href, signal: ctl.signal })
      .then((r) => { if (append) focusIdx = s.items.length; dispatch({ type: 'loaded', seq, total: r.total, items: r.items, append }); })
      .catch((e) => { if (e && e.name === 'AbortError') return; dispatch({ type: 'failed', seq, error: e instanceof LibraryError ? e : offlineError(), more: append }); });
  }

  function scheduleRetry() {
    clearTimeout(timer); clearInterval(countdown);
    const secs = retryDelay(s.error);
    retryAt = Date.now() + secs * 1000;
    timer = setTimeout(() => { clearInterval(countdown); dispatch({ type: 'retry' }); }, secs * 1000);
    countdown = setInterval(render, 1000);
  }

  function cover(it) {
    const box = h('div', { class: 'ol-cover', 'aria-hidden': 'true' });
    const ph = () => box.replaceChildren(h('span', { class: 'ol-ph', text: Array.from(it.title)[0] || '书' }));
    const img = h('img', { src: coverUrl(it.id), alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', width: 56, height: 80 });
    img.addEventListener('error', ph);
    box.append(img);
    return box;
  }

  async function add(it) {
    if (adding) return;
    const token = getCloudCfg().token;
    adding = it.id; rowErr.delete(it.id); render();
    const setLabel = (t) => { if (progressBtn) progressBtn.lastChild.textContent = t; };
    setLabel('0%');
    try {
      if (!token) throw missingTokenError();
      const name = bookFileName(it.id, it.title);
      const bytes = await downloadBook({
        id: it.id, images: images.checked, token, baseHref: location.href,
        onProgress: (n, total) => setLabel(total ? `下载 ${Math.min(100, Math.round((n / total) * 100))}%` : `下载 ${fmtSize(n)}`),
      });
      setLabel('处理中…');
      const meta = await ctx.importFile(new File([bytes], name, { type: 'application/epub+zip' }), { origin: originOf(it.id), onStatus: setLabel });
      added.set(it.id, meta.id);
      toast(`已加入书架：${it.title}`);
    } catch (e) {
      rowErr.set(it.id, asLibErr(e));
    } finally {
      adding = null; progressBtn = null; render();
      const again = list.querySelector(`[data-gid="${it.id}"] button`);   // the re-render dropped focus
      if (again && panel.el.isConnected) again.focus({ preventScroll: true });
    }
  }

  function row(it) {
    const authors = it.authors.length ? it.authors.join('; ') : '作者未知';
    const err = rowErr.get(it.id);
    let action;
    if (added.has(it.id)) {
      action = h('div', { class: 'ol-act' }, h('span', { class: 'ol-added' }, icon('eye', 16), '已添加'),
        h('button', { class: 'btn small', type: 'button', 'aria-label': `打开 ${it.title}`, onclick: () => { panel.close(); ctx.openBook(added.get(it.id)); } }, '打开'));
    } else {
      const busy = adding === it.id;
      const btn = h('button', { class: 'btn primary small', type: 'button', disabled: !!adding, 'aria-label': `加入书架：${it.title}`, 'aria-busy': busy ? 'true' : false, onclick: () => add(it) },
        busy ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : icon('plus', 16), h('span', { text: busy ? '下载中…' : '加入书架' }));
      if (busy) progressBtn = btn;
      action = h('div', { class: 'ol-act' }, btn);
    }
    return h('li', { class: 'ol-row', dataset: { gid: it.id } }, cover(it),
      h('div', { class: 'ol-info' }, h('div', { class: 'ol-title', text: it.title }), h('div', { class: 'ol-auth', text: authors }), h('div', { class: 'ol-id', text: `ID ${it.id}${it.lang ? ' · ' + it.lang : ''}` }),
        err ? h('p', { class: 'ol-err', role: 'alert' }, err.message, err.kind === 'auth' ? h('button', { class: 'btn small', type: 'button', onclick: () => ctx.openSettings() }, '打开设置') : null) : null),
      action);
  }

  function statusView() {
    const e = s.error;
    switch (s.status) {
      case 'idle': return h('p', { class: 'hint', text: '输入书名或作者，按回车搜索。' });
      case 'loading': return h('p', { class: 'ol-busy' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), `正在搜索“${s.q}”…`);
      case 'empty': return h('p', { class: 'ol-msg', text: `没有找到“${s.q}”相关的书。试试换个关键词或语言。` });
      case 'error': return errorView(e);
      default: return s.error ? errorView(e) : (s.total ? h('p', { class: 'hint', text: `共 ${s.total} 本，已显示 ${s.items.length} 本` }) : null);
    }
  }

  function errorView(e) {
    const kids = [h('p', { class: 'ol-msg', text: e.message })];
    if (e.kind === 'auth') kids.push(h('button', { class: 'btn small', type: 'button', onclick: () => ctx.openSettings() }, '打开设置'));
    else if (e.kind === 'loading') {
      const left = Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
      kids.push(h('p', { class: 'hint', text: canAutoRetry(s) ? `${left} 秒后自动重试…` : '多次重试仍未就绪。' }));
      if (!canAutoRetry(s)) kids.push(h('button', { class: 'btn small', type: 'button', onclick: () => { s = { ...s, retries: 0 }; submit(); } }, '重试'));
    } else kids.push(h('button', { class: 'btn small', type: 'button', onclick: () => (s.status === 'error' ? submit() : dispatch({ type: 'more' })) }, '重试'));
    return h('div', { class: `ol-state ${e.kind}`, role: e.kind === 'loading' ? null : 'alert' }, kids);
  }

  function render() {
    progressBtn = null;
    status.replaceChildren(...[statusView()].filter(Boolean));
    list.replaceChildren(...s.items.map(row));
    moreBox.replaceChildren(...(hasMore(s) ? [h('button', { class: 'btn', type: 'button', disabled: s.more, onclick: () => dispatch({ type: 'more' }) }, s.more ? '加载中…' : `加载更多（${s.items.length}/${s.total}）`)] : []));
    if (focusIdx >= 0) { const b = list.children[focusIdx] && list.children[focusIdx].querySelector('button'); if (b) b.focus({ preventScroll: false }); focusIdx = -1; }
  }

  render();
  refreshAdded();
  return panel;
}
