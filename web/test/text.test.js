import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { decodeText, unwrapLatinText } from '../js/core/textenc.js';
import { findChapters, buildSections, sectionParagraphs, cn2int } from '../js/core/chapters.js';
import { golden, repoRoot } from './helpers.js';

test('UTF-8 (with and without BOM) and UTF-16', () => {
  const t = '你好，世界\r\n第二行\r\n';
  assert.deepEqual(decodeText(new TextEncoder().encode(t)), { text: '你好，世界\n第二行\n', encoding: 'utf-8' });
  const bom = Uint8Array.from([0xef, 0xbb, 0xbf, ...new TextEncoder().encode(t)]);
  assert.equal(decodeText(bom).text, '你好，世界\n第二行\n');
  const u16 = new Uint8Array(2 + t.length * 2);
  u16[0] = 0xff; u16[1] = 0xfe;
  for (let i = 0; i < t.length; i++) { u16[2 + i * 2] = t.charCodeAt(i) & 255; u16[3 + i * 2] = t.charCodeAt(i) >> 8; }
  assert.equal(decodeText(u16).encoding, 'utf-16le');
  assert.equal(decodeText(u16).text, '你好，世界\n第二行\n');
});

test('GB18030 and Big5 are detected (bytes produced by Python str.encode)', () => {
  const gbk = Uint8Array.from(Buffer.from('c4e3bac3a3accac0bde7a1a3d5e2cac7d2bbb8f6b2e2cad4a3accfa3cdfbc4dcb9bbd5fdc8b7cab6b1f0b1e0c2eba1a3b5dad2bbd5c220d6d8c9fa', 'hex'));
  const r = decodeText(gbk);
  assert.equal(r.encoding, 'gb18030');
  assert.equal(r.text, '你好，世界。这是一个测试，希望能够正确识别编码。第一章 重生');
  const big5 = Uint8Array.from(Buffer.from('b36fac4fa440add3c163c5e9a4a4a4e5aabab4fab8d5a141a7c6b1e6afe0b0f7a5bfbd54b0bbb4fabd73bd58a143b2c4a440b3b920adaba5cd', 'hex'));
  const r2 = decodeText(big5);
  assert.equal(r2.encoding, 'big5');
  assert.equal(r2.text, '這是一個繁體中文的測試，希望能夠正確偵測編碼。第一章 重生');
});

test('chapter detection equals the Python scan_chapters (sample book + synthetic + English)', () => {
  const sample = decodeText(new Uint8Array(readFileSync(join(repoRoot, '示例小说.txt')))).text;
  assert.equal(sample.length, golden.sample_len);
  const norm = (a) => a.map(([p, t]) => [p, t]);
  const withLead = (text, ch) => {
    const c = findChapters(text);
    if (c.length && c[0][0] !== 0) c.unshift([0, '开篇']);
    return norm(c);
  };
  assert.deepEqual(withLead(sample), golden.sample_chapters);
  assert.deepEqual(withLead(golden.synthetic_text), golden.synthetic_chapters);
  assert.deepEqual(withLead(golden.en_text), golden.en_chapters);
});

test('cn2int matches Python', () => {
  for (const [s, v] of golden.cn2int) assert.equal(cn2int(s), v, s);
});

test('buildSections: titles, continuation splitting and no-marker chunking', () => {
  const secs = buildSections(golden.synthetic_text);
  assert.ok(secs.length >= 7);
  assert.equal(secs[0].start, 0);
  for (let i = 1; i < secs.length; i++) assert.equal(secs[i].start, secs[i - 1].end);   // contiguous, no gaps
  assert.equal(secs[secs.length - 1].end, golden.synthetic_text.length);

  const plain = ('没有任何章节标记的一行文字。\n').repeat(10000);   // ~150k chars, no markers
  const chunks = buildSections(plain, 40000);
  assert.ok(chunks.length >= 3 && chunks.every((c) => c.end - c.start <= 40000 * 1.25 + 1));
  assert.match(chunks[0].title, /^第 1 部分$/);
  assert.equal(chunks[chunks.length - 1].end, plain.length);

  const long = '第一章 长章节\n' + ('很长的一段话。\n').repeat(12000) + '第二章 中间\n' + ('中间的内容很多很多。\n').repeat(30)
    + '第三章 结束\n' + ('结尾的内容也很多很多。\n').repeat(30);
  const ls = buildSections(long, 40000);
  const cont = ls.filter((s) => !s.toc);
  assert.ok(cont.length >= 1 && cont[0].title.endsWith('（续）'));
  assert.equal(ls.filter((s) => s.toc).length, 3);
});

test('sectionParagraphs trims indentation and drops empty lines', () => {
  const t = '　　第一段。\n\n  第二段。  \n';
  assert.deepEqual(sectionParagraphs(t, { start: 0, end: t.length }), ['第一段。', '第二段。']);
});

test('very long single paragraph is linear (no quadratic wrap)', () => {
  const huge = '字'.repeat(3_000_000);
  const t0 = performance.now();
  const secs = buildSections(huge);
  const paras = secs.map((s) => sectionParagraphs(huge, s));
  assert.ok(paras.length >= 1);
  assert.ok(performance.now() - t0 < 2000, 'took ' + (performance.now() - t0));
});

test('unwrapLatinText merges hard-wrapped paragraphs but leaves CJK and short-line text alone', () => {
  const line = 'The quick brown fox jumps over the lazy dog again and again.';
  const wrapped = `${line}\n${line}\nlast bit\n\n${line}\n${line}\n`;
  assert.equal(unwrapLatinText(wrapped).split('\n').length < wrapped.split('\n').length, true);
  const cjk = '中文一行\n中文一行\n\n中文又一行\n';
  assert.equal(unwrapLatinText(cjk), cjk);
  const poem = 'short\nlines\nhere\n\nmore\nshort\n';
  assert.equal(unwrapLatinText(poem), poem);
});
