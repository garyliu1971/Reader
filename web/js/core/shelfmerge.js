// Device-file overlay logic, ported from pyreader/shelf.py (validate_overlay / merge / set_field / set_progress /
// prune_progress / to_wire / reconcile_own). The Python code is authoritative; keep the two in step.
//
// View form (in memory):
//   {version:1, _view:true, device_id, _clock:[...],
//    entries:{bid:{fav:[bool,ts,dev], hidden:[bool,ts,dev], meta:[{fp,title},ts,dev]}},
//    progress:{bid:{percent, updated(sec), device}}}
// Wire form (to_wire): registers are [value, ts_ms]; the writer is the file name's device_id.
// Unknown fields / rekeyed_from are not preserved (the web app never writes them; each device only writes its own file).

import { safeComponent, validBookId } from './names.js';

export const WIRE_VERSION = 1;
export const FUTURE_LIMIT_MS = 24 * 3600 * 1000;
export const CLAMPED_TS = 1;
export const MAX_ENTRIES = 20000;
export const MAX_STR = 256;
export const DIGEST_KEEP = 500;
export const MAX_WIRE_BYTES = 1024 * 1024;
const REG_FIELDS = ['fav', 'hidden'];
const BIDI_RE = /[؜‎‏‪-‮⁦-⁩]/u;

/** canonical JSON (sorted keys) used for deterministic tie-breaks */
export function canon(v) {
  return JSON.stringify(sortKeys(v));
}
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === 'object') {
    const o = {};
    for (const k of Object.keys(v).sort()) o[k] = sortKeys(v[k]);
    return o;
  }
  return v;
}

export function cleanStr(s, limit = MAX_STR) {
  if (typeof s !== 'string') return null;
  let out = '';
  for (const ch of s) {
    if (/\p{Cc}|\p{Cs}/u.test(ch) || BIDI_RE.test(ch)) continue;
    out += ch;
  }
  return Array.from(out).slice(0, limit).join('');
}

function num(x, dflt = null) {
  return typeof x === 'number' && Number.isFinite(x) ? x : dflt;
}

/** -> [ts, clamped] | [null,false] */
function parseTs(x, nowMs) {
  if (typeof x !== 'number' || !Number.isFinite(x) || !Number.isInteger(x) || x <= 0) return [null, false];
  if (x > nowMs + FUTURE_LIMIT_MS) return [CLAMPED_TS, true];
  return [x, false];
}

function parseReg(raw, nowMs, devDefault, view) {
  if (!Array.isArray(raw) || raw.length < 2) return null;
  const [ts, clamped] = parseTs(raw[1], nowMs);
  if (ts === null) return null;
  let dev = devDefault;
  if (view && raw.length >= 3) {
    const d = safeComponent(raw[2]);
    if (d) dev = d;
  }
  if (!dev) return null;
  return { value: raw[0], ts, dev, clamped };
}

function cleanMeta(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const out = {};
  if (v.fp != null) {
    const f = safeComponent(v.fp);
    if (f) out.fp = f;
  }
  const t = cleanStr(v.title);
  if (t) out.title = t;
  return Object.keys(out).length ? out : null;
}

/**
 * Validate one overlay (remote content must pass here before merging).
 * @param opts.deviceId writer taken from the validated file name (JSON-internal device_id is then ignored)
 * @returns {[clean|null, issues[]]}
 */
