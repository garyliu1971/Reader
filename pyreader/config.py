"""PyReader 全局配置：路径、排版常量、主题、AI 预设、JSON 读写、字体选择、多媒体。"""
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from .store import load_json, save_json  # noqa: F401  原子读写已移到 store.py，这里保留旧入口


# ---- 数据存储路径 ----
def resolve_app_dir(env=None) -> str:
    """数据目录：环境变量 PYREADER_HOME（测试隔离 / 便携部署）优先，否则 ~/.pyreader。
    必须在下面定义 APP_DIR 之前求值，所以放在模块最前面。"""
    home = ((os.environ if env is None else env).get("PYREADER_HOME") or "").strip()
    if home:
        return os.path.abspath(os.path.expanduser(home))
    return os.path.join(os.path.expanduser("~"), ".pyreader")


APP_DIR = resolve_app_dir()
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH    = os.path.join(APP_DIR, "config.json")
BOOKMARKS_PATH = os.path.join(APP_DIR, "bookmarks.json")
CACHE_DIR      = os.path.join(APP_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


# ---- 网络访问策略（Reader 自己的代理，不读系统代理）----
# 背景：Windows 上 urllib 会自动读取注册表里的系统代理（HKCU\...\Internet Settings 的
# ProxyEnable / ProxyServer），环境变量 HTTP(S)_PROXY 同样会被读取。实测：系统代理指向一个
# 已失效的本地端口（sing-box 退出/规则变更后残留）时，Azure TTS 报 WinError 10054 或 TLS
# 握手超时，DeepSeek 一直卡住，表现为「朗读没声音、翻译转圈」——而绕过代理直连是通的。
#
# 所以 Reader 完全不看系统代理，只认自己的设置（设置界面 / .env 的 PYREADER_PROXY）：
#   proxy 为空     → 直连（走 sing-box TUN 时仍会被 TUN 接管，按 sing-box 路由规则分流）
#   proxy 非空     → 先走该代理（如 sing-box 的 mixed 入站 http://127.0.0.1:1081）
# 并且带**自动降级**：代理在连接层失败时立刻改直连重试，并冷却一段时间不再碰代理，
# 避免像今天那样每句朗读都白等一次 30s 超时。
NET_PROXY = os.environ.get("PYREADER_PROXY", "").strip()   # 启动时会被 cfg["proxy"] 覆盖
PROXY_COOLDOWN = 120.0        # 代理连不上后，多少秒内直接走直连
_proxy_down_until = 0.0
_proxy_opener = None
_direct_opener = None


def set_net_proxy(url: str):
    """设置 Reader 专用代理（如 http://127.0.0.1:1081）；空字符串 = 直连。"""
    global NET_PROXY, _proxy_opener, _proxy_down_until
    NET_PROXY = (url or "").strip()
    _proxy_opener = None            # 代理地址变了 → 重建 opener
    _proxy_down_until = 0.0         # 手改设置后重新给代理一次机会


# 跨主机重定向时剥掉凭据：urllib 默认会把 Authorization 原样带到新主机，
# 恶意/被劫持的服务器可借一个 302 拿到 WebDAV 密码或 API key。
_CRED_HEADERS = {"authorization", "proxy-authorization", "api-key", "x-api-key", "cookie"}


def _origin(url: str):
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "").lower()
    port = u.port or {"http": 80, "https": 443}.get(scheme)
    return scheme, (u.hostname or "").lower(), port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(req.full_url) != _origin(new.full_url):
            for hdrs in (new.headers, new.unredirected_hdrs):
                for k in [k for k in hdrs if k.lower() in _CRED_HEADERS]:
                    del hdrs[k]
        return new


def _opener(use_proxy: bool):
    global _proxy_opener, _direct_opener
    if use_proxy:
        if _proxy_opener is None:
            _proxy_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": NET_PROXY, "https": NET_PROXY}),
                _SafeRedirectHandler())
        return _proxy_opener
    if _direct_opener is None:
        # 显式空的 ProxyHandler → 直连，不读注册表/环境变量代理
        _direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                     _SafeRedirectHandler())
    return _direct_opener


