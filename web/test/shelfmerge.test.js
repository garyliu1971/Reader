import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  validateOverlay, merge, newOverlay, setField, setProgress, pruneProgress, toWire, wireText, reconcileOwn, contentKey, fieldValue, canon,
} from '../js/core/shelfmerge.js';
import { golden, NOW } from './helpers.js';

const strip = (v) => JSON.parse(JSON.stringify(v));

test('validate_overlay equals Python (views, clamping, skipped entries)', () => {
  const [va, issues] = validateOverlay(golden.overlay_inputs.a, NOW, { deviceId: 'dev-a' });
  assert.deepEqual(strip(va), golden.validate_a.view);
  assert.equal(issues.length, golden.validate_a.n_issues);
  assert.deepEqual(va._clock, ['dev-a']);                              // remote:b.txt future ts -> clock anomaly
  assert.equal(va.entries['remote:b.txt'].fav[1], 1);                  // clamped to the oldest value
});

test('merge equals Python, is commutative, idempotent, associative', () => {
  const [va] = validateOverlay(golden.overlay_inputs.a, NOW, { deviceId: 'dev-a' });
  const [vb] = validateOverlay(golden.overlay_inputs.b, NOW, { deviceId: 'dev-b' });
  const ab = merge([va, vb], NOW);
  assert.deepEqual(strip(ab), golden.merge_ab);
  assert.deepEqual(strip(merge([vb, va], NOW)), golden.merge_ba);
  assert.deepEqual(strip(merge([ab, ab], NOW)), golden.merge_ab);
  const [vc] = validateOverlay({ version: 1, entries: { 'remote:z.txt': { fav: [true, NOW - 1] } } }, NOW, { deviceId: 'dev-c' });
  assert.deepEqual(strip(merge([merge([va, vb], NOW), vc], NOW)), strip(merge([va, merge([vb, vc], NOW)], NOW)));
  // tie on ts: larger device id wins (hidden register tie in the fixture)
  assert.equal(fieldValue(ab, 'local:' + '1'.repeat(40), 'hidden'), false);
});

test('set_field / set_progress / to_wire / wire text equal Python', () => {
  const m = golden.merge_ab;
  const ov = newOverlay('web-123abc', 'My Phone', 'gary');
  const t1 = setField(ov, 'remote:a.epub', 'fav', true, { merged: m, nowMs: NOW });
  const t2 = setField(ov, 'remote:a.epub', 'hidden', true, { nowMs: NOW, meta: { fp: 'ff00', title: 'T' } });
  setProgress(ov, 'remote:a.epub', 61.25, NOW / 1000);
  setProgress(ov, 'local:' + '2'.repeat(40), 3.0, NOW / 1000 - 5);
  assert.deepEqual([t1, t2], golden.set_field_ts);
  assert.deepEqual(toWire(ov), golden.wire);
  assert.deepEqual(JSON.parse(wireText(ov)), golden.wire);
  assert.equal(wireText(ov), JSON.stringify(JSON.parse(wireText(ov)), null, 1));   // stable key order, indent 1
});

test('click always beats what the user just saw (hybrid clock) even with an old local clock', () => {
  const ov = newOverlay('web-aaaaaa');
  const seen = { entries: { 'remote:a.epub': { fav: [true, NOW + 5000, 'dev-x'] } }, _view: true, progress: {}, _clock: [] };
  const ts = setField(ov, 'remote:a.epub', 'fav', false, { merged: seen, nowMs: NOW });
  assert.equal(ts, NOW + 5001);
  const merged = merge([ov, seen], NOW);
  assert.equal(fieldValue(merged, 'remote:a.epub', 'fav'), false);
});

test('to_wire only writes whitelisted keys of this device', () => {
  const ov = newOverlay('web-123abc', 'h', 'gary');
  ov._secret = 'x';
  ov.entries['remote:a.txt'] = { fav: [true, 5, 'web-123abc'], other: [true, 5], hidden: [true, 6, 'someone-else'], extra: { a: 1 } };
  ov.entries['not an id'] = { fav: [true, 5, 'web-123abc'] };
  ov.progress['remote:a.txt'] = { percent: 10, updated: 7, device: 'web-123abc' };
  ov.progress['remote:b.txt'] = { percent: 10, updated: 7, device: 'other-dev' };
  const w = toWire(ov);
  assert.deepEqual(Object.keys(w).sort(), ['device_id', 'entries', 'hostname', 'progress', 'user', 'version']);
  assert.deepEqual(w.entries, { 'remote:a.txt': { fav: [true, 5] } });
  assert.deepEqual(Object.keys(w.progress), ['remote:a.txt']);
  assert.equal(JSON.stringify(w).includes('secret'), false);
  assert.throws(() => toWire({ entries: {} }), /device_id/);
});