export function validateOverlay(raw, nowMs = Date.now(), opts = {}) {
  const issues = [];
  let view = !!opts.view;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return [null, ['overlay: not an object']];
  const ver = raw.version === undefined ? WIRE_VERSION : raw.version;
  if (typeof ver !== 'number' || !Number.isInteger(ver)) return [null, ['overlay: bad version']];
  if (ver > WIRE_VERSION) return [null, [`overlay: version ${ver} is newer than supported`]];
  let dev;
  if (opts.deviceId !== undefined && opts.deviceId !== null) {
    dev = safeComponent(opts.deviceId);
    if (!dev) return [null, ['overlay: bad device_id']];
    view = false;
  } else {
    dev = raw.device_id != null ? safeComponent(raw.device_id) : null;
    if (raw.device_id != null && !dev) return [null, ['overlay: bad device_id']];
    view = view || raw._view === true;
    if (!dev && !view) return [null, ['overlay: missing device_id']];
  }
  const nowS = nowMs / 1000;
  const clock = new Set();
  if (view && Array.isArray(raw._clock)) for (const x of raw._clock) { const d = safeComponent(x); if (d) clock.add(d); }

  const outEntries = {};
  let entries = raw.entries === undefined ? {} : raw.entries;
  if (!entries || typeof entries !== 'object' || Array.isArray(entries)) { issues.push('entries: not an object'); entries = {}; }
  let bids = Object.keys(entries).sort();
  if (bids.length > MAX_ENTRIES) { issues.push('entries: too many, truncated'); bids = bids.slice(0, MAX_ENTRIES); }
  for (const bid of bids) {
    const ent = entries[bid];
    if (!validBookId(bid) || !ent || typeof ent !== 'object' || Array.isArray(ent)) { issues.push(`entry ${bid.slice(0, 60)}: skipped`); continue; }
    const clean = {};
    for (const f of REG_FIELDS) {
      if (!(f in ent)) continue;
      const r = parseReg(ent[f], nowMs, dev, view);
      if (r === null || typeof r.value !== 'boolean') { issues.push(`entry ${bid.slice(0, 60)}.${f}: skipped`); continue; }
      clean[f] = [r.value, r.ts, r.dev];
      if (r.clamped) clock.add(r.dev);
    }
    if ('meta' in ent) {
      const r = parseReg(ent.meta, nowMs, dev, view);
      const m = r ? cleanMeta(r.value) : null;
      if (r === null || m === null) issues.push(`entry ${bid.slice(0, 60)}.meta: skipped`);
      else { clean.meta = [m, r.ts, r.dev]; if (r.clamped) clock.add(r.dev); }
    }
    if (Object.keys(clean).length) outEntries[bid] = clean;
  }

  const outProg = {};
  let prog = raw.progress === undefined ? {} : raw.progress;
  if (!prog || typeof prog !== 'object' || Array.isArray(prog)) { issues.push('progress: not an object'); prog = {}; }
  let pids = Object.keys(prog).sort();
  if (pids.length > MAX_ENTRIES) { issues.push('progress: too many, truncated'); pids = pids.slice(0, MAX_ENTRIES); }
  for (const bid of pids) {
    const rec = prog[bid];
    if (!validBookId(bid) || !rec || typeof rec !== 'object' || Array.isArray(rec)) { issues.push(`progress ${bid.slice(0, 60)}: skipped`); continue; }
    const pct = num(rec.percent);
    let upd = num(rec.updated);
    if (pct === null || upd === null || upd <= 0) { issues.push(`progress ${bid.slice(0, 60)}: skipped`); continue; }
    let d = dev;
    if (view) d = safeComponent(rec.device) || dev;
    if (!d) continue;
    if (upd > nowS + FUTURE_LIMIT_MS / 1000) { upd = CLAMPED_TS / 1000; clock.add(d); }
    outProg[bid] = { percent: Math.min(100, Math.max(0, pct)), updated: upd, device: d };
  }

  const clean = { version: WIRE_VERSION, _view: true, entries: outEntries, progress: outProg, _clock: [...clock].sort() };
  if (dev) clean.device_id = dev;
  if (opts.deviceId != null || !view) {
    const h = cleanStr(raw.hostname, 64);
    if (h) clean.hostname = h;
    const u = raw.user != null ? safeComponent(raw.user) : null;
    if (u) clean.user = u;
  }
  return [clean, issues];
}

function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
function regCmp(a, b) {   // (ts, dev, canon(value)) lexicographic
  return cmp(a[1], b[1]) || cmp(a[2], b[2]) || cmp(canon(a[0]), canon(b[0]));
}

