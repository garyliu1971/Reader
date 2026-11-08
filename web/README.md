# PyReader Web

PyReader 的浏览器版伴侣阅读器（静态 PWA）。桌面版（Qt）仍是主产品；网页版只做两件事：**好用的核心阅读体验**，以及**通过现有 WebDAV 同步与桌面版共享书架和阅读进度**。不追求功能对等。

* 纯静态：没有服务器代码，没有构建步骤，原生 ES module。第三方库放在 `vendor/`（固定版本，见 `vendor/README.md`）。
* 格式：EPUB（自带解析，目录/封面/图片/内部链接）、TXT（自动识别 UTF-8/UTF-16/GB18030/Big5，章节识别移植自 `pyreader/textio.py`）、Markdown、HTML、PDF（v1 仅渲染翻页，无文字层/搜索）。
* 不支持（界面会明确拒绝）：MOBI/AZW3、DOCX、PPTX、音频、漫画压缩包。

## 本地运行

```bash
# 在仓库根目录
python -m http.server 8123 --directory web       # 然后打开 http://127.0.0.1:8123/
node --test web/test                              # 单元 + 同步互操作测试（需要 node >= 22、python）
```

* Service Worker / 安装需要 `https://` 或 `localhost`/`127.0.0.1`。
* 生成测试书：`python web/tools/make_test_books.py OUTDIR`（EPUB/TXT/GBK TXT/Markdown/HTML/PDF/大文件）。
* 本地 WebDAV（带 CORS）：`python web/tools/devdav.py --auth user:pass --seed 某目录`。它复用 `tests/fakedav.py`（Qt 测试用的假服务器，只读引用），只在 `web/tools/` 里加了一层 CORS；监听 127.0.0.1，输出 `READY http://127.0.0.1:端口/dav`。
* 真实浏览器烟测（可选，不属于 `node --test`）：在 `web/tools/browser-smoke/` 里 `npm i playwright-core axe-core --no-save`，先生成测试书（见上）并启动静态服务器，再 `node reader.mjs`（阅读/重排/目录/搜索/书签/主题/GBK TXT）、`formats-touch-large.mjs`（13 MB TXT、PDF、触摸点击区/滑动、选择复制）、`sync-e2e.mjs`（真实 CORS 预检、下载、进度推送与不回退、对等设备文件、无 DELETE、离线 PWA）、`big-epub-upload.mjs`（大 EPUB、拖拽导入、上传不覆盖）、`security.mjs`（净化）、`a11y.mjs`（axe-core）、`pinch.mjs`、`swupdate.mjs`（自带小服务器同时提供旧/新几个版本，验证“新版本可用”提示、点击刷新后换版、旧版页面升级路径；需要 git 和 tar）。环境变量：`SMOKE_TMP`（默认系统临时目录/pyreader-web-smoke）、`CHROME_PATH`、`APP_URL`。
* 重新生成与 Python 实现对拍的黄金数据：`python web/tools/gen_golden.py`；重新生成图标：`python web/tools/gen_icons.py`。

## 部署（静态托管）

把 `web/` 目录原样上传即可（Azure Static Web Apps、Azure Storage 静态网站、GitHub Pages 都行）。要求：

1. 必须 `https://`（PWA、Service Worker、`crypto`/Storage 持久化都要安全上下文）。
2. `.js` 以 JavaScript MIME 提供（pdf.js 已从 `.mjs` 重命名为 `.js`，就是为了绕开 `.mjs` MIME 问题）。
3. 升级版本时递增 `sw.js` 里的 `CACHE`；`test/shell.test.js` 会检查预缓存列表覆盖了所有模块。
4. 源站要把 WebDAV 服务器的 CORS 允许来源配成这个站点的 origin（见下）。

## WebDAV 服务器要求（CORS）与 Caddy 配置

浏览器从你的静态站点跨域访问 WebDAV，服务器**必须**：

* 允许方法：`OPTIONS, GET, PUT, PROPFIND, MKCOL, MOVE`
* 允许请求头：`Authorization, Depth, Content-Type, Destination, Overwrite, If-None-Match`
* `Access-Control-Allow-Origin` **回显**应用的 origin（不能用 `*`，因为要带 `Authorization`；应用以 `credentials: 'omit'` + 显式头发请求，不依赖 cookie）
* **预检（OPTIONS）必须不经认证就成功**（浏览器的预检不带 `Authorization`）
* 建议 `Access-Control-Expose-Headers: Date, ETag, Content-Length, Last-Modified`（`Date` 用来校正本机时钟偏差，写进度时间戳时参考服务器时间；没有它也能工作，只是不校正）

可直接粘贴的 Caddyfile 片段（**未在真实 Caddy 上验证语法，请先 `caddy validate`；我们没有碰任何线上服务器**）。关键点：预检在 `handle` 里先于认证应答，认证放进后面的 `handle`：

