import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fitScale, layerBox, itemsToText, isTextless, normalizeSelection } from '../js/parse/pdftext.js';

test('fitScale fits the limiting side and survives bad input', () => {
  assert.equal(fitScale(600, 800, 300, 1000), 0.5);
  assert.equal(fitScale(600, 800, 1200, 400), 0.5);
  assert.equal(fitScale(0, 0, 100, 100), 1);
  assert.equal(fitScale(600, 800, -5, 100), 1);
});

test('layerBox floors like the canvas and carries the scale factor', () => {
  assert.deepEqual(layerBox(595.28, 841.89, 1.5), { width: 892, height: 1262, scaleFactor: 1.5 });
});

test('itemsToText joins items, ends lines on hasEOL, skips markers', () => {
  const items = [
    { str: 'Hello', hasEOL: false }, { str: ' ', hasEOL: false }, { str: 'world', hasEOL: true },
    { type: 'beginMarkedContent' },
    { str: 'Second line  ', hasEOL: true }, { str: 'tail', hasEOL: false },
  ];
  assert.equal(itemsToText(items), 'Hello world\nSecond line\ntail');
});

test('itemsToText de-hyphenates only a letter-hyphen followed by lowercase', () => {
  assert.equal(itemsToText([{ str: 'exam-', hasEOL: true }, { str: 'ple text', hasEOL: true }]), 'example text');
  assert.equal(itemsToText([{ str: 'well-', hasEOL: true }, { str: 'Known', hasEOL: true }]), 'well-\nKnown');
  assert.equal(itemsToText([{ str: '1990-', hasEOL: true }, { str: 'ab', hasEOL: true }]), '1990-\nab');
});

test('itemsToText tolerates empty input and collapses blank runs', () => {
  assert.equal(itemsToText(null), '');
  assert.equal(itemsToText([]), '');
  assert.equal(itemsToText([{ str: 'a', hasEOL: true }, { str: '', hasEOL: true }, { str: '', hasEOL: true }, { str: 'b', hasEOL: true }]), 'a\n\nb');
});

test('isTextless detects scanned pages', () => {
  assert.equal(isTextless(''), true);
  assert.equal(isTextless(' \n . '), true);
  assert.equal(isTextless('第一章'), false);
});

test('normalizeSelection joins wrapped lines: space for Latin, none for CJK', () => {
  assert.equal(normalizeSelection('the quick\nbrown fox'), 'the quick brown fox');
  assert.equal(normalizeSelection('天地玄黄\n宇宙洪荒'), '天地玄黄宇宙洪荒');
  assert.equal(normalizeSelection('  one \n\n two '), 'one two');
  assert.equal(normalizeSelection(null), '');
});
