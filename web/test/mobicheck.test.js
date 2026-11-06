// mobicheck.js: DRM / truncation / KFX / Print Replica detection and link parsing, on synthetic in-memory PDB files.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  mobiFormatOf, inspectMobi, parseExth, sniffImageType, parseFilepos, parseKindlePos, PEEK_RECORD0, MOBI_MESSAGES,
} from '../js/parse/mobicheck.js';

const enc = (s) => Uint8Array.from(s, (c) => c.charCodeAt(0));
const be32 = (v) => [(v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255];

/** minimal PDB+MOBI: record 0 = PalmDOC header + MOBI header (+ optional EXTH), then `nText` text records and one image record */
function makeMobi({ encryption = 0, compression = 2, nText = 3, type = 'BOOK', creator = 'MOBI', exth = null, mobiTag = 'MOBI', rec1 = null } = {}) {
  let ex = new Uint8Array(0);
  if (exth) {
    const body = Uint8Array.from(exth.flatMap(([t, data]) => [...be32(t), ...be32(8 + data.length), ...data]));
    ex = Uint8Array.from([...enc('EXTH'), ...be32(12 + body.length), ...be32(exth.length), ...body]);
  }
  const r0 = new Uint8Array(248 + ex.length);
  const dv = new DataView(r0.buffer);
  dv.setUint16(0, compression); dv.setUint16(8, nText); dv.setUint16(10, 4096); dv.setUint16(12, encryption);
  r0.set(enc(mobiTag), 16); dv.setUint32(20, 232); dv.setUint32(28, 65001); dv.setUint32(36, 6);
  if (exth) dv.setUint32(128, 0x40);
  r0.set(ex, 248);
  const records = [r0];
  for (let i = 0; i < nText; i++) records.push(new Uint8Array(300).fill(65 + i));
  records.push(new Uint8Array(500).fill(1));
  records.push(Uint8Array.from([0xe9, 0x8e, 0x0d, 0x0a]));   // EOF record, like real files
  if (rec1) records[1] = Uint8Array.from(rec1);   // record 1 = the one sniffed for Print Replica
  const n = records.length;
  const out = new Uint8Array(78 + 8 * n + 2 + records.reduce((a, r) => a + r.length, 0));
  const o = new DataView(out.buffer);
  out.set(enc(type), 60); out.set(enc(creator), 64); o.setUint16(76, n);
  let pos = 78 + 8 * n + 2;
  records.forEach((r, i) => { o.setUint32(78 + i * 8, pos); out.set(r, pos); pos += r.length; });
  return out;
}
const judge = (bytes, extra = {}) => inspectMobi({ head: bytes.subarray(0, 78 + 8 * 64 + 2 + PEEK_RECORD0), fileSize: bytes.length, ...extra });

test('extension mapping', () => {
  for (const n of ['a.mobi', 'A.AZW3', 'b.azw', 'x/y.PRC']) assert.equal(mobiFormatOf(n), 'mobi', n);
  for (const n of ['a.epub', 'mobi', 'a.mobi.txt', null]) assert.equal(mobiFormatOf(n), null, String(n));
});

test('a plain MOBI is accepted', () => {
  const r = judge(makeMobi());
  assert.equal(r.ok, true);
  assert.equal(r.numTextRecords, 3);
  assert.equal(r.compression, 2);
  assert.equal(r.version, 6);
});

test('DRM: PalmDOC encryption field != 0 is rejected (types 1 and 2), with a clear message', () => {
  for (const e of [1, 2, 0xffff]) {
    const r = judge(makeMobi({ encryption: e }));
    assert.equal(r.ok, false, `enc ${e}`);
    assert.equal(r.code, 'drm');
    assert.match(r.message, /DRM/);
  }
});

test('DRM in the KF8 half of a combined file is rejected', () => {
  const bytes = makeMobi();
  const kf8Rec0 = new Uint8Array(64); new DataView(kf8Rec0.buffer).setUint16(12, 2);
  assert.equal(judge(bytes, { boundaryRec0: kf8Rec0 }).code, 'drm');
  assert.equal(judge(bytes, { boundaryRec0: new Uint8Array(64) }).ok, true);
});

test('DRM wins over truncation (a protected, cut file reports DRM)', () => {
  const full = makeMobi({ encryption: 2 });
  assert.equal(judge(full.subarray(0, full.length - 400)).code, 'drm');
});

test('truncated files are rejected at several cut points', () => {
  const full = makeMobi();
  for (const keep of [0.95, 0.8, 0.6, 0.3]) {
    const cut = full.subarray(0, Math.floor(full.length * keep));
    const r = inspectMobi({ head: cut, fileSize: cut.length });
    assert.equal(r.ok, false, `keep ${keep}`);
    assert.ok(['truncated', 'corrupt'].includes(r.code), `${keep}: ${r.code}`);
  }
  const cutTable = full.subarray(0, 100);   // inside the record table
  assert.equal(inspectMobi({ head: cutTable, fileSize: cutTable.length }).code, 'truncated');
});

test('last record cut to zero bytes is still detected as truncated', () => {
  const full = makeMobi();
  const n = new DataView(full.buffer).getUint16(76);
  const lastStart = new DataView(full.buffer).getUint32(78 + (n - 1) * 8);
  const cut = full.subarray(0, lastStart);
  assert.equal(inspectMobi({ head: cut, fileSize: cut.length }).code, 'truncated');
});

test('wrong / foreign formats get specific messages', () => {
  assert.equal(judge(enc('PK\x03\x04' + 'x'.repeat(200))).code, 'kfx');
  assert.equal(judge(Uint8Array.from([...enc('CONT'), ...new Uint8Array(200)])).code, 'kfx');
  assert.equal(judge(Uint8Array.from([0xea, ...enc('DRMION'), ...new Uint8Array(200)])).code, 'kfx');
  assert.equal(judge(makeMobi({ type: 'TEXt', creator: 'REAd' })).code, 'palmdoc-text');
  assert.equal(judge(makeMobi({ type: 'PDF ', creator: 'CARX' })).code, 'print-replica');
  assert.equal(judge(makeMobi({ rec1: enc('%MOP') }), { rec1: enc('%MOP') }).code, 'print-replica');
  assert.equal(judge(makeMobi({ type: 'BOOK', creator: 'XXXX' })).code, 'not-mobi');
  assert.equal(judge(enc('hello world, plain text '.repeat(10))).code, 'not-mobi');
  assert.equal(judge(makeMobi({ mobiTag: 'XXXX' })).ok, false);
  assert.equal(inspectMobi({ head: new Uint8Array(0), fileSize: 0 }).code, 'not-mobi');
  assert.equal(judge(makeMobi({ compression: 7 })).code, 'bad-compression');
});

test('every failure code has a Chinese user message', () => {
  for (const code of ['drm', 'truncated', 'kfx', 'print-replica', 'not-mobi', 'corrupt']) assert.match(MOBI_MESSAGES[code], /[一-鿿]/);
});

test('EXTH: boundary (121) and fixed layout (122) are read; garbage EXTH is harmless', () => {
  const r = judge(makeMobi({ exth: [[121, be32(7)], [122, enc('true')], [100, enc('author')]] }));
  assert.equal(r.ok, true);
  assert.equal(r.exth.boundary, 7);
  assert.equal(r.exth.fixedLayout, 'true');
  assert.deepEqual(parseExth(new Uint8Array(10)), {});
  const bad = makeMobi({ exth: [[121, be32(7)]] });
  assert.doesNotThrow(() => judge(bad.subarray(0, 400)));
});

test('image sniffing', () => {
  assert.equal(sniffImageType(Uint8Array.from([0xff, 0xd8, 0xff, 0xe0])), 'image/jpeg');
  assert.equal(sniffImageType(Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 1])), 'image/png');
  assert.equal(sniffImageType(enc('GIF89a')), 'image/gif');
  assert.equal(sniffImageType(enc('<svg xmlns')), '');
  assert.equal(sniffImageType(null), '');
});

test('link forms', () => {
  assert.deepEqual(parseFilepos('#filepos0000012345'), { digits: '0000012345', number: 12345, id: 'filepos0000012345' });
  assert.equal(parseFilepos('filepos:0000000007').number, 7);
  assert.equal(parseFilepos('https://x/filepos1'), null);
  assert.equal(parseFilepos('#filepos12a'), null);
  const k = parseKindlePos('#kindle:pos:fid:000A:off:0000000010');
  assert.equal(k.fid, 10); assert.equal(k.off, 32); assert.equal(k.raw, 'kindle:pos:fid:000A:off:0000000010');
  assert.equal(parseKindlePos('kindle:pos:fid:zz:off:1'), null);
  assert.equal(parseKindlePos('javascript:alert(1)'), null);
});
