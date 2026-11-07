// Read-aloud panel UX logic (pure, no DOM): which engine is in use, the status line, the online-voice group of the reader's
// voice dropdown and what a dropdown choice means. The reader panel and the app settings page share the stored cloud config
// (store/cloudcfg.js), so both dropdowns edit the same `voice` value.
import { cloudReady, FALLBACK_VOICES } from './cloudtts.js';

export const CLOUD_PREFIX = 'cloud:';
export const GROUP_LABEL = '在线（Azure）';
export const HINT_INCOMPLETE = '已填写令牌，但未启用在线朗读';
export const HINT_NO_ONLINE = '在线朗读尚未就绪：请在 书架 → 设置 → 在线朗读 中填写令牌并启用。';

/** online reading is on AND the user did not pick a browser voice in the reader panel */
export const cloudActive = (c) => cloudReady(c) && c.engine !== 'browser';

/** a token is saved but enabled/consent is missing */
export const tokenIncomplete = (c) => !!c && !!c.token && !cloudReady(c);

export const cloudVoiceList = (c) => (c && c.voices && c.voices.length ? c.voices : FALLBACK_VOICES);

export function voiceName(c, id) {
  if (!id) return '自动';
  const v = cloudVoiceList(c).find((x) => x.id === id);
  return v ? v.name : id;
}

/** {kind: 'browser'|'cloud', text, warn?} for the status line */
export function engineStatus(c) {
  if (cloudActive(c)) return { kind: 'cloud', text: `当前使用：在线语音（${voiceName(c, c.voice)}）`, warn: '' };
  return { kind: 'browser', text: '当前使用：浏览器语音', warn: tokenIncomplete(c) ? HINT_INCOMPLETE : '' };
}

/** one string for a status line element */
export const statusText = (c) => { const s = engineStatus(c); return s.warn ? `${s.text}。${s.warn}` : s.text; };

/** online options for the dropdown group; empty unless online reading is ready */
export function cloudOptions(c) {
  if (!cloudReady(c)) return [];
  return [{ value: CLOUD_PREFIX, label: '在线：自动（按文字语言选择）' },
    ...cloudVoiceList(c).map((v) => ({ value: CLOUD_PREFIX + v.id, label: `${v.name}（${v.lang}${v.gender ? ' · ' + v.gender : ''}）` }))];
}

/** dropdown value that reflects the current state */
export function selectedValue(c, ttsVoice) {
  return cloudActive(c) ? CLOUD_PREFIX + (c.voice || '') : (ttsVoice || '');
}

/**
 * What choosing `value` does. Returns {cloudPatch, ttsVoice, hint}: cloudPatch is merged into the cloud config
 * (null = none), ttsVoice is the new browser voice (undefined = unchanged), hint is a message to show (or '').
 */
export function applyChoice(value, c) {
  if (typeof value === 'string' && value.startsWith(CLOUD_PREFIX)) {
    const voice = value.slice(CLOUD_PREFIX.length);
    if (!cloudReady(c)) return { cloudPatch: null, ttsVoice: undefined, hint: tokenIncomplete(c) ? HINT_INCOMPLETE : HINT_NO_ONLINE };
    return { cloudPatch: { voice, engine: 'cloud' }, ttsVoice: undefined, hint: '' };
  }
  return { cloudPatch: c && c.engine !== 'browser' ? { engine: 'browser' } : null, ttsVoice: value || '', hint: '' };
}
