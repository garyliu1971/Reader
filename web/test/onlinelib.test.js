import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { here } from './helpers.js';
import {
  endpoint, searchUrl, downloadUrl, bookFileName, classifyStatus, retryDelay, normalizeItems, originOf, gutenbergIdOf, addedIds, coverUrl,
  searchBooks, downloadBook, initialState, reduce, hasMore, canAutoRetry, LibraryError, MAX_AUTO_RETRIES,
} from '../js/core/onlinelib.js';

const BASE = 'https://x.example/reader/index.html#/';

test('endpoint is relative to the app dir like /tts/', () => {
  assert.equal(endpoint('search', BASE), 'https://x.example/books/v1/search');
  assert.equal(endpoint('search', 'http://127.0.0.1:8123/index.html'), 'http://127.0.0.1:8123/books/v1/search');
});

test('searchUrl: trims, encodes, clamps, falls back to en', () => {
  const u = new URL(searchUrl({ q: '  war & peace ', lang: 'fr', limit: 25, offset: 50 }, BASE));
  assert.equal(u.pathname, '/books/v1/search');
  assert.equal(u.searchParams.get('q'), 'war & peace');
  assert.equal(u.searchParams.get('lang'), 'fr');
  assert.equal(u.searchParams.get('limit'), '25');
  assert.equal(u.searchParams.get('offset'), '50');
  const v = new URL(searchUrl({ q: 'x', lang: 'xx', limit: 9999, offset: -4 }, BASE));
  assert.equal(v.searchParams.get('lang'), 'en');
  assert.equal(v.searchParams.get('limit'), '50');
  assert.equal(v.searchParams.get('offset'), '0');
});

test('downloadUrl: default noimages, images -> epub3, bad id rejected', () => {
  assert.equal(downloadUrl('1342', false, BASE), 'https://x.example/books/v1/download/1342?variant=noimages');
  assert.equal(downloadUrl('1342', true, BASE), 'https://x.example/books/v1/download/1342?variant=epub3');
  assert.throws(() => downloadUrl('../etc', false, BASE));
  assert.throws(() => downloadUrl('12a', false, BASE));
});

test('bookFileName: pg<id> - <title>.epub, sanitised and bounded', () => {
  assert.equal(bookFileName('1342', 'Pride and Prejudice'), 'pg1342 - Pride and Prejudice.epub');
  assert.equal(bookFileName('7', 'A/B: C*?"<>|\\ D'), 'pg7 - A B C D.epub');
  assert.equal(bookFileName('7', '  ...  '), 'pg7.epub');
  assert.equal(bookFileName('7', ''), 'pg7.epub');
  assert.equal(bookFileName('7', 'x\n\ty'), 'pg7 - x y.epub');
  assert.ok(Array.from(bookFileName('7', '书'.repeat(300))).length <= 100 + 'pg7 - .epub'.length);
});

test('origin helpers and added set', () => {
  assert.equal(originOf('84'), 'gutenberg:84');
  assert.equal(gutenbergIdOf('gutenberg:84'), '84');
  assert.equal(gutenbergIdOf('other:84'), null);
  assert.equal(gutenbergIdOf(undefined), null);
  assert.deepEqual([...addedIds([{ origin: 'gutenberg:1' }, { origin: null }, {}, { origin: 'gutenberg:x' }, { origin: 'gutenberg:2' }])].sort(), ['1', '2']);
  assert.equal(coverUrl('84'), 'https://www.gutenberg.org/cache/epub/84/pg84.cover.medium.jpg');
});