test('prune keeps the most recent 500-style digest, tie -> larger id; Python parity', () => {
  const ov = newOverlay('web-x');
  for (let i = 0; i < 12; i++) setProgress(ov, 'local:' + i.toString(16).padStart(40, '0'), i, 1000 + (i % 5));
  pruneProgress(ov, 5);
  assert.deepEqual(Object.keys(ov.progress).sort(), golden.prune_kept);
});

test('validate rejects bad documents and keeps robust to junk', () => {
  assert.equal(validateOverlay(null)[0], null);
  assert.equal(validateOverlay([])[0], null);
  assert.equal(validateOverlay({ version: 2 }, NOW, { deviceId: 'a' })[0], null);
  assert.equal(validateOverlay({ version: true }, NOW, { deviceId: 'a' })[0], null);
  assert.equal(validateOverlay({}, NOW, { deviceId: '../x' })[0], null);
  assert.equal(validateOverlay({}, NOW)[0], null);                      // no device id anywhere
  const [v] = validateOverlay({ entries: 5, progress: [1] }, NOW, { deviceId: 'dev-a' });
  assert.deepEqual(v.entries, {});
  const [v2] = validateOverlay({ entries: { 'remote:a': { fav: [true, 1.5] } }, progress: { 'remote:a': { percent: 'x', updated: 5 } } }, NOW, { deviceId: 'dev-a' });
  assert.deepEqual(v2.entries, {});
  assert.deepEqual(v2.progress, {});
  // JSON-internal device_id is ignored when the file name supplies the writer
  const [v3] = validateOverlay({ device_id: 'evil', entries: { 'remote:a': { fav: [true, 5] } } }, NOW, { deviceId: 'dev-a' });
  assert.equal(v3.entries['remote:a'].fav[2], 'dev-a');
  // 100k junk entries are bounded
  const big = {};
  for (let i = 0; i < 25000; i++) big['remote:b' + i] = { fav: [true, 5] };
  const [v4, i4] = validateOverlay({ entries: big }, NOW, { deviceId: 'dev-a' });
  assert.equal(Object.keys(v4.entries).length, 20000);
  assert.ok(i4.some((s) => s.includes('too many')));
});

test('reconcileOwn: adopts newer remote values, flags when remote is behind', () => {
  const local = newOverlay('web-aaaaaa');
  setField(local, 'remote:a.epub', 'fav', true, { nowMs: NOW });
  setProgress(local, 'remote:a.epub', 10, NOW / 1000);
  // remote is a stale copy lacking the progress and with older fav
  const [stale] = validateOverlay({ entries: { 'remote:a.epub': { fav: [false, NOW - 100] } } }, NOW, { deviceId: 'web-aaaaaa' });
  let r = reconcileOwn(local, stale, NOW);
  assert.equal(r.adopted, null);
  assert.equal(r.behind, true);
  // remote is a newer clone: values adopted
  const [clone] = validateOverlay({ entries: { 'remote:a.epub': { fav: [false, NOW + 1000] } }, progress: { 'remote:a.epub': { percent: 50, updated: NOW / 1000 + 1 } } }, NOW, { deviceId: 'web-aaaaaa' });
  r = reconcileOwn(local, clone, NOW);
  assert.ok(r.adopted);
  assert.equal(fieldValue(r.adopted, 'remote:a.epub', 'fav'), false);
  assert.equal(r.adopted.progress['remote:a.epub'].percent, 50);
  // identical content: nothing to do
  const [same] = validateOverlay(toWire(local), NOW, { deviceId: 'web-aaaaaa' });
  r = reconcileOwn(local, same, NOW);
  assert.equal(r.adopted, null);
  assert.equal(r.behind, false);
  assert.equal(contentKey(local), contentKey(same, 'web-aaaaaa'));
  assert.equal(canon({ b: 1, a: [2, { d: 1, c: 2 }] }), '{"a":[2,{"c":2,"d":1}],"b":1}');
});
