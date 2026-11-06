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
export function progressFileName(bid) {
  return sha1Hex(bid).slice(0, 20) + '.json';
}

export function progressPath(user, bid) {
  return `users/${user || 'default'}/progress/${progressFileName(bid)}`;
}
