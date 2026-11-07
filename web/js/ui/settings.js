// App settings dialog: WebDAV sync, appearance, storage, about.
import { h, openPanel, toast, confirmDialog, fmtSize } from './dom.js';
import { syncMgr } from '../sync/manager.js';
import { loadSettings, saveSettings, applyTheme } from '../store/settings.js';
import { storageEstimate, persistRequest, storageMode, db } from '../store/db.js';
import { checkServerUrl } from '../sync/propfind.js';
import { corsNote } from '../core/origin.js';
import { CONSENT_TEXT, FALLBACK_VOICES, curateVoices, formatUsage, cloudReady } from '../core/cloudtts.js';
import { getCloudCfg, saveCloudCfg } from '../store/cloudcfg.js';
import { CloudApi, CloudEngine } from '../reader/cloudengine.js';

export const APP_VERSION = '0.1.0';

export async function openSettingsDialog({ onChange = () => {} } = {}) {
  const cfg = syncMgr.cfg;
  const url = h('input', { type: 'url', value: cfg.url, placeholder: 'https://dav.example.com/dav', autocomplete: 'off', spellcheck: false, 'aria-label': 'WebDAV 地址' });
  const user = h('input', { type: 'text', value: cfg.user, autocomplete: 'off', spellcheck: false, 'aria-label': '用户名' });
  const pass = h('input', { type: 'password', value: cfg.password, autocomplete: 'off', 'aria-label': '密码' });
  const syncUser = h('input', { type: 'text', value: cfg.syncUser, autocomplete: 'off', spellcheck: false, 'aria-label': '同步用户名' });
  const devName = h('input', { type: 'text', value: cfg.deviceName, maxLength: 64, 'aria-label': '设备名称' });
  const devId = h('input', { type: 'text', value: cfg.deviceId, autocomplete: 'off', spellcheck: false, 'aria-label': '设备 ID' });
  const enabled = h('input', { type: 'checkbox', checked: !!cfg.enabled, id: 'sync-enabled' });
  const msg = h('p', { class: 'hint', role: 'status' });
  const urlWarn = h('div', { class: 'warn', hidden: true });
  const checkUrl = () => {
    const r = url.value.trim() ? checkServerUrl(url.value) : { ok: true };
    urlWarn.hidden = r.ok;
    urlWarn.textContent = r.ok ? '' : r.error;
  };
  const corsHint = h('p', { class: 'hint' });
  const updateCors = () => { corsHint.textContent = corsNote(url.value, location.origin); };
  updateCors();
  url.addEventListener('input', () => { checkUrl(); updateCors(); });

  const form = () => ({ enabled: enabled.checked, url: url.value, user: user.value, password: pass.value, syncUser: syncUser.value, deviceName: devName.value, deviceId: devId.value });
  const saveBtn = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
    const f = form();
    if (f.enabled && !f.url.trim()) { msg.textContent = '请填写 WebDAV 地址'; return; }
    const r = await syncMgr.saveConfig(f);
    if (!r.ok) { msg.textContent = r.errors.join('；'); return; }
    msg.textContent = '已保存。';
    if (syncMgr.enabled()) {
      saveBtn.disabled = true; msg.textContent = '已保存，正在测试连接…';
      try {
        const t = await syncMgr.test();
        msg.textContent = `连接成功${t.libraryExists ? '' : '（服务器上还没有 library 文件夹）'}。正在同步…`;
        const res = await syncMgr.syncNow({ force: true });
        msg.textContent = res && res.errors.length ? `同步失败：${res.errors[0].message}` : `同步完成：${syncMgr.lib.files.length} 本云端书籍。`;
      } catch (e) { msg.textContent = `连接失败：${e && e.message ? e.message : e}`; }
      saveBtn.disabled = false;
    }
    onChange();
  } }, '保存并测试连接');
  const offBtn = h('button', { class: 'btn', type: 'button', onclick: async () => {
    if (!(await confirmDialog('关闭同步并清除保存在本浏览器里的服务器账号和密码？（本地书籍与进度保留）', { ok: '关闭并清除' }))) return;
    await syncMgr.disable();
    user.value = ''; pass.value = ''; enabled.checked = false;
    msg.textContent = '已关闭同步并清除凭据。';
    onChange();
  } }, '关闭同步并清除凭据');

  const settings = await loadSettings();
  const themeSeg = h('div', { class: 'seg' }, [['auto', '跟随系统'], ['light', '明亮'], ['dark', '夜间'], ['sepia', '护眼黄'], ['eye', '护眼绿']].map(([v, l]) => h('button', {
    type: 'button', dataset: { v }, 'aria-pressed': String(settings.theme === v),
    onclick: async () => { settings.theme = v; applyTheme(v); await saveSettings(settings); themeSeg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === v))); },
  }, l)));

  const storageInfo = h('p', { class: 'hint' });
  const refreshStorage = async () => {
    const est = await storageEstimate();
    const persisted = navigator.storage && navigator.storage.persisted ? await navigator.storage.persisted().catch(() => false) : false;
    storageInfo.textContent = `${storageMode() === 'memory' ? '⚠ 浏览器禁用了持久存储，本次会话的书籍关闭页面后会丢失。' : ''}${est ? `已用 ${fmtSize(est.usage || 0)} / 配额 ${fmtSize(est.quota || 0)}。` : ''}${persisted ? '（已申请持久存储）' : ''}`;
  };
  refreshStorage();


  // ---- online read-aloud (opt-in per device)
  const cc = getCloudCfg();
  const api = new CloudApi({ fetchFn: (...a) => fetch(...a), getCfg: getCloudCfg, baseHref: location.href });
  const cloudMsg = h('p', { class: 'hint', role: 'status' });
  const cloudOn = h('input', { type: 'checkbox', checked: cc.enabled && cc.consent, id: 'cloud-enabled' });
  const cloudToken = h('input', { type: 'password', value: cc.token, autocomplete: 'off', spellcheck: false, placeholder: '粘贴管理员给你的令牌', 'aria-label': '在线朗读令牌' });
  const voiceSel = h('select', { 'aria-label': '在线音色', onchange: () => saveCloudCfg({ voice: voiceSel.value }) });
  const fillVoices = () => {
    const c = getCloudCfg();
    const list = c.voices.length ? c.voices : FALLBACK_VOICES;
    voiceSel.replaceChildren(h('option', { value: '', text: '自动（按文字语言选择）' }), ...list.map((v) => h('option', { value: v.id, text: `${v.name}（${v.lang}${v.gender ? ' · ' + v.gender : ''}）` })));
    voiceSel.value = c.voice;
  };
  fillVoices();
  const saveToken = async () => { await saveCloudCfg({ token: cloudToken.value }); };
  cloudToken.addEventListener('change', saveToken);
  cloudOn.addEventListener('change', async () => {
    if (cloudOn.checked) {
      if (!(await confirmDialog(CONSENT_TEXT, { ok: '同意并开启' }))) { cloudOn.checked = false; return; }
      await saveCloudCfg({ enabled: true, consent: true, token: cloudToken.value });
      cloudMsg.textContent = cloudReady(getCloudCfg()) ? '已开启。' : '已开启，但还需要填写令牌。';
    } else { await saveCloudCfg({ enabled: false }); cloudMsg.textContent = '已关闭，朗读使用浏览器语音。'; }
  });
  const guarded = async (label, fn) => {
    await saveToken();
    if (!getCloudCfg().token) { cloudMsg.textContent = '请先填写令牌。'; return; }
    cloudMsg.textContent = label + '…';
    try { await fn(); } catch (e) { cloudMsg.textContent = (e && e.message) || String(e); }
  };
  const usageBtn = h('button', { class: 'btn small', type: 'button', onclick: () => guarded('正在查询用量', async () => { cloudMsg.textContent = '用量：' + (formatUsage(await api.usage()) || '无数据'); }) }, '查看用量');
  const voicesBtn = h('button', { class: 'btn small', type: 'button', onclick: () => guarded('正在获取音色', async () => {
    const vs = curateVoices(await api.voices());
    await saveCloudCfg({ voices: vs });
    fillVoices();
    cloudMsg.textContent = `已获取 ${vs.length} 个音色。`;
  }) }, '刷新音色');
  const testBtn = h('button', { class: 'btn small', type: 'button', onclick: () => {
    // the element is created and primed inside this tap, so the browser allows the sample to play
    const eng = new CloudEngine({ audio: new Audio(), api });
    eng.prime();
    guarded('正在合成试听', async () => {
      const r = await eng.speak('这是在线朗读示例：天地玄黄，宇宙洪荒。', () => true, { cloud: getCloudCfg(), ttsRate: 1 });
      cloudMsg.textContent = r === 'end' ? '试听成功，令牌和服务器可用。' : (eng.lastError ? eng.lastError.message : '试听失败：无法播放音频（' + r + '）。');
    });
  } }, '测试');

  const body = [
    h('h3', { text: 'WebDAV 同步（与桌面版 PyReader 共享书库和进度）' }),
    h('div', { class: 'warn', text: '⚠ 账号和密码只保存在本浏览器的 IndexedDB 中（明文），任何能打开这台设备这个浏览器资料的人都能读到。请只在你自己的设备上使用，并使用权限受限的专用账号；公用电脑请不要保存。' }),
    h('label', { class: 'row' }, enabled, h('span', { text: '启用同步' })),
    h('label', { class: 'field' }, h('span', null, 'WebDAV 地址（必须 https://；仅局域网/本机地址允许 http://）'), url), urlWarn,
    h('label', { class: 'field' }, h('span', null, '用户名'), user),
    h('label', { class: 'field' }, h('span', null, '密码'), pass),
    h('label', { class: 'field' }, h('span', null, '同步用户名（与桌面版设置里的“同步用户名”一致，默认 default）'), syncUser),
    h('label', { class: 'field' }, h('span', null, '本设备名称（其他设备显示“某某读到了 xx%”）'), devName),
    h('label', { class: 'field' }, h('span', null, '设备 ID（本设备独有；每台设备只写自己的文件。默认 web-xxxxxx）'), devId),
    h('div', { class: 'row' }, saveBtn, offBtn), msg,
    corsHint,
    h('h3', { text: '在线朗读（可选）' }),
    h('p', { class: 'hint', text: CONSENT_TEXT }),
    h('label', { class: 'row' }, cloudOn, h('span', { text: '启用在线朗读' })),
    h('label', { class: 'field' }, h('span', null, '访问令牌（只保存在本设备）'), cloudToken),
    h('label', { class: 'field' }, h('span', null, '音色'), voiceSel),
    h('div', { class: 'row' }, testBtn, voicesBtn, usageBtn), cloudMsg,
    h('h3', { text: '外观' }), themeSeg,
    h('h3', { text: '存储' }), storageInfo,
    h('div', { class: 'row' },
      h('button', { class: 'btn', type: 'button', onclick: async () => { const ok = await persistRequest(); toast(ok ? '已申请持久存储' : '浏览器没有授予持久存储（空间紧张时数据可能被清理）'); refreshStorage(); } }, '申请持久存储'),
      h('button', { class: 'btn danger', type: 'button', onclick: async () => {
        if (!(await confirmDialog('清除本浏览器里的全部书籍、书签、进度和设置？（不会动云端任何文件）', { ok: '全部清除', danger: true }))) return;
        for (const store of ['books', 'files', 'covers', 'progress', 'bookmarks', 'kv']) for (const row of await db.getAll(store)) await db.del(store, row[store === 'progress' ? 'bid' : store === 'kv' ? 'key' : 'id']);
        toast('已清除，正在重新加载…'); setTimeout(() => location.reload(), 600);
      } }, '清除全部本地数据')),
    h('h3', { text: '关于' }),
    h('dl', { class: 'kv' }, h('dt', { text: '版本' }), h('dd', { text: `PyReader Web ${APP_VERSION}` }),
      h('dt', { text: '设备 ID' }), h('dd', { text: syncMgr.cfg.deviceId }),
      h('dt', { text: '数据' }), h('dd', { text: '书籍、封面、进度、书签、设置都保存在本浏览器（IndexedDB）。书签/笔记不会同步（桌面版也不同步 bookmarks.json）。' })),
  ];
  return openPanel({ title: '设置', side: 'dialog', wide: true, content: body });
}
