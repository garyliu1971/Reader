// book_id rules, identical to pyreader/sync.py book_id() and progress_path().
import { sha1Hex } from './sha1.js';

/** Local file: "local:" + sha1(utf8(basename + String(size))). Raw name, no normalization (same as Qt). */
export function localBookId(fileName, size) {
  return 'local:' + sha1Hex(`${fileName}${size}`);
}

/** Remote library copy: "remote:" + rel, verbatim. */
export function remoteBookId(rel) {
  return 'remote:' + rel;
}

/** Remote file name of a per-book progress record: sha1(book_id)[:20]. */
/**
 * Other ids the SAME book may be keyed by on another device. The Qt app keys a book by "local:"+sha1(name+size) when it
 * reads it from an ordinary folder, but by "remote:<rel>" only for copies under its remote_books cache; the web app
 * always uses "remote:<rel>" for library downloads. Without aliasing the two sides write/read different progress files.
 */
export function aliasIds(meta) {
  if (!meta || meta.source !== 'remote' || !meta.name || !(meta.size > 0)) return [];
  const a = localBookId(meta.name, meta.size);
  return a === meta.id ? [] : [a];
}

export function progressFileName(bid) {
  return sha1Hex(bid).slice(0, 20) + '.json';
}

export function progressPath(user, bid) {
  return `users/${user || 'default'}/progress/${progressFileName(bid)}`;
}