```caddyfile
# 允许的网页来源（正则里按需增减；本地调试可加 http://localhost:8123）
(pyreader_cors) {
	@pr_origin header_regexp pr_origin Origin ^(https://reader\.example\.com|http://localhost:8123)$
	@pr_preflight {
		method OPTIONS
		header_regexp pr_origin Origin ^(https://reader\.example\.com|http://localhost:8123)$
	}
	header @pr_origin {
		Access-Control-Allow-Origin "{http.regexp.pr_origin.1}"
		Access-Control-Expose-Headers "Date, ETag, Content-Length, Last-Modified"
		Vary Origin
	}
	handle @pr_preflight {
		header Access-Control-Allow-Methods "OPTIONS, GET, PUT, PROPFIND, MKCOL, MOVE"
		header Access-Control-Allow-Headers "Authorization, Depth, Content-Type, Destination, Overwrite, If-None-Match"
		header Access-Control-Max-Age "86400"
		respond 204
	}
}

dav.example.com {
	import pyreader_cors
	handle {
		basic_auth {
			gary <caddy hash-password 生成的哈希>
		}
		# 使用 mholt/caddy-webdav 模块（需要 `order webdav before file_server` 全局选项）
		webdav {
			root /srv/dav
		}
	}
}
```

验证（把 ORIGIN 换成你的站点）：

```bash
curl -si -X OPTIONS https://dav.example.com/ -H 'Origin: https://reader.example.com' \
  -H 'Access-Control-Request-Method: PROPFIND' -H 'Access-Control-Request-Headers: authorization,depth,content-type' | head -20
# 期望 204/200，且有 Access-Control-Allow-Origin: https://reader.example.com 以及上面的 Allow-Methods/Allow-Headers
```

本仓库的 `tools/devdav.py` 就是按这份契约实现的参照服务器；`test/sync.test.js` 与浏览器烟测都针对它跑过（真实 Chrome 的 CORS 预检通过）。

## 与桌面版的同步（线上格式以 `pyreader/*.py` 为准）

服务器布局（与 Qt 完全一致）：

```
<base>/library/<rel>                                   书库（网页版只读取；上传是手动操作）
<base>/users/<user>/progress/<sha1(book_id)[:20]>.json 单本书进度
<base>/users/<user>/shelf/<device_id>.json             设备文件（每台设备只写自己的）
```

* `book_id`：云端书 `remote:<rel>`（逐字，不做 Unicode 规范化）；本地文件 `local:` + `sha1(文件名 + 字节数)`。从云端下载的书永远用 `remote:`。
* 单本书进度 `{"book_id","offset","percent","chapter","updated","device"}`：`updated` 是 unix **秒**（浮点），严格更新者胜；写之前先 GET，**绝不覆盖更新的记录**；时间戳优先用服务器 `Date` 校正（需要 Expose-Headers），超前本机时钟以服务器为准。`percent` 总是正确的；`offset` 是桌面版自己提取文本里的字符下标，网页端算不出来，而桌面版对 `0 <= offset < 文本长度` 的值是直接采信的（写 0 会让桌面版跳到书首），所以网页端**故意写一个超出范围的值（1e12）**，桌面版就会改用 `percent` 换算。
* 设备文件：`version=1`；`fav`/`hidden`/`meta` 是 `[值, ts_ms]` 寄存器，LWW 合并；`progress` 摘要 `{bid:{percent,updated}}` 只保留最近 500 条；`ts = max(now_ms, 合并视图里该寄存器的 ts + 1)`；超过现在 + 24 小时的 ts 视为最旧。只写白名单字段（`toWire`），小于 1 MiB；推送后回读校验。网页端不写 `meta`（桌面版靠它做跨设备同一本书匹配，见“限制”）。
* 安全：**没有任何代码路径发 DELETE**；下载流式读取并校验大小（`Content-Length` 与列表一致，上限 300 MB）；PROPFIND 防御式解析（拒绝 DTD/实体、体积 ≤ 8 MB、只收直接子项、忽略以 `.`/`~$` 开头的名字与 `*.part`、名字过 `safe_segment`）；上传走 `.<name>.part` + `MOVE`（`Overwrite: F`、`If-None-Match: *`），云端已有同名文件（NFC+忽略大小写）一律拒绝，绝不覆盖。
* 凭据：只存在本浏览器的 IndexedDB（明文，设置页有醒目警告）。拒绝向非本地/私有主机使用 `http://`。设备 ID 默认 `web-<6 位十六进制>`。
* 书签/笔记不同步（桌面版也不同步 `bookmarks.json`）。**以后**如要同步，设计成一个单独的可选文件（例如 `users/<user>/web-bookmarks/<device_id>.json`），现在没有实现。

## 阅读器要点

