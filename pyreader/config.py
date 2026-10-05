"""PyReader 全局配置：路径、排版常量、主题、AI 预设、JSON 读写、字体选择、多媒体。"""
import os
import json
from pathlib import Path

# ---- 数据存储路径 ----
APP_DIR = os.path.join(os.path.expanduser("~"), ".pyreader")
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH    = os.path.join(APP_DIR, "config.json")
BOOKMARKS_PATH = os.path.join(APP_DIR, "bookmarks.json")
CACHE_DIR      = os.path.join(APP_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

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
FLIP_MS = 380            # 3D 翻页动画时长（毫秒）
FLIP_PERSPECTIVE = 5.0   # 翻页透视强度（相机距离 = 页宽 × 该系数，越小越夸张/文字越压缩）

# ---- 多媒体（语音朗读用）----
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    _HAS_MULTIMEDIA = True
except Exception:
    QMediaPlayer = QAudioOutput = None
    _HAS_MULTIMEDIA = False


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


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

DEFAULT_CONFIG = {"font_family": "", "font_size": 15,
                  "line_spacing": 1.5, "margin_x": 28.0, "margin_y": 44.0,
                  "outer": 16.0, "gutter": 36.0, "para_spacing": 0.6,
                  "api_key": "",
                  "api_base": "https://YOUR-RESOURCE.openai.azure.com/openai/v1",
                  "model": "gpt-4o-mini",
                  "api_version": "",
                  "tts_voice": "zh-CN-XiaoxiaoNeural", "tts_rate": "+0%",
                  "theme": DEFAULT_THEME, "recent": [],
                  "bilingual": False, "bilingual_target": "中文",
                  "flip_effect": DEFAULT_FLIP_EFFECT}


# ---- 从 .env 自动读取 Azure AI Foundry 配置 ----
# 搜索顺序（优先级递减）：
#   1. Reader 项目根目录 .env          ← 发给别人时放这里（最优先）
#   2. Reader 同级目录 coding-agent/.env  ← 开发者共用
# 仅在 config.json 尚无 api_key 时作为默认值使用（不会覆盖用户已保存的设置）
_ENV_SEARCH_PATHS = [
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
    """读取 coding-agent/.env，返回可直接合并进 cfg 的字典。

    变量映射：
      DEEPSEEK_BASE_URL          → api_base
      DEEPSEEK_API_KEY           → api_key
      DEEPSEEK_MODEL             → model
      CODING_AGENT_TEMPERATURE   → temperature（"none" 表示不传该参数）
    （coding-agent 沿用了 DEEPSEEK_ 前缀，实际已指向 Azure AI Foundry）
    """
    for p in _ENV_SEARCH_PATHS:
        env = _parse_dotenv(p)
        if env.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_BASE_URL"):
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
