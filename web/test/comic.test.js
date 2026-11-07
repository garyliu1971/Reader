// Image-only (comic) epub detection: the same rule and cases as the desktop app's is_image_epub (pyreader/comic.py).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { detectComic, sampleIndexes, THRESHOLD, THRESHOLD_HINTED, SAMPLE_PAGES } from '../js/core/comic.js';
import { scanMarkup } from '../js/parse/epub.js';

const pic = (chars = 0) => ({ imgs: 1, chars });
const text = (chars = 1500) => ({ imgs: 0, chars });
/** a book of n body pages (+ cover wrapper and colophon, which the rule skips) where page i gets mk(i) */
const book = (n, mk) => [text(15), ...Array.from({ length: n }, (_, i) => mk(i)), text(30)];

test('constants of the shared rule', () => {
  assert.equal(THRESHOLD, 0.8);
  assert.equal(THRESHOLD_HINTED, 0.5);
  assert.equal(SAMPLE_PAGES, 12);
});

test('sampleIndexes: all pages when few, else 12 evenly spaced starting at the first', () => {
  assert.deepEqual(sampleIndexes(5), [0, 1, 2, 3, 4]);
  assert.deepEqual(sampleIndexes(12), Array.from({ length: 12 }, (_, i) => i));
  assert.deepEqual(sampleIndexes(100), [0, 8, 16, 25, 33, 41, 50, 58, 66, 75, 83, 91]);
  assert.deepEqual(sampleIndexes(0), []);
});

test('positives: plain 150-page comic, captions, translator note, picture book, hinted book-type', () => {
  assert.deepEqual(detectComic(book(150, () => pic())), { comic: true, score: 1 });
  assert.equal(detectComic(book(100, (i) => pic(i % 7 === 3 ? 80 : 0))).comic, true, '15% captioned pages (80 chars): at most 2 of 12 sampled');
  assert.equal(detectComic(book(100, (i) => (i === 41 ? pic(400) : pic()))).comic, true, 'one 400-char translator-note page');
  assert.equal(detectComic(book(60, () => pic(30))).comic, true, 'picture book with at most 30 chars per page');
  assert.equal(detectComic(book(60, () => pic(50))).comic, true, '50 chars is still a picture page');
  const sixty = book(100, (i) => (i < 60 ? pic() : text()));   // 7 of 12 sampled pages are pictures: 0.58
  assert.equal(detectComic(sixty).comic, false);
  assert.equal(detectComic(sixty, 'comic').comic, true, 'book-type=comic relaxes the threshold to 0.5');
  assert.equal(detectComic(sixty, 'manga').comic, true);
  assert.equal(detectComic(sixty, 'COMICS').comic, true);
  assert.equal(detectComic(sixty, 'novel').comic, false);
});

test('negatives: text novel, illustrated books, fixed layout, scans with an OCR text layer', () => {
  assert.equal(detectComic(book(200, () => text())).comic, false);
  assert.equal(detectComic(book(100, (i) => (i < 30 ? pic() : text()))).comic, false, '30% full-page plates');
  assert.equal(detectComic(book(100, (i) => (i < 50 ? pic() : text()))).comic, false, '50% full-page plates');
  const photo = detectComic(book(100, (i) => (i < 70 ? pic() : text())));
  assert.equal(photo.comic, false, '70% plates is 9/12 = 0.75, still under 0.80');
  assert.equal(photo.score, 0.75);
  assert.equal(detectComic(book(80, () => ({ imgs: 1, chars: 300 }))).comic, false, 'fixed layout: background image + 300 chars per page');
  assert.equal(detectComic(book(80, () => ({ imgs: 1, chars: 1800 }))).comic, false, 'scan with a hidden OCR text layer');
  assert.equal(detectComic([]).comic, false);
  assert.equal(detectComic(undefined).comic, false);
  assert.equal(detectComic(null, 'comic').comic, false);
});

test('spine trimming: the cover wrapper and colophon never decide, short spines use every page', () => {
  assert.equal(detectComic([text(900), pic(), pic(), pic(), pic(), text(900)]).comic, true, '6 documents: first and last skipped');
  assert.equal(detectComic([text(900), pic(), pic(), text(900)]).comic, false, '4 documents: all counted, 2 of 4');
  assert.equal(detectComic([pic(), pic(), pic(), pic()]).comic, true);
  assert.equal(detectComic([pic(), pic(), pic()]).comic, true);
});

