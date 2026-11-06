// DOM-free helpers for the MOBI / AZW / AZW3 / PRC adapter (parse/mobi.js). Everything here works on plain bytes so that
// node --test can exercise it. The vendored foliate-js mobi.js does NOT check for DRM, does not notice truncated files and
// knows nothing about KFX / Print Replica; these checks are ours and run BEFORE the vendored parser touches the file.
//
// Layout facts used (Palm database + MOBI, all big-endian):
//   file offset 0   : 32-byte name; 60..67 = type + creator ("BOOKMOBI"); 76 = number of records (u16)
//   file offset 78  : record table, 8 bytes per record, first 4 bytes = absolute file offset of the record
//   record 0        : PalmDOC header (+0 compression u16, +8 text records u16, +12 ENCRYPTION u16), then "MOBI" header at +16
//                     (+20 header length, +28 text encoding, +36 version, +128 EXTH flags), then optional EXTH block.

export const MOBI_EXTS = ['.mobi', '.azw', '.azw3', '.prc'];
/** the book object's `format` label for every one of the extensions above */
export const MOBI_FORMAT = 'mobi';

/** 'mobi' for .mobi/.azw/.azw3/.prc (any case), else null. `name` may be a path. */
export function mobiFormatOf(name) {
  if (typeof name !== 'string') return null;
  const i = name.lastIndexOf('.');
  if (i < 0) return null;
  return MOBI_EXTS.includes(name.slice(i).toLowerCase()) ? MOBI_FORMAT : null;
}

export const MOBI_MESSAGES = {
  'not-mobi': '不是有效的 MOBI / AZW 文件（文件头不匹配，可能是别的格式改了扩展名）',
  'palmdoc-text': '这是旧式 PalmDOC 文本（.prc/.pdb），不是 MOBI 电子书，暂不支持',
  kfx: '这是 KFX（Kindle 新格式）文件，暂不支持；请改用 EPUB、MOBI 或 AZW3 版本',
  'print-replica': '这是 Kindle 印刷版式（Print Replica / AZW4，内含 PDF）文件，暂不支持；请直接使用对应的 PDF 文件',
  drm: '这本书带有 DRM 版权保护（已加密），无法打开。本应用不提供解除保护的功能；如果你拥有这本书的合法使用权，请自行用 Calibre 等工具处理后再导入',
  truncated: '文件不完整（可能下载或传输中断，文件被截断），请重新获取完整文件后再导入',
  corrupt: '文件已损坏，无法解析',
  'bad-compression': '不支持这本书的压缩方式',
  'too-big': '这本书的文字部分过大，无法在浏览器里打开',
  'fixed-layout': '这是固定版式（漫画/杂志）的 AZW3，暂不支持',
  'no-text': '这本书里没有可阅读的文字',
  parse: '无法解析这个电子书文件（可能已损坏，或使用了特殊格式）',
};

export class MobiError extends Error {
  constructor(code, message = MOBI_MESSAGES[code] || MOBI_MESSAGES.parse) {
    super(message);
    this.name = 'MobiError';
    this.code = code;
  }
}

const dv = (b) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const u16 = (b, o) => (o + 2 <= b.length ? dv(b).getUint16(o) : undefined);
const u32 = (b, o) => (o + 4 <= b.length ? dv(b).getUint32(o) : undefined);
const ascii = (b, o, n) => { let s = ''; for (let i = o; i < o + n && i < b.length; i++) s += String.fromCharCode(b[i]); return s; };

export const PDB_HEADER_LEN = 78;
export const PEEK_RECORD0 = 128 * 1024;        // how much of record 0 the adapter hands to inspectMobi (EXTH lives there)
export const MAX_TEXT_RECORDS = 20000;         // x 4 KiB = ~80 MB of text: sanity bound for the decode-everything-up-front parser
export const SUPPORTED_COMPRESSION = [1, 2, 17480];   // none, PalmDOC, HUFF/CDIC

