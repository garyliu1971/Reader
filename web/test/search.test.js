import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compileQuery, findAll, countIn, totalHits, rankOf, stepHit, hitAtOrAfter, seekHit, sectionOrder, snippet, MAX_HITS, MAX_QUERY } from '../js/core/search.js';

const hits = (text, q, limit) => {
  const flat = findAll(text, compileQuery(q), limit);
  const out = [];
  for (let i = 0; i < flat.length; i += 2) out.push([flat[i], flat[i + 1]]);
  return out;
};

test('blank queries compile to null and find nothing', () => {
  for (const q of ['', '   ', '\n\t', null, undefined]) assert.equal(compileQuery(q), null);
  assert.deepEqual(findAll('abc', null), []);
});

test('case-insensitive for Latin, accented and Greek text, offsets index the ORIGINAL text', () => {
  assert.deepEqual(hits('Hello hELLO HeLLo', 'hello'), [[0, 5], [6, 5], [12, 5]]);
  assert.deepEqual(hits('Über ÜBER über', 'über'), [[0, 4], [5, 4], [10, 4]]);
  assert.deepEqual(hits('ΣΟΦΙΑ σοφια', 'Σοφια'), [[0, 5], [6, 5]]);
  assert.deepEqual(hits('İstanbul', 'stanbul'), [[1, 7]]);   // the dotted capital I must not shift later offsets
});

test('CJK: plain substrings, punctuation, astral characters', () => {
  const t = '许多年以后，他仍然记得那个傍晚。许多年以后，钟声依旧。';
  assert.deepEqual(hits(t, '许多年以后'), [[0, 5], [16, 5]]);
  assert.deepEqual(hits(t, '，他'), [[5, 2]]);
  assert.deepEqual(hits('a𠮷b𠮷', '𠮷'), [[1, 2], [4, 2]]);   // surrogate pairs: offsets and lengths are UTF-16 code units
  assert.deepEqual(hits('夜色渐深', '昼'), []);
});

test('matches are non-overlapping and left to right', () => {
  assert.deepEqual(hits('aaaa', 'aa'), [[0, 2], [2, 2]]);
  assert.deepEqual(hits('abababa', 'aba'), [[0, 3], [4, 3]]);
  assert.deepEqual(hits('xx', 'xxx'), []);
});

test('regex syntax in the query is literal', () => {
  const t = 'cost (a+b) * [c] ^d$ e.f \\ g|h {1} ? x.y';
  for (const q of ['(a+b)', '*', '[c]', '^d$', 'e.f', '\\', 'g|h', '{1}', '?']) assert.equal(hits(t, q).length, 1, q);
  assert.deepEqual(hits('abc', '.'), []);
  assert.deepEqual(hits('a.c abc', 'a.c'), [[0, 3]]);
});

test('whitespace in the query matches any whitespace run in the text', () => {
  assert.deepEqual(hits('the quick\n  brown fox', 'quick brown'), [[4, 13]]);
  assert.deepEqual(hits('quick brown', 'quick  brown'), [[0, 11]]);
  assert.deepEqual(hits('  hello  ', '  hello '), [[2, 5]]);   // leading/trailing blanks of the query are ignored
});

test('limit caps the number of hits and the default is MAX_HITS', () => {
  assert.equal(hits('a'.repeat(100), 'a', 7).length, 7);
  assert.deepEqual(hits('a'.repeat(100), 'a', 0), []);
  assert.equal(findAll('a'.repeat(MAX_HITS + 50), compileQuery('a')).length / 2, MAX_HITS);
});

test('the compiled regex can be reused: lastIndex does not leak between texts', () => {
  const re = compileQuery('ab');
  assert.deepEqual(findAll('xxab', re), [2, 2]);
  assert.deepEqual(findAll('ab', re), [0, 2]);
});

// sections: 0 -> hits at 3,10 ; 1 -> none ; 2 -> 5 ; 3 -> (not scanned) ; 4 -> 0,7
const M = [[3, 1, 10, 1], undefined, [5, 2], undefined, [0, 1, 7, 1]];

test('counting helpers and rank', () => {
  assert.equal(countIn(M, 0), 2);
  assert.equal(countIn(M, 1), 0);
  assert.equal(countIn(M, 3), 0);
  assert.equal(countIn(M, 99), 0);
  assert.equal(totalHits(M), 5);
  assert.equal(totalHits([]), 0);
  assert.equal(rankOf(M, { s: 0, k: 0 }), 1);
  assert.equal(rankOf(M, { s: 2, k: 0 }), 3);
  assert.equal(rankOf(M, { s: 4, k: 1 }), 5);
});

