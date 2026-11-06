import { test } from 'node:test';
import assert from 'node:assert/strict';
import { continueCandidate, ago, DONE_PERCENT } from '../js/core/recent.js';

const e = (id, touched, percent, extra = {}) => ({ id, touched, percent, ...extra });

test('continue candidate is the most recently touched unfinished book', () => {
  const list = [e('a', 100, 10), e('b', 300, 55), e('c', 200, 20)];
  assert.equal(continueCandidate(list).id, 'b');
});

test('finished, hidden, uploading and never-touched books are skipped', () => {
  const list = [
    e('done', 900, DONE_PERCENT), e('hid', 800, 5, { hidden: true }), e('up', 700, 5, { uploading: true }),
    e('never', 0, 0), e('ok', 100, 30),
  ];
  assert.equal(continueCandidate(list).id, 'ok');
  assert.equal(continueCandidate([e('x', 5, 99.5)]), null);
  assert.equal(continueCandidate([]), null);
});

test('a just-opened book at 0% can be continued (opened but not yet read)', () => {
  assert.equal(continueCandidate([e('fresh', 50, 0)]).id, 'fresh');
});

test('ties keep the first entry', () => {
  assert.equal(continueCandidate([e('a', 10, 1), e('b', 10, 2)]).id, 'a');
});

test('ago words', () => {
  const now = Date.UTC(2026, 10, 6, 12, 0, 0);
  const t = (sec) => now - sec * 1000;
  assert.equal(ago(t(5), now), '刚刚');
  assert.equal(ago(t(60 * 5), now), '5 分钟前');
  assert.equal(ago(t(3600 * 3), now), '3 小时前');
  assert.equal(ago(t(86400 * 1.5), now), '昨天');
  assert.equal(ago(t(86400 * 6), now), '6 天前');
  assert.match(ago(t(86400 * 90), now), /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(ago(now + 5000, now), '刚刚', 'clock skew (future) does not produce negative text');
});
