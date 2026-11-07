import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ivUrl, encodeRel } from '../js/core/handoff.js';
import { DavClient } from '../js/sync/webdav.js';

const REL = '小说/チ。-关于地球的运动- - 卷01.epub';
const params = (u) => new URLSearchParams(u.slice(u.indexOf('#') + 1));

test('ivUrl: same-origin /iv/ hash with open, src, bid, back; dir only when the book declares one', () => {
  const u = ivUrl(REL);
  assert.ok(u.startsWith('/iv/#open='));
  assert.ok(u.endsWith('&back=/reader/'));
  const p = params(u);
  assert.equal(p.get('open'), REL);
  assert.equal(p.get('src'), 'dav');
  assert.equal(p.get('bid'), 'remote:' + REL);
  assert.equal(p.get('back'), '/reader/');
  assert.equal(p.has('dir'), false);
  assert.equal(params(ivUrl(REL, { ppd: 'rtl' })).get('dir'), 'rtl');
  assert.equal(params(ivUrl(REL, { ppd: 'ltr' })).get('dir'), 'ltr');
  assert.equal(params(ivUrl(REL, { ppd: 'weird' })).has('dir'), false);
  assert.equal(params(ivUrl(REL, { ppd: null })).has('dir'), false);
  assert.equal(ivUrl(REL, { bid: 'remote:' + REL }), u);
});

test('ivUrl: CJK, spaces, ideographic full stop, # & + % ? survive a decode round trip and never break the hash', () => {
  for (const rel of [REL, 'a b/c#d&e+f%g?h.epub', '漫画/第 1 卷。#1&2.epub', 'x/ü/é.epub']) {
    const u = ivUrl(rel);
    assert.equal(u.split('#').length, 2, 'only one #: ' + u);
    const p = params(u);
    assert.equal(p.get('open'), rel);
    assert.equal(p.get('bid'), 'remote:' + rel);
    assert.equal([...p.keys()].join(','), 'open,src,bid,back', 'no stray parameters from the path');
    assert.ok(!/[ +]/.test(u), 'no raw space or plus');
  }
});

test('encodeRel is exactly what DavClient.url sends for the same path', () => {
  const dav = new DavClient({ baseUrl: 'https://dav.example/base' });
  for (const rel of [REL, 'a b/c#d&e+f%g?h.epub', 'single.epub', '漫画\\子目录\\卷 2.epub', '/lead//double/x.epub']) {
    assert.equal(dav.url(rel), 'https://dav.example/base/' + encodeRel(rel));
  }
});

test('ivUrl never carries credentials and rejects path tricks', () => {
  const u = ivUrl(REL, { ppd: 'rtl', bid: 'remote:' + REL });
  assert.ok(!/@|password|user|token|auth/i.test(u.replace(/%[0-9A-F]{2}/g, '')), u);
  assert.ok(!/^[a-z]+:/i.test(u), 'relative, same-origin');
  assert.equal(ivUrl(''), '');
  assert.equal(ivUrl(null), '');
  assert.throws(() => ivUrl('a/../b.epub'), /illegal/);
  assert.throws(() => ivUrl('./b.epub'), /illegal/);
  assert.throws(() => ivUrl('///'), /illegal/);
});
