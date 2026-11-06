import { test } from 'node:test';
import assert from 'node:assert/strict';
import { continueCandidate, touchedAt, ago, DONE_PERCENT, DWELL_MS } from '../js/core/recent.js';

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

test('a book at 0% that was read (dwell stamp) can be continued', () => {
  assert.equal(continueCandidate([e('fresh', 50, 0)]).id, 'fresh');
});

test('touchedAt: newest of saved progress, dwell stamp and remote progress', () => {
  assert.equal(touchedAt({ lastRead: 100, readAt: 300 }, 200), 300);
  assert.equal(touchedAt({ lastRead: 100, readAt: 0 }, 200), 200);
  assert.equal(touchedAt({ lastRead: 400, readAt: 300 }, 0), 400);
  assert.equal(touchedAt({}, 0), 0);
  assert.equal(touchedAt({ lastRead: undefined, readAt: undefined }), 0);
});

test('merely opening a book (legacy openedAt, nothing read) is never touched', () => {
  const b = { lastRead: 0, readAt: 0, openedAt: 9_000 };   // openedAt: stamped on every open by older builds
  assert.equal(touchedAt(b, 0), 0);
  const real = { lastRead: 1_000, readAt: 0 };
  const entries = [e('read', touchedAt(real, 0), 40), e('openedOnly', touchedAt(b, 0), 0)];
  assert.equal(continueCandidate(entries).id, 'read', 'a later open must not displace the real last-read book');
  assert.equal(continueCandidate([entries[1]]), null, 'a shelf where nothing was read has no continue card');
});

test('a newer remote progress still wins over a local dwell stamp, an older one does not', () => {
  const local = { lastRead: 1_000, readAt: 5_000 };
  assert.equal(touchedAt(local, 7_000), 7_000);
  assert.equal(touchedAt(local, 2_000), 5_000);
});

test('dwell threshold is a short but non-trivial time', () => {
  assert.ok(DWELL_MS >= 10_000 && DWELL_MS <= 120_000);
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