test('classifyStatus', () => {
  assert.equal(classifyStatus(401).kind, 'auth');
  assert.match(classifyStatus(401).message, /在线朗读/);
  assert.equal(classifyStatus(403).kind, 'auth');
  const q = classifyStatus(429, { error: 'rate', retryAfter: 120 });
  assert.equal(q.kind, 'quota'); assert.equal(q.retryAfter, 120); assert.match(q.message, /2 分钟/);
  assert.equal(classifyStatus(429, null).retryAfter, null);
  const l = classifyStatus(503, { error: 'catalog_loading', retryAfter: 5 });
  assert.equal(l.kind, 'loading'); assert.equal(retryDelay(l), 5);
  assert.equal(classifyStatus(503, null).kind, 'http');
  assert.equal(retryDelay(classifyStatus(503, { error: 'catalog_loading', retryAfter: 9999 })), 30);
  assert.equal(retryDelay(classifyStatus(503, { error: 'catalog_loading' })), 2);
  assert.equal(classifyStatus(500).kind, 'http');
});

test('normalizeItems drops junk and keeps ids as strings', () => {
  const r = normalizeItems([{ id: 84, title: ' Frankenstein ', authors: ['Shelley, Mary'], lang: 'en' }, { id: 'x' }, null, { id: '5', authors: [{ name: 'A' }, 3] }]);
  assert.deepEqual(r.map((i) => i.id), ['84', '5']);
  assert.equal(r[0].title, 'Frankenstein');
  assert.deepEqual(r[1].authors, ['A']);
  assert.equal(r[1].title, 'Gutenberg #5');
});

const res = (status, body, headers = {}) => ({ ok: status >= 200 && status < 300, status, headers: { get: (k) => headers[k.toLowerCase()] ?? null }, json: async () => body, body: null, arrayBuffer: async () => body });

test('searchBooks: sends bearer token, parses, maps errors, no network without token', async () => {
  let seen;
  const f = async (url, init) => { seen = { url, init }; return res(200, { total: 3, items: [{ id: 1, title: 'T', authors: [], lang: 'en' }] }); };
  const r = await searchBooks({ q: 'a', lang: 'en', token: 'tok', baseHref: BASE, fetch: f });
  assert.equal(r.total, 3); assert.equal(r.items.length, 1);
  assert.equal(seen.init.headers.Authorization, 'Bearer tok');
  let called = false;
  await assert.rejects(searchBooks({ q: 'a', token: '', baseHref: BASE, fetch: async () => { called = true; } }), (e) => e.kind === 'auth');
  assert.equal(called, false);
  await assert.rejects(searchBooks({ q: 'a', token: 't', baseHref: BASE, fetch: async () => res(503, { error: 'catalog_loading', retryAfter: 3 }) }), (e) => e.kind === 'loading' && e.retryAfter === 3);
  await assert.rejects(searchBooks({ q: 'a', token: 't', baseHref: BASE, fetch: async () => res(429, { retryAfter: 60 }) }), (e) => e.kind === 'quota');
  await assert.rejects(searchBooks({ q: 'a', token: 't', baseHref: BASE, fetch: async () => { throw new TypeError('Failed to fetch'); } }), (e) => e instanceof LibraryError && e.kind === 'offline');
});

test('downloadBook streams with progress and enforces limits', async () => {
  const parts = [new Uint8Array([1, 2]), new Uint8Array([3])];
  let i = 0;
  const body = { getReader: () => ({ read: async () => (i < parts.length ? { done: false, value: parts[i++] } : { done: true }), cancel: async () => {} }) };
  const seen = [];
  const f = async () => ({ ok: true, status: 200, headers: { get: (k) => (k.toLowerCase() === 'content-length' ? '3' : null) }, body });
  const out = await downloadBook({ id: '5', token: 't', baseHref: BASE, fetch: f, onProgress: (n, t) => seen.push([n, t]) });
  assert.deepEqual([...out], [1, 2, 3]);
  assert.deepEqual(seen, [[2, 3], [3, 3]]);
  await assert.rejects(downloadBook({ id: '5', token: 't', baseHref: BASE, maxBytes: 2, fetch: async () => ({ ok: true, status: 200, headers: { get: () => '9' }, body }) }), (e) => e.kind === 'http');
  await assert.rejects(downloadBook({ id: '5', token: 't', baseHref: BASE, fetch: async () => res(401, null) }), (e) => e.kind === 'auth');
});