/** number of PDB records from the first 78 bytes, or null if too short */
export function pdbRecordCount(head) { return head && head.length >= PDB_HEADER_LEN ? u16(head, 76) : null; }

/** absolute record offsets from the record table (needs 78 + 8*n bytes in `head`), or null */
export function recordOffsets(head, numRecords) {
  if (!head || numRecords == null || head.length < PDB_HEADER_LEN + numRecords * 8) return null;
  const out = new Array(numRecords);
  for (let i = 0; i < numRecords; i++) out[i] = u32(head, PDB_HEADER_LEN + i * 8);
  return out;
}

/** EXTH block of record 0 -> {boundary, fixedLayout, ...} (only the few fields we act on); {} if none/invalid */
export function parseExth(rec0) {
  const out = {};
  const flags = u32(rec0, 128);
  const hdrLen = u32(rec0, 20);
  if (flags === undefined || hdrLen === undefined || !(flags & 0x40)) return out;
  let o = 16 + hdrLen;
  if (ascii(rec0, o, 4) !== 'EXTH') return out;
  const count = u32(rec0, o + 8);
  if (count === undefined) return out;
  o += 12;
  for (let i = 0; i < count && i < 1000; i++) {
    const type = u32(rec0, o), len = u32(rec0, o + 4);
    if (type === undefined || len === undefined || len < 8 || o + len > rec0.length) break;
    if (type === 121 && len === 12) out.boundary = u32(rec0, o + 8);
    else if (type === 122) out.fixedLayout = ascii(rec0, o + 8, len - 8);
    o += len;
  }
  return out;
}

/** encryption type of a record-0 buffer: 0 none, 1 old Mobipocket, 2 Mobipocket (undefined if too short) */
export function encryptionOf(rec0) { return u16(rec0, 12); }

/**
 * Judge a file before the vendored parser sees it.
 * @param {{head: Uint8Array, fileSize: number, rec1?: Uint8Array, boundaryRec0?: Uint8Array}} p
 *   head   = first bytes of the file: at least 78 + 8*numRecords + PEEK_RECORD0 (or the whole file if smaller)
 *   rec1   = first few bytes of record 1 (print-replica sniffing), optional
 *   boundaryRec0 = record 0 of the KF8 half of a combined file (the adapter fetches it when EXTH 121 exists), optional
 * @returns {{ok: true, numRecords, offsets, version, compression, numTextRecords, encoding, exth} | {ok: false, code, message}}
 */