* 版面：CSS 多列分页。位置锚点 = `{章节, 文本偏移, 比例, 12 字上下文}`，**从不用页码**；视口/面板/字号任何变化都从偏移重新定位页。目录/书签/搜索/设置都是覆盖层，不改变正文宽度。
* 主题：明亮 / 夜间（页面本身变暗）/ 护眼黄 / 护眼绿 / 跟随系统；字体、字号、行距、页边距、段间距、首行缩进、两端对齐、分页或连续滚动。
* 操作：左右点击区翻页、点中间呼出设置/进度栏、滑动翻页、方向键/PageUp/PageDown/空格、`Ctrl/⌘ +/-/0` 与双指捏合调字号、`T` 目录、`B` 书签、`Ctrl/⌘+F` 或 `/` 全文搜索（顶部搜索条：`n / 总数`，`Enter`/`F3` 下一个，`Shift+Enter` 上一个，列表按钮看全部命中；命中高亮，关闭即清除）、`Esc` 返回。**文字可选择复制**（选区存在时点击不翻页）。
* 大文件：TXT 在 Web Worker 里解码+分章；EPUB 逐章扫描并让出主线程；一章最多 4 万字符一屏布局（TXT）；没有对整本书做字符串拼接循环。

## 更新（Service Worker）

* 新版本部署后，浏览器在打开页面/回到标签页（最多每 5 分钟一次）时检查 `sw.js`。新 worker 装好后**不会自动接管**：页面底部出现“有新版本可用  [刷新] [稍后]”，只有点“刷新”才会让新 worker 接管并重新加载一次；阅读中、朗读中不会被自动刷新（朗读中或正在导入时点“刷新”会先确认；刷新前会先保存阅读进度）。正在等待期间，当前页面继续使用旧版本缓存（旧 worker 在新 worker 安装/等待时不再把网络上的新文件写进旧缓存），懒加载的模块不会和运行中的 JS 版本错位。
* 部署时照旧要改 `sw.js` 里的 `CACHE`（否则浏览器认为 `sw.js` 没变，不会触发更新）。
* **从没有提示的旧版（v7 及更早）升级**：旧页面不会回答新 worker 的探测（等待最多 2 秒；只统计本应用目录下的窗口，同源的其它页面如 WebDAV 网页不算），新 worker 看到有“静默”窗口就立刻接管（和旧版行为一致）；用户照旧刷新一到两次即可，之后的版本才有提示。新页面 + 旧 worker 不会出问题（页面只是在 worker 变化时才弹提示）。
* 只有同源且在 `sw.js` 所在目录下的 GET 请求会被处理；同源的 WebDAV 路径、跨域请求完全不经过 worker。

## 目录

```
web/
  index.html  manifest.webmanifest  sw.js  css/app.css  icons/
  js/app.js            启动、路由、同步调度、PWA 注册
  js/library.js        导入/元数据/封面/进度/书签（IndexedDB）
  js/core/*            纯逻辑（node 可测）：names bookid sha1 textenc chapters anchor progress shelfmerge origin swupdate
  js/parse/*           epub textbooks(txt/md/html) txt.worker sanitize pdf
  js/reader/*          paginator（分页引擎）reader（界面、手势、进度）
  js/sync/*            propfind（防御式解析）webdav sync（引擎）manager（调度/配置）
  js/ui/*              dom shelf settings icons update（新版本提示）
  js/store/*           db（IndexedDB 封装，失败退化为内存）settings
  vendor/              jszip marked pdf.js（固定版本 + 许可）
  test/*.test.js       node:test
  tools/               devdav.py gen_golden.py gen_icons.py make_test_books.py
```

## 已知限制

见仓库根的汇报；要点：PDF 无文字层/搜索/书签文本；无 MOBI/DOCX/PPTX/音频/漫画；封面生成色块；网页端不写 `meta` 所以桌面版不会把网页端的本地书与它自己的本地书按指纹合并；只有主书库（`library/`），不支持桌面版的“额外书库源”；浏览器存储配额受限，可申请持久存储。

## 在线朗读（可选，默认关闭）

* 书架 → 设置 → 在线朗读：开启时需同意“书中文字（逐句）会发送到服务器并转发给 Azure / edge-tts”；令牌（静态 Bearer）只保存在本设备 IndexedDB（键 `tts.cloud`）。默认仍用浏览器自带语音。
* 接口（与 `/reader/` 同源，路径 `../tts/v1/`）：`POST speak`（`{text<=200, voice, rate}` → `audio/mpeg`；401 令牌错误、413 过长、429 `{error,retryAfter}` 额度用尽、502 上游失败）、`GET voices`、`GET usage`。
* 播放：单个 `<audio>`（由点“朗读”的手势解锁），向前预取 3 句，语速用 `playbackRate`；额度用尽/断网/无法自动播放时自动回退浏览器语音并提示。SW 不处理 `/tts/`。
* 测试：`node --test web/test`（含 stub 服务器契约测试）；`web/tools/browser-smoke/cloudtts.mjs` 用本地 stub 做真实浏览器冒烟。
