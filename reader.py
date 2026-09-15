# reader.py —— 基于 PySide6 的文本小说阅读器（支持大文件惰性分页）
# 双击 start.bat 或命令行:  python reader.py 小说.txt
import sys, json, re, os, threading, bisect, urllib.request, asyncio, time, zipfile, posixpath, html, io, wave
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional

from PySide6.QtCore import Qt, QRectF, QSizeF, QPointF, Signal, QObject, QTimer, QEvent, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QColor, QPen, QNativeGestureEvent, QBrush, QLinearGradient
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    _HAS_MULTIMEDIA = True
except Exception:
    QMediaPlayer = QAudioOutput = None
    _HAS_MULTIMEDIA = False
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
    QToolBar, QMenu, QFontDialog, QTextEdit, QVBoxLayout, QLabel, QFileDialog,
    QMessageBox, QDialog, QLineEdit, QFormLayout, QDialogButtonBox,
    QCheckBox, QComboBox, QProgressBar, QSlider, QSwipeGesture, QPanGesture,
    QSpinBox, QDoubleSpinBox, QDockWidget,
)

APP_DIR = os.path.join(os.path.expanduser("~"), ".pyreader")
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
BOOKMARKS_PATH = os.path.join(APP_DIR, "bookmarks.json")

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

# ============ 分页（只分"一章"的量，所以永远很快） ============
class Line:
    __slots__ = ("text", "start", "end", "indent", "gap_after")
    def __init__(self, text, start, end, indent=False, gap_after=0.0):
        self.text, self.start, self.end = text, start, end
        self.indent, self.gap_after = indent, gap_after

class Page:
    __slots__ = ("index", "start", "end", "lines")
    def __init__(self, index, start, end, lines):
        self.index, self.start, self.end, self.lines = index, start, end, lines

def wrap_line(fm: QFontMetricsF, text: str, max_w: float) -> Tuple[str, str]:
    if not text:
        return "", ""
    if fm.horizontalAdvance(text) <= max_w:
        return text, ""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fm.horizontalAdvance(text[:mid]) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    lo = max(lo, 1)
    return text[:lo], text[lo:]

def paginate(text: str, font: QFont, page_size: QSizeF, base_offset: int = 0,
             line_spacing: float = LINE_SPACING, margin_x: float = MARGIN_X,
             margin_y: float = MARGIN_Y,
             para_spacing: float = PARA_SPACING) -> List[Page]:
    fm = QFontMetricsF(font)
    line_h = fm.height() * line_spacing
    para_gap = fm.height() * para_spacing
    usable_h = page_size.height() - 2 * margin_y
    max_w = page_size.width() - 2 * margin_x

    lines: List[Line] = []
    pos = 0
    indent_w = fm.horizontalAdvance("中") * INDENT_SPACES
    for para in text.split("\n"):
        p_start = base_offset + pos
        pos += len(para) + 1
        if para == "":
            lines.append(Line("", p_start, p_start))
            continue
        use_indent = para[0] not in (" ", "\u3000", "\t")
        rest, off = para, p_start
        first = True
        while rest:
            w = max_w - (indent_w if (use_indent and first) else 0.0)
            ln, rest = wrap_line(fm, rest, w)
            lines.append(Line(ln, off, off + len(ln), indent=(use_indent and first)))
            off += len(ln)
            first = False
        lines[-1].gap_after = para_gap          # 段末留白（段间距）

    # 按“累计高度”装页（因为段间距使行高不均匀）
    pages, cur, cur_h = [], [], 0.0
    for ln in lines:
        if cur and cur_h + line_h > usable_h:
            pages.append(Page(len(pages), cur[0].start, cur[-1].end, cur))
            cur, cur_h = [], 0.0
        cur.append(ln)
        cur_h += line_h + ln.gap_after
    if cur:
        pages.append(Page(len(pages), cur[0].start, cur[-1].end, cur))
    return pages

# ============ 章节扫描（一次 C 级正则扫描） ============
CHAPTER_RE = re.compile(
    r"^\s*(?:第[零一二三四五六七八九十百千万0-9]{1,10}[章节回卷集部篇][^\n]{0,40}|"
    r"(?:序章|楔子|引子|终章|尾声|番外[^\n]{0,30})|#{1,6}[ \t]+[^\n]+)\s*$",
    re.MULTILINE)

def scan_chapters(text: str) -> List[Tuple[int, str]]:
    chapters = [(m.start(), m.group(0).strip()) for m in CHAPTER_RE.finditer(text)]
    if not chapters:
        # 无任何章节 → 按块切成伪章节，保证惰性分页可用
        chapters = [(i, f"第 {i // CHUNK + 1} 部分") for i in range(0, len(text), CHUNK)]
    elif chapters[0][0] != 0:
        chapters.insert(0, (0, "开篇"))
    return chapters

# ============ 惰性分页器 ============
class _Chapter:
    __slots__ = ("index", "title", "start", "end")
    def __init__(self, index, title, start, end):
        self.index, self.title, self.start, self.end = index, title, start, end

class LazyPager:
    """按章节惰性分页，只缓存最近几章，其余按需计算。"""
    def __init__(self, text: str, chapters: List[Tuple[int, str]]):
        self.text = text
        self._params = {"font": QFont(), "page_size": QSizeF(400, 500),
                        "line_spacing": LINE_SPACING, "margin_x": MARGIN_X,
                        "margin_y": MARGIN_Y, "para_spacing": PARA_SPACING}
        # 归一化：把超大章节再切小，保证单次分页永远轻量
        self.chapters = []
        for i, (off, title) in enumerate(chapters):
            end = chapters[i + 1][0] if i + 1 < len(chapters) else len(text)
            if end - off <= CHUNK:
                self.chapters.append(_Chapter(len(self.chapters), title, off, end))
            else:
                k, p = 1, off
                while p < end:
                    e = min(p + CHUNK, end)
                    self.chapters.append(_Chapter(len(self.chapters), f"{title} ({k})", p, e))
                    p, k = e, k + 1
        self._cache = {}

    def set_params(self, params):
        self._params = params
        self._cache.clear()          # 布局变了，缓存全部失效

    def chapter_index_at(self, offset: int) -> int:
        starts = [c.start for c in self.chapters]
        return max(0, bisect.bisect_right(starts, offset) - 1)

    def pages_of(self, ci: int) -> List[Page]:
        pages = self._cache.get(ci)
        if pages is None:
            c = self.chapters[ci]
            p = self._params
            pages = paginate(self.text[c.start:c.end], p["font"], p["page_size"],
                             base_offset=c.start, line_spacing=p["line_spacing"],
                             margin_x=p["margin_x"], margin_y=p["margin_y"],
                             para_spacing=p.get("para_spacing", PARA_SPACING))
            self._cache[ci] = pages
            # 淘汰最远的章节，保留当前章节及其邻居（跨章翻页无感）
            if len(self._cache) > MAX_CACHE:
                far = sorted((abs(k - ci), k) for k in self._cache if k != ci)
                for _, k in far:
                    if len(self._cache) <= MAX_CACHE:
                        break
                    del self._cache[k]
        return pages

# ============ 编码自动识别（网文 txt 编码五花八门） ============
def decode_text(raw: bytes) -> str:
    """自动识别 UTF-8 / UTF-16 / GBK(GB18030) / Big5，乱码时选替换符最少的。"""
    if raw.startswith(b'\xff\xfe'):
        return raw.decode('utf-16-le', errors='replace').lstrip('\ufeff')
    if raw.startswith(b'\xfe\xff'):
        return raw.decode('utf-16-be', errors='replace').lstrip('\ufeff')
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw.decode('utf-8', errors='replace').lstrip('\ufeff')
    try:
        return raw.decode('utf-8')            # 严格 UTF-8 优先
    except UnicodeDecodeError:
        pass
    best_text, best_count = None, None
    for enc in ('utf-8', 'gb18030', 'big5'):  # 混合/损坏文件：谁乱码少用谁
        t = raw.decode(enc, errors='replace')
        c = t.count('\ufffd')
        if best_count is None or c < best_count:
            best_text, best_count = t, c
    return best_text

# ============ epub / HTML 解析（标准库，零依赖） ============
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def _html_to_text(raw: bytes):
    """XHTML/HTML → (纯文本, 标题)。标题取第一个 h1-h6。"""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("utf-8", errors="replace")
    # 去掉 script/style 块
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    # 标题
    title = ""
    m = re.search(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", s)
    if m:
        title = html.unescape(re.sub(r"(?s)<[^>]+>", "", m.group(1))).strip()
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", s)
        if m:
            title = html.unescape(re.sub(r"(?s)<[^>]+>", "", m.group(1))).strip()
    # 块级元素 → 换行
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|blockquote)>", "\n", s)
    # 去所有标签
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s)
    # 压缩空白
    s = re.sub(r"[ \t\r]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip(), title

def load_epub(path):
    """解析 epub：返回 (全文, 章节[(offset,标题)...], 书名)。"""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = None
        for el in container.iter():
            if _strip_ns(el.tag) == "rootfile":
                opf_path = el.get("full-path")
                break
        if not opf_path or opf_path not in z.namelist():
            raise RuntimeError("无效的 epub：找不到 OPF 清单文件")
        opf = ET.fromstring(z.read(opf_path))
        opf_dir = posixpath.dirname(opf_path)
        manifest, spine, title = {}, [], ""
        for el in opf.iter():
            t = _strip_ns(el.tag)
            if t == "item":
                i, href = el.get("id"), el.get("href")
                if i and href:
                    manifest[i] = href
            elif t == "itemref":
                if el.get("idref"):
                    spine.append(el.get("idref"))
            elif t == "title":
                title = (el.text or "").strip()
        parts, chapters = [], []
        offset = 0
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, unquote(href)))
            if full not in z.namelist():
                continue
            ch_text, ch_title = _html_to_text(z.read(full))
            if not ch_text:
                continue
            chapters.append((offset, ch_title or f"第 {len(chapters) + 1} 章"))
            parts.append(ch_text)
            offset += len(ch_text) + 2
        if not parts:
            raise RuntimeError("epub 中没有可读取的正文")
        return "\n\n".join(parts), chapters, title

