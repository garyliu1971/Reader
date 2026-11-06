// Chapter detection for plain text, ported from pyreader/textio.py (_classify/_find_chapters/...).
// Offsets are UTF-16 indices into the decoded text.

const FW_DIGITS = { '０': '0', '１': '1', '２': '2', '３': '3', '４': '4', '５': '5', '６': '6', '７': '7', '８': '8', '９': '9' };
const CN_DIGIT = {
  零: 0, 〇: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 两: 2, 兩: 2, 壹: 1, 贰: 2, 貳: 2,
  叁: 3, 參: 3, 肆: 4, 伍: 5, 陆: 6, 陸: 6, 柒: 7, 捌: 8, 玖: 9,
};
const CN_UNIT = { 十: 10, 拾: 10, 百: 100, 佰: 100, 千: 1000, 仟: 1000 };
const CN_SPECIAL = { 廿: 20, 卅: 30, 卌: 40 };

/** 中文/阿拉伯/全角/大写数字 -> number, or null. */
export function cn2int(str) {
  if (!str) return null;
  const s = str.trim().replace(/[０-９]/g, (c) => FW_DIGITS[c]);
  if (!s) return null;
  const chars = Array.from(s);
  if (chars.every((c) => /[0-9]/.test(c) || c in CN_DIGIT)) {
    return parseInt(chars.map((c) => (/[0-9]/.test(c) ? c : String(CN_DIGIT[c]))).join(''), 10);
  }
  let total = 0, section = 0, num = 0;
  for (const c of chars) {
    if (/[0-9]/.test(c)) num = parseInt(c, 10);
    else if (c in CN_DIGIT) num = CN_DIGIT[c];
    else if (c in CN_UNIT) { section += (num || 1) * CN_UNIT[c]; num = 0; }
    else if (c in CN_SPECIAL) { section += CN_SPECIAL[c]; num = 0; }
    else if (c === '万') { section += num; total += (section || 1) * 10000; section = num = 0; }
    else return null;
  }
  return total + section + num;
}

const ROMAN = { i: 1, v: 5, x: 10, l: 50, c: 100, d: 500, m: 1000 };
function roman2int(str) {
  const s = str.toLowerCase();
  if (!s || [...s].some((c) => !(c in ROMAN))) return null;
  let total = 0;
  for (let i = 0; i < s.length; i++) {
    const v = ROMAN[s[i]];
    total += i + 1 < s.length && ROMAN[s[i + 1]] > v ? -v : v;
  }
  let out = '', n = total;   // reject non-canonical forms like "iiii": round-trip and compare
  for (const [val, sym] of [[1000, 'm'], [900, 'cm'], [500, 'd'], [400, 'cd'], [100, 'c'], [90, 'xc'], [50, 'l'], [40, 'xl'], [10, 'x'], [9, 'ix'], [5, 'v'], [4, 'iv'], [1, 'i']]) {
    while (n >= val) { out += sym; n -= val; }
  }
  return out === s && total > 0 ? total : null;
}

const EN_WORDS = {};
'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'
  .split(' ').forEach((w, i) => { EN_WORDS[w] = i + 1; });

const NUMCH = '零〇一二三四五六七八九十百千万两兩廿卅壹贰貳叁參肆伍陆陸柒捌玖拾佰仟0-9０-９';
const UNITS = '章节節回卷集部篇话話幕';
const SEP = ' \\t\u3000';
const ZH_RE = new RegExp(`^【?第[${SEP}]*([${NUMCH}]{1,12})[${SEP}]*([${UNITS}])】?(.*)$`, 'u');
const EN_RE = new RegExp('^(chapter|chap\\.?|part|book|section)[ \\t]+(\\d{1,4}|[ivxlcdm]{1,8}|'
  + Object.keys(EN_WORDS).join('|') + ')\\b[ \\t]*(?:[.:\\-–—)][ \\t]*)?(.*)$', 'iu');
const SPECIAL_RE = new RegExp(
  '^(序章|序言|前言|楔子|引子|引言|终章|終章|尾声|尾聲|后记|後記|番外篇|番外|附录|附錄|致谢|致謝|'
  + '作者的话|完本感言|序|跋|prologue|epilogue|preface|foreword|afterword|introduction|appendix)'
  + `([${SEP}]*[:：、\\-—.．][${SEP}]*|[${SEP}]+)?(.*)$`, 'iu');
const NUMDOT_RE = new RegExp(`^(\\d{1,4})[${SEP}]*[.．、][${SEP}]*(\\D.*|)$`, 'u');
const DUN_RE = new RegExp(`^([${NUMCH}]{1,6})[${SEP}]*[、．.][${SEP}]*(.*)$`, 'u');
const MD_RE = /^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$/u;
// Prefilter: only short lines that could be titles are classified at all (keeps this linear on huge books).
const PREFILTER_RE = new RegExp(
  `^[${SEP}]*(?:[【#第序前楔引终終尾后後番跋附致作完${NUMCH}]|(?:chap|part|book|section|prologue|`
  + 'epilogue|preface|foreword|afterword|introduction|appendix))[^\\n]{0,100}$', 'gimu');

