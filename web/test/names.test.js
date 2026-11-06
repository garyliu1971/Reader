import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { safeComponent, safeSegment, normalizeRel, validBookId, validRemoteRel, isHiddenName } from '../js/core/names.js';
import { sha1Hex } from '../js/core/sha1.js';
import { localBookId, remoteBookId, progressFileName, progressPath } from '../js/core/bookid.js';
import { golden } from './helpers.js';

test('safe_component matches the Python implementation', () => {
  for (const [input, expected] of golden.safe_component) assert.equal(safeComponent(input), expected, JSON.stringify(input));
});

test('safe_segment matches the Python implementation', () => {
  for (const [input, expected] of golden.safe_segment) assert.equal(safeSegment(input), expected, JSON.stringify(input));
});

test('normalize_rel matches the Python implementation', () => {
  for (const [input, expected] of golden.normalize_rel) assert.equal(normalizeRel(input), expected, JSON.stringify(input));
});

test('valid_book_id matches the Python implementation', () => {
  for (const [input, expected] of golden.valid_book_id) assert.equal(validBookId(input), expected, input);
});

test('validRemoteRel: hidden, .part and unsupported extensions are refused', () => {
  assert.equal(validRemoteRel('a.epub'), true);
  assert.equal(validRemoteRel('dir/b.txt'), true);
  assert.equal(validRemoteRel('a.PDF'), true);
  assert.equal(validRemoteRel('.x.epub.part'), false);
  assert.equal(validRemoteRel('a.epub.part'), false);
  assert.equal(validRemoteRel('~$a.docx'), false);
  assert.equal(validRemoteRel('a.exe'), false);
  assert.equal(validRemoteRel('a.mp3'), false);   // the Python side accepts it; the web reader cannot open it
  assert.equal(isHiddenName('.a'), true);
  assert.equal(isHiddenName('~$a'), true);
  assert.equal(isHiddenName('a'), false);
});

test('sha1 equals node:crypto for many lengths (padding boundaries)', () => {
  for (let n = 0; n < 200; n++) {
    const bytes = Uint8Array.from({ length: n }, (_, i) => (i * 31 + n) & 0xff);
    assert.equal(sha1Hex(bytes), createHash('sha1').update(bytes).digest('hex'), `len ${n}`);
  }
  assert.equal(sha1Hex('中文abc'), createHash('sha1').update('中文abc', 'utf8').digest('hex'));
});

test('book ids and progress file names equal the Python values', () => {
  for (const [name, size, id] of golden.local_ids) assert.equal(localBookId(name, size), id, name);
  for (const [rel, file] of golden.progress_files) {
    assert.equal(progressFileName(remoteBookId(rel)), file, rel);
    assert.equal(progressPath('gary', remoteBookId(rel)), `users/gary/progress/${file}`);
  }
  assert.equal(progressPath('', 'remote:a'), `users/default/progress/${progressFileName('remote:a')}`);
});

test('remote book ids are verbatim (no unicode normalization)', () => {
  assert.notEqual(remoteBookId('é.txt'), remoteBookId('é.txt'));
  assert.equal(remoteBookId('科幻/三体.epub'), 'remote:科幻/三体.epub');
});