test('one or two xhtml documents (webtoon): at least 10 images and at most 50 chars per image', () => {
  assert.equal(detectComic([{ imgs: 40, chars: 40 }]).comic, true);
  assert.equal(detectComic([{ imgs: 20, chars: 10 }, { imgs: 20, chars: 10 }]).comic, true);
  assert.equal(detectComic([{ imgs: 9, chars: 0 }]).comic, false, 'fewer than 10 images');
  assert.equal(detectComic([{ imgs: 3, chars: 90000 }]).comic, false, 'single-xhtml novel with 3 images');
  assert.equal(detectComic([{ imgs: 10, chars: 501 }]).comic, false, 'over 50 chars per image on average');
});

test('scanMarkup measures images and visible characters', () => {
  const page = '<html><head><title>very long head title</title><style>p{}</style></head><body><div><img src="a.jpg"/></div></body></html>';
  assert.deepEqual(scanMarkup(page), { len: 0, title: '', imgs: 1, chars: 0 });
  const svg = '<body><svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="p.jpg" width="1" height="1"/></svg></body>';
  assert.equal(scanMarkup(svg).imgs, 1);
  const cap = '<body><h1>Title  A</h1>\n<p>a b\n c</p><img src="x"/><IMG src="y"/><script>var x = 1;</script></body>';
  const r = scanMarkup(cap);
  assert.equal(r.imgs, 2);
  assert.equal(r.chars, 'TitleAabc'.length);
  assert.equal(r.len, 'Title A a b c'.length, 'len keeps the collapsed single spaces, chars drops all whitespace');
  assert.equal(scanMarkup('<body><p>&nbsp;&#160;&amp;</p></body>').chars, 1, 'non-breaking spaces are whitespace');
});

test('scanMarkup ignores HTML comments: no images, no characters from them', () => {
  const r = scanMarkup('<body><!-- <img src="old.jpg"/> hidden caption --><p>ab</p><!--\n<img src="x"/>\n<image href="y"/>\n--><img src="real.jpg"/></body>');
  assert.equal(r.imgs, 1);
  assert.equal(r.chars, 2);
  assert.equal(r.len, 2);
  assert.deepEqual(scanMarkup('<body><!-- only a comment --></body>'), { len: 0, title: '', imgs: 0, chars: 0 });
  assert.equal(scanMarkup('<body><!-- <h1>Not a title</h1> --><h2>Real</h2></body>').title, 'Real');
});

test('scanMarkup decodes numeric, hex and common named entities before counting', () => {
  assert.equal(scanMarkup('<p>&#x4e2d;&#20013;&#X4E2D;</p>').chars, 3, 'a reference is one visible character');
  assert.equal(scanMarkup('<p>&#x4e2d;&#x6587;</p>').len, 2);
  assert.equal(scanMarkup('<p>&#x1F600;</p>').chars, 2, 'astral code point = one surrogate pair, like the DOM text length');
  assert.equal(scanMarkup('<p>&amp;&lt;&gt;&quot;&apos;</p>').chars, 5);
  assert.equal(scanMarkup('<p>&amp;lt;</p>').len, 4, '"&lt;" literal text: decoded once only');
  assert.equal(scanMarkup('<p>&nbsp;&#160;&#xA0; </p>').chars, 0, 'non-breaking spaces are whitespace');
  assert.equal(scanMarkup('<p>&#0;&#xD800;&#x110000;</p>').chars, 3, 'invalid code points become one replacement char each');
  assert.equal(scanMarkup('<p>&unknown;</p>').chars, '&unknown;'.length, 'unknown names are left alone');
  assert.equal(scanMarkup('<h1>&#x4e2d;&amp;A</h1>').title, '中&A');
});

test('scanMarkup + detectComic on a generated 100-page image book and a text book', () => {
  const comicPage = (i) => scanMarkup(`<html><head><title>p${i}</title></head><body><img src="images/${i}.jpg"/></body></html>`);
  const stats = [scanMarkup('<body><h1>チ。</h1><img src="cover.jpg"/></body>'), ...Array.from({ length: 100 }, (_, i) => comicPage(i)), scanMarkup('<body><p>end</p></body>')];
  assert.equal(detectComic(stats, 'comic').comic, true);
  const novel = Array.from({ length: 30 }, () => scanMarkup('<body><p>' + '很长的一段正文。'.repeat(80) + '</p></body>'));
  assert.equal(detectComic(novel).comic, false);
});