const NO_BODY_START = '的了是，,;；';
const CLOSERS = { '”': '“', '」': '「', '』': '『', '）': '（', ')': '(', '》': '《', '】': '【', '’': '‘', '"': '"' };
const NO_JOIN_SPECIAL = new Set(['番外', '番外篇', '附录', '附錄']);

function isSentence(str) {
  const s = str.trim();
  if (!s) return false;
  if (s.includes('。')) return true;
  const c = s[s.length - 1];
  if ('！？…!?，,；;、'.includes(c)) return true;
  if (c === '.') return true;
  const op = CLOSERS[c];
  if (op !== undefined) {
    if (c === '"') return (s.split('"').length - 1) % 2 === 1;
    return !s.includes(op);
  }
  return false;
}

/** one line -> {fam, num, title} or null */
export function classify(line) {
  let m = ZH_RE.exec(line);
  if (m) {
    const tail = m[3];
    if (tail && (NO_BODY_START.includes(tail[0]) || tail.trim().length > 40 || isSentence(tail))) return null;
    const n = cn2int(m[1]);
    if (n === null) return null;
    const unit = { 節: '节', 話: '话' }[m[2]] || m[2];
    return { fam: 'zh:' + unit, num: n, title: line };
  }
  m = EN_RE.exec(line);
  if (m) {
    const tail = m[3];
    if (tail && (tail.length > 60 || isSentence(tail))) return null;
    const w = m[2].toLowerCase();
    const n = /^\d+$/.test(w) ? parseInt(w, 10) : (EN_WORDS[w] || roman2int(w));
    if (!n) return null;
    const kind = m[1].toLowerCase().startsWith('chap') ? 'chapter' : m[1].toLowerCase();
    return { fam: 'en:' + kind, num: n, title: line };
  }
  m = SPECIAL_RE.exec(line);
  if (m) {
    const kw = m[1], sep = m[2] || '', tail = m[3];
    if (tail && (tail.length > 30 || isSentence(tail))) return null;
    if (tail && !sep && !NO_JOIN_SPECIAL.has(kw)) return null;
    return { fam: 'special', num: null, title: line };
  }
  m = MD_RE.exec(line);
  if (m) return { fam: 'md', num: null, title: m[1].trim() };
  if (isSentence(line)) return null;
  m = NUMDOT_RE.exec(line);
  if (m) return { fam: 'numdot', num: parseInt(m[1], 10), title: line };
  m = DUN_RE.exec(line);
  if (m) {
    const n = cn2int(m[1]);
    if (n !== null && m[2].length <= 40) return { fam: 'dun', num: n, title: line };
  }
  return null;
}

function trimSep(s) { return s.replace(/^[ \t\u3000]+|[ \t\u3000]+$/g, ''); }

function collectCandidates(text) {
  const out = [];
  PREFILTER_RE.lastIndex = 0;
  let m;
  while ((m = PREFILTER_RE.exec(text)) !== null) {
    if (m[0].length === 0) { PREFILTER_RE.lastIndex++; continue; }
    const raw = m[0];
    const line = trimSep(raw);
    if (!line || line.length > 80) continue;
    const r = classify(line);
    if (r === null) continue;
    out.push({ pos: m.index, end: m.index + raw.replace(/[ \t\u3000]+$/, '').length, title: r.title, fam: r.fam, num: r.num });
  }
  return out;
}

function dropTocClusters(cands, textLen) {
  const limit = Math.max(20000, Math.floor(textLen / 10));
  const out = [];
  let i = 0;
  const n = cands.length;
  while (i < n) {
    let j = i;
    while (j + 1 < n && cands[j + 1].pos - cands[j].end < 60) {
      const a = cands[j], b = cands[j + 1];
      if (a.fam === b.fam && a.num !== null && b.num !== null && b.num <= a.num) break;   // numbering wrapped: the TOC page ended
      j++;
    }
    if (!(j - i + 1 >= 5 && cands[i].pos < limit)) for (let k = i; k <= j; k++) out.push(cands[k]);
    i = j + 1;
  }
  return out;
}

function pruneOutliers(cs) {
  const out = [];
  cs.forEach((c, i) => {
    const prev = out[out.length - 1];
    if (prev && c.num !== null && prev.num !== null) {
      const d = c.num - prev.num;
      if (d <= 0 && c.num !== 1) return;   // repeat / regression: a reference inside the body text
      if (d > 2 && i + 1 < cs.length && cs[i + 1].num !== null && [1, 2].includes(cs[i + 1].num - prev.num)) return;
    }
    out.push(c);
  });
  return out;
}