test('state machine: search -> loaded -> more -> loaded; stale answers ignored', () => {
  let s = initialState('en');
  assert.equal(s.status, 'idle');
  s = reduce(s, { type: 'search', q: '  ', lang: 'en' });
  assert.equal(s.status, 'idle');
  s = reduce(s, { type: 'search', q: 'austen', lang: 'en' });
  assert.equal(s.status, 'loading');
  const seq1 = s.seq;
  s = reduce(s, { type: 'search', q: 'austen emma', lang: 'en' });   // supersedes
  s = reduce(s, { type: 'loaded', seq: seq1, total: 1, items: [{ id: '1' }], append: false });
  assert.equal(s.status, 'loading');   // stale ignored
  s = reduce(s, { type: 'loaded', seq: s.seq, total: 3, items: [{ id: '1' }, { id: '2' }], append: false });
  assert.equal(s.status, 'ready'); assert.ok(hasMore(s));
  s = reduce(s, { type: 'more' });
  assert.ok(s.more);
  assert.equal(reduce(s, { type: 'more' }), s);   // not twice
  s = reduce(s, { type: 'loaded', seq: s.seq, total: 3, items: [{ id: '2' }, { id: '3' }], append: true });
  assert.deepEqual(s.items.map((i) => i.id), ['1', '2', '3']);   // de-duplicated
  assert.ok(!hasMore(s)); assert.ok(!s.more);
  assert.equal(reduce(s, { type: 'more' }), s);   // nothing more
});

test('state machine: empty, errors, load-more error keeps the list, auto retry only for catalog_loading', () => {
  let s = reduce(initialState(), { type: 'search', q: 'zzz', lang: 'en' });
  s = reduce(s, { type: 'loaded', seq: s.seq, total: 0, items: [], append: false });
  assert.equal(s.status, 'empty');

  s = reduce(initialState(), { type: 'search', q: 'a', lang: 'en' });
  s = reduce(s, { type: 'failed', seq: s.seq, error: classifyStatus(503, { error: 'catalog_loading', retryAfter: 1 }) });
  assert.equal(s.status, 'error'); assert.ok(canAutoRetry(s));
  s = reduce(s, { type: 'retry' });
  assert.equal(s.status, 'loading'); assert.equal(s.retries, 1);
  s = reduce(s, { type: 'failed', seq: s.seq, error: classifyStatus(401) });
  assert.ok(!canAutoRetry(s));
  assert.equal(reduce(s, { type: 'retry' }), s);
  let t = { ...s, error: classifyStatus(503, { error: 'catalog_loading' }), retries: MAX_AUTO_RETRIES };
  assert.ok(!canAutoRetry(t));

  s = reduce(initialState(), { type: 'search', q: 'a', lang: 'en' });
  s = reduce(s, { type: 'loaded', seq: s.seq, total: 50, items: [{ id: '1' }], append: false });
  s = reduce(s, { type: 'more' });
  s = reduce(s, { type: 'failed', seq: s.seq, error: classifyStatus(429, { retryAfter: 30 }), more: true });
  assert.equal(s.status, 'ready'); assert.equal(s.items.length, 1); assert.equal(s.error.kind, 'quota'); assert.ok(!s.more);
});

test('sw.js never handles /books/ (authenticated downloads) and the new modules are precached', () => {
  const sw = readFileSync(join(here, '..', 'sw.js'), 'utf8');
  assert.match(sw, /pathname\.includes\('\/books\/'\)\) return;/);
  assert.ok(sw.indexOf("includes('/books/')") < sw.indexOf('e.respondWith'));
  assert.ok(sw.includes("'js/core/onlinelib.js'") && sw.includes("'js/ui/onlinelib.js'"));
  assert.match(sw, /const CACHE = 'pyreader-web-v14'/);
});
