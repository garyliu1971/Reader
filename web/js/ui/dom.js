// Tiny DOM helpers: element builder, toasts, modal dialogs/drawers with focus handling.

export function h(tag, attrs = null, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === false || v === null || v === undefined) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k in el && k !== 'list' && k !== 'form') { try { el[k] = v; } catch { el.setAttribute(k, v); } }
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

let toastBox = null;
export function toast(msg, { ms = 3200, action = null } = {}) {
  if (!toastBox) {
    toastBox = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
    document.body.append(toastBox);
  }
  const t = h('div', { class: 'toast' }, h('span', { text: msg }));
  if (action) {
    t.append(h('button', { class: 'btn small', type: 'button', onclick: () => { action.run(); t.remove(); } }, action.label));
  }
  toastBox.append(t);
  setTimeout(() => t.remove(), action ? Math.max(ms, 8000) : ms);
  return t;
}

// Escape closes the topmost overlay whatever has focus (a disabled/removed button drops focus to <body>).
const panelStack = [];
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && panelStack.length) { e.stopPropagation(); e.preventDefault(); panelStack[panelStack.length - 1].close(); }
}, true);

/**
 * Overlay panel (drawer or dialog). Opens on top of the page without touching the reading layout.
 * @returns {{el, close, body}}
 */
export function openPanel({ title, side = 'dialog', content, onClose = null, label = null, wide = false }) {
  const opener = document.activeElement;
  const body = h('div', { class: 'panel-body' }, content);
  const closeBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': '关闭', onclick: () => close() }, '✕');
  const panel = h('section', { class: `panel panel-${side}${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': label || title },
    h('header', { class: 'panel-head' }, h('h2', { text: title }), closeBtn), body);
  const scrim = h('div', { class: 'scrim', onclick: () => close() });
  const root = h('div', { class: `overlay-root side-${side}` }, scrim, panel);
  document.body.append(root);
  requestAnimationFrame(() => root.classList.add('open'));
  let closed = false;
  const entry = { close: () => close() };
  panelStack.push(entry);
  function close() {
    if (closed) return;
    closed = true;
    const i = panelStack.indexOf(entry);
    if (i >= 0) panelStack.splice(i, 1);
    root.classList.remove('open');
    setTimeout(() => root.remove(), 180);
    if (opener && opener.focus) { try { opener.focus({ preventScroll: true }); } catch { /* ignore */ } }
    if (onClose) onClose();
  }
  setTimeout(() => (panel.querySelector('[autofocus]') || closeBtn).focus({ preventScroll: true }), 30);
  return { el: root, close, body, panel };
}

export function confirmDialog(message, { ok = '确定', cancel = '取消', danger = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); p.close(); } };
    const p = openPanel({
      title: '请确认', side: 'dialog',
      content: [h('p', { text: message }), h('div', { class: 'row end' },
        h('button', { class: 'btn', type: 'button', onclick: () => finish(false) }, cancel),
        h('button', { class: `btn primary${danger ? ' danger' : ''}`, type: 'button', autofocus: true, onclick: () => finish(true) }, ok))],
      onClose: () => { if (!done) { done = true; resolve(false); } },
    });
  });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
export const debounce = (fn, ms) => { let t; const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; d.flush = () => { clearTimeout(t); fn(); }; d.cancel = () => clearTimeout(t); return d; };

export function fmtSize(n) {
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + ' GB';
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB';
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(0) + ' KB';
  return n + ' B';
}
