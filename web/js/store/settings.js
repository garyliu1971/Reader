// Reader display settings (per device, persisted in IndexedDB). Applying them = setting CSS variables / data attributes.
import { kvGet, kvSet } from './db.js';

export const DEFAULTS = {
  theme: 'auto',            // auto | light | dark | sepia | eye
  fontSize: 19,             // px
  fontFamily: 'serif',      // serif | sans | kai | system
  lineHeight: 1.85,
  margin: 28,               // px, left/right text margin
  indent: true,             // 2em first-line indent
  justify: true,
  paraSpace: 0.35,          // em between paragraphs
  mode: 'paged',            // paged | scroll
};
export const LIMITS = { fontSize: [12, 40], lineHeight: [1.3, 2.6], margin: [8, 80], paraSpace: [0, 1.2] };

export const FONT_STACKS = {
  serif: '"Noto Serif SC","Source Han Serif SC","Songti SC","STSong","SimSun","Noto Serif",Georgia,serif',
  sans: '"PingFang SC","Microsoft YaHei","Noto Sans SC","Source Han Sans SC",system-ui,-apple-system,"Segoe UI",sans-serif',
  kai: '"Kaiti SC","STKaiti","KaiTi","BiauKai","DFKai-SB",serif',
  system: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif',
};

const clamp = (v, [lo, hi]) => Math.min(hi, Math.max(lo, v));

export function normalize(s) {
  const o = { ...DEFAULTS, ...(s || {}) };
  if (!['auto', 'light', 'dark', 'sepia', 'eye'].includes(o.theme)) o.theme = 'auto';
  if (!(o.fontFamily in FONT_STACKS)) o.fontFamily = 'serif';
  if (!['paged', 'scroll'].includes(o.mode)) o.mode = 'paged';
  for (const k of Object.keys(LIMITS)) { o[k] = Number(o[k]); if (!Number.isFinite(o[k])) o[k] = DEFAULTS[k]; o[k] = clamp(o[k], LIMITS[k]); }
  o.indent = !!o.indent; o.justify = !!o.justify;
  return o;
}

export async function loadSettings() { return normalize(await kvGet('reader.settings', null)); }
export function saveSettings(s) { return kvSet('reader.settings', s); }

/** Apply to the document root: the whole app (shelf and reader) follows the theme, not just the page area. */
export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', theme);
  const dark = theme === 'dark' || (theme === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue('--bg').trim() || (dark ? '#121212' : '#ffffff');
}

export function applyTypography(el, s) {
  el.style.setProperty('--fs', s.fontSize + 'px');
  el.style.setProperty('--lh', String(s.lineHeight));
  el.style.setProperty('--mx', s.margin + 'px');
  el.style.setProperty('--ff', FONT_STACKS[s.fontFamily]);
  el.style.setProperty('--ps', s.paraSpace + 'em');
  el.dataset.indent = s.indent ? '1' : '0';
  el.dataset.justify = s.justify ? '1' : '0';
}