test('stepHit walks hits across sections and wraps at both ends', () => {
  let c = null;
  const seen = [];
  for (let i = 0; i < 6; i++) { c = stepHit(M, c, 1); seen.push(`${c.s}.${c.k}`); }
  assert.deepEqual(seen, ['0.0', '0.1', '2.0', '4.0', '4.1', '0.0']);
  seen.length = 0;
  c = null;
  for (let i = 0; i < 6; i++) { c = stepHit(M, c, -1); seen.push(`${c.s}.${c.k}`); }
  assert.deepEqual(seen, ['4.1', '4.0', '2.0', '0.1', '0.0', '4.1']);
  assert.equal(stepHit([], null, 1), null);
  assert.equal(stepHit([undefined, undefined], { s: 0, k: 0 }, -1), null);
  assert.deepEqual(stepHit([[1, 1]], { s: 0, k: 0 }, 1), { s: 0, k: 0 });   // a single hit steps onto itself
  assert.deepEqual(stepHit([[1, 1]], { s: 0, k: 0 }, -1), { s: 0, k: 0 });
});

test('hitAtOrAfter / seekHit: first hit at or after a reading position', () => {
  assert.equal(hitAtOrAfter([3, 1, 10, 1], 0), 0);
  assert.equal(hitAtOrAfter([3, 1, 10, 1], 3), 0);
  assert.equal(hitAtOrAfter([3, 1, 10, 1], 4), 1);
  assert.equal(hitAtOrAfter([3, 1, 10, 1], 11), 2);
  assert.equal(hitAtOrAfter([], 0), 0);
  assert.deepEqual(seekHit(M, 0, 0), { s: 0, k: 0 });
  assert.deepEqual(seekHit(M, 0, 4), { s: 0, k: 1 });
  assert.deepEqual(seekHit(M, 0, 11), { s: 2, k: 0 });      // current section exhausted: next section with hits
  assert.deepEqual(seekHit(M, 1, 0), { s: 2, k: 0 });       // current section has none
  assert.deepEqual(seekHit(M, 4, 8), null);                 // nothing after, no wrap
  assert.deepEqual(seekHit(M, 4, 8, true), { s: 0, k: 0 }); // wraps to the start of the book
  assert.equal(seekHit([], 0, 0, true), null);
});

test('sectionOrder starts at the reader and wraps once around', () => {
  assert.deepEqual(sectionOrder(5, 2), [2, 3, 4, 0, 1]);
  assert.deepEqual(sectionOrder(3, 0), [0, 1, 2]);
  assert.deepEqual(sectionOrder(3, 9), [2, 0, 1]);   // out-of-range start is clamped
  assert.deepEqual(sectionOrder(0, 0), []);
  assert.deepEqual(sectionOrder(1, 0), [0]);
});

test('snippet: context around a hit, whitespace collapsed, lead flag only when text was cut', () => {
  const t = 'abc\n\n  def ghi jkl mno pqr stu vwx yz';
  assert.deepEqual(snippet(t, 0, 3), { lead: false, before: '', hit: 'abc', after: ' def ghi jkl mno pqr stu ' });
  const s = snippet('0123456789'.repeat(5), 25, 2, 4, 3);
  assert.deepEqual(s, { lead: true, before: '1234', hit: '56', after: '789' });
  assert.deepEqual(snippet('xy', 1, 1), { lead: false, before: 'x', hit: 'y', after: '' });
});

test('end to end over sections: scan from the reader, jump, step back around the book', () => {
  const secs = ['零一二三 foo 四', '没有', 'Foo FOO', 'x'];
  const re = compileQuery('foo');
  const matches = [];
  for (const i of sectionOrder(secs.length, 2)) { const f = findAll(secs[i], re); if (f.length) matches[i] = f; }
  assert.equal(totalHits(matches), 3);
  const start = seekHit(matches, 2, 1);   // reader is in section 2 at offset 1: "FOO" is the first hit after it
  assert.deepEqual(start, { s: 2, k: 1 });
  assert.equal(rankOf(matches, start), 3);
  assert.deepEqual(stepHit(matches, start, 1), { s: 0, k: 0 });
  assert.deepEqual(stepHit(matches, { s: 0, k: 0 }, -1), { s: 2, k: 1 });
});

test('an enormous query is cut to MAX_QUERY and still compiles and runs', () => {
  const re = compileQuery('字'.repeat(50000));
  assert.ok(re);
  assert.deepEqual(findAll('字'.repeat(MAX_QUERY + 5), re), [0, MAX_QUERY]);
});
