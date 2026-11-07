// Choice panel for an image-only book (comic epub): open it in ImageViewer (/iv/, same origin) or read it here anyway.
// Used by the shelf (before the reader opens) and by the reader (deep links / history entries that still point at the book).
import { h, openPanel } from './dom.js';
import { ivUrl } from '../core/handoff.js';

/**
 * @param o {title, rel (library path or null), ppd, bid,
 *           onUpload: function | null (local-only book: offer the existing 上传到云端书库 action),
 *           onAnyway(), onCancel()}  Closing the panel by any other way than 仍在此阅读 calls onCancel.
 */
export function comicPrompt({ title, rel, ppd = null, bid = null, onUpload = null, onAnyway, onCancel = () => {} }) {
  let chosen = false;
  const choose = (fn) => () => { chosen = true; p.close(); fn(); };
  const url = rel ? ivUrl(rel, { ppd, bid }) : '';
  const actions = [];
  if (url) {
    // a plain link (new tab): the PWA scope is /reader/, so /iv/ is another app; navigation keeps the reader's history untouched
    actions.push(h('a', { class: 'btn primary', href: url, target: '_blank', rel: 'noopener', autofocus: true, onclick: () => setTimeout(() => p.close(), 0) }, '在 ImageViewer 打开'));
  } else if (onUpload) {
    actions.push(h('button', { class: 'btn primary', type: 'button', autofocus: true, onclick: choose(onUpload) }, '先上传到云端书库…'));
  }
  actions.push(h('button', { class: 'btn', type: 'button', autofocus: !actions.length, onclick: choose(onAnyway) }, '仍在此阅读'));
  const p = openPanel({
    title: '这是一本图片型漫画',
    side: 'dialog',
    content: [
      h('p', { text: title }),
      h('p', { class: 'hint', text: '整本书都是图片页。在这里只能一页一页地看，没有双页和从右往左翻页；ImageViewer 更适合阅读漫画。' }),
      !url && h('p', { class: 'hint', text: onUpload ? '这本书只在本机，要用 ImageViewer 阅读，需要先上传到云端书库。' : '这本书不在云端书库里，无法用 ImageViewer 打开（需要先开启 WebDAV 同步并上传）。' }),
      h('div', { class: 'row end' }, actions),
    ],
    onClose: () => { if (!chosen) onCancel(); },
  });
  return { ...p, dismiss: () => { chosen = true; p.close(); } };   // dismiss: close without onCancel (the page moved on by itself)
}
