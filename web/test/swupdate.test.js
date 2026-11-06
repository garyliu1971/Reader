// Update-prompt decisions (core/swupdate.js) and the constants sw.js has to repeat.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { here } from './helpers.js';
import { updateAction, shouldCheck, reloadAllowed, MSG_PING, MSG_SKIP, PING_TIMEOUT_MS } from '../js/core/swupdate.js';

const S = { hadController: true, hasWaiting: false, controllerSwapped: false, requested: false, reloaded: false };
const act = (o) => updateAction({ ...S, ...o });

test('nothing happens without a new worker', () => assert.equal(act({}), 'none'));

test('a waiting worker prompts but never reloads on its own', () => {
  assert.equal(act({ hasWaiting: true }), 'prompt');
  assert.equal(act({ hasWaiting: true, requested: true }), 'prompt');   // clicked, waiting for the worker to take over
});

test('reload only after the user asked AND the new worker took control', () => {
  assert.equal(act({ requested: true, controllerSwapped: true }), 'reload');
  assert.equal(act({ requested: true, controllerSwapped: true, hasWaiting: true }), 'reload');
});

test('a worker that took over without a click (old page, other tab) prompts instead of reloading', () => {
  assert.equal(act({ controllerSwapped: true }), 'prompt');
});

test('first install (no controller before) never prompts or reloads', () => {
  assert.equal(act({ hadController: false, hasWaiting: true }), 'none');
  assert.equal(act({ hadController: false, controllerSwapped: true, requested: true }), 'none');
});

test('after a reload was issued nothing more happens (loop guard)', () => {
  assert.equal(act({ reloaded: true, requested: true, controllerSwapped: true }), 'none');
  assert.equal(act({ reloaded: true, hasWaiting: true }), 'none');
});

test('update checks are throttled; a clock that went backwards does not block forever', () => {
  assert.equal(shouldCheck(1000, 0), true);
  assert.equal(shouldCheck(1000 + 60_000, 1000), false);
  assert.equal(shouldCheck(1000 + 5 * 60_000, 1000), true);
  assert.equal(shouldCheck(500, 1000), true);
});

test('reload guard: allowed first time, blocked right after a reload, allowed again later', () => {
  assert.equal(reloadAllowed(5000, 0), true);
  assert.equal(reloadAllowed(5000, undefined), true);
  assert.equal(reloadAllowed(5000, 1000), false);
  assert.equal(reloadAllowed(11_000, 1000), true);
  assert.equal(reloadAllowed(500, 1000), true);
});

test('sw.js repeats the message constants exactly (it is a classic script and cannot import them)', () => {
  const sw = readFileSync(join(here, '..', 'sw.js'), 'utf8');
  assert.ok(sw.includes(`const MSG_PING = '${MSG_PING}';`));
  assert.ok(sw.includes(`const MSG_SKIP = '${MSG_SKIP}';`));
  assert.ok(sw.includes(`const PING_TIMEOUT_MS = ${PING_TIMEOUT_MS};`));
});

test('sw.js keeps its safety invariants: own scope only, same-origin only, GET only, no DAV methods', () => {
  const sw = readFileSync(join(here, '..', 'sw.js'), 'utf8');
  assert.match(sw, /url\.origin !== location\.origin \|\| !url\.pathname\.startsWith\(SCOPE_PATH\)\) return;/);
  assert.match(sw, /req\.method !== 'GET'\) return;/);
  assert.ok(!/PROPFIND|DELETE|MOVE/.test(sw));
  assert.match(sw, /self\.addEventListener\('message'/);
});
