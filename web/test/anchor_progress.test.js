import { test } from 'node:test';
import assert from 'node:assert/strict';
import { positionToPercent, percentToPosition, makeAnchor, resolveOffset, cumulative } from '../js/core/anchor.js';
import { mergeProgress, parseProgressBody, makeRecord, stampNow, decideRemote, OFFSET_UNKNOWN } from '../js/core/progress.js';

const lens = [100, 0, 300, 600];   // includes an empty section

test('percent <-> position round trip, monotonic, empty sections skipped', () => {
  assert.deepEqual(cumulative(lens), [0, 100, 100, 400, 1000]);
  assert.equal(positionToPercent(lens, 0, 0), 0);
  assert.equal(positionToPercent(lens, 0, 1), 10);
  assert.equal(positionToPercent(lens, 2, 0.5), 25);
  assert.equal(positionToPercent(lens, 3, 1), 100);
  let prev = -1;
  for (let p = 0; p <= 100; p += 0.5) {
    const { s, f } = percentToPosition(lens, p);
    assert.ok(lens[s] > 0 || p === 100, `section ${s} at ${p}% should not be empty`);
    const back = positionToPercent(lens, s, f);
    assert.ok(Math.abs(back - p) < 1e-9, `${p} -> ${back}`);
    assert.ok(back >= prev);
    prev = back;
  }
  assert.deepEqual(percentToPosition(lens, 100), { s: 3, f: 1 });
  assert.deepEqual(percentToPosition([], 50), { s: 0, f: 0 });
  assert.deepEqual(percentToPosition(lens, NaN), { s: 0, f: 0 });
});

test('anchor survives text changes via context, falls back to fraction', () => {
  const text = '甲乙丙丁戊己庚辛壬癸'.repeat(30) + '这里是锚点上下文附近的内容' + '后面还有很多'.repeat(20);
  const off = text.indexOf('锚点上下文');
  const a = makeAnchor(5, off, text);
  assert.equal(a.s, 5);
  assert.equal(resolveOffset(a, text), off);
  // text gets a prefix inserted (e.g. different sanitizing): context search relocates
  const shifted = '新增的一段' + text;
  assert.equal(resolveOffset(a, shifted), off + 5);
  // context gone: fraction fallback stays in range
  const other = 'x'.repeat(text.length * 2);
  const r = resolveOffset(a, other);
  assert.ok(r >= 0 && r <= other.length);
  assert.equal(resolveOffset({ s: 0, o: 99999, f: 0.5, c: '' }, 'abc'), 3);
});

test('progress merge: strictly newer wins, tie keeps local', () => {
  const a = { updated: 100, percent: 10 }, b = { updated: 100, percent: 20 }, c = { updated: 101, percent: 30 };
  assert.equal(mergeProgress(a, b), a);
  assert.equal(mergeProgress(a, c), c);
  assert.equal(mergeProgress(null, b), b);
  assert.equal(mergeProgress(a, null), a);
  assert.equal(decideRemote(a, c), 'adopt');
  assert.equal(decideRemote(a, b), 'keep');
  assert.equal(decideRemote(null, b), 'adopt');
  assert.equal(decideRemote(a, null), 'none');
});

test('parseProgressBody validates book_id, numbers and the 24h future limit', () => {
  const now = 1_800_000_000;
  const ok = JSON.stringify({ book_id: 'remote:a', offset: 99, percent: 42.5, chapter: '第一章', updated: now - 5, device: 'pc' });
  assert.deepEqual(parseProgressBody(ok, 'remote:a', now), { percent: 42.5, updated: now - 5, device: 'pc', chapter: '第一章' });
  assert.equal(parseProgressBody(ok, 'remote:other', now), null);
  assert.equal(parseProgressBody('not json', 'remote:a', now), null);
  assert.equal(parseProgressBody('[]', 'remote:a', now), null);
  const bad = (o) => JSON.stringify({ book_id: 'remote:a', percent: 1, updated: now, ...o });
  assert.equal(parseProgressBody(bad({ percent: -1 }), 'remote:a', now), null);
  assert.equal(parseProgressBody(bad({ percent: '5' }), 'remote:a', now), null);
  assert.equal(parseProgressBody(bad({ updated: 0 }), 'remote:a', now), null);
  assert.equal(parseProgressBody(bad({ updated: now + 25 * 3600 }), 'remote:a', now), null);
  assert.notEqual(parseProgressBody(bad({ updated: now + 3600 }), 'remote:a', now), null);
  assert.equal(parseProgressBody(bad({ percent: 250 }), 'remote:a', now).percent, 100);
});

test('makeRecord output has exactly the wire keys; stampNow uses server skew', () => {
  const r = makeRecord({ bid: 'remote:a', percent: 33.33333333, chapter: 'c', updated: 5, device: 'web' });
  assert.deepEqual(Object.keys(r).sort(), ['book_id', 'chapter', 'device', 'offset', 'percent', 'updated']);
  assert.equal(r.offset, OFFSET_UNKNOWN);   // out of range on purpose: the desktop then resolves by percent (0 would mean 'start of book')
  assert.equal(r.percent, 33.333);
  assert.equal(makeRecord({ bid: 'x', percent: 500, updated: 1, device: 'd' }).percent, 100);
  assert.equal(stampNow(1_000_000, 0), 1000);
  assert.equal(stampNow(1_000_000, -5000), 995);       // local clock ahead of the server by 5 s
  assert.equal(stampNow(1_000_000, NaN), 1000);
});