def _log_net(msg: str):
    try:
        with open(os.path.join(APP_DIR, "tts.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _fresh_request(req):
    """克隆一个 Request。

    urllib 的 ProxyHandler 会**就地改写** Request（`set_proxy()` 把 host 改成代理地址），
    所以代理试失败后，同一个 Request 对象再去直连会连到代理端口上（实测报 WinError 10061）。
    这里每次尝试都从 full_url 重新构造一个干净的 Request。"""
    r = urllib.request.Request(req.full_url, data=req.data, method=req.get_method())
    r.headers = dict(req.headers)
    r.unredirected_hdrs = dict(req.unredirected_hdrs)
    r.origin_req_host = req.origin_req_host
    r.unverifiable = req.unverifiable
    return r


def net_urlopen(req, timeout=30):
    """统一的 urlopen 入口：先试 Reader 自己的代理（若配了），连接层失败则降级直连。

    HTTPError（4xx/5xx）是服务端正常应答，直接抛出，不触发降级重试。"""
    global _proxy_down_until
    try_proxy = bool(NET_PROXY) and time.time() >= _proxy_down_until
    last_err = None
    for use_proxy in ([True, False] if try_proxy else [False]):
        try:
            return _opener(use_proxy).open(_fresh_request(req), timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last_err = e
            if use_proxy:
                _proxy_down_until = time.time() + PROXY_COOLDOWN
                _log_net(f"[网络] 代理 {NET_PROXY} 连不上（{e}），{int(PROXY_COOLDOWN)}s 内改直连")
    raise last_err


def bundled_models_dir():
    """返回随程序一起分发的离线模型根目录（内含 sherpa_models/、piper_models/），
    用于 U 盘/安装包「一键离线」分发（Melo / Kokoro / Piper 模型无需再联网下载）。

    查找位置：
      - PyInstaller / 安装版：<exe 所在目录>/models
      - 源码运行：Reader/models（项目根目录下）
    不存在时返回 None。"""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots = [os.path.join(exe_dir, "models")]
    else:
        roots = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")]
    for r in roots:
        if os.path.isdir(r):
            return r
    return None

# ---- 排版常量 ----
OUTER = 16.0            # 页面距窗口边距
MARGIN_X = 24.0         # 页内文字左右边距
MARGIN_Y = 44.0         # 页内文字上下边距
GUTTER = 32.0           # 书脊
LINE_SPACING = 1.25
CHUNK = 200_000         # 无章节时，伪章节的字符数
MAX_CACHE = 6           # 缓存的章节数
SWIPE_THRESHOLD = 80.0  # 手势翻页触发阈值（像素）
WHEEL_THRESHOLD = 80.0  # 滚轮/滑动翻页触发阈值（角度单位）
INDENT_SPACES = 2       # 段落首行缩进（中文字符数）
PARA_SPACING = 0.5      # 段落间距（行高的倍数）
CLICK_ZONE = 0.28       # 单击左右两侧翻页的触发宽度（占窗口比例）
FLIP_MS = 800            # 3D 翻页动画时长（毫秒）
FLIP_STRIPS = 20          # 卷曲页竖带数（越大越平滑，越慢）
FLIP_CURL = 0.9           # 卷曲强度：自由端相对书脊端滞后的最大角度(弧度)，0=刚性平板
FLIP_TILT = 0.14          # 斜翻强度：上下角自由边位置差（0=不斜）
FLIP_PERSPECTIVE = 5.0   # 翻页透视强度（相机距离 = 页宽 × 该系数，越小越夸张/文字越压缩）

# ---- 多媒体（语音朗读用）----
# 实际播放用 QAudioSink 持续流（见 .ttsplayer.TtsPlayer），这里只是检测 QtMultimedia 模块是否可用。
try:
    from PySide6.QtMultimedia import QAudioSink  # noqa: F401  仅用于模块可用性探测
    _HAS_MULTIMEDIA = True
except Exception:
    _HAS_MULTIMEDIA = False


# 配色主题：bg=桌面背景，page=书页，border=书页边框，text=正文，header=页眉，pageno=页码
THEMES = {
    "夜间（默认）": {"bg": "#1b1b1e", "page": "#fdfdfb", "border": "#d8d8d8",
                 "text": "#1a1a1a", "header": "#9a9a9a", "pageno": "#a0a0a0"},
    "纯白":     {"bg": "#e9e9eb", "page": "#ffffff", "border": "#d5d5d5",
                 "text": "#1a1a1a", "header": "#9a9a9a", "pageno": "#a0a0a0"},
    "米黄（护眼）": {"bg": "#c9b899", "page": "#f6edd8", "border": "#dccba6",
                 "text": "#3a3226", "header": "#9a8768", "pageno": "#9a8768"},
    "浅绿（护眼）": {"bg": "#b7c6af", "page": "#eaf1e1", "border": "#c9d6c0",
                 "text": "#2c3828", "header": "#7d8c72", "pageno": "#7d8c72"},
}
DEFAULT_THEME = "夜间（默认）"

# 翻页方式
FLIP_EFFECTS = ["3D 翻书", "平移滑动", "直接切换"]
DEFAULT_FLIP_EFFECT = "3D 翻书"

# 单页模式：单页最大宽度（窗口再宽也居中显示，避免行过长）
SINGLE_MAX_W = 760.0


def pages_per_view(single: bool) -> int:
    """每屏页数：单页模式 1，默认双页跨页 2。"""
    return 1 if single else 2


def view_count(npages: int, ppv: int) -> int:
    """章内共有多少屏（跨页）。"""
    return (npages + ppv - 1) // ppv


def view_of_page(page_idx: int, ppv: int) -> int:
    """页下标 → 所在屏序号。"""
    return page_idx // ppv


def first_page_of_view(view_idx: int, ppv: int) -> int:
    """屏序号 → 该屏第一页的页下标。"""
    return view_idx * ppv


def _default_device_name():
    try:
        import socket
        return socket.gethostname() or "device"
    except Exception:
        return "device"


DEFAULT_CONFIG = {"font_family": "", "font_size": 15,
                  "line_spacing": 1.5, "margin_x": 28.0, "margin_y": 44.0,
                  "outer": 16.0, "gutter": 36.0, "para_spacing": 0.6,
                  "api_key": "",
                  "api_base": "https://YOUR-RESOURCE.openai.azure.com/openai/v1",
                  "model": "gpt-4o-mini",
                  "api_version": "",
                  "tts_voice": "sherpa:melo-zh_en", "tts_rate": "+0%",
                  "proxy": "",
                  "azure_tts_key": "", "azure_tts_region": "",
                  "azure_tts_key2": "", "azure_tts_region2": "",
                  "theme": DEFAULT_THEME, "recent": [], "library_dirs": [],
                  "bilingual": False, "bilingual_target": "中文",
                  "flip_effect": DEFAULT_FLIP_EFFECT,
                  "single_page": False,
                  # WebDAV 进度同步（默认关闭；这些键只存本机 config.json，绝不上传）
                  "webdav_enabled": False, "webdav_url": "",
                  "webdav_user": "", "webdav_password": "", "webdav_allow_insecure": False,
                  "webdav_max_book_mb": 1024,
                  # 额外的只读 WebDAV 书库（多数据源）：只列书目 + 下载，不记进度、不同步。
                  # 每项 {name, url, user, password, allow_insecure, enabled}；name 兼作书架目录前缀。
                  "webdav_libraries": [],
                  "sync_user_name": "default", "device_name": _default_device_name(),
                  # 书架（非机密的界面偏好；书架数据本身在 ~/.pyreader/shelf/，不进 config.json）
                  "shelf_default_view": "grid", "shelf_default_sort": "last_read",
                  "shelf_start_in_shelf": False}


# ---- 从 .env 自动读取 AI 配置（DeepSeek 翻译 + Azure Speech 朗读）----
# 搜索顺序（优先级递减）：
#   1. 打包/安装版：exe 同目录 .env         ← 便携版 / 安装目录（随包分发）
#   2. Reader 项目根目录 .env              ← 源码运行时放这里（最优先）
#   3. Reader 同级目录 coding-agent/.env   ← 开发者共用
# 仅在 config.json 尚无 api_key 时作为默认值使用（不会覆盖用户已保存的设置）
def _env_search_paths():
    if getattr(sys, "frozen", False):
        # PyInstaller/安装版：.env 与 exe 同目录（U 盘便携版 / {app} 安装目录）
        return [Path(os.path.dirname(os.path.abspath(sys.executable))) / ".env"]
    return [
        Path(__file__).parent.parent / ".env",                            # Reader/.env  ← 发布时放这里
        Path(__file__).parent.parent.parent / "coding-agent" / ".env",   # ../coding-agent/.env
    ]


def _parse_dotenv(path: Path) -> dict:
    """极简 .env 解析：支持 KEY=VALUE，忽略注释和空行，去除引号。"""
    result = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"\'')
            result[k] = v
    except Exception:
        pass
    return result


def load_env_defaults() -> dict:
    """读取 .env，返回可直接合并进 cfg 的字典。

    变量映射：
      DEEPSEEK_BASE_URL          → api_base
      DEEPSEEK_API_KEY           → api_key
      DEEPSEEK_MODEL             → model
      CODING_AGENT_TEMPERATURE   → temperature（"none" 表示不传该参数）
      AZURE_TTS_KEY              → azure_tts_key
      AZURE_TTS_REGION           → azure_tts_region
      AZURE_TTS_KEY2             → azure_tts_key2
      AZURE_TTS_REGION2          → azure_tts_region2
      PYREADER_PROXY             → proxy（Reader 专用代理，如 sing-box 的 http://127.0.0.1:1081）
      WEBDAV_URL                 → webdav_url（WebDAV 书库地址；用户名/密码不进 .env，由用户在设置界面填写）
    """
    for p in _env_search_paths():
        env = _parse_dotenv(p)
        if (env.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_BASE_URL")
                or env.get("AZURE_TTS_KEY") or env.get("WEBDAV_URL")):
            result = {}
            if env.get("DEEPSEEK_BASE_URL"):
                result["api_base"] = env["DEEPSEEK_BASE_URL"]
            if env.get("DEEPSEEK_API_KEY"):
                result["api_key"] = env["DEEPSEEK_API_KEY"]
            if env.get("DEEPSEEK_MODEL"):
                result["model"] = env["DEEPSEEK_MODEL"]
            # CODING_AGENT_TEMPERATURE=none 表示略去 temperature 字段（gpt-5 系列要求）
            t = env.get("CODING_AGENT_TEMPERATURE", "").strip().lower()
            result["temperature"] = "none" if t == "none" else (float(t) if t else 0.2)
            if env.get("AZURE_TTS_KEY"):
                result["azure_tts_key"] = env["AZURE_TTS_KEY"]
            if env.get("AZURE_TTS_REGION"):
                result["azure_tts_region"] = env["AZURE_TTS_REGION"]
            if env.get("AZURE_TTS_KEY2"):
                result["azure_tts_key2"] = env["AZURE_TTS_KEY2"]
            if env.get("AZURE_TTS_REGION2"):
                result["azure_tts_region2"] = env["AZURE_TTS_REGION2"]
            if env.get("PYREADER_PROXY"):
                result["proxy"] = env["PYREADER_PROXY"]
            if env.get("WEBDAV_URL"):
                result["webdav_url"] = env["WEBDAV_URL"]
            return result
    return {}


def default_cjk_font() -> str:
    """优先选一个好看的中文阅读字体。"""
    try:
        from PySide6.QtGui import QFontDatabase
        fams = set(QFontDatabase.families())
        for name in ("微软雅黑", "Microsoft YaHei", "思源宋体", "Source Han Serif SC",
                     "Noto Serif CJK SC", "宋体", "SimSun", "等线", "DengXian",
                     "PingFang SC", "苹方"):
            if name in fams:
                return name
    except Exception:
        pass
    return ""


# AI 服务商预设（设置对话框下拉用）
# Azure AI Foundry v1 格式：https://<resource>.openai.azure.com/openai/v1
#   - 无需 api-version，直接兼容 OpenAI SDK
#   - model 填写你的「部署名称」（Deployment Name），如 gpt-4o-mini
AI_PRESETS = {
    "Azure AI Foundry (v1)": ("https://YOUR-RESOURCE.openai.azure.com/openai/v1", "gpt-4o-mini"),
    "OpenAI":               ("https://api.openai.com/v1", "gpt-4o-mini"),
    "DeepSeek":             ("https://api.deepseek.com", "deepseek-chat"),
    "通义千问":              ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "本地 Ollama":          ("http://localhost:11434/v1", "llama3"),
    "自定义":               ("", ""),
}
