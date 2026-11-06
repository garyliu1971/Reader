// Encoding detection for plain-text books (UTF-8 / UTF-16 BOM / GB18030 / Big5), like pyreader/textio.decode_text.
// Pure + synchronous so it runs in a worker and in node tests.

export function normalizeNewlines(t) {
  return t.indexOf('\r') >= 0 ? t.replace(/\r\n?/g, '\n') : t;
}

function dec(label, bytes, fatal = false, stream = false) {
  return new TextDecoder(label, { fatal }).decode(bytes, { stream });
}

/** Lower is better: replacement chars dominate, then "unlikely" chars (PUA, compat ideographs, control codes). */
export function garbageScore(s) {
  let bad = 0;
  let odd = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0xfffd) bad++;
    else if ((c >= 0xe000 && c <= 0xf8ff) || (c >= 0xf900 && c <= 0xfaff)) odd++;
    else if (c < 0x20 && c !== 0x09 && c !== 0x0a && c !== 0x0d) odd++;
  }
  return bad * 10 + odd * 3;
}

/**
 * @param {Uint8Array} raw
 * @returns {{text: string, encoding: string}}
 */
export function decodeText(raw) {
  if (raw.length >= 2 && raw[0] === 0xff && raw[1] === 0xfe) {
    return { text: normalizeNewlines(dec('utf-16le', raw.subarray(2))), encoding: 'utf-16le' };
  }
  if (raw.length >= 2 && raw[0] === 0xfe && raw[1] === 0xff) {
    return { text: normalizeNewlines(dec('utf-16be', raw.subarray(2))), encoding: 'utf-16be' };
  }
  if (raw.length >= 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf) {
    return { text: normalizeNewlines(dec('utf-8', raw.subarray(3))), encoding: 'utf-8' };
  }
  try {
    return { text: normalizeNewlines(dec('utf-8', raw, true)), encoding: 'utf-8' };   // strict UTF-8 first
  } catch { /* not valid UTF-8 */ }
  // Not valid UTF-8: judge candidates on a bounded sample so a 100 MB file is not decoded three times.
  const sampleLen = Math.min(raw.length, 1 << 20);
  const sample = raw.subarray(0, sampleLen);
  let best = null;
  for (const enc of ['gb18030', 'big5', 'utf-8']) {
    const t = dec(enc, sample, false, sampleLen < raw.length);   // stream: a char cut at the sample edge is not an error
    const score = garbageScore(t);
    if (best === null || score < best.score) best = { enc, score };
  }
  return { text: normalizeNewlines(dec(best.enc, raw)), encoding: best.enc };
}

export function looksLikeCjk(text) {
  const sample = text.slice(0, 20000);
  let cjk = 0;
  for (let i = 0; i < sample.length; i++) {
    const c = sample.charCodeAt(i);
    if (c >= 0x3400 && c <= 0x9fff) cjk++;
  }
  return sample.length > 0 && cjk / sample.length > 0.05;
}

/**
 * Western hard-wrapped plain text (Project Gutenberg ~70 cols): merge single newlines inside blank-line separated
 * paragraphs. Same safety rules as textio.unwrap_latin_text. Linear time: pieces are joined once, never appended
 * to a growing string in a loop.
 */
export function unwrapLatinText(text) {
  if (!/\n[ \t]*\n/.test(text) || looksLikeCjk(text)) return text;
  const parts = text.split(/(\n[ \t]*\n+)/);
  for (let i = 0; i < parts.length; i += 2) {
    const blk = parts[i];
    if (blk.indexOf('\n') < 0) continue;
    const lines = blk.split('\n');
    let ok = true;
    for (let k = 0; k < lines.length - 1 && ok; k++) if (lines[k].length < 40) ok = false;
    for (let k = 1; k < lines.length && ok; k++) if (lines[k][0] === ' ' || lines[k][0] === '\t') ok = false;
    if (ok) parts[i] = lines.map((l, k) => (k ? l.trim() : l.trimEnd())).join(' ');
  }
  return parts.join('');
}
