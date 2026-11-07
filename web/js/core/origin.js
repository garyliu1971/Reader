// Same-origin check for the WebDAV server, so the UI only talks about CORS when it can actually be the problem
// (the app is often served by the same host/proxy as the WebDAV server, where CORS never applies).

/** true when `serverUrl` is NOT on `pageOrigin` (or when that cannot be decided: unparseable URL, opaque origin) */
export function isCrossOrigin(serverUrl, pageOrigin) {
  if (!pageOrigin || pageOrigin === 'null') return true;
  try { return new URL(String(serverUrl || '').trim(), pageOrigin).origin !== pageOrigin; } catch { return true; }
}

/** suffix for a failed-request error message */
export function networkHint(serverUrl, pageOrigin) {
  return isCrossOrigin(serverUrl, pageOrigin)
    ? ' (网络/CORS：请检查服务器的 CORS 配置，见 web/README.md)'
    : ' (网络错误：服务器与本页面同源，不涉及 CORS；请检查网络连接和服务器是否可达)';
}

/** the hint paragraph under the server URL in settings; `serverUrl` may still be empty */
export function corsNote(serverUrl, pageOrigin) {
  if (!String(serverUrl || '').trim()) return '如果服务器与本页面不在同一个网址（域名/端口），服务器必须允许跨域（CORS）；同源则无需配置。配置示例见仓库 web/README.md。';
  return isCrossOrigin(serverUrl, pageOrigin)
    ? '服务器与本页面不同源，必须允许跨域（CORS）。配置示例见仓库 web/README.md。'
    : '服务器与本页面同源，无需配置 CORS。';
}