def load_html_file(path):
    """HTML 文件 → (全文, 章节, 标题)。"""
    with open(path, "rb") as f:
        raw = f.read()
    text, title = _html_to_text(raw)
    chapters = scan_chapters(text)
    return text, chapters, title

_MD_FENCE_RE = re.compile(r"^```")
_MD_ATX_TITLE_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$")
_MD_SETEXT_TITLE_RE = re.compile(r"^([^\n]+)\n(=+)\s*$", re.MULTILINE)
_MD_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_MD_BLOCKQUOTE_RE = re.compile(r"^(\s*)>\s?")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_MD_EM_RE = re.compile(r"(\*|_)(.+?)\1")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]*)`")

def _markdown_to_text(md: str):
    """Markdown → (清理后正文, 标题)。零依赖正则转换：ATX 标题行原样保留
    （供 scan_chapters/CHAPTER_RE 识别为章节），其余内联语法去除/替换为可读符号。"""
    title = ""
    m = _MD_ATX_TITLE_RE.match(next((ln for ln in md.splitlines() if ln.strip()), ""))
    if m:
        title = m.group(1).strip()
    else:
        m = _MD_SETEXT_TITLE_RE.search(md)
        if m:
            title = m.group(1).strip()

    out_lines = []
    in_fence = False
    for line in md.splitlines():
        if _MD_FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            out_lines.append(line)
            continue
        if _MD_HR_RE.match(line):
            out_lines.append("")
            continue
        if _MD_ATX_TITLE_RE.match(line) or re.match(r"^\s{0,3}#{1,6}[ \t]+\S", line):
            out_lines.append(line)  # 保留原始标题行，供 CHAPTER_RE 识别
            continue
        line = _MD_BLOCKQUOTE_RE.sub(r"\1", line)
        line = _MD_BULLET_RE.sub(r"\1• ", line)
        line = _MD_IMAGE_RE.sub(lambda mm: f"[图片: {mm.group(1)}]" if mm.group(1) else "[图片]", line)
        line = _MD_LINK_RE.sub(r"\1", line)
        line = _MD_BOLD_RE.sub(r"\2", line)
        line = _MD_EM_RE.sub(r"\2", line)
        line = _MD_INLINE_CODE_RE.sub(r"\1", line)
        out_lines.append(line)

    text = "\n".join(out_lines)
    text = re.sub(r"[ \t\r]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip(), title

def load_markdown(path):
    """Markdown 文件 → (清理后正文, 章节, 标题)。"""
    with open(path, "rb") as f:
        raw = f.read()
    text = decode_text(raw)
    text, title = _markdown_to_text(text)
    chapters = scan_chapters(text)
    return text, chapters, title

def load_pdf(path):
    """解析 PDF（仅限有文字层的，不支持扫描版）→ (全文, 章节, 书名)。
    按原始页面逐页提取文字后拼接，不保留 PDF 原本的分页/排版 -- 交给
    LazyPager 按字符重新分页，跟 epub/html/txt 走同一套流程。"""
    try:
        import pymupdf
    except Exception as e:
        raise RuntimeError("未安装 PyMuPDF，请执行：pip install pymupdf") from e
    doc = pymupdf.open(path)
    try:
        title = (doc.metadata or {}).get("title", "") or ""
        parts = []
        for page in doc:
            t = page.get_text("text")
            if t and t.strip():
                parts.append(t.strip())
        if not parts:
            raise RuntimeError("此 PDF 没有可提取的文字层（可能是扫描版/图片 PDF，暂不支持，需要 OCR）")
    finally:
        doc.close()
    text = "\n\n".join(parts)
    text = re.sub(r"[ \t\r]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    chapters = scan_chapters(text)
    return text, chapters, title.strip()

def load_kindle(path):
    """解析 Kindle 格式（mobi/azw/azw3/prc）→ (全文, 章节, 书名)。
    KF8 (azw3) 会被解成 epub 复用现有解析，旧版 mobi 解成 html。"""
    try:
        import mobi
    except Exception as e:
        raise RuntimeError("未安装 mobi 库，请执行：pip install mobi") from e
    try:
        from loguru import logger as _mobi_logger
        _mobi_logger.remove()        # 关闭 kindleunpack 的调试日志输出
    except Exception:
        pass
    try:
        _tempdir, out = mobi.extract(path)
    except Exception as e:
        raise RuntimeError(
            "无法解析此 Kindle 文件（若为带 DRM 保护的官方电子书，请先用 Calibre 去除保护）：" + str(e)) from e
    ext = os.path.splitext(out)[1].lower()
    if ext == ".epub":
        return load_epub(out)        # KF8 (azw3) → epub
    if ext in (".html", ".htm"):
        return load_html_file(out)   # 旧版 mobi/azw/prc → html
    raise RuntimeError("此 Kindle 文件为 PDF 型（Print Replica），暂不支持，请先转换为 EPUB")

# ============ 后台加载（不阻塞 UI） ============
class BookLoader(QObject):
    loaded = Signal(str, list, str)   # (全文, 章节, 书名)
    failed = Signal(str)
    progress = Signal(int, str)       # (百分比, 说明)
    def __init__(self, path):
        super().__init__()
        self.path = path
    def run(self):
        try:
            ext = os.path.splitext(self.path)[1].lower()
            if ext == ".epub":
                self.progress.emit(10, "解析 epub…")
                text, chapters, title = load_epub(self.path)
            elif ext in (".html", ".htm"):
                self.progress.emit(10, "读取文件…")
                text, chapters, title = load_html_file(self.path)
            elif ext in (".mobi", ".azw", ".azw3", ".azw8", ".prc"):
                self.progress.emit(10, "解析 Kindle 文件…")
                text, chapters, title = load_kindle(self.path)
            elif ext == ".pdf":
                self.progress.emit(10, "解析 PDF…")
                text, chapters, title = load_pdf(self.path)
            elif ext in (".md", ".markdown"):
                self.progress.emit(10, "解析 Markdown…")
                text, chapters, title = load_markdown(self.path)
            elif ext == ".kfx":
                raise RuntimeError("暂不支持 KFX 格式，请先用 Calibre 转换为 EPUB 或 MOBI")
            else:
                self.progress.emit(5, "读取文件…")
                with open(self.path, "rb") as f:
                    raw = f.read()
                self.progress.emit(40, "解码…")
                text = decode_text(raw)
                self.progress.emit(65, "扫描章节…")
                chapters = scan_chapters(text)
                title = ""
            self.progress.emit(95, "完成")
            self.loaded.emit(text, chapters, title)
        except Exception as e:
            self.failed.emit(str(e))

# ============ AI 工具 ============
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

DEFAULT_CONFIG = {"font_family": "", "font_size": 15,
                  "line_spacing": 1.5, "margin_x": 28.0, "margin_y": 44.0,
                  "outer": 16.0, "gutter": 36.0, "para_spacing": 0.6,
                  "api_key": "", "api_base": "https://api.openai.com/v1",
                  "model": "gpt-4o-mini",
                  "tts_voice": "zh-CN-XiaoxiaoNeural", "tts_rate": "+0%",
                  "theme": DEFAULT_THEME, "recent": [],
                  "bilingual": False, "bilingual_target": "中文"}

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

def call_llm(prompt, cfg):
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("未配置 API Key（工具栏→设置）")
    base = (cfg.get("api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    payload = {"model": (cfg.get("model") or "").strip(), "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.2}
    # 本地 Ollama 的混合推理模型（如 Qwen3.5）默认会先生成一段隐藏思考再给答案，
    # 翻译这种简单任务用不上，加 think=false 能省一部分延迟（实测约减 15~20%）。
    # 这是 Ollama 专有字段，只在打到本地 Ollama 时加，避免传给云端 API 报错。
    host = urlparse(base).hostname or ""
    if host in ("localhost", "127.0.0.1"):
        payload["think"] = False
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

def translate(text, cfg):
    return call_llm(f"把下面这段小说文本翻译成简体中文，只输出译文：\n{text}", cfg)

def translate_to(text, cfg, target="中文"):
    if target == "英文":
        return call_llm(f"把下面这段小说文本翻译成地道的英文，只输出译文：\n{text}", cfg)
    return call_llm(f"把下面这段小说文本翻译成简体中文，只输出译文：\n{text}", cfg)

def lookup(word, cfg):
    return call_llm(f"解释词语“{word}”：给出词性、释义和一条例句。", cfg)

class AiWorker(QObject):
    done = Signal(str); failed = Signal(str)
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:
            self.failed.emit(str(e))

# ============ 语音朗读（edge-tts） ============
TTS_VOICES = [
    ("晓晓（女·温柔，默认）", "zh-CN-XiaoxiaoNeural"),
    ("晓伊（女·活泼）", "zh-CN-XiaoyiNeural"),
    ("晓墨（女·可爱）", "zh-CN-XiaomoNeural"),
    ("云希（男·少年）", "zh-CN-YunxiNeural"),
    ("云扬（男·新闻）", "zh-CN-YunyangNeural"),
    ("云健（男）", "zh-CN-YunjianNeural"),
    ("晓北（东北女声）", "zh-CN-liaoning-XiaobeiNeural"),
    ("晓妮（陕西女声）", "zh-CN-shaanxi-XiaoniNeural"),
    ("晓臻（台湾女声）", "zh-TW-HsiaoChenNeural"),
    ("云哲（台湾男声）", "zh-TW-YunJheNeural"),
    ("Aria（英文·美式·女）", "en-US-AriaNeural"),
    ("Jenny（英文·美式·女）", "en-US-JennyNeural"),
    ("Guy（英文·美式·男）", "en-US-GuyNeural"),
    ("Sonia（英文·英式·女）", "en-GB-SoniaNeural"),
    ("Ryan（英文·英式·男）", "en-GB-RyanNeural"),
    ("Nanami（日文·女）", "ja-JP-NanamiNeural"),
    ("Keita（日文·男）", "ja-JP-KeitaNeural"),
    ("Huayan（中文·离线，无需联网）", "piper:zh_CN-huayan-medium"),
    ("Lessac（英文·离线，无需联网）", "piper:en_US-lessac-medium"),
]
TTS_RATES = ["-50%", "-25%", "-10%", "+0%", "+10%", "+25%", "+50%", "+100%"]
TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# Piper 离线语音：voice id -> Hugging Face piper-voices 仓库里的路径前缀
PIPER_VOICES = {
    "zh_CN-huayan-medium": "zh/zh_CN/huayan/medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium",
}
PIPER_MODEL_BASE_URL = "https://hf-mirror.com/rhasspy/piper-voices/resolve/main"
_piper_voice_cache = {}
_piper_voice_cache_lock = threading.Lock()

def _piper_model_dir():
    d = os.path.join(APP_DIR, "piper_models")
    os.makedirs(d, exist_ok=True)
    return d

def _ensure_piper_model(voice_id: str) -> str:
    """确保 voice_id 对应的 Piper 模型文件（.onnx + .onnx.json）已下载到本地，返回 onnx 路径。"""
    if voice_id not in PIPER_VOICES:
        raise RuntimeError(f"未知的离线语音：{voice_id}")
    d = _piper_model_dir()
    onnx_path = os.path.join(d, f"{voice_id}.onnx")
    cfg_path = onnx_path + ".json"
    prefix = PIPER_VOICES[voice_id]
    if not (os.path.isfile(onnx_path) and os.path.isfile(cfg_path)):
        _log_tts(f"[离线朗读] 首次使用，正在下载模型：{voice_id}（约 60MB，需联网一次）")
        for path, url in ((onnx_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx"),
                          (cfg_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx.json")):
            tmp = path + ".part"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                    out.write(resp.read())
                os.replace(tmp, path)
            except Exception as e:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise RuntimeError(f"离线语音模型下载失败：{e}") from e
    return onnx_path

def _piper_rate_to_length_scale(rate: str) -> float:
    """把 edge-tts 风格的 "+10%"/"-25%" 语速转成 Piper 的 length_scale（越大越慢）。"""
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    pct = max(-90, min(200, pct))
    return 1.0 / (1.0 + pct / 100.0)

def _load_piper_voice(voice_id: str):
    with _piper_voice_cache_lock:
        voice = _piper_voice_cache.get(voice_id)
        if voice is not None:
            return voice
        try:
            from piper import PiperVoice
        except Exception as e:
            raise RuntimeError("未安装 piper-tts，请执行：pip install piper-tts") from e
        onnx_path = _ensure_piper_model(voice_id)
        voice = PiperVoice.load(onnx_path)
        _piper_voice_cache[voice_id] = voice
        return voice

def synthesize_tts_piper(text: str, voice_id: str, rate: str) -> bytes:
    """离线合成（Piper），返回 wav 字节。"""
    from piper import SynthesisConfig
    voice = _load_piper_voice(voice_id)
    syn_config = SynthesisConfig(length_scale=_piper_rate_to_length_scale(rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return buf.getvalue()

def split_sentences(text: str, base: int = 0):
    """把文本切成句子：返回 [(起始偏移, 结束偏移, 句子文本)]，偏移为全局字符偏移。"""
    res = []
    seg_start = 0
    i = 0
    n = len(text)
    buf_len = 0
    while i < n:
        ch = text[i]
        buf_len += 1
        if ch in "。！？!?；;" or ch == "\n" or buf_len >= 100:
            end = i + 1
            seg = text[seg_start:end]
            if seg.strip():
                res.append((base + seg_start, base + end, seg))
            seg_start = end
            buf_len = 0
        i += 1
    if seg_start < n:
        seg = text[seg_start:n]
        if seg.strip():
            res.append((base + seg_start, base + n, seg))
    return res

def synthesize_tts(text: str, voice: str, rate: str) -> bytes:
    """把一段文本合成音频字节。voice 以 "piper:" 开头走本地离线合成（wav），
    否则走 edge-tts 云端合成（mp3，直连，带重试）。"""
    if voice.startswith("piper:"):
        return synthesize_tts_piper(text, voice[len("piper:"):], rate)
    try:
        import edge_tts
    except Exception as e:
        raise RuntimeError("未安装 edge-tts，请执行：pip install edge-tts") from e
    last_err = None
    for attempt in range(3):
        try:
            return _synth_once(edge_tts, text, voice, rate)
        except Exception as e:
            last_err = e
            _log_tts(f"[重试] 第{attempt + 1}次失败（原文前30字：{text[:30]!r}）：{e}")
            time.sleep(0.6 * (attempt + 1))   # 0.6s / 1.2s 退避后重试
    raise RuntimeError(f"语音合成失败（已重试3次）：{last_err}")

def _log_tts(msg):
    """把朗读相关事件写入 ~/.pyreader/tts.log。"""
    try:
        with open(os.path.join(APP_DIR, "tts.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def _synth_once(edge_tts, text, voice, rate):
    async def go():
        com = edge_tts.Communicate(text, voice, rate=rate)
        out = []
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                out.append(chunk["data"])
        if not out:
            raise RuntimeError("edge-tts 未返回音频")
        return b"".join(out)
    return asyncio.run(go())

class TtsWorker(QObject):
    ready = Signal(int, bytes)     # (句子序号, mp3 字节)
    failed = Signal(str)
    def __init__(self, seq, text, voice, rate):
        super().__init__()
        self.seq, self.text, self.voice, self.rate = seq, text, voice, rate
    def run(self):
        try:
            self.ready.emit(self.seq, synthesize_tts(self.text, self.voice, self.rate))
        except Exception as e:
            self.failed.emit(str(e))

class TranslateWorker(QObject):
    done = Signal(int, str)        # (句子序号, 译文)
    failed = Signal(int, str)      # (句子序号, 错误信息)
    def __init__(self, seq, text, cfg, target):
        super().__init__()
        self.seq, self.text, self.cfg, self.target = seq, text, cfg, target
    def run(self):
        try:
            self.done.emit(self.seq, translate_to(self.text, self.cfg, self.target))
        except Exception as e:
            self.failed.emit(self.seq, str(e))

# ============ 双页视图（显示"当前章节"的页） ============
class PageView(QWidget):
    pageChanged = Signal(int)          # 左页的全局字符偏移
    needChapter = Signal(int, bool)    # (相对章序号 ±1, 是否跳到末页)

    def __init__(self):
        super().__init__()
        self.pages: List[Page] = []
        self.full_text = ""
        self.font = QFont(); self.font.setPointSize(16)
        self.line_spacing = LINE_SPACING
        self.margin_x = MARGIN_X; self.margin_y = MARGIN_Y
        self.outer = OUTER; self.gutter = GUTTER
        self.para_spacing = PARA_SPACING
        self.line_h = QFontMetricsF(self.font).height() * self.line_spacing
        self.spread = 0
        self.sel_start = self.sel_end = -1
        self.read_start = self.read_end = -1   # 朗读高亮范围
        self.search_starts = []                # 搜索匹配起始偏移列表
        self.search_len = 0
        self.search_cur = -1                   # 当前匹配的起始偏移
        # 主题配色
        self.bg = "#1b1b1e"; self.page_color = "#fdfdfb"; self.page_border = "#d8d8d8"
        self.text_color = "#1a1a1a"; self.header_color = "#9a9a9a"; self.pageno_color = "#a0a0a0"
        self.selecting = False
        self._press_pos = None
        self._dragged = False
        self.book_title = ""
        self.chapter_title = ""
        self.chars_per_page = 400.0
        self.setMouseTracking(True)
        self.setMinimumSize(600, 500)
        # 触摸板/触摸屏手势翻页
        self._pan_dx = 0.0
        self._pan_dy = 0.0
        self._wheel_acc = 0.0      # 滚轮滑动累计
        self._touch_gesture = False  # mac 原生手势进行中
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.grabGesture(Qt.GestureType.SwipeGesture)
        self.grabGesture(Qt.GestureType.PanGesture)

    def set_layout(self, layout):
        self.font = layout["font"]
        self.line_spacing = layout["line_spacing"]
        self.margin_x = layout["margin_x"]
        self.margin_y = layout["margin_y"]
        self.outer = layout["outer"]
        self.gutter = layout["gutter"]
        self.para_spacing = layout.get("para_spacing", PARA_SPACING)
        self.line_h = QFontMetricsF(self.font).height() * self.line_spacing
        self.update()

    def set_theme(self, colors):
        self.bg = colors["bg"]
        self.page_color = colors["page"]
        self.page_border = colors["border"]
        self.text_color = colors["text"]
        self.header_color = colors["header"]
        self.pageno_color = colors["pageno"]
        self.update()

    def pagination_params(self):
        """把当前布局转成 LazyPager 分页所需的参数（含由控件尺寸推算的页宽高）。"""
        pw = (self.width() - self.gutter - 2 * self.outer) / 2
        ph = self.height() - 2 * self.outer
        return {"font": self.font, "page_size": QSizeF(pw, ph),
                "line_spacing": self.line_spacing, "margin_x": self.margin_x,
                "margin_y": self.margin_y, "para_spacing": self.para_spacing}

    def page_rects(self):
        w, h = self.width(), self.height()
        pw = (w - self.gutter - 2 * self.outer) / 2
        left = QRectF(self.outer, self.outer, pw, h - 2 * self.outer)
        right = QRectF(self.outer + pw + self.gutter, self.outer, pw, h - 2 * self.outer)
        return left, right

    def left_page_offset(self) -> int:
        if self.pages and self.spread * 2 < len(self.pages):
            return self.pages[self.spread * 2].start
        return 0

    def flip(self, delta: int):
        if not self.pages:
            return
        total = (len(self.pages) + 1) // 2
        ns = self.spread + delta
        if ns < 0:
            self.needChapter.emit(-1, True)       # 去上一章末页
        elif ns >= total:
            self.needChapter.emit(1, False)       # 去下一章首页
        else:
            self.spread = ns
            self.pageChanged.emit(self.left_page_offset())
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.bg))              # 桌面背景（随主题）
        left, right = self.page_rects()
        li, ri = self.spread * 2, self.spread * 2 + 1
        self._draw_gutter_shadow(p, left, right)               # 书脊阴影
        if li < len(self.pages):
            self._draw_page(p, left, self.pages[li], self.book_title)
        if ri < len(self.pages):
            self._draw_page(p, right, self.pages[ri], self.chapter_title)
        p.end()

    def _draw_gutter_shadow(self, p, left, right):
        """书脊处一道柔和的竖向阴影，模拟装订。"""
        x0 = left.right() - 10
        x1 = right.left() + 10
        g = QLinearGradient(x0, 0, x1, 0)
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(0.5, QColor(0, 0, 0, 80))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(QRectF(x0, left.top(), x1 - x0, left.height()))

    def _draw_page(self, p, rect, page, header_text):
        # 四周轻微投影，模拟书页浮在深色桌面上
        for i in (6, 4, 2):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, int(22 / i)))
            p.drawRect(rect.adjusted(-i, -i, i, i))
        # 书页
        p.setPen(QPen(QColor(self.page_border), 1))
        p.setBrush(QColor(self.page_color))
        p.drawRect(rect)

        fm = QFontMetricsF(self.font)
        p.setFont(self.font); p.setPen(QColor(self.text_color))
        indent_w = fm.horizontalAdvance("中") * INDENT_SPACES
        tx = rect.left() + self.margin_x
        y = rect.top() + self.margin_y          # 每行的“顶端”
        for ln in page.lines:
            if ln.text:
                ix = indent_w if ln.indent else 0.0
                line_x = tx + ix
                rs, re_ = max(ln.start, self.read_start), min(ln.end, self.read_end)
                if rs < re_:
                    x1 = line_x + fm.horizontalAdvance(ln.text[:rs - ln.start])
                    x2 = line_x + fm.horizontalAdvance(ln.text[:re_ - ln.start])
                    p.fillRect(QRectF(x1, y, x2 - x1, fm.height()), QColor("#ffe08a"))
                # 搜索高亮：当前匹配亮橙，其余淡黄
                if self.search_starts and self.search_len > 0:
                    lo = bisect.bisect_left(self.search_starts, ln.start)
                    hi = bisect.bisect_left(self.search_starts, ln.end)
                    for k in range(lo, hi):
                        ms = self.search_starts[k]
                        me = min(ms + self.search_len, ln.end)
                        x1 = line_x + fm.horizontalAdvance(ln.text[:ms - ln.start])
                        x2 = line_x + fm.horizontalAdvance(ln.text[:me - ln.start])
                        col = QColor("#ffb340") if ms == self.search_cur else QColor("#ffe9a8")
                        p.fillRect(QRectF(x1, y, x2 - x1, fm.height()), col)
                s, e = max(ln.start, self.sel_start), min(ln.end, self.sel_end)
                if s < e:
                    x1 = line_x + fm.horizontalAdvance(ln.text[:s - ln.start])
                    x2 = line_x + fm.horizontalAdvance(ln.text[:e - ln.start])
                    p.fillRect(QRectF(x1, y, x2 - x1, fm.height()), QColor("#cfe3ff"))
                p.drawText(QPointF(line_x, y + fm.ascent()), ln.text)
            y += self.line_h + ln.gap_after

        # 页眉（顶部居中，左侧书名 / 右侧章节），与正文留出清晰间距
        if header_text:
            hf = QFont(self.font); hf.setPointSize(max(9, self.font.pointSize() - 4))
            p.setFont(hf); p.setPen(QColor(self.header_color))
            p.drawText(QRectF(rect.left(), rect.top() + 6, rect.width(), self.margin_y - 18),
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                       header_text)
            p.setFont(self.font)
        # 页码（底部居中：当前 / 总页数）
        pg = max(1, int(page.start / max(1.0, self.chars_per_page)) + 1)
        total = max(1, int(len(self.full_text) / max(1.0, self.chars_per_page)))
        pf = QFont(self.font); pf.setPointSize(max(9, self.font.pointSize() - 4))
        p.setFont(pf); p.setPen(QColor(self.pageno_color))
        p.drawText(QRectF(rect.left(), rect.bottom() - self.margin_y + 10, rect.width(), self.margin_y - 18),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   f"{pg} / {total}")
        p.setFont(self.font)

    def _hit(self, pos):
        left, right = self.page_rects()
        for rect, pi in ((left, self.spread * 2), (right, self.spread * 2 + 1)):
            if pi < len(self.pages) and rect.contains(pos):
                page = self.pages[pi]
                fm = QFontMetricsF(self.font)
                y = rect.top() + self.margin_y
                ln = page.lines[-1] if page.lines else None
                for cand in page.lines:                 # 按累计高度定位行
                    h = self.line_h + cand.gap_after
                    if pos.y() < y + h:
                        ln = cand
                        break
                    y += h
                if ln is None:
                    return None
                ix = (fm.horizontalAdvance("中") * INDENT_SPACES) if ln.indent else 0.0
                x = pos.x() - (rect.left() + self.margin_x + ix)
                lo, hi = 0, len(ln.text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fm.horizontalAdvance(ln.text[:mid]) <= x:
                        lo = mid
                    else:
                        hi = mid - 1
                return ln.start + lo
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position()
            self._dragged = False
            off = self._hit(e.position())
            if off is not None:
                self.sel_start = self.sel_end = off
                self.selecting = True
                self.update()

    def mouseMoveEvent(self, e):
        if self.selecting:
            if self._press_pos is not None and \
               (e.position() - self._press_pos).manhattanLength() > 6:
                self._dragged = True          # 拖动 = 选字，不翻页
            off = self._hit(e.position())
            if off is not None:
                self.sel_end = off
                self.update()
        else:
            # 悬停：左右两侧显示手型，提示可点击翻页
            w = self.width(); x = e.position().x()
            if x < w * CLICK_ZONE or x > w * (1 - CLICK_ZONE):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                l, r = self.page_rects()
                self.setCursor(Qt.CursorShape.IBeamCursor
                               if (l.contains(e.position()) or r.contains(e.position()))
                               else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        dragged = self._dragged
        self.selecting = False
        self._dragged = False
        if dragged:
            if self.sel_start > self.sel_end:
                self.sel_start, self.sel_end = self.sel_end, self.sel_start
            self.update()
            return
        if e.button() == Qt.MouseButton.LeftButton:      # 单击（未拖动）= 翻页
            w = self.width(); x = e.position().x()
            self.sel_start = self.sel_end = -1           # 单击清除选择
            if x < w * CLICK_ZONE:
                self.flip(-1)                            # 点左侧 → 上一页
            elif x > w * (1 - CLICK_ZONE):
                self.flip(1)                             # 点右侧 → 下一页
            else:
                self.update()

    def selected_text(self):
        if 0 <= self.sel_start and self.sel_end - self.sel_start > 0:
            return self.full_text[self.sel_start:self.sel_end]
        return ""

    # ---- 触摸板 / 触摸屏手势翻页 ----
    def event(self, e):
        if e.type() == QEvent.Type.NativeGesture:
            return self._on_native_gesture(e)
        if e.type() == QEvent.Type.Gesture:
            return self._on_gesture(e)
        return super().event(e)

    def _on_native_gesture(self, e: QNativeGestureEvent):
        """Windows 精密触摸板 / macOS 触控板的原生手势（两指滑动）。"""
        gt = e.gestureType()
        if gt == Qt.NativeGestureType.BeginNativeGesture:
            self._pan_dx = self._pan_dy = 0.0
            self._touch_gesture = True
            return True
        if gt == Qt.NativeGestureType.EndNativeGesture:
            self._touch_gesture = False
            self._apply_swipe(self._pan_dx, self._pan_dy)
            self._pan_dx = self._pan_dy = 0.0
            return True
        if gt == Qt.NativeGestureType.PanNativeGesture:
            self._pan_dx += e.value()          # 累加该帧横向位移
            return True
        if gt == Qt.NativeGestureType.SwipeNativeGesture:
            self.flip(1 if e.value() < 0 else -1)   # 三指横滑
            return True
        return False

    def _on_gesture(self, e):
        """触摸屏手势（QGesture，含 QSwipeGesture / QPanGesture）。"""
        for g in e.gestures():
            if g.state() == Qt.GestureState.GestureFinished:
                if isinstance(g, QSwipeGesture):
                    ang = g.swipeAngle()          # 0=右 90=上 180=左 270=下
                    if 45 <= ang < 135:           # 上滑 → 下一页
                        self.flip(1)
                    elif 135 <= ang < 225:        # 左滑 → 下一页
                        self.flip(1)
                    elif 225 <= ang < 315:        # 下滑 → 上一页
                        self.flip(-1)
                    else:                         # 右滑 → 上一页
                        self.flip(-1)
                elif isinstance(g, QPanGesture):
                    d = g.offset()
                    self._apply_swipe(d.x(), d.y())
        return True

    def _apply_swipe(self, dx, dy):
        """按累积位移判定方向：左/上滑 = 下一页，右/下滑 = 上一页。"""
        ax, ay = abs(dx), abs(dy)
        if max(ax, ay) < SWIPE_THRESHOLD:
            return
        if ax >= ay:       # 左右滑动为主
            self.flip(1 if dx < 0 else -1)
        else:              # 上下滑动为主
            self.flip(1 if dy < 0 else -1)

    def wheelEvent(self, e):
        """鼠标滚轮 / 触摸板两指滑动 → 翻页。
        Windows 触摸板把滑动上报为滚轮事件，所以这里处理最通用。"""
        if self._touch_gesture:      # mac 原生手势进行中，交给手势处理避免重复
            e.accept()
            return
        d = e.angleDelta()
        axis = d.x() if abs(d.x()) > abs(d.y()) else d.y()   # 横向占优则横滑
        if axis == 0:
            e.accept()
            return
        self._wheel_acc += axis
        if abs(self._wheel_acc) >= WHEEL_THRESHOLD:
            self.flip(1 if self._wheel_acc < 0 else -1)      # 下/左滑 = 下一页
            self._wheel_acc = 0.0
        e.accept()

# ============ 设置对话框 ============
AI_PRESETS = {
    "OpenAI":     ("https://api.openai.com/v1", "gpt-4o-mini"),
    "DeepSeek":   ("https://api.deepseek.com", "deepseek-chat"),
    "通义千问":    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "本地 Ollama": ("http://localhost:11434/v1", "ornith-1.5:9b"),
    "自定义":      ("", ""),
}

class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.preset = QComboBox()
        self.preset.addItems(list(AI_PRESETS.keys()))
        self.key = QLineEdit(cfg.get("api_key", "")); self.key.setEchoMode(QLineEdit.Password)
        self.base = QLineEdit(cfg.get("api_base", ""))
        self.model = QLineEdit(cfg.get("model", ""))
        # 排版参数
        self.font_family = QComboBox()
        self.font_family.addItem("系统默认", "")
        try:
            from PySide6.QtGui import QFontDatabase
            avail = set(QFontDatabase.families())
            for name in ("微软雅黑", "Microsoft YaHei", "宋体", "SimSun", "楷体", "KaiTi",
                         "黑体", "SimHei", "等线", "DengXian", "仿宋", "FangSong",
                         "思源宋体", "Source Han Serif SC", "Noto Serif CJK SC",
                         "思源黑体", "Source Han Sans SC", "PingFang SC"):
                if name in avail:
                    self.font_family.addItem(name, name)
        except Exception:
            pass
        i = self.font_family.findData(cfg.get("font_family", ""))
        self.font_family.setCurrentIndex(i if i >= 0 else 0)
        self.theme = QComboBox()
        self.theme.addItems(list(THEMES.keys()))
        ti = self.theme.findText(cfg.get("theme", DEFAULT_THEME))
        self.theme.setCurrentIndex(ti if ti >= 0 else 0)
        self.font_size = QSpinBox(); self.font_size.setRange(10, 48)
        self.font_size.setValue(cfg.get("font_size", 16))
        self.line_spacing = QDoubleSpinBox(); self.line_spacing.setRange(0.8, 3.0)
        self.line_spacing.setSingleStep(0.05); self.line_spacing.setValue(cfg.get("line_spacing", 1.25))
        self.para_spacing = QDoubleSpinBox(); self.para_spacing.setRange(0.0, 3.0)
        self.para_spacing.setSingleStep(0.1); self.para_spacing.setValue(cfg.get("para_spacing", 0.5))
        self.margin_x = QSpinBox(); self.margin_x.setRange(0, 120)
        self.margin_x.setValue(cfg.get("margin_x", 24))
        self.margin_y = QSpinBox(); self.margin_y.setRange(0, 120)
        self.margin_y.setValue(cfg.get("margin_y", 44))
        self.outer = QSpinBox(); self.outer.setRange(0, 80)
        self.outer.setValue(cfg.get("outer", 16))
        self.gutter = QSpinBox(); self.gutter.setRange(0, 160)
        self.gutter.setValue(cfg.get("gutter", 32))
        # 语音朗读（edge-tts）
        self.tts_voice = QComboBox()
        for label, vid in TTS_VOICES:
            self.tts_voice.addItem(label, vid)
        vi = self.tts_voice.findData(cfg.get("tts_voice", TTS_DEFAULT_VOICE))
        self.tts_voice.setCurrentIndex(vi if vi >= 0 else 0)
        self.tts_rate = QComboBox()
        self.tts_rate.addItems(TTS_RATES)
        ri = self.tts_rate.findText(cfg.get("tts_rate", "+0%"))
        self.tts_rate.setCurrentIndex(ri if ri >= 0 else 3)
        # 双语朗读（朗读时 AI 翻译）
        self.bilingual = QCheckBox("朗读时自动翻译并显示")
        self.bilingual.setChecked(bool(cfg.get("bilingual", False)))
        self.bilingual_target = QComboBox()
        self.bilingual_target.addItems(["中文", "英文"])
        bi = self.bilingual_target.findText(cfg.get("bilingual_target", "中文"))
        self.bilingual_target.setCurrentIndex(bi if bi >= 0 else 0)
        # 根据当前 base 反推预设（先设值，后连信号，避免误触发覆盖用户自定义模型）
        cur = cfg.get("api_base", "").rstrip("/")
        matched = False
        for name, (b, m) in AI_PRESETS.items():
            if b and b.rstrip("/") == cur:
                self.preset.setCurrentText(name); matched = True; break
        if not matched:
            self.preset.setCurrentText("自定义")
        self.preset.currentTextChanged.connect(self._apply)
        form = QFormLayout(self)
        form.addRow("服务商预设", self.preset)
        form.addRow("API Key", self.key)
        form.addRow("API Base", self.base)
        form.addRow("模型", self.model)
        form.addRow("排版", QLabel("（调整后立即生效）"))
        form.addRow("字体", self.font_family)
        form.addRow("主题", self.theme)
        form.addRow("字号", self.font_size)
        form.addRow("行距", self.line_spacing)
        form.addRow("段落间距", self.para_spacing)
        form.addRow("左右边距", self.margin_x)
        form.addRow("上下边距", self.margin_y)
        form.addRow("页边距(外)", self.outer)
        form.addRow("书脊", self.gutter)
        form.addRow("语音朗读", QLabel("（edge-tts，需联网）"))
        form.addRow("朗读音色", self.tts_voice)
        form.addRow("朗读语速", self.tts_rate)
        form.addRow("双语朗读", QLabel("（朗读时 AI 翻译，需 API Key）"))
        form.addRow("双语翻译", self.bilingual)
        form.addRow("翻译目标", self.bilingual_target)
        btn = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn.accepted.connect(self.accept); btn.rejected.connect(self.reject)
        form.addRow(btn)

    def _apply(self, name):
        b, m = AI_PRESETS.get(name, ("", ""))
        if b: self.base.setText(b)
        if m: self.model.setText(m)

# ============ 主窗口 ============
class MainWindow(QMainWindow):
    def __init__(self, book_path=None):
        super().__init__()
        self.cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
        self.bookmarks = load_json(BOOKMARKS_PATH, {})
        self.book_path = book_path
        self.full_text = ""
        self.pager: Optional[LazyPager] = None
        self.cur_chapter = 0
        self.loader = None

        self.view = PageView()
        self.view.set_layout(self._make_layout())
        self.view.set_theme(THEMES.get(self.cfg.get("theme"), THEMES[DEFAULT_THEME]))
        self.search_matches = []; self.search_query = ""; self.search_cur = -1
        self.toc = QListWidget()
        self.bm_list = QListWidget()
        self.ai_out = QTextEdit(); self.ai_out.setReadOnly(True)

        left = QWidget(); lv = QVBoxLayout(left)
        lv.addWidget(QLabel("目录")); lv.addWidget(self.toc)
        lv.addWidget(QLabel("书签")); lv.addWidget(self.bm_list)
        right = QWidget(); rv = QVBoxLayout(right)
        rv.addWidget(self.ai_out)

        self.setCentralWidget(self.view)                 # 沉浸阅读：默认全宽
        self.toc_dock = QDockWidget("目录 / 书签", self)
        self.toc_dock.setWidget(left)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.toc_dock)
        self.toc_dock.hide()                             # 默认隐藏
        self.ai_dock = QDockWidget("AI 工具", self)
        self.ai_dock.setWidget(right)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.ai_dock)
        self.ai_dock.hide()                              # 默认隐藏
        self.bilingual_out = QTextEdit(); self.bilingual_out.setReadOnly(True)
        biw = QWidget(); biv = QVBoxLayout(biw)
        biv.addWidget(QLabel("原文 / 译文（随朗读更新）")); biv.addWidget(self.bilingual_out)
        self.bilingual_dock = QDockWidget("🌐 双语翻译", self)
        self.bilingual_dock.setWidget(biw)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.bilingual_dock)
        self.bilingual_dock.hide()                       # 默认隐藏

        # ---- 语音朗读（edge-tts）----
        self.player = QMediaPlayer(self) if _HAS_MULTIMEDIA else None
        if self.player:
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
            self.player.mediaStatusChanged.connect(self._on_tts_media_status)
        self.tts_on = False
        self.tts_paused = False
        self.tts_chapter = 0
        self.tts_epoch = 0            # 章节代际，用于忽略过期合成结果
        self.tts_sentences = []       # [(start, end, text)]
        self.tts_idx = 0
        self.tts_cache = {}           # seq -> mp3 字节
        self.tts_inflight = set()
        self.tts_workers = {}         # seq -> TtsWorker（防止信号前被 GC）
        self.tts_buf = None
        self.bilingual_on = bool(self.cfg.get("bilingual", False))
        self.tr_cache = {}            # seq -> 译文
        self.tr_inflight = set()
        self.tr_workers = {}

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True); self._resize_timer.setInterval(200)
        self._resize_timer.timeout.connect(self.reload_current)

        self._build_toolbar(); self._build_menus()
        self._rebuild_recent_menu()
        self._update_bilingual_act()
        if self.bilingual_on:
            self.bilingual_dock.show()

        # 底部状态栏：加载进度条 + 阅读进度（可拖动跳转）
        self.load_bar = QProgressBar()
        self.load_bar.setRange(0, 100); self.load_bar.setFixedWidth(180)
        self.load_bar.hide()
        self.pos_label = QLabel("0.0%"); self.pos_label.setFixedWidth(52)
        self.read_slider = QSlider(Qt.Orientation.Horizontal)
        self.read_slider.setRange(0, 1000); self.read_slider.setFixedWidth(220)
        self.read_slider.setToolTip("拖动跳转到指定位置")
        self.read_slider.sliderMoved.connect(self._on_slider_move)
        self.read_slider.sliderReleased.connect(self._on_slider_release)
        self.statusBar().addPermanentWidget(self.read_slider)
        self.statusBar().addPermanentWidget(self.pos_label)
        self.statusBar().addPermanentWidget(self.load_bar)
        # 定时关闭（睡前听书）
        self.sleep_left = 0
        self.sleep_label = QLabel("")
        self.sleep_label.hide()
        self.sleep_timer = QTimer(self)
        self.sleep_timer.setInterval(1000)
        self.sleep_timer.timeout.connect(self._on_sleep_tick)
        self.statusBar().addPermanentWidget(self.sleep_label)

        self.statusBar().showMessage("就绪")
        self.resize(1280, 800)
        if book_path:
            self.load_book(book_path)

    def _build_toolbar(self):
        tb = QToolBar(); self.addToolBar(tb)
        tb.addAction("打开", self.open_book)
        tb.addAction("字体", self.choose_font)
        tb.addAction("设置", self.open_settings)
        tb.addSeparator()
        tb.addAction("◀ 上一页", lambda: self.view.flip(-1))
        tb.addAction("下一页 ▶", lambda: self.view.flip(1))
        tb.addAction("添加书签", self.add_bookmark)
        tb.addSeparator()
        self.tts_act = tb.addAction("🔊 朗读", self.toggle_reading)
        self.stop_act = tb.addAction("⏹ 停止", self.stop_reading)
        self.stop_act.setEnabled(False)
        self.bilingual_act = tb.addAction("🌐 双语", self.toggle_bilingual)
        self.bilingual_act.setCheckable(True)
        self.sleep_act = tb.addAction("⏰ 定时")
        self.sleep_act.setMenu(self._build_sleep_menu())
        tb.addSeparator()
        tb.addAction(self.toc_dock.toggleViewAction())   # 目录 显示/隐藏
        tb.addAction(self.ai_dock.toggleViewAction())    # AI 工具 显示/隐藏
        # 最近打开（下拉菜单）
        self.recent_menu = QMenu("最近打开", self)
        self.recent_act = tb.addAction("🕘 最近")
        self.recent_act.setMenu(self.recent_menu)
        # 全文搜索
        tb.addSeparator()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索 Ctrl+F")
        self.search_box.setFixedWidth(150)
        self.search_box.textChanged.connect(self._on_search_changed)
        self.search_box.returnPressed.connect(self._search_next)
        tb.addWidget(self.search_box)
        tb.addAction("↑", self._search_prev)
        tb.addAction("↓", self._search_next)

    def _build_menus(self):
        self.toc.itemClicked.connect(lambda it: self.goto_offset(it.data(Qt.ItemDataRole.UserRole)))
        self.bm_list.itemClicked.connect(lambda it: self.goto_offset(it.data(Qt.ItemDataRole.UserRole)))
        self.view.needChapter.connect(self._need_chapter)
        self.view.pageChanged.connect(lambda _: self._update_status())
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._show_ctx_menu)

    # ---- 打开：全在后台线程，主线程不卡 ----
    def _make_layout(self):
        c = self.cfg
        fam = c.get("font_family", "") or default_cjk_font()
        f = QFont(fam) if fam else QFont()
        f.setPointSize(c.get("font_size", 16))
        return {"font": f, "line_spacing": c.get("line_spacing", LINE_SPACING),
                "margin_x": c.get("margin_x", MARGIN_X),
                "margin_y": c.get("margin_y", MARGIN_Y),
                "outer": c.get("outer", OUTER), "gutter": c.get("gutter", GUTTER),
                "para_spacing": c.get("para_spacing", PARA_SPACING)}

    def open_book(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开小说", "",
            "电子书 (*.txt *.md *.markdown *.epub *.html *.htm *.mobi *.azw *.azw3 *.prc *.pdf);;文本 (*.txt *.md *.markdown);;EPUB (*.epub);;Kindle (*.mobi *.azw *.azw3 *.prc);;网页 (*.html *.htm);;PDF (*.pdf)")
        if path:
            self.load_book(path)

    def load_book(self, path):
        self._tts_stop()
        self.book_path = path
        self.load_bar.show()
        self.load_bar.setValue(0)
        self.statusBar().showMessage("正在读取文件…")
        self.loader = BookLoader(path)
        self.loader.progress.connect(self._on_load_progress)
        self.loader.loaded.connect(self._on_book_loaded)
        self.loader.failed.connect(self._on_load_failed)
        threading.Thread(target=self.loader.run, daemon=True).start()

    def _on_load_progress(self, pct, msg):
        self.load_bar.setValue(pct)
        self.statusBar().showMessage(msg)

    def _on_load_failed(self, msg):
        self.load_bar.hide()
        QMessageBox.critical(self, "错误", msg)

    def _on_book_loaded(self, text, chapters, title=""):
        self.load_bar.hide()
        self.full_text = text
        self.view.set_layout(self._make_layout())
        self.pager = LazyPager(text, chapters)
        self.pager.set_params(self.view.pagination_params())
        self.view.full_text = text
        self.view.book_title = title or os.path.splitext(os.path.basename(self.book_path))[0]
        self._build_toc()
        self._load_bookmarks()
        self.setWindowTitle(os.path.basename(self.book_path) + " — PyReader")
        prog = self.bookmarks.get(self.book_path, {}).get("_progress_", 0)
        self.goto_offset(prog)
        self._update_status()
        self._add_recent(self.book_path)

    # ---- 章节导航（惰性分页的入口） ----
    def load_chapter(self, ci, goto_end=False):
        if not self.pager or not self.pager.chapters:
            return
        ci = max(0, min(len(self.pager.chapters) - 1, ci))
        self.cur_chapter = ci
        self.view.pages = self.pager.pages_of(ci)      # 只分这一章，毫秒级
        self.view.chapter_title = self.pager.chapters[ci].title
        pages = self.pager.pages_of(ci)
        ch = self.pager.chapters[ci]
        self.view.chars_per_page = max(1, (ch.end - ch.start) / max(1, len(pages))) if pages else 400.0
        self.view.spread = ((len(self.view.pages) - 1) // 2) if goto_end else 0
        self.view.update()

    def goto_offset(self, offset):
        if not self.pager:
            return
        ci = self.pager.chapter_index_at(offset)
        self.load_chapter(ci)
        for p in self.pager.pages_of(ci):
            if p.start <= offset < p.end:
                self.view.spread = p.index // 2
                break
        self.view.update()

    def _need_chapter(self, delta, goto_end):
        self.load_chapter(self.cur_chapter + delta, goto_end)
        self._update_status()

    # ---- 字体 / 缩放：只重排当前章节 + 防抖 ----
    def choose_font(self):
        ok, font = QFontDialog.getFont(self.view.font, self)
        if ok:
            self.cfg["font_family"] = font.family()
            self.cfg["font_size"] = font.pointSize()
            save_json(CONFIG_PATH, self.cfg)
            self.reload_current()

    def reload_current(self):
        if not self.pager:
            return
        off = self.view.left_page_offset()
        self.view.set_layout(self._make_layout())
        self.pager.set_params(self.view.pagination_params())
        self.load_chapter(self.cur_chapter)
        self.goto_offset(off)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.pager:
            self._resize_timer.start()      # 200ms 内只重排一次

    # ---- 目录 / 书签 ----
    def _build_toc(self):
        self.toc.clear()
        for off, title in [(c.start, c.title) for c in self.pager.chapters]:
            it = QListWidgetItem(title)
            it.setData(Qt.ItemDataRole.UserRole, off)
            self.toc.addItem(it)

    def add_bookmark(self):
        if not self.book_path:
            return
        off = self.view.left_page_offset()
        chapter = next((c.title for c in reversed(self.pager.chapters)
                        if c.start <= off), "未知章节")
        bms = self.bookmarks.setdefault(self.book_path, {})
        bms[f"bm_{int(os.times().elapsed * 1000)}"] = {"chapter": chapter, "offset": off}
        save_json(BOOKMARKS_PATH, self.bookmarks)
        self._load_bookmarks()

    def _load_bookmarks(self):
        self.bm_list.clear()
        for k, v in self.bookmarks.get(self.book_path, {}).items():
            if k == "_progress_":
                continue
            it = QListWidgetItem(f"{v['chapter']}  ·  位置{v['offset']}")
            it.setData(Qt.ItemDataRole.UserRole, v["offset"])
            self.bm_list.addItem(it)

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg.update({
                "api_key": dlg.key.text().strip(), "api_base": dlg.base.text().strip(), "model": dlg.model.text().strip(),
                "font_family": dlg.font_family.currentData(),
                "font_size": dlg.font_size.value(), "line_spacing": dlg.line_spacing.value(),
                "para_spacing": dlg.para_spacing.value(),
                "margin_x": dlg.margin_x.value(), "margin_y": dlg.margin_y.value(),
                "outer": dlg.outer.value(), "gutter": dlg.gutter.value(),
                "tts_voice": dlg.tts_voice.currentData(),
                "tts_rate": dlg.tts_rate.currentText(),
                "theme": dlg.theme.currentText(),
                "bilingual": dlg.bilingual.isChecked(),
                "bilingual_target": dlg.bilingual_target.currentText(),
            })
            save_json(CONFIG_PATH, self.cfg)
            self.view.set_theme(THEMES.get(self.cfg["theme"], THEMES[DEFAULT_THEME]))
            self.bilingual_on = bool(self.cfg["bilingual"])
            if self.bilingual_on:
                self.bilingual_dock.show()
            else:
                self.bilingual_dock.hide()
            self._update_bilingual_act()
            self.reload_current()

    def _show_ctx_menu(self, pos):
        text = self.view.selected_text().strip()
        if not text:
            return
        menu = QMenu(self)
        menu.addAction("🌐 翻译选中内容", lambda: self._run_ai(translate, text))
        menu.addAction("📖 查词典", lambda: self._run_ai(lookup, text))
        menu.addAction("🔖 加入书签", self.add_bookmark)
        menu.exec(self.view.mapToGlobal(pos))

    def _run_ai(self, fn, text):
        self.ai_out.setPlainText("处理中……")
        self.worker = AiWorker(fn, text, self.cfg)
        self.worker.done.connect(self.ai_out.setPlainText)
        self.worker.failed.connect(lambda m: self.ai_out.setPlainText("出错：" + m))
        threading.Thread(target=self.worker.run, daemon=True).start()

    # ---- 双语翻译（朗读时 AI 翻译）----
    def toggle_bilingual(self):
        if not self.cfg.get("api_key"):
            self.statusBar().showMessage("双语翻译需要先配置 API Key（工具栏→设置）")
            self.bilingual_act.setChecked(False)
            return
        self.bilingual_on = not self.bilingual_on
        self.cfg["bilingual"] = self.bilingual_on
        save_json(CONFIG_PATH, self.cfg)
        if self.bilingual_on:
            self.bilingual_dock.show()
        else:
            self.bilingual_dock.hide()
        self._update_bilingual_act()

    def _update_bilingual_act(self):
        self.bilingual_act.setChecked(self.bilingual_on)

    def _tr_kick(self, seq):
        if not self.bilingual_on or seq < 0 or seq >= len(self.tts_sentences):
            return
        if seq in self.tr_inflight or seq in self.tr_cache:
            return
        _, _, text = self.tts_sentences[seq]
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return
        self.tr_inflight.add(seq)
        epoch = self.tts_epoch
        w = TranslateWorker(seq, clean, self.cfg, self.cfg.get("bilingual_target", "中文"))
        w.done.connect(lambda s, t, ep=epoch: self._tr_on_done(ep, s, t))
        w.failed.connect(lambda s, m, ep=epoch: self._tr_on_failed(ep, s, m))
        self.tr_workers[seq] = w
        threading.Thread(target=w.run, daemon=True).start()

    def _tr_on_done(self, epoch, seq, translated):
        if epoch != self.tts_epoch:
            return
        self.tr_inflight.discard(seq)
        self.tr_workers.pop(seq, None)
        self.tr_cache[seq] = translated
        if seq == self.tts_idx and self.bilingual_on:
            self._update_bilingual(seq)

    def _tr_on_failed(self, epoch, seq, msg):
        if epoch != self.tts_epoch:
            return
        self.tr_inflight.discard(seq)
        self.tr_workers.pop(seq, None)
        if "401" in msg or "Authorization" in msg:
            hint = "API Key 无效，请到 工具栏→设置 重新填写"
        elif "未配置 API Key" in msg:
            hint = "未配置 API Key（工具栏→设置）"
        else:
            hint = msg[:120]
        self.tr_cache[seq] = f"（翻译失败：{hint}）"
        if seq == self.tts_idx and self.bilingual_on:
            self._update_bilingual(seq)

    def _update_bilingual(self, seq):
        if not self.bilingual_on or seq < 0 or seq >= len(self.tts_sentences):
            return
        _, _, text = self.tts_sentences[seq]
        clean = re.sub(r"\s+", " ", text).strip()
        tr = self.tr_cache.get(seq)
        if tr is None:
            self.bilingual_out.setPlainText(f"原文：{clean}\n\n译文：（翻译中…）")
        else:
            self.bilingual_out.setPlainText(f"原文：{clean}\n\n译文：{tr}")

    # ---- 语音朗读（edge-tts）----
    def toggle_reading(self):
        if not _HAS_MULTIMEDIA:
            self.statusBar().showMessage("未安装 QtMultimedia，无法朗读")
            return
        if self.tts_on and not self.tts_paused:
            self.player.pause(); self.tts_paused = True
            self._update_tts_actions(); return
        if self.tts_on and self.tts_paused:
            self.tts_paused = False
            self._update_tts_actions()
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
                self.player.play()
            else:
                data = self.tts_cache.pop(self.tts_idx, None)
                if data is not None:
                    self._tts_play_data(self.tts_idx, data)
                else:
                    self._tts_kick(self.tts_idx)
            return
        if not self.pager or not self.full_text:
            return
        self.tts_cache = {}; self.tts_inflight = set(); self.tts_workers = {}
        self.tts_on = True; self.tts_paused = False
        self._update_tts_actions()
        if self._tts_load_chapter(self.cur_chapter, start_off=self.view.left_page_offset()):
            self._tts_start_sentence(0)
        else:
            self._tts_next_chapter()

    def stop_reading(self):
        self._tts_stop()

    def _tts_load_chapter(self, ci, start_off=None):
        """把第 ci 章的文本切成句子，返回是否有可朗读句子。"""
        ch = self.pager.chapters[ci]
        if start_off is None:
            start_off = ch.start
        start_off = max(start_off, ch.start)
        sents = split_sentences(self.full_text[ch.start:ch.end], base=ch.start)
        sents = [(s, e, t) for s, e, t in sents if e > start_off]
        # 过滤纯标点/无语义内容的句子（edge-tts 无法合成，会报 No audio）
        sents = [(s, e, t) for s, e, t in sents if re.search(r"[0-9A-Za-z\u4e00-\u9fff]", t)]
        sents = [(s, e, t) for s, e, t in sents if re.sub(r"\s+", " ", t).strip()]
        self.tts_chapter = ci
        self.tts_sentences = sents
        self.tts_epoch += 1            # 换章：旧缓存/在途结果全部作废
        self.tts_cache.clear(); self.tts_inflight.clear(); self.tts_workers.clear()
        self.tr_cache.clear(); self.tr_inflight.clear(); self.tr_workers.clear()
        return bool(sents)

    def _tts_next_chapter(self):
        nc = self.tts_chapter + 1
        while nc < len(self.pager.chapters):
            if self._tts_load_chapter(nc):
                self._tts_start_sentence(0)
                return
            nc += 1
        self._tts_stop(finished=True)

    def _tts_start_sentence(self, seq):
        self.tts_idx = seq
        s, e, _ = self.tts_sentences[seq]
        self.view.read_start, self.view.read_end = s, e
        self._reveal_offset(s)
        self.statusBar().showMessage(
            f"🔊 朗读中 · 第{self.tts_chapter + 1}章 · {seq + 1}/{len(self.tts_sentences)}句")
        if self.bilingual_on:
            self._update_bilingual(seq)
            self._tr_kick(seq)
        data = self.tts_cache.pop(seq, None)
        if data is not None:
            self._tts_play_data(seq, data)
        else:
            self._tts_kick(seq)
        nxt = seq + 1
        if nxt < len(self.tts_sentences) and nxt not in self.tts_cache and nxt not in self.tts_inflight:
            self._tts_kick(nxt)
        if self.bilingual_on and nxt < len(self.tts_sentences):
            self._tr_kick(nxt)

    def _tts_kick(self, seq):
        if seq in self.tts_inflight or seq in self.tts_cache:
            return
        _, _, text = self.tts_sentences[seq]
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return
        self.tts_inflight.add(seq)
        epoch = self.tts_epoch
        w = TtsWorker(seq, clean,
                      self.cfg.get("tts_voice", TTS_DEFAULT_VOICE),
                      self.cfg.get("tts_rate", "+0%"))
        w.ready.connect(lambda s, d, ep=epoch: self._tts_on_ready(ep, s, d))
        w.failed.connect(lambda m, ep=epoch: self._tts_failed(ep, m))
        self.tts_workers[(epoch, seq)] = w  # 保持引用，防止队列信号前被 GC
        threading.Thread(target=w.run, daemon=True).start()

    def _tts_on_ready(self, epoch, seq, data):
        if not self.tts_on or epoch != self.tts_epoch:
            return
        self.tts_workers.pop((epoch, seq), None)
        self.tts_inflight.discard(seq)
        if seq == self.tts_idx and not self.tts_paused \
           and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._tts_play_data(seq, data)     # 当前句：直接播，不入缓存
        else:
            self.tts_cache[seq] = data         # 预取句：先缓存

    def _tts_play_data(self, seq, data):
        if not self.tts_on or seq != self.tts_idx:
            return
        if not data:
            self._tts_advance()
            return
        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self.tts_buf = buf
        self.player.setSourceDevice(buf)
        self.player.play()

    def _tts_advance(self):
        nxt = self.tts_idx + 1
        if nxt < len(self.tts_sentences):
            self._tts_start_sentence(nxt)
        else:
            self._tts_next_chapter()

    def _on_tts_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            # 延迟到下一轮事件循环，避免在媒体信号回调里重入 play() 导致卡死
            QTimer.singleShot(0, self._tts_advance)

    def _tts_failed(self, epoch, msg):
        if not self.tts_on or epoch != self.tts_epoch:
            return
        self.tts_inflight.clear(); self.tts_workers.clear()
        self._tts_stop()
        self.statusBar().showMessage("朗读失败：" + msg)
        _log_tts(f"[失败] {msg}")

    def _tts_stop(self, finished=False):
        was_on = self.tts_on
        self.tts_on = False; self.tts_paused = False
        self.tts_idx = 0
        self.tts_chapter = self.cur_chapter
        self.tts_epoch += 1
        self.tts_sentences = []
        self.tts_cache = {}; self.tts_inflight = set(); self.tts_workers = {}
        self.tr_cache = {}; self.tr_inflight = set(); self.tr_workers = {}
        if self.player:
            self.player.stop()
        self.tts_buf = None
        self.view.read_start = self.view.read_end = -1
        self.view.update()
        self._update_tts_actions()
        if finished:
            self.statusBar().showMessage("全书朗读结束")
        elif was_on:
            self.statusBar().showMessage("已停止朗读")

    def _update_tts_actions(self):
        if self.tts_on and not self.tts_paused:
            self.tts_act.setText("⏸ 暂停朗读"); self.stop_act.setEnabled(True)
        elif self.tts_on and self.tts_paused:
            self.tts_act.setText("▶ 继续朗读"); self.stop_act.setEnabled(True)
        else:
            self.tts_act.setText("🔊 朗读"); self.stop_act.setEnabled(False)

    def _reveal_offset(self, off):
        if not self.pager:
            return
        pages = self.view.pages
        vis = pages[self.view.spread * 2:self.view.spread * 2 + 2]
        if vis and vis[0].start <= off < vis[-1].end:
            self.view.update()
            return
        self.goto_offset(off)

    def _update_status(self):
        if self.pager and self.full_text:
            off = self.view.left_page_offset()
            pct = off / max(1, len(self.full_text)) * 100
            self.statusBar().showMessage(
                f"第 {self.cur_chapter + 1}/{len(self.pager.chapters)} 章 · {pct:.1f}%")
            self.read_slider.blockSignals(True)
            self.read_slider.setValue(int(pct * 10))
            self.read_slider.blockSignals(False)
            self.pos_label.setText(f"{pct:.1f}%")
            # 页眉/页码数据
            ch = self.pager.chapters[self.cur_chapter]
            self.view.chapter_title = ch.title
            pages = self.pager.pages_of(self.cur_chapter)
            self.view.chars_per_page = max(1, (ch.end - ch.start) / max(1, len(pages))) if pages else 400.0
            self.view.update()

    # ---- 全文搜索 ----
    def _find_all(self, query):
        if not query or not self.full_text:
            return []
        starts, pos, n = [], 0, len(self.full_text)
        while True:
            pos = self.full_text.find(query, pos)
            if pos == -1:
                break
            starts.append(pos)
            pos += 1
            if len(starts) >= 5000:      # 上限，防止巨量匹配拖慢 UI
                break
        return starts

    def _on_search_changed(self, text):
        self.search_query = text
        self.view.search_starts = []; self.view.search_len = 0; self.view.search_cur = -1
        if not text:
            self.search_matches = []; self.search_cur = -1
            self.view.update()
            return
        self.search_matches = self._find_all(text)
        self.search_cur = -1
        if self.search_matches:
            self._goto_search(0)
        else:
            self.view.update()
            self.statusBar().showMessage("搜索：无匹配")

    def _goto_search(self, idx):
        if not self.search_matches:
            return
        idx %= len(self.search_matches)
        self.search_cur = idx
        off = self.search_matches[idx]
        self.view.search_starts = self.search_matches
        self.view.search_len = len(self.search_query)
        self.view.search_cur = off
        self.goto_offset(off)
        self.statusBar().showMessage(f"搜索：第 {idx + 1}/{len(self.search_matches)} 个匹配")

    def _search_next(self):
        if self.search_matches:
            self._goto_search(self.search_cur + 1)

    def _search_prev(self):
        if self.search_matches:
            self._goto_search(self.search_cur - 1)

    # ---- 定时关闭（睡前听书）----
    def _build_sleep_menu(self):
        m = QMenu(self)
        for mins in (15, 30, 45, 60):
            m.addAction(f"{mins} 分钟后停止", lambda ms=mins: self._set_sleep(ms * 60))
        m.addSeparator()
        m.addAction("关闭定时", self._cancel_sleep)
        return m

    def _set_sleep(self, seconds):
        self.sleep_left = seconds
        self.sleep_timer.start()
        self.sleep_label.show()
        self._update_sleep_label()
        self.statusBar().showMessage(f"⏰ 已开启定时，{seconds // 60} 分钟后停止朗读")

    def _cancel_sleep(self):
        self.sleep_timer.stop()
        self.sleep_left = 0
        self.sleep_label.hide()
        self.sleep_label.setText("")

    def _on_sleep_tick(self):
        self.sleep_left -= 1
        if self.sleep_left <= 0:
            self._cancel_sleep()
            if self.tts_on:
                self.stop_reading()
            self.statusBar().showMessage("⏰ 定时时间到，已停止朗读")
        else:
            self._update_sleep_label()

    def _update_sleep_label(self):
        m, s = divmod(max(0, self.sleep_left), 60)
        self.sleep_label.setText(f"⏰ {m:02d}:{s:02d}")

    # ---- 最近打开 ----
    def _rebuild_recent_menu(self):
        self.recent_menu.clear()
        for p in self.cfg.get("recent", []):
            if os.path.exists(p):
                self.recent_menu.addAction(os.path.basename(p), lambda p=p: self.load_book(p))
        if self.recent_menu.isEmpty():
            self.recent_menu.addAction("（暂无）").setEnabled(False)

    def _add_recent(self, path):
        rec = list(self.cfg.get("recent", []))
        if path in rec:
            rec.remove(path)
        rec.insert(0, path)
        self.cfg["recent"] = rec[:8]
        save_json(CONFIG_PATH, self.cfg)
        self._rebuild_recent_menu()

    # ---- 进度条拖动跳转 ----
    def _on_slider_move(self, val):
        if not self.full_text:
            return
        pct = val / 1000 * 100
        self.pos_label.setText(f"{pct:.1f}%")
        ci = self.pager.chapter_index_at(int(val / 1000 * len(self.full_text)))
        self.statusBar().showMessage(
            f"跳转预览：第 {ci + 1}/{len(self.pager.chapters)} 章 ({pct:.1f}%)")

    def _on_slider_release(self):
        if not self.full_text:
            return
        off = int(self.read_slider.value() / 1000 * len(self.full_text))
        self.goto_offset(off)
        self._update_status()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_F and (e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.search_box.setFocus(); self.search_box.selectAll(); return
        if e.key() == Qt.Key.Key_F3:
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._search_prev()
            else:
                self._search_next()
            return
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.view.flip(-1)
        elif e.key() in (Qt.Key.Key_Right, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
            self.view.flip(1)
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.tts_on = False
        if self.player:
            self.player.stop()
        if self.book_path and self.pager:
            self.bookmarks.setdefault(self.book_path, {})["_progress_"] = self.view.left_page_offset()
            save_json(BOOKMARKS_PATH, self.bookmarks)
        super().closeEvent(e)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow(sys.argv[1] if len(sys.argv) > 1 else None)
    win.show()
    sys.exit(app.exec())