export function inspectMobi({ head, fileSize, rec1 = null, boundaryRec0 = null }) {
  const bad = (code) => ({ ok: false, code, message: MOBI_MESSAGES[code] });
  if (!head || !head.length || !(fileSize > 0)) return bad('not-mobi');
  // KFX container / Ion DRM / KFX-zip are not PDB files at all
  if (ascii(head, 0, 4) === 'CONT' || (head[0] === 0xea && ascii(head, 1, 6) === 'DRMION')) return bad('kfx');
  if (ascii(head, 0, 4) === 'PK\x03\x04') return bad('kfx');   // .kfx-zip / .azw3 that is really a zip (kfx-zip or a renamed epub)
  if (head.length < PDB_HEADER_LEN || fileSize < PDB_HEADER_LEN) return bad('not-mobi');
  const type = ascii(head, 60, 4), creator = ascii(head, 64, 4);
  if (type === 'PDF ' || creator === 'CARX') return bad('print-replica');
  if (type === 'TEXt' && creator === 'REAd') return bad('palmdoc-text');
  if (type !== 'BOOK' || creator !== 'MOBI') return bad('not-mobi');

  const numRecords = u16(head, 76);
  if (!numRecords || numRecords < 2) return bad('corrupt');
  if (PDB_HEADER_LEN + numRecords * 8 > fileSize) return bad('truncated');   // not even the record table is complete
  const offsets = recordOffsets(head, numRecords);
  if (!offsets) return bad('corrupt');
  for (let i = 1; i < numRecords; i++) if (offsets[i] < offsets[i - 1]) return bad('corrupt');
  if (offsets[0] < PDB_HEADER_LEN + numRecords * 8 || offsets[0] >= fileSize) return bad(offsets[0] >= fileSize ? 'truncated' : 'corrupt');

  // record 0 (as far as the peek reaches)
  const rec0 = head.subarray(offsets[0], Math.min(head.length, offsets[1], offsets[0] + PEEK_RECORD0));
  if (rec0.length < 132 || ascii(rec0, 16, 4) !== 'MOBI') return bad(offsets[1] > fileSize ? 'truncated' : 'corrupt');

  // DRM first: a protected book is the more useful answer than "truncated", and its text must never reach the decoder
  const enc = encryptionOf(rec0);
  if (enc !== 0) return bad('drm');
  if (boundaryRec0 && encryptionOf(boundaryRec0) !== 0) return bad('drm');

  // truncation: every record must start inside the file (the table is at the front, so a cut file still lists all records)
  if (offsets[numRecords - 1] >= fileSize) return bad('truncated');

  if (rec1 && rec1.length >= 4 && (ascii(rec1, 0, 4) === '%MOP' || ascii(rec1, 0, 5) === '%PDF-')) return bad('print-replica');

  const compression = u16(rec0, 0), numTextRecords = u16(rec0, 8);
  if (!SUPPORTED_COMPRESSION.includes(compression)) return bad('bad-compression');
  if (numTextRecords >= numRecords) return bad('corrupt');
  if (numTextRecords > MAX_TEXT_RECORDS) return bad('too-big');
  const exth = parseExth(rec0);
  return { ok: true, numRecords, offsets, version: u32(rec0, 36), compression, numTextRecords, encoding: u32(rec0, 28), exth };
}

/** image MIME type from magic bytes ('' if it is not an image we show). mobi.js hands back covers as untyped Blobs. */
export function sniffImageType(b) {
  if (!b || b.length < 4) return '';
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'image/png';
  if (ascii(b, 0, 4) === 'GIF8') return 'image/gif';
  if (ascii(b, 0, 4) === 'RIFF' && ascii(b, 8, 4) === 'WEBP') return 'image/webp';
  if (b[0] === 0x42 && b[1] === 0x4d) return 'image/bmp';
  return '';
}

// ---------------------------------------------------------------- internal link forms
// The sanitizer only lets "#..." and scheme-less hrefs through, so a MOBI's `filepos` / `kindle:pos` targets are rewritten to
//   MOBI6: #filepos<digits>       (the id of the <a id="filepos<digits>"> anchor mobi.js inserts; digits kept verbatim)
//   KF8  : #kindle:pos:fid:XXXX:off:YYYYYYYYYY
// and parsed back here by the adapter's resolveHref.
export const fileposId = (digits) => `filepos${digits}`;
export const fileposHref = (digits) => `#filepos${digits}`;

/** '#filepos0000012345' | 'filepos:0000012345' | 'filepos0000012345' -> {digits, number, id} or null */
export function parseFilepos(href) {
  const m = /^#?filepos:?(\d{1,12})$/i.exec(String(href || '').trim());
  return m ? { digits: m[1], number: Number(m[1]), id: fileposId(m[1]) } : null;
}

/** '#kindle:pos:fid:0001:off:0000000000' -> {fid, off, raw} (base-32 numbers) or null */
export function parseKindlePos(href) {
  const m = /^#?(kindle:pos:fid:([0-9a-v]{1,8}):off:([0-9a-v]{1,12}))$/i.exec(String(href || '').trim());
  if (!m) return null;
  return { fid: parseInt(m[2], 32), off: parseInt(m[3], 32), raw: m[1] };
}
export const kindlePosHref = (raw) => '#' + raw;
