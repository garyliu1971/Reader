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

test('comic (image-only) books: percent follows (section + fraction) / n, never a false 100 from a few title characters', () => {
  const n = 151;
  const lens = [17, ...new Array(n - 1).fill(0)];   // cover title only, 150 picture sections: the real-world case
  assert.equal(positionToPercent(lens, 1, 0), 100, 'text lengths alone say "finished" at section 1');
  const opts = { comic: true };
  assert.equal(positionToPercent(lens, 0, 0, opts), 0, 'cover = 0');
  assert.ok(Math.abs(positionToPercent(lens, 0, 1, opts) - (1 / n) * 100) < 1e-9, 'end of the cover section is one section in');
  assert.ok(Math.abs(positionToPercent(lens, 61, 0.5, opts) - (61.5 / n) * 100) < 1e-9);
  assert.ok(positionToPercent(lens, n - 1, 0, opts) < 100, 'start of the last section is not finished');
  assert.equal(positionToPercent(lens, n - 1, 1, opts), 100, 'end of the last section');
  assert.equal(positionToPercent(lens, 999, 0, opts), 100);
  assert.equal(positionToPercent(lens, -3, 0, opts), 0);
  assert.equal(positionToPercent([], 0, 0.5, opts), 0);
  let prev = -1;
  for (let s = 0; s < n; s++) {
    for (const f of [0, 0.25, 0.5, 1]) {
      const p = positionToPercent(lens, s, f, opts);
      assert.ok(p >= prev, `monotonic at ${s}/${f}: ${p} < ${prev}`);
      assert.ok(s === n - 1 && f === 1 ? p === 100 : p < 100, `no false 100 at ${s}/${f}: ${p}`);
      prev = p;
    }
    const p0 = positionToPercent(lens, s, 0, opts);
    const at = percentToPosition(lens, p0, opts);
    assert.equal(at.s, s, `round trip of section ${s}`);
    assert.ok(at.f < 1e-9, `fraction noise only: ${at.f}`);
    const back = percentToPosition(lens, positionToPercent(lens, s, 0.5, opts), opts);
    assert.equal(back.s, s);
    assert.ok(Math.abs(back.f - 0.5) < 1e-6);
  }
  assert.deepEqual(percentToPosition(lens, 100, opts), { s: n - 1, f: 1 });
  assert.deepEqual(percentToPosition(lens, 0, opts), { s: 0, f: 0 });
  assert.deepEqual(percentToPosition([], 50, opts), { s: 0, f: 0 });
  // text books are unaffected when the option is absent or false
  assert.equal(positionToPercent([100, 0, 300, 600], 2, 0.5, { comic: false }), 25);
  assert.deepEqual(percentToPosition([100, 0, 300, 600], 100, { comic: false }), { s: 3, f: 1 });
});

test('comic with a single section (webtoon, one long xhtml): the fraction is the progress, never stuck at 0', () => {
  const opts = { comic: true };
  for (const lens of [[0], [12]]) {   // no text at all, or a few characters of caption
    assert.equal(positionToPercent(lens, 0, 0, opts), 0, 'cover = 0');
    assert.equal(positionToPercent(lens, 0, 0.5, opts), 50);
    assert.ok(Math.abs(positionToPercent(lens, 0, 0.9, opts) - 90) < 1e-9);
    assert.equal(positionToPercent(lens, 0, 1, opts), 100, 'end of the page');
    assert.equal(positionToPercent(lens, 0, 2, opts), 100, 'fraction is clamped');
    assert.equal(positionToPercent(lens, 0, -1, opts), 0);
    assert.deepEqual(percentToPosition(lens, 0, opts), { s: 0, f: 0 });
    assert.deepEqual(percentToPosition(lens, 100, opts), { s: 0, f: 1 });
    const back = percentToPosition(lens, 37, opts);
    assert.equal(back.s, 0);
    assert.ok(Math.abs(back.f - 0.37) < 1e-9);
  }
});

test('multi-section comic: section starts split the range evenly and never reach 100 before the last section ends', () => {
  const opts = { comic: true };
  const lens = [3, 0, 0, 0];
  assert.deepEqual([0, 1, 2, 3].map((s) => positionToPercent(lens, s, 0, opts)), [0, 25, 50, 75]);
  assert.equal(positionToPercent(lens, 3, 0.999, opts) < 100, true);
  assert.equal(positionToPercent(lens, 3, 1, opts), 100);
  assert.deepEqual(percentToPosition(lens, 50, opts), { s: 2, f: 0 });
  assert.deepEqual(percentToPosition(lens, 62.5, opts), { s: 2, f: 0.5 });
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
