import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Translate, guessScript, chooseDirection, baseLang, MAX_CHARS } from '../js/reader/translate.js';

test('script guess', () => {
  assert.equal(guessScript('吾輩は猫である'), 'ja');
  assert.equal(guessScript('안녕하세요'), 'ko');
  assert.equal(guessScript('天地玄黄，宇宙洪荒。'), 'zh');
  assert.equal(guessScript('The quick brown fox'), null);
  assert.equal(guessScript('Bonjour le monde 你好'), null, 'a little Han inside Latin text is not Chinese');
});

test('direction never maps a language onto itself', () => {
  assert.deepEqual(chooseDirection('en', 'zh'), { source: 'en', target: 'zh' });
  assert.deepEqual(chooseDirection('zh', 'zh'), { source: 'zh', target: 'en' });
  assert.deepEqual(chooseDirection('zh-Hant', 'zh-Hans'), { source: 'zh', target: 'en' });
  assert.deepEqual(chooseDirection('en-US', 'en'), { source: 'en', target: 'zh' });
  assert.equal(baseLang('zh-Hans'), 'zh');
});

function fakeT({ avail = 'available', failCreate = false, failTranslate = false } = {}) {
  const calls = { avail: [], create: [], translate: [] };
  return {
    calls,
    availability: async (o) => { calls.avail.push(o); return typeof avail === 'function' ? avail(o) : avail; },
    create: async (o) => {
      calls.create.push(o);
      if (failCreate) throw new Error('boom');
      if (o.monitor) o.monitor({ addEventListener: (_, cb) => { cb({ loaded: 0.5 }); cb({ loaded: 1 }); } });
      return { translate: async (t) => { calls.translate.push(t); if (failTranslate) throw new Error('nope'); return `[${o.sourceLanguage}>${o.targetLanguage}] ${t}`; } };
    },
  };
}
const fakeLD = (lang, avail = 'available') => ({ availability: async () => avail, create: async () => ({ detect: async () => [{ detectedLanguage: lang, confidence: 0.9 }] }) });

test('translates English to Chinese, detector decides the source, whitespace is collapsed', async () => {
  const T = fakeT();
  const tr = new Translate({ T, LD: fakeLD('fr') });
  const r = await tr.translate('  Bonjour\n  le   monde ', { target: 'zh' });
  assert.deepEqual([r.source, r.target, r.text], ['fr', 'zh', '[fr>zh] Bonjour\nle monde'], 'paragraph breaks are kept, runs of spaces collapse');
});

test('Chinese selection goes to English; Japanese goes to the target', async () => {
  const tr = new Translate({ T: fakeT() });
  assert.equal((await tr.translate('天地玄黄', { target: 'zh' })).target, 'en');
  const j = await tr.translate('吾輩は猫である', { target: 'zh' });
  assert.deepEqual([j.source, j.target], ['ja', 'zh']);
});

test('translator instances are cached per direction; progress is reported once per create', async () => {
  const T = fakeT();
  const tr = new Translate({ T, LD: fakeLD('en') });
  const seen = [];
  await tr.translate('one', { onProgress: (p) => seen.push(p) });
  await tr.translate('two', { onProgress: (p) => seen.push(p) });
  assert.equal(T.calls.create.length, 1);
  assert.deepEqual(seen, [50, 100]);
});

test('unavailable pair, too long text, empty text and engine errors give typed errors', async () => {
  const code = async (p) => { try { await p; return 'none'; } catch (e) { return e.code; } };
  assert.equal(await code(new Translate({ T: fakeT({ avail: 'unavailable' }) }).translate('hello')), 'unsupported');
  assert.equal(await code(new Translate({ T: fakeT() }).translate('a'.repeat(MAX_CHARS + 1))), 'too-long');
  assert.equal(await code(new Translate({ T: fakeT() }).translate('   ')), 'empty');
  assert.equal(await code(new Translate({ T: fakeT({ failCreate: true }) }).translate('hello')), 'failed');
  const T = fakeT({ failTranslate: true });
  const tr = new Translate({ T });
  assert.equal(await code(tr.translate('hello')), 'failed');
  assert.equal(tr.cache.size, 0, 'a translator that failed is not kept');
});

test('a detector that still has to be downloaded never delays the translation: English assumed, detector warmed up', async () => {
  let created = 0;
  const LD = { availability: async () => 'downloadable', create: async () => { created++; return { detect: async () => [{ detectedLanguage: 'fr' }] }; } };
  const tr = new Translate({ T: fakeT(), LD });
  assert.equal((await tr.translate('Bonjour le monde')).source, 'en');
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(created, 1, 'warm-up started once');
  assert.equal((await tr.translate('Bonjour le monde encore')).source, 'fr', 'next time the warmed detector is used');
  assert.equal(created, 1);
});

test('a hanging detector times out instead of blocking', async () => {
  const LD = { availability: async () => 'available', create: async () => ({ detect: () => new Promise(() => {}) }) };
  const tr = new Translate({ T: fakeT(), LD });
  const t0 = Date.now();
  assert.equal((await tr.translate('Hello world')).source, 'en');
  assert.ok(Date.now() - t0 < 4000);
});

test('NotAllowedError from create() asks the user to click again', async () => {
  const T = { availability: async () => 'downloadable', create: async () => { const e = new Error('x'); e.name = 'NotAllowedError'; throw e; } };
  await assert.rejects(new Translate({ T }).translate('Hello'), (e) => e.code === 'activation' && /再点一次/.test(e.message));
});

test('no detector: Latin text is assumed English', async () => {
  const r = await new Translate({ T: fakeT() }).translate('Hello there');
  assert.equal(r.source, 'en');
});

test('supported() looks at the global', () => {
  assert.equal(Translate.supported({}), false);
  assert.equal(Translate.supported({ Translator: { create() {} } }), true);
});