function familyStats(cs, textLen) {
  const n = cs.length;
  let cont = 1;
  if (n >= 2 && cs.every((c) => c.num !== null)) {
    let ok = 0;
    for (let i = 0; i + 1 < n; i++) {
      const d = cs[i + 1].num - cs[i].num;
      if (d === 1 || d === 2 || cs[i + 1].num === 1) ok++;
    }
    cont = ok / (n - 1);
  }
  const gaps = [];
  for (let i = 0; i + 1 < n; i++) gaps.push(cs[i + 1].pos - cs[i].end);
  gaps.push(textLen - cs[n - 1].end);
  const spacing = gaps.filter((g) => g >= 100).length / gaps.length;
  return { n, cont, spacing };
}

/** -> [[pos, title], ...] sorted by pos; [] if nothing trustworthy */
export function findChapters(text) {
  const cands = dropTocClusters(collectCandidates(text), text.length);
  if (!cands.length) return [];
  const fams = new Map();
  for (const c of cands) {
    if (!fams.has(c.fam)) fams.set(c.fam, []);
    fams.get(c.fam).push(c);
  }
  let chosen = [];
  const secondary = [];
  for (const [fam, list] of fams) {
    let cs = list;
    if (fam.startsWith('zh:') || fam.startsWith('en:')) {
      cs = pruneOutliers(cs);
      if (!cs.length) continue;
      const { n, cont, spacing } = familyStats(cs, text.length);
      if ((n === 1 && cs[0].num === 1) || (n >= 2 && cont >= 0.6 && spacing >= 0.6)) chosen = chosen.concat(cs);
    } else if (fam === 'special' || fam === 'md') {
      chosen = chosen.concat(cs);
    } else {
      cs = pruneOutliers(cs);
      if (!cs.length) continue;
      const { n, cont, spacing } = familyStats(cs, text.length);
      if (n >= 3 && cont >= 0.7 && spacing >= 0.8 && text.length / n >= 300) secondary.push([n, cs]);
    }
  }
  if (!chosen.some((c) => c.fam !== 'special' && c.fam !== 'md') && secondary.length) {
    chosen = chosen.concat(secondary.reduce((a, b) => (b[0] > a[0] ? b : a))[1]);
  }
  if (!chosen.length) return [];
  chosen.sort((a, b) => a.pos - b.pos);
  const out = [];
  for (const c of chosen) {
    if (out.length && out[out.length - 1][0] === c.pos) continue;
    out.push([c.pos, c.title]);
  }
  return out;
}

export const MAX_SECTION_CHARS = 40000;   // one reading section (laid out at once) stays bounded

/**
 * Split decoded text into reading sections.
 * @returns {{title:string,start:number,end:number,toc:boolean}[]}  `toc:false` = continuation of a long chapter
 */
export function buildSections(text, maxChars = MAX_SECTION_CHARS) {
  let chapters = findChapters(text);
  const n = text.length;
  let untitled = false;
  if (chapters.length) {
    if (chapters[0][0] !== 0) {
      if (text.slice(0, chapters[0][0]).trim().length > 0) chapters.unshift([0, '开篇']);
      else chapters[0] = [0, chapters[0][1]];
    }
  } else {
    chapters = [[0, '']];   // no markers: chunked below as "第 N 部分"
    untitled = true;
  }
  const out = [];
  chapters.forEach(([pos, title], i) => {
    const end = i + 1 < chapters.length ? chapters[i + 1][0] : n;
    let start = pos;
    let part = 0;
    while (start < end) {
      let cut = end;
      if (end - start > maxChars * 5 / 4) {
        const lo = start + Math.floor(maxChars / 2), hi = start + maxChars;
        let c = text.lastIndexOf('\n\n', hi);
        if (c < lo) c = text.lastIndexOf('\n', hi);
        if (c < lo) c = text.indexOf('\n', hi);
        cut = c >= lo && c < end ? c + 1 : Math.min(hi, end);
      }
      const label = untitled ? `第 ${out.length + 1} 部分` : title;
      out.push({ title: part && !untitled ? `${label}（续）` : label, start, end: cut, toc: part === 0 || untitled });
      start = cut;
      part++;
    }
  });
  if (!out.length) out.push({ title: '', start: 0, end: n, toc: true });
  return out;
}

/** Paragraphs of a section: one per non-empty line, trimmed (Chinese txt convention: one line = one paragraph). */
export function sectionParagraphs(text, sec) {
  const out = [];
  for (const line of text.slice(sec.start, sec.end).split('\n')) {
    const t = line.replace(/^[\s\u3000]+|[\s\u3000]+$/g, '');
    if (t) out.push(t);
  }
  return out;
}