/** Merge any number of overlays (raw or views). LWW registers; progress by (updated, device, percent). */
export function merge(overlays, nowMs = Date.now()) {
  const entries = {};
  const progress = {};
  const clock = new Set();
  for (const ov of overlays) {
    const isView = ov && typeof ov === 'object' && ov._view === true;
    const [clean] = validateOverlay(ov, nowMs, isView ? { view: true } : {});
    if (!clean) continue;
    clean._clock.forEach((d) => clock.add(d));
    for (const [bid, ent] of Object.entries(clean.entries)) {
      const cur = entries[bid] || (entries[bid] = {});
      for (const [k, v] of Object.entries(ent)) if (!(k in cur) || regCmp(v, cur[k]) > 0) cur[k] = v;
    }
    for (const [bid, rec] of Object.entries(clean.progress)) {
      const old = progress[bid];
      const c = old ? (cmp(rec.updated, old.updated) || cmp(rec.device, old.device) || cmp(rec.percent, old.percent)) : 1;
      if (c > 0) progress[bid] = rec;
    }
  }
  return { version: WIRE_VERSION, _view: true, entries, progress, _clock: [...clock].sort() };
}

export function newOverlay(deviceId, hostname = '', user = '') {
  const dev = safeComponent(deviceId);
  if (!dev) throw new Error('bad device_id');
  const ov = { version: WIRE_VERSION, _view: true, device_id: dev, entries: {}, progress: {}, _clock: [], _rev: 0 };
  const h = hostname && cleanStr(hostname, 64);
  if (h) ov.hostname = h;
  if (user && safeComponent(user)) ov.user = safeComponent(user);
  return ov;
}

export function fieldValue(view, bid, name, dflt = undefined) {
  const reg = ((view || {}).entries || {})[bid]?.[name];
  return Array.isArray(reg) && reg.length >= 2 ? reg[0] : dflt;
}

export function effectiveTs(view, bid, name, nowMs) {
  const reg = ((view || {}).entries || {})[bid]?.[name];
  if (Array.isArray(reg) && reg.length >= 2) return parseTs(reg[1], nowMs)[0] || 0;
  return 0;
}

/**
 * Write fav/hidden on this device. Hybrid logical clock: ts = max(now + skew, effective ts of the merged view + 1),
 * so a user's click always beats what they just saw. Returns the ts written. Bumps ov._rev.
 */
export function setField(ov, bid, name, value, { merged = null, nowMs = Date.now(), skewMs = 0, meta = null } = {}) {
  const dev = ov && safeComponent(ov.device_id);
  if (!dev) throw new Error('overlay has no device_id');
  if (!REG_FIELDS.includes(name)) throw new Error(`unknown field ${name}`);
  if (typeof value !== 'boolean') throw new Error('value must be bool');
  if (!validBookId(bid)) throw new Error('bad book id');
  const base = Math.trunc(nowMs) + Math.trunc(skewMs);
  ov.entries = ov.entries || {};
  if (!(bid in ov.entries) && Object.keys(ov.entries).length >= MAX_ENTRIES) throw new Error('too many overlay entries');
  const eff = (f) => Math.max(effectiveTs(ov, bid, f, base), effectiveTs(merged, bid, f, base));
  const ts = Math.max(base, eff(name) + 1);
  const ent = ov.entries[bid] || (ov.entries[bid] = {});
  ent[name] = [value, ts, dev];
  if (meta) {
    const m = cleanMeta(meta);
    if (m) {
      let cur = fieldValue(merged, bid, 'meta');
      if (cur === undefined) cur = fieldValue(ov, bid, 'meta');
      if (canon(cur) !== canon(m)) ent.meta = [m, Math.max(base, eff('meta') + 1), dev];
    }
  }
  ov._rev = (ov._rev || 0) + 1;
  return ts;
}

export function setProgress(ov, bid, percent, updatedSec = Date.now() / 1000) {
  const dev = ov && safeComponent(ov.device_id);
  if (!dev || !validBookId(bid)) throw new Error('bad overlay or book id');
  const p = num(percent);
  const u = num(updatedSec);
  if (p === null || u === null) throw new Error('bad percent/updated');
  ov.progress = ov.progress || {};
  if (!(bid in ov.progress) && Object.keys(ov.progress).length >= MAX_ENTRIES) throw new Error('too many progress entries');
  ov.progress[bid] = { percent: Math.min(100, Math.max(0, p)), updated: u, device: dev };
  ov._rev = (ov._rev || 0) + 1;
}

