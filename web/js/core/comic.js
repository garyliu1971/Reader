// Image-only ("comic") EPUB detection, pure and DOM-free. The SAME rule as the desktop app (pyreader/comic.py is_image_epub);
// keep thresholds and sampling in step. Input is what epub.js scan() measured per spine document:
//   stats = [{imgs: <img> + <svg><image> count, chars: visible text characters without any whitespace}, ...]  (spine order)
//   bookType = lower-cased OPF <meta name="book-type"> ('' when absent)
// Never the sole trigger: the book-type hint only relaxes the threshold, and text length / image bytes / page direction are
// not used at all (an illustrated novel scores far lower than a comic on this measure; see test/comic.test.js).

export const SAMPLE_PAGES = 12;        // at most this many (evenly spaced) body pages are inspected
export const PICTURE_MAX_CHARS = 50;   // a picture page: >= 1 image and at most this many visible characters
export const THRESHOLD = 0.8;          // share of picture pages that makes a comic
export const THRESHOLD_HINTED = 0.5;   // ... when the OPF itself says comic/manga
export const HINT_TYPES = ['comic', 'manga', 'comics'];
export const WEBTOON_MIN_IMAGES = 10;  // fewer than 3 spine documents (one long page of images)
export const MIN_SPINE = 3;
export const TRIM_SPINE = 4;           // longer than this: skip the first (cover wrapper) and last (colophon) document

/** i-th of k evenly spaced indexes out of n (n > k): floor(i * n / k) */
export function sampleIndexes(n, k = SAMPLE_PAGES) {
  if (n <= k) return Array.from({ length: n }, (_, i) => i);
  return Array.from({ length: k }, (_, i) => Math.floor((i * n) / k));
}

/** @returns {{comic: boolean, score: number}} score = share of picture pages in the sample (webtoon rule: 1 or 0) */
export function detectComic(stats, bookType = '') {
  const spine = Array.isArray(stats) ? stats : [];
  if (!spine.length) return { comic: false, score: 0 };
  if (spine.length < MIN_SPINE) {
    const imgs = spine.reduce((a, p) => a + (p.imgs || 0), 0);
    const chars = spine.reduce((a, p) => a + (p.chars || 0), 0);
    const comic = imgs >= WEBTOON_MIN_IMAGES && chars <= PICTURE_MAX_CHARS * imgs;
    return { comic, score: comic ? 1 : 0 };
  }
  const inner = spine.length > TRIM_SPINE ? spine.slice(1, -1) : spine;
  const sample = sampleIndexes(inner.length).map((i) => inner[i]);
  const pictures = sample.filter((p) => (p.imgs || 0) >= 1 && (p.chars || 0) <= PICTURE_MAX_CHARS).length;
  const score = pictures / sample.length;
  const thr = HINT_TYPES.includes(String(bookType || '').toLowerCase()) ? THRESHOLD_HINTED : THRESHOLD;
  return { comic: score >= thr, score };
}
