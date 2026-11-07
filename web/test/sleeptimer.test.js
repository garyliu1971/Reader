import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeTimer, remainingMs, isDue, formatRemaining, timerLabel, SLEEP_MINUTES } from '../js/core/sleeptimer.js';
import { navDir } from '../js/core/search.js';

test('makeTimer: minutes, chapter, and invalid choices', () => {
  assert.deepEqual(SLEEP_MINUTES, [15, 30, 45, 60]);
  assert.deepEqual(makeTimer(30, 1000), { kind: 'time', endAt: 1000 + 30 * 60000 });
  assert.deepEqual(makeTimer('45', 0), { kind: 'time', endAt: 45 * 60000 });
  assert.deepEqual(makeTimer('chapter', 5), { kind: 'chapter' });
  assert.equal(makeTimer(7, 0), null);
  assert.equal(makeTimer(null, 0), null);
  assert.equal(makeTimer('abc', 0), null);
});

test('remainingMs / isDue follow the clock; chapter and no timer never expire by clock', () => {
  const t = makeTimer(15, 1000);
  assert.equal(remainingMs(t, 1000), 15 * 60000);
  assert.equal(remainingMs(t, t.endAt - 1), 1);
  assert.equal(remainingMs(t, t.endAt + 999), 0);
  assert.equal(isDue(t, t.endAt - 1), false);
  assert.equal(isDue(t, t.endAt), true);
  assert.equal(isDue(t, t.endAt + 1e9), true);   // a throttled background tab wakes up late
  const c = makeTimer('chapter', 0);
  assert.equal(remainingMs(c, 1e12), null);
  assert.equal(isDue(c, 1e12), false);
  assert.equal(remainingMs(null, 0), null);
  assert.equal(isDue(null, 0), false);
});

test('formatRemaining rounds up and switches to h:mm:ss', () => {
  assert.equal(formatRemaining(0), '00:00');
  assert.equal(formatRemaining(1), '00:01');
  assert.equal(formatRemaining(59001), '01:00');
  assert.equal(formatRemaining(15 * 60000), '15:00');
  assert.equal(formatRemaining(60 * 60000), '1:00:00');
  assert.equal(formatRemaining(3661000), '1:01:01');
  assert.equal(formatRemaining(-5), '00:00');
});

test('timerLabel', () => {
  assert.equal(timerLabel(null, 0), '定时');
  assert.equal(timerLabel({ kind: 'chapter' }, 0), '本章末');
  assert.equal(timerLabel(makeTimer(15, 0), 60000), '14:00');
});

test('navDir: F3 / Ctrl+G step, Shift reverses, other keys ignored', () => {
  assert.equal(navDir({ key: 'F3' }), 1);
  assert.equal(navDir({ key: 'F3', shiftKey: true }), -1);
  assert.equal(navDir({ key: 'g', ctrlKey: true }), 1);
  assert.equal(navDir({ key: 'G', metaKey: true, shiftKey: true }), -1);
  assert.equal(navDir({ key: 'g' }), 0);
  assert.equal(navDir({ key: 'g', ctrlKey: true, altKey: true }), 0);
  assert.equal(navDir({ key: 'F3', ctrlKey: true }), 0);
  assert.equal(navDir({ key: 'Enter' }), 0);
});
