// Local library: import, metadata, covers, opening books. Everything persists in IndexedDB (see store/db.js).
import { db, deleteBookData } from './store/db.js';
import { localBookId, remoteBookId } from './core/bookid.js';
import { extOf } from './core/names.js';
import { openEpub } from './parse/epub.js';
import { parseTxtAsync, txtBook, richTextBook } from './parse/textbooks.js';
import { openPdf } from './parse/pdf.js';

export const FORMATS = { '.txt': 'txt', '.md': 'md', '.markdown': 'md', '.epub': 'epub', '.pdf': 'pdf', '.html': 'html', '.htm': 'html' };
export const ACCEPT = Object.keys(FORMATS).join(',');
export const MAX_IMPORT_BYTES = 300 * 1024 * 1024;

const stem = (name) => name.replace(/\.[^.]+$/, '');

export function formatOf(name) { return FORMATS[extOf(name)] || null; }

async function makeThumb(blob, maxW = 240) {
  try {
    const bmp = await createImageBitmap(blob);
    const scale = Math.min(1, maxW / bmp.width);
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(bmp.width * scale));
    c.height = Math.max(1, Math.round(bmp.height * scale));
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close && bmp.close();
    return await new Promise((res) => c.toBlob((b) => res(b), 'image/jpeg', 0.82));
  } catch {
    return blob.size < 400 * 1024 ? blob : null;
  }
}

export async function getBook(id) { return db.get('books', id); }
export async function allBooks() { return db.getAll('books'); }

/**
 * Import a File/Blob. `remote` = {rel, size, mtime} when it was downloaded from the WebDAV library (id = remote:<rel>).
 * @returns {{meta, duplicate: boolean}}
 */
export async function importBlob(file, { name = file.name, remote = null, onStatus = () => {} } = {}) {
  const format = formatOf(name);
  if (!format) throw new Error(`暂不支持 ${extOf(name) || '该'} 格式（支持 EPUB、TXT、Markdown、HTML、PDF）`);
  if (file.size > MAX_IMPORT_BYTES) throw new Error(`文件过大（${Math.round(file.size / 1048576)} MB，上限 ${MAX_IMPORT_BYTES / 1048576} MB）`);
  if (file.size === 0) throw new Error('文件是空的');
  const id = remote ? remoteBookId(remote.rel) : localBookId(name, file.size);
  const existing = await db.get('books', id);
  if (existing && (await db.get('files', id))) return { meta: existing, duplicate: true };

  const meta = {
    id, name, format, size: file.size, title: stem(name), author: '', addedAt: Date.now(), openedAt: 0, percent: 0,
    lens: [], headings: null, pageCount: 0, source: remote ? 'remote' : 'local', rel: remote ? remote.rel : null,
    remoteSize: remote ? remote.size : 0, remoteMtime: remote ? remote.mtime : 0,
  };
  let coverBlob = null;
  onStatus(`解析 ${name}…`);
  if (format === 'epub') {
    const book = await openEpub(file);
    await book.scan((f) => onStatus(`解析 ${name}… ${Math.round(f * 100)}%`));
    meta.lens = book.lens;
    meta.headings = book.sections.map((s) => s.heading || '');
    if (book.title) meta.title = book.title;
    meta.author = book.author;
    try { const c = await book.cover(); if (c) coverBlob = await makeThumb(c); } catch { /* generated tile instead */ }
    book.close();
  } else if (format === 'txt') {
    const r = await parseTxtAsync(await file.arrayBuffer());
    meta.lens = r.lens;
    meta.encoding = r.encoding;
  } else if (format === 'pdf') {
    const book = await openPdf(file);
    meta.pageCount = book.pageCount;
    meta.lens = book.lens;
    if (book.title && book.title.trim()) meta.title = book.title.trim();
    meta.author = book.author || '';
    try { const c = await book.cover(); if (c) coverBlob = c; } catch { /* generated tile */ }
    book.close();
  } else {
    const book = await richTextBook(new Uint8Array(await file.arrayBuffer()), format, stem(name));
    meta.lens = book.lens;
    if (book.title) meta.title = book.title;
  }
  onStatus('保存到本地…');
  await db.put('files', { id, blob: file instanceof File ? file.slice(0, file.size, file.type) : file });
  if (coverBlob) await db.put('covers', { id, blob: coverBlob });
  await db.put('books', meta);
  return { meta, duplicate: false };
}

/** Open a stored book: returns {meta, book}. */
export async function openStored(id) {
  const meta = await db.get('books', id);
  const row = await db.get('files', id);
  if (!meta || !row) throw new Error('本地没有这本书的文件（可能已被清理），请重新导入或从云端下载');
  const blob = row.blob;
  let book;
  if (meta.format === 'epub') {
    book = await openEpub(blob);
    if (meta.lens && meta.lens.length === book.sections.length) {
      book.lens = meta.lens;
      book.sections.forEach((s, i) => { s.heading = meta.headings ? meta.headings[i] : ''; });
      book.finishTitles();
    } else {
      await book.scan();
      meta.lens = book.lens;
      await db.put('books', meta);
    }
  } else if (meta.format === 'txt') {
    const r = await parseTxtAsync(await blob.arrayBuffer());
    book = txtBook({ ...r, title: meta.title });
  } else if (meta.format === 'pdf') {
    book = await openPdf(blob);
  } else {
    book = await richTextBook(new Uint8Array(await blob.arrayBuffer()), meta.format, meta.title);
  }
  if (!book.title) book.title = meta.title;
  return { meta, book };
}

export async function coverUrl(id) {
  const row = await db.get('covers', id);
  return row ? URL.createObjectURL(row.blob) : null;
}

export async function removeBook(id) { await deleteBookData(id); }

export async function touchOpened(meta) { meta.openedAt = Date.now(); await db.put('books', meta); }

// ---- progress (local, per book)
export const getProgress = (bid) => db.get('progress', bid);
export async function saveProgress(rec) {
  await db.put('progress', rec);
  const meta = await db.get('books', rec.bid);
  if (meta) { meta.percent = rec.percent; if (rec.updated * 1000 > (meta.lastRead || 0)) meta.lastRead = rec.updated * 1000; await db.put('books', meta); }
}

// ---- bookmarks (local only; Qt never syncs bookmarks.json, so neither do we)
export const getBookmarks = (bid) => db.byIndex('bookmarks', 'bid', bid);
export const addBookmark = (bm) => db.put('bookmarks', bm);
export const deleteBookmark = (id) => db.del('bookmarks', id);
