// Pure TXT pipeline (runs in a Worker; also used on the main thread as fallback and by tests):
// bytes -> decoded text -> sections -> per-section text lengths.
import { decodeText, unwrapLatinText } from '../core/textenc.js';
import { buildSections, sectionParagraphs } from '../core/chapters.js';

export function parseTxt(bytes) {
  const { text: decoded, encoding } = decodeText(bytes);
  const text = unwrapLatinText(decoded);
  const sections = buildSections(text);
  const lens = sections.map((s) => sectionParagraphs(text, s).reduce((n, p) => n + p.length, 0));
  return { text, sections, lens, encoding };
}