/** Keep only the `keep` most recently updated progress digests (ties: larger book id stays). Returns #dropped. */
export function pruneProgress(ov, keep = DIGEST_KEEP) {
  const prog = ov && ov.progress;
  if (!prog || typeof prog !== 'object') return 0;
  const ids = Object.keys(prog);
  if (ids.length <= keep) return 0;
  ids.sort((a, b) => cmp(num(prog[b]?.updated, 0), num(prog[a]?.updated, 0)) || cmp(b, a));
  const drop = ids.slice(Math.max(0, keep));
  for (const b of drop) delete prog[b];
  ov._rev = (ov._rev || 0) + 1;
  return drop.length;
}

/** Whitelisted upload form. Only this device's registers/progress; no `_` keys, no unknown fields. */
export function toWire(ov, deviceId = null) {
  const dev = safeComponent(deviceId != null ? deviceId : ov && ov.device_id);
  if (!dev) throw new Error('overlay has no device_id');
  const wire = { version: WIRE_VERSION, device_id: dev };
  const h = cleanStr(ov.hostname, 64);
  if (h) wire.hostname = h;
  const u = ov.user != null ? safeComponent(ov.user) : null;
  if (u) wire.user = u;
  const ents = {};
  const src = ov.entries && typeof ov.entries === 'object' ? ov.entries : {};
  const posInt = (x) => typeof x === 'number' && Number.isInteger(x) && x > 0;
  for (const bid of Object.keys(src).sort()) {
    const ent = src[bid];
    if (!validBookId(bid) || !ent || typeof ent !== 'object') continue;
    const out = {};
    for (const f of REG_FIELDS) {
      const reg = ent[f];
      if (Array.isArray(reg) && reg.length >= 2 && typeof reg[0] === 'boolean' && posInt(reg[1]) && (reg.length < 3 || reg[2] === dev)) {
        out[f] = [reg[0], reg[1]];
      }
    }
    const reg = ent.meta;
    if (Array.isArray(reg) && reg.length >= 2 && posInt(reg[1]) && (reg.length < 3 || reg[2] === dev)) {
      const m = cleanMeta(reg[0]);
      if (m) out.meta = [m, reg[1]];
    }
    if (Object.keys(out).length) ents[bid] = out;
  }
  wire.entries = ents;
  const prog = {};
  const psrc = ov.progress && typeof ov.progress === 'object' ? ov.progress : {};
  for (const bid of Object.keys(psrc).sort()) {
    const rec = psrc[bid];
    if (!validBookId(bid) || !rec || typeof rec !== 'object' || (rec.device !== undefined && rec.device !== dev)) continue;
    const pct = num(rec.percent), upd = num(rec.updated);
    if (pct === null || upd === null || upd <= 0) continue;
    prog[bid] = { percent: Math.min(100, Math.max(0, pct)), updated: upd };
  }
  wire.progress = prog;
  return wire;
}

/** Deterministic JSON text (sorted keys, indent 1) like Python's wire_bytes. */
export function wireText(ov, deviceId = null) {
  return JSON.stringify(sortKeys(toWire(ov, deviceId)), null, 1);
}

/** Content hash string (entries + progress only): hostname differences alone do not count as a change. */
export function contentKey(ov, deviceId = null) {
  const w = toWire(ov, deviceId != null ? deviceId : ov.device_id);
  return canon([w.entries, w.progress]);
}

export function hasContent(ov) {
  return !!ov && (Object.keys(ov.entries || {}).length > 0 || Object.keys(ov.progress || {}).length > 0);
}

/**
 * Self-healing against our own remote file. `remote` = validateOverlay(body, {deviceId: own}) view.
 * @returns {{adopted: object|null, behind: boolean}} adopted = local with remote's newer values merged in (or null);
 *          behind = remote lacks something local has -> re-PUT.
 */
export function reconcileOwn(local, remote, nowMs = Date.now()) {
  const m = merge([local, remote], nowMs);
  const cand = { ...local, entries: m.entries, progress: m.progress, _clock: m._clock };
  pruneProgress(cand);
  const changed = contentKey(cand) !== contentKey(local);
  const behind = contentKey(cand) !== contentKey(remote, local.device_id);
  return { adopted: changed ? cand : null, behind };
}
