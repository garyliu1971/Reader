// Defensive PROPFIND (multistatus) parsing + URL/host safety helpers. No DOM, so it runs in node tests and workers.
// The server is untrusted: DTD/entities are refused outright, body size is capped, only direct children are returned.

import { safeSegment, isHiddenName } from '../core/names.js';

export const PROPFIND_MAX_BYTES = 8 * 1024 * 1024;
export const MAX_RESPONSES = 20000;

export class DavError extends Error {
  constructor(msg, status = null) { super(msg); this.name = 'DavError'; this.status = status; }
}

const cmpKey = (s) => s.normalize('NFC').toLowerCase();

const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
export function unescapeXml(s) {
  return s.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);/g, (m, g) => {
    if (g[0] === '#') {
      const cp = g[1] === 'x' ? parseInt(g.slice(2), 16) : parseInt(g.slice(1), 10);
      return Number.isFinite(cp) && cp > 0 && cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff) ? String.fromCodePoint(cp) : '';
    }
    return ENT[g];
  });
}

function elementText(block, name) {
  const re = new RegExp(`<(?:[\\w.-]+:)?${name}(?:\\s[^>]*)?>([\\s\\S]*?)</(?:[\\w.-]+:)?${name}\\s*>`);
  const m = re.exec(block);
  if (!m) return null;
  const inner = m[1].replace(/^\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*$/, (_, c) => c.replace(/&/g, '&amp;').replace(/</g, '&lt;'));
  return unescapeXml(inner).trim();
}

function pathOf(href) {
  let p = href;
  const m = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^/?#]*/.exec(p);
  if (m) p = p.slice(m[0].length);
  p = p.split(/[?#]/, 1)[0];
  try { return decodeURIComponent(p); } catch { return null; }
}

/**
 * @param {string} xml response text
 * @param {string} requestPath decoded URL path that was requested (e.g. "/dav/library")
 * @returns {{name:string,isDir:boolean,size:number,mtime:number,etag:string}[]} direct children only
 */
export function parseMultistatus(xml, requestPath, { maxBytes = PROPFIND_MAX_BYTES } = {}) {
  if (typeof xml !== 'string') throw new DavError('bad multistatus: not text');
  if (xml.length > maxBytes) throw new DavError('multistatus too large');
  if (/<!doctype|<!entity/i.test(xml)) throw new DavError('unsafe multistatus XML (DTD/entity not allowed)');
  const text = xml.replace(/<!--[\s\S]*?-->/g, '');
  if (!/<(?:[\w.-]+:)?multistatus[\s>]/.test(text)) throw new DavError('bad multistatus XML');
  const baseKey = cmpKey(requestPath.replace(/\/+$/, ''));
  const out = [];
  const respRe = /<(?:[\w.-]+:)?response(?:\s[^>]*)?>([\s\S]*?)<\/(?:[\w.-]+:)?response\s*>/g;
  let rm;
  let count = 0;
  while ((rm = respRe.exec(text)) !== null) {
    if (++count > MAX_RESPONSES) throw new DavError('multistatus: too many entries');
    const block = rm[1];
    const href = elementText(block, 'href') || '';
    const hp = pathOf(href);
    if (hp === null) continue;
    let isDir = false, size = 0, mtime = 0, etag = '';
    const psRe = /<(?:[\w.-]+:)?propstat(?:\s[^>]*)?>([\s\S]*?)<\/(?:[\w.-]+:)?propstat\s*>/g;
    let pm;
    while ((pm = psRe.exec(block)) !== null) {
      const ps = pm[1];
      const st = elementText(ps, 'status') || 'HTTP/1.1 200 OK';
      if (!/\s200(\s|$)/.test(st)) continue;
      const rt = /<(?:[\w.-]+:)?resourcetype(?:\s[^>]*)?>([\s\S]*?)<\/(?:[\w.-]+:)?resourcetype\s*>/.exec(ps);
      if (rt && /<(?:[\w.-]+:)?collection\b/.test(rt[1])) isDir = true;
      const cl = elementText(ps, 'getcontentlength');
      if (cl && /^\d{1,15}$/.test(cl)) size = parseInt(cl, 10);
      const lm = elementText(ps, 'getlastmodified');
      if (lm) { const t = Date.parse(lm); if (Number.isFinite(t)) mtime = t / 1000; }
      const et = elementText(ps, 'getetag');
      if (et) etag = et.slice(0, 200);
    }
    const full = hp.replace(/\/+$/, '');
    if (cmpKey(full) === baseKey) continue;   // the collection itself
    const i = full.lastIndexOf('/');
    const parent = i < 0 ? '' : full.slice(0, i);
    const name = full.slice(i + 1);
    // direct children only: drops '..' traversal, other paths, '.' and empty names
    if (cmpKey(parent) !== baseKey || !name || name === '.' || name === '..') continue;
    out.push({ name, isDir, size, mtime, etag });
  }
  return out;
}

/** Entry names the library listing accepts at all (before extension filtering). */
export function acceptableName(name) {
  return safeSegment(name) !== null && !isHiddenName(name) && !name.toLowerCase().endsWith('.part');
}

// ---------------------------------------------------------------- transport safety (port of webdav.host_is_private)
export function hostIsPrivate(host) {
  const h = (host || '').trim().replace(/^\[|\]$/g, '').toLowerCase().replace(/\.$/, '');
  if (!h) return false;
  if (h === 'localhost' || h.endsWith('.localhost')) return true;
  const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    if ([1, 2, 3, 4].some((i) => Number(v4[i]) > 255)) return false;
    return a === 10 || a === 127 || a === 0 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)
      || (a === 169 && b === 254) || (a === 100 && b >= 64 && b <= 127);
  }
  if (h.includes(':')) {   // IPv6 literal
    return h === '::1' || h.startsWith('fe80:') || /^f[cd][0-9a-f]{2}:/.test(h);
  }
  return !h.includes('.') || /\.(local|lan|home|internal|home\.arpa|intranet)$/.test(h);
}

/**
 * Validate the user-entered server URL. Plain http:// is only allowed for private/local hosts.
 * @returns {{ok:boolean, url?:string, error?:string}}
 */
export function checkServerUrl(input) {
  let u;
  try { u = new URL((input || '').trim()); } catch { return { ok: false, error: '网址格式不正确' }; }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return { ok: false, error: '只支持 http(s) 网址' };
  if (u.username || u.password) return { ok: false, error: '请不要把账号密码写进网址，分开填写' };
  if (u.protocol === 'http:' && !hostIsPrivate(u.hostname)) {
    return { ok: false, error: '公网主机必须使用 https://（拒绝以明文发送密码）' };
  }
  u.hash = ''; u.search = '';
  return { ok: true, url: u.toString().replace(/\/+$/, '') };
}
