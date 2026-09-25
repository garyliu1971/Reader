# reader.py —— 基于 PySide6 的文本小说阅读器（支持大文件惰性分页）
# 双击 start.bat 或命令行:  python reader.py 小说.txt
import sys, json, re, os, threading, bisect, urllib.request, asyncio, time, zipfile, posixpath, html, io, wave, math
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional
import numpy as np
from PIL import Image

from PySide6.QtCore import Qt, QRectF, QSizeF, QPointF, Signal, QObject, QTimer, QEvent, QBuffer, QByteArray, QIODevice, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QColor, QPen, QNativeGestureEvent, QBrush, QLinearGradient, QPixmap, QTransform, QRadialGradient, QPainterPath
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
FLIP_MS = 380            # 3D 翻页动画时长（毫秒）
FLIP_PERSPECTIVE = 5.0   # 翻页透视强度（相机距离 = 页宽 × 该系数，越小越夸张/文字越压缩）

# ============ 分页（只分"一章"的量，所以永远很快） ============
class Line:
    __slots__ = ("text", "start", "end", "indent", "gap_after",
                 "runs", "style", "marker", "height", "ascent", "map", "image")
    def __init__(self, text, start, end, indent=False, gap_after=0.0,
                 runs=None, style=None, marker=None, height=None, ascent=None, map=None,
                 image=None):
        self.text, self.start, self.end = text, start, end
        self.indent, self.gap_after = indent, gap_after
        self.runs, self.style, self.marker = runs, style, marker
        self.height, self.ascent, self.map = height, ascent, map
        self.image = image

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
            lines.append(Line(ln, off, off + len(ln),
                              indent=(indent_w if (use_indent and first) else 0.0)))
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

# ============ Markdown 渲染（接近专业 md 阅读器） ============
# 行内样式位
MD_BOLD = 1
MD_ITALIC = 2
MD_CODE = 4
MD_STRIKE = 8
MD_LINK = 16

# 标题字号缩放（相对正文字号）与段后间距（正文行高倍数）
MD_H_SCALE = {1: 1.9, 2: 1.55, 3: 1.3, 4: 1.12, 5: 1.0, 6: 0.92}
MD_H_GAP = {1: 0.9, 2: 0.7, 3: 0.55, 4: 0.45, 5: 0.35, 6: 0.3}
MD_LIST_INDENT = 24.0      # 每层列表缩进（像素）
MD_QUOTE_INDENT = 18.0     # 引用缩进
MD_BAR_W = 3.0             # 引用竖线宽度
MD_CODE_BG = "#f2f2ef"     # 代码块背景
MD_INLINE_CODE_BG = "#eef0f1"
MD_LINK_COLOR = "#0b6bcb"
MD_INLINE_CODE_COLOR = "#c7254e"
MD_QUOTE_COLOR = "#6a6a6a"
MD_BAR_COLOR = "#c9c9c9"
MD_HR_COLOR = "#d6d6d6"

# markdown 分页单元：以 H1 为章（无 H1 则整篇连续排版，更像 md 阅读器）
MD_CHAPTER_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*\r?$", re.MULTILINE)
# 目录用：所有 H1-H6 标题
MD_HEAD_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*\r?$", re.MULTILINE)

def md_chapters(text):
    chapters = [(m.start(), m.group(1).strip()) for m in MD_CHAPTER_RE.finditer(text)]
    if not chapters:
        return [(0, "正文")]
    if chapters[0][0] != 0:
        chapters.insert(0, (0, "开篇"))
    return chapters

def _md_mono_font(base: QFont) -> QFont:
    f = QFont(base)
    f.setFamilies(["Consolas", "Courier New", "Menlo", "DejaVu Sans Mono", "monospace"])
    f.setPointSizeF(base.pointSizeF() * 0.94)
    return f

def _md_style_font(base: QFont, bits: int) -> QFont:
    f = _md_mono_font(base) if (bits & MD_CODE) else QFont(base)
    if bits & MD_BOLD:
        f.setBold(True)
    if bits & MD_ITALIC:
        f.setItalic(True)
    return f

def _md_heading_font(base: QFont, level: int) -> QFont:
    f = QFont(base)
    f.setPointSizeF(base.pointSizeF() * MD_H_SCALE.get(level, 1.0))
    f.setBold(True)
    return f

def _fit_chars(fm: QFontMetricsF, text: str, max_w: float) -> int:
    if not text or max_w <= 0:
        return 0
    if fm.horizontalAdvance(text) <= max_w:
        return len(text)
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fm.horizontalAdvance(text[:mid]) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return max(lo, 1)

_INLINE_ESCAPE = set("\\`*_~[]()#!<>")

def parse_inline(s: str, base: int):
    """解析一行 markdown 内联文本 → 片段 [(显示文本, 样式位, 源起, 源止)]（绝对偏移）。"""
    runs = []

    def walk(t, off, add_bits):
        i, n, plain = 0, len(t), 0

        def flush(j):
            nonlocal plain
            if j > plain:
                runs.append((t[plain:j], add_bits, off + plain, off + j))
            plain = j

        while i < n:
            c = t[i]
            if c == "\\" and i + 1 < n and t[i + 1] in _INLINE_ESCAPE:
                flush(i)
                runs.append((t[i + 1], add_bits, off + i, off + i + 2))
                i += 2
                plain = i
                continue
            if c == "`":
                m = re.match(r"`+", t[i:])
                ticks = m.group(0)
                close = t.find(ticks, i + len(ticks))
                if close != -1:
                    flush(i)
                    runs.append((t[i + len(ticks):close], add_bits | MD_CODE,
                                 off + i + len(ticks), off + close))
                    i = close + len(ticks)
                    plain = i
                    continue
            if c == "!" and i + 1 < n and t[i + 1] == "[":
                j = t.find("]", i + 2)
                if j != -1 and j + 1 < n and t[j + 1] == "(":
                    k = t.find(")", j + 2)
                    if k != -1:
                        flush(i)
                        walk(t[i + 2:j], off + i + 2, add_bits | MD_LINK)
                        i = k + 1
                        plain = i
                        continue
            if c == "[":
                j = t.find("]", i + 1)
                if j != -1 and j + 1 < n and t[j + 1] == "(":
                    k = t.find(")", j + 2)
                    if k != -1:
                        flush(i)
                        walk(t[i + 1:j], off + i + 1, add_bits | MD_LINK)
                        i = k + 1
                        plain = i
                        continue
            if c in ("*", "_"):
                d2 = t[i:i + 2]
                delim = d2 if (len(d2) == 2 and d2[0] == d2[1]) else c
                close = t.find(delim, i + len(delim))
                if close != -1:
                    flush(i)
                    bits = MD_BOLD if len(delim) == 2 else MD_ITALIC
                    walk(t[i + len(delim):close], off + i + len(delim), add_bits | bits)
                    i = close + len(delim)
                    plain = i
                    continue
            if c == "~" and i + 1 < n and t[i + 1] == "~":
                close = t.find("~~", i + 2)
                if close != -1:
                    flush(i)
                    walk(t[i + 2:close], off + i + 2, add_bits | MD_STRIKE)
                    i = close + 2
                    plain = i
                    continue
            if c == "&":
                m = re.match(r"&[a-zA-Z][a-zA-Z0-9]*;|&#[0-9]+;", t[i:])
                if m:
                    flush(i)
                    runs.append((html.unescape(m.group(0)), add_bits, off + i, off + i + m.end()))
                    i += m.end()
                    plain = i
                    continue
            i += 1
        flush(n)

    walk(s, base, 0)
    return runs

def _wrap_pieces(pieces, max_w, base_font, word_wrap=True):
    """把片段按宽度折成若干显示行。每个片段=(文本,样式位,源起,源止)。

    word_wrap=True：英文按单词折行（不在单词中间断开），中文/无空格文本按字符折行。
    word_wrap=False：完全按字符折行（保留行首/行内空格，用于代码块）。"""
    if not word_wrap:
        lines, cur, cur_w = [], [], 0.0
        for (t, bits, s0, s1) in pieces:
            f = _md_style_font(base_font, bits)
            fm = QFontMetricsF(f)
            rest, off = t, 0
            while rest:
                if cur and cur_w + 0.5 >= max_w:
                    lines.append(cur)
                    cur, cur_w = [], 0.0
                k = _fit_chars(fm, rest, max_w - cur_w)
                if k <= 0:
                    k = 1
                seg = rest[:k]
                cur.append((seg, bits, s0 + off, s0 + off + k))
                cur_w += fm.horizontalAdvance(seg)
                rest, off = rest[k:], off + k
        if cur:
            lines.append(cur)
        return lines
    toks = []
    for (t, bits, s0, s1) in pieces:
        i, n = 0, len(t)
        while i < n:
            j = i
            if t[i] == " ":
                while j < n and t[j] == " ":
                    j += 1
            else:
                while j < n and t[j] != " ":
                    j += 1
            toks.append((t[i:j], bits, s0 + i, s0 + j, t[i] == " "))
            i = j
    lines, cur, cur_w = [], [], 0.0
    for (tk, bits, ts0, ts1, is_sp) in toks:
        f = _md_style_font(base_font, bits)
        fm = QFontMetricsF(f)
        if is_sp:
            if not cur:
                continue
            tw = fm.horizontalAdvance(tk)
            if cur_w + tw <= max_w:
                cur.append((tk, bits, ts0, ts1))
                cur_w += tw
            continue
        if cur and cur_w + fm.horizontalAdvance(tk) > max_w:
            lines.append(cur)
            cur, cur_w = [], 0.0
        rest, roff = tk, 0
        while rest:
            avail = max_w - cur_w
            if avail <= 0:
                lines.append(cur)
                cur, cur_w = [], 0.0
                avail = max_w
            if fm.horizontalAdvance(rest) <= avail:
                cur.append((rest, bits, ts0 + roff, ts1))
                cur_w += fm.horizontalAdvance(rest)
                break
            k = _fit_chars(fm, rest, avail)
            if k <= 0:
                k = 1
            seg = rest[:k]
            cur.append((seg, bits, ts0 + roff, ts0 + roff + k))
            cur_w += fm.horizontalAdvance(seg)
            rest = rest[k:]
            roff += k
            lines.append(cur)
            cur, cur_w = [], 0.0
    if cur:
        lines.append(cur)
    return lines

def _line_from_pieces(pieces, start, end, **kw):
    text = "".join(p[0] for p in pieces)
    runs, mp, d = [], [], 0
    for (t, bits, s0, s1) in pieces:
        runs.append((d, len(t), bits))
        mp.append((d, d + len(t), s0, s1))
        d += len(t)
    return Line(text, start, end, runs=runs, map=mp, **kw)

def _line_disp_to_src(ln: Line, d: int) -> int:
    if not ln.map:
        return ln.start + d
    for d0, d1, s0, s1 in ln.map:
        if d0 <= d < d1:
            return s0 + (d - d0)
    return ln.map[-1][3] if ln.map else ln.end

def _line_src_to_disp(ln: Line, s: int) -> int:
    if not ln.map:
        return s - ln.start
    last = 0
    for d0, d1, s0, s1 in ln.map:
        if s0 <= s < s1:
            return d0 + (s - s0)
        last = d1
    return last

def _pieces_from_lines(plines, base):
    pieces = []
    for idx, (t, s) in enumerate(plines):
        pieces.extend(parse_inline(t, base + s))
        if idx < len(plines) - 1:
            nl = s + len(t)
            pieces.append((" ", 0, base + nl, base + nl + 1))
    return pieces

def _md_split_lines(text):
    res, i, n = [], 0, len(text)
    while i < n:
        j = text.find("\n", i)
        if j == -1:
            j = n
        e = j
        if e > i and text[e - 1] == "\r":
            e -= 1
        res.append((text[i:e], i))
        if j == n:
            break
        i = j + 1
    return res

def _is_hr(lt):
    s = lt.strip()
    if s.startswith("|"):
        return False
    return bool(re.match(r"^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$", s))

def _is_list_item(lt):
    return re.match(r"^(\s*)([-*+]|\d+[.)])\s+", lt) is not None

def _is_table_sep(lt):
    s = lt.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return False
    cells = s.strip("|").split("|")
    return bool(cells) and all(re.match(r"^:?-{1,}:?$", c.strip()) for c in cells)

def _split_table_row(lt, ls):
    cells, i = [], 0
    while i < len(lt) and lt[i] in " \t":
        i += 1
    if i < len(lt) and lt[i] == "|":
        i += 1
    while True:
        j = lt.find("|", i)
        if j == -1:
            j = len(lt)
        raw = lt[i:j]
        lead = len(raw) - len(raw.lstrip(" \t"))
        cells.append((raw.strip(), ls + i + lead))
        if j == len(lt):
            break
        i = j + 1
    return cells

def _parse_aligns(lt):
    aligns = []
    for part in lt.strip().strip("|").split("|"):
        p = part.strip()
        if p.startswith(":") and p.endswith(":"):
            aligns.append("center")
        elif p.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns

def _md_blocks(text):
    lines = _md_split_lines(text)
    blocks, i, n = [], 0, len(lines)

    def line_end(k):
        if k >= n:
            return len(text)
        lt, ls = lines[k]
        return ls + len(lt) + (1 if ls + len(lt) < len(text) else 0)

    def is_block_start(lt, nxt):
        s = lt.strip()
        if re.match(r"^(#{1,6})[ \t]+", lt):
            return True
        if re.match(r"^(`{3,}|~{3,})", lt):
            return True
        if _is_hr(lt) or _is_list_item(lt) or lt.startswith(">"):
            return True
        if nxt is not None and s.startswith("|") and _is_table_sep(nxt):
            return True
        return False

    while i < n:
        lt, ls = lines[i]
        if not lt.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,6})[ \t]+(.*)$", lt)
        if m:
            content = re.sub(r"[ \t]+#+[ \t]*$", "", m.group(2)).strip()
            blocks.append({"type": "heading", "level": len(m.group(1)),
                           "content": content, "content_start": ls + m.start(2),
                           "start": ls, "end": line_end(i)})
            i += 1
            continue
        m = re.match(r"^(`{3,}|~{3,})[ \t]*(.*)$", lt)
        if m:
            fence_ch, fence_len = m.group(1)[0], len(m.group(1))
            lang = m.group(2).strip()
            j, body = i + 1, []
            while j < n:
                if re.match(r"^" + re.escape(fence_ch) + r"{%d,}[ \t]*$" % fence_len, lines[j][0].strip()):
                    break
                body.append(lines[j])
                j += 1
            blocks.append({"type": "code", "lang": lang, "start": ls,
                           "end": line_end(j) if j < n else len(text), "body": body})
            i = j + 1 if j < n else n
            continue
        if lt.strip().startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1][0]):
            header = _split_table_row(lt, ls)
            aligns = _parse_aligns(lines[i + 1][0])
            j, rows = i + 2, []
            while j < n and lines[j][0].strip().startswith("|"):
                rows.append(_split_table_row(lines[j][0], lines[j][1]))
                j += 1
            blocks.append({"type": "table", "header": header, "align": aligns,
                           "rows": rows, "start": ls, "end": line_end(j - 1)})
            i = j
            continue
        if _is_hr(lt):
            blocks.append({"type": "hr", "start": ls, "end": line_end(i)})
            i += 1
            continue
        if _is_list_item(lt):
            items = []
            while i < n:
                lt2, ls2 = lines[i]
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lt2)
                if m:
                    items.append({"marker": m.group(2), "depth": len(m.group(1)) // 2,
                                  "lines": [(m.group(3), ls2 + m.start(3))]})
                    i += 1
                    continue
                cm = re.match(r"^(\s{2,})(\S.*)$", lt2)
                if cm and items:
                    items[-1]["lines"].append((cm.group(2), ls2 + len(cm.group(1))))
                    i += 1
                    continue
                break
            blocks.append({"type": "list", "items": items, "start": ls,
                           "end": line_end(i - 1) if i > 0 else ls})
            continue
        if lt.startswith(">"):
            qlines = []
            while i < n and lines[i][0].startswith(">"):
                qt, qs = lines[i]
                depth, k = 0, 0
                while k < len(qt) and qt[k] == ">":
                    depth += 1
                    k += 1
                if k < len(qt) and qt[k] == " ":
                    k += 1
                qlines.append((qt[k:], qs + k, depth))
                i += 1
            blocks.append({"type": "quote", "lines": qlines, "depth": qlines[0][2],
                           "start": ls, "end": line_end(i - 1)})
            continue
        plines = []
        while i < n:
            lt2, ls2 = lines[i]
            if not lt2.strip() or is_block_start(lt2, lines[i + 1][0] if i + 1 < n else None):
                break
            plines.append((lt2, ls2))
            i += 1
        blocks.append({"type": "para", "lines": plines, "start": ls,
                       "end": line_end(i - 1) if i > 0 else ls})
    return blocks

def _render_heading(blk, base, font, max_w, line_spacing, line_h):
    level = blk["level"]
    pieces = parse_inline(blk["content"], base + blk["content_start"])
    hf = _md_heading_font(font, level)
    hfm = QFontMetricsF(hf)
    hh = hfm.height() * line_spacing * (1.15 if level <= 2 else 1.0)
    asc = hfm.ascent()
    wrapped = _wrap_pieces(pieces, max_w, hf)
    lines = [_line_from_pieces(w, (w[0][2] if w else base + blk["content_start"]),
                               (w[-1][3] if w else base + blk["content_start"]),
                               style="h%d" % level, height=hh, ascent=asc, indent=0.0)
             for w in wrapped]
    if lines:
        lines[-1].gap_after = MD_H_GAP.get(level, 0.4) * line_h
    return lines

def _render_para(blk, base, font, max_w, line_spacing, line_h, para_gap):
    pieces = _pieces_from_lines(blk["lines"], base)
    fm = QFontMetricsF(font)
    asc = fm.ascent()
    wrapped = _wrap_pieces(pieces, max_w, font)
    lines = [_line_from_pieces(w, (w[0][2] if w else base + blk["start"]),
                               (w[-1][3] if w else base + blk["start"]),
                               indent=0.0, height=line_h, ascent=asc)
             for w in wrapped]
    if lines:
        lines[-1].gap_after = para_gap
    return lines

def _render_quote(blk, base, font, max_w, line_spacing, line_h, para_gap):
    indent = MD_QUOTE_INDENT * blk["depth"]
    plines = [(t, s) for (t, s, _d) in blk["lines"]]
    pieces = _pieces_from_lines(plines, base)
    fm = QFontMetricsF(font)
    asc = fm.ascent()
    wrapped = _wrap_pieces(pieces, max_w - indent, font)
    lines = [_line_from_pieces(w, (w[0][2] if w else base + blk["start"]),
                               (w[-1][3] if w else base + blk["start"]),
                               indent=indent, height=line_h, ascent=asc, style="quote")
             for w in wrapped]
    if lines:
        lines[-1].gap_after = para_gap
    return lines

def _render_list(blk, base, font, max_w, line_spacing, line_h, para_gap):
    fm = QFontMetricsF(font)
    asc = fm.ascent()
    lines = []
    for item in blk["items"]:
        marker = item["marker"]
        content_indent = item["depth"] * MD_LIST_INDENT + fm.horizontalAdvance(marker) + 8.0
        pieces = _pieces_from_lines(item["lines"], base)
        wrapped = _wrap_pieces(pieces, max_w - content_indent, font)
        before = len(lines)
        for k, w in enumerate(wrapped):
            lines.append(_line_from_pieces(
                w, (w[0][2] if w else base + item["lines"][0][1]),
                (w[-1][3] if w else base + item["lines"][0][1]),
                indent=content_indent, marker=(marker if k == 0 else None),
                height=line_h, ascent=asc))
        if len(lines) > before:
            lines[-1].gap_after = para_gap * 0.4
    if lines:
        lines[-1].gap_after = para_gap
    return lines

def _render_code(blk, base, font, max_w, line_spacing, line_h, para_gap):
    cf = _md_mono_font(font)
    cfm = QFontMetricsF(cf)
    asc = cfm.ascent()
    hh = cfm.height() * line_spacing
    lines = []
    for (t, s) in blk["body"]:
        if t == "":
            lines.append(Line("", base + s, base + s, indent=0.0, style="code",
                              height=hh, ascent=asc))
            continue
        wrapped = _wrap_pieces([(t, MD_CODE, base + s, base + s + len(t))], max_w, font,
                               word_wrap=False)
        for w in wrapped:
            lines.append(_line_from_pieces(w, base + s, base + s + len(t),
                                           indent=0.0, style="code", height=hh, ascent=asc))
    if not lines:
        lines.append(Line("", base + blk["start"], base + blk["end"], indent=0.0,
                          style="code", height=hh, ascent=asc))
    lines[-1].gap_after = para_gap
    return lines

def _shrink_widths(widths, avail, min_w):
    ws = list(widths)
    for _ in range(24):
        total = sum(ws)
        if total <= avail:
            break
        shrinkable = [(i, ws[i] - min_w) for i in range(len(ws)) if ws[i] > min_w]
        cap = sum(x for _, x in shrinkable)
        if cap <= 0:
            break
        take = min(total - avail, cap)
        for i, room in shrinkable:
            ws[i] -= take * (room / cap)
    return ws

def _table_lines(hprep, rprep, aligns, base, blk_start, blk_end, font, max_w, line_h, para_gap):
    """通用表格排版：hprep/rprep 为 [(显示文本, 片段, 源偏移), ...]（源偏移为局部，加 base）。
    列宽超页宽时自动收缩，单元格自动换行；markdown 与 PDF 表格共用。"""
    fm = QFontMetricsF(font)
    asc = fm.ascent()
    space_w = max(1.0, fm.horizontalAdvance(" "))
    sep = fm.horizontalAdvance("  ")
    ncol = max([len(hprep)] + [len(r) for r in rprep] + [len(aligns)])
    aligns = (list(aligns) + ["left"] * ncol)[:ncol]
    widths = [0.0] * ncol
    for row in [hprep] + rprep:
        for j, (disp, _p, _s) in enumerate(row):
            widths[j] = max(widths[j], fm.horizontalAdvance(disp))
    avail = max_w - sep * max(0, ncol - 1)
    min_w = fm.horizontalAdvance("中") * 2.0
    if avail > 0 and sum(widths) > avail:
        widths = _shrink_widths(widths, avail, min_w)

    def row_lines(prep_row, bold):
        wrapped = []
        for j in range(ncol):
            _disp, pp, _s = prep_row[j] if j < len(prep_row) else ("", [], 0)
            wrapped.append(_wrap_pieces(pp, max(widths[j], 1.0), font) if pp else [[]])
        nlines = max(len(c) for c in wrapped)
        out, row_s0, row_s1 = [], None, None
        for k in range(nlines):
            pieces = []
            for j in range(ncol):
                cl = wrapped[j]
                wl = cl[k] if k < len(cl) else []
                disp = "".join(p[0] for p in wl)
                tw = fm.horizontalAdvance(disp)
                w = widths[j]
                a = aligns[j]
                left = (w - tw) if a == "right" else ((w - tw) / 2 if a == "center" else 0.0)
                left = max(0.0, left)
                right = max(0.0, w - tw - left)
                _d, _pp, s = prep_row[j] if j < len(prep_row) else ("", [], 0)
                cs = wl[0][2] if wl else base + s
                ce = wl[-1][3] if wl else base + s
                if wl:
                    if row_s0 is None:
                        row_s0 = cs
                    row_s1 = ce
                if left > 0:
                    pieces.append((" " * max(1, int(round(left / space_w))), 0, cs, cs))
                for (dt, bits, ss0, ss1) in wl:
                    pieces.append((dt, (bits | MD_BOLD) if bold else bits, ss0, ss1))
                if right > 0:
                    pieces.append((" " * max(1, int(round(right / space_w))), 0, ce, ce))
                if j < ncol - 1:
                    pieces.append(("  ", 0, ce, ce))
            out.append(_line_from_pieces(pieces,
                                         row_s0 if row_s0 is not None else base + blk_start,
                                         row_s1 if row_s1 is not None else base + blk_start,
                                         indent=0.0, height=line_h, ascent=asc))
        return out

    lines = row_lines(hprep, bold=True)
    lines.append(Line("", base + blk_start, base + blk_end, indent=0.0,
                      style="hr", height=line_h * 0.5, ascent=asc))
    for r in rprep:
        lines.extend(row_lines(r, bold=False))
    lines[-1].gap_after = para_gap
    return lines

def _render_table(blk, base, font, max_w, line_spacing, line_h, para_gap):
    def prep(cells):
        out = []
        for (t, s) in cells:
            pieces = parse_inline(t, base + s)
            out.append(("".join(p[0] for p in pieces), pieces, s))
        return out

    hprep = prep(blk["header"])
    rprep = [prep(r) for r in blk["rows"]]
    return _table_lines(hprep, rprep, blk["align"], base, blk["start"], blk["end"],
                        font, max_w, line_h, para_gap)

def render_md_lines(text, base_offset, font, max_w, line_spacing, para_spacing, line_h):
    """把一段 markdown 文本渲染成带样式与源偏移映射的 Line 列表（绝对偏移）。"""
    fm = QFontMetricsF(font)
    para_gap = fm.height() * para_spacing
    out = []
    for blk in _md_blocks(text):
        t = blk["type"]
        if t == "heading":
            out.extend(_render_heading(blk, base_offset, font, max_w, line_spacing, line_h))
        elif t == "code":
            out.extend(_render_code(blk, base_offset, font, max_w, line_spacing, line_h, para_gap))
        elif t == "table":
            out.extend(_render_table(blk, base_offset, font, max_w, line_spacing, line_h, para_gap))
        elif t == "list":
            out.extend(_render_list(blk, base_offset, font, max_w, line_spacing, line_h, para_gap))
        elif t == "quote":
            out.extend(_render_quote(blk, base_offset, font, max_w, line_spacing, line_h, para_gap))
        elif t == "hr":
            out.append(Line("", base_offset + blk["start"], base_offset + blk["end"],
                            indent=0.0, style="hr", height=line_h * 0.5,
                            ascent=fm.ascent(), gap_after=para_gap))
        elif t == "para":
            out.extend(_render_para(blk, base_offset, font, max_w, line_spacing, line_h, para_gap))
    return out

def _pack_lines(lines, usable_h, default_line_h):
    pages, cur, cur_h = [], [], 0.0
    for ln in lines:
        lh = ln.height if ln.height else default_line_h
        if cur and cur_h + lh > usable_h:
            pages.append(Page(len(pages), cur[0].start, cur[-1].end, cur))
            cur, cur_h = [], 0.0
        cur.append(ln)
        cur_h += lh + ln.gap_after
    if cur:
        pages.append(Page(len(pages), cur[0].start, cur[-1].end, cur))
    elif not pages:
        pages.append(Page(0, 0, 0, []))
    return pages

# ============ 章节扫描（一次 C 级正则扫描） ============
CHAPTER_RE = re.compile(
    r"^\s*(?:第[零一二三四五六七八九十百千万0-9]{1,10}[章节回卷集部篇][^\n]{0,40}|"
    r"(?:序章|楔子|引子|终章|尾声|番外[^\n]{0,30})|#{1,6}[ \t]+[^\n]+)\s*$",
    re.MULTILINE)

def scan_chapters(text: str) -> List[Tuple[int, str]]:
    chapters = []
    for m in CHAPTER_RE.finditer(text):
        title = re.sub(r"^#{1,6}[ \t]+", "", m.group(0).strip())
        chapters.append((m.start(), title))
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
    def __init__(self, text: str, chapters: List[Tuple[int, str]], md: bool = False, pdf_blocks=None, scan_pages=None):
        self.text = text
        self.md = md
        self.pdf_blocks = pdf_blocks
        self.scan_pages = scan_pages
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
            if self.scan_pages is not None:
                # 扫描版：一章 = 一页，直接构造含整页位图的 Page
                spec = self.scan_pages[c.start]
                max_w = p["page_size"].width() - 2 * p["margin_x"]
                max_h = p["page_size"].height() - 2 * p["margin_y"]
                iw, ih = spec["w"], spec["h"]
                scale = min(max_w / max(1.0, iw), max_h / max(1.0, ih)) if iw and ih else 1.0
                scale = min(scale, 1.0)   # 不放大超过原始分辨率，避免模糊
                dw, dh = iw * scale, ih * scale
                ln = Line("", c.start, c.end, indent=0.0, style="image",
                          height=dh, ascent=0.0, image=(spec["img_id"], dw, dh))
                pages = [Page(0, c.start, c.end, [ln])]
            elif self.pdf_blocks is not None:
                max_w = p["page_size"].width() - 2 * p["margin_x"]
                usable_h = p["page_size"].height() - 2 * p["margin_y"]
                line_h = QFontMetricsF(p["font"]).height() * p["line_spacing"]
                bs = [b for b in self.pdf_blocks if c.start <= b.get("start", 0) < c.end]
                lines = render_pdf_lines(bs, p["font"], max_w, p["line_spacing"],
                                         p.get("para_spacing", PARA_SPACING), line_h,
                                         max_h=usable_h * 0.92)
                pages = _pack_lines(lines, usable_h, line_h)
            elif self.md:
                max_w = p["page_size"].width() - 2 * p["margin_x"]
                line_h = QFontMetricsF(p["font"]).height() * p["line_spacing"]
                lines = render_md_lines(self.text[c.start:c.end], c.start, p["font"],
                                        max_w, p["line_spacing"],
                                        p.get("para_spacing", PARA_SPACING), line_h)
                pages = _pack_lines(lines, p["page_size"].height() - 2 * p["margin_y"], line_h)
            else:
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

MD_EXTS = (".md", ".markdown")
_MD_ATX_TITLE_RE = re.compile(r"^\s{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MD_SETEXT_TITLE_RE = re.compile(r"^([^\n]+)\n(=+)\s*$", re.MULTILINE)

def md_title(text: str) -> str:
    """从 markdown 提取书名：优先首个 ATX 标题，其次 setext(=) 标题。"""
    m = _MD_ATX_TITLE_RE.match(next((ln for ln in text.splitlines() if ln.strip()), ""))
    if m:
        return m.group(1).strip()
    m = _MD_SETEXT_TITLE_RE.search(text)
    return m.group(1).strip() if m else ""

def load_markdown(path):
    """Markdown 文件 → (原始正文, 章节, 书名)。

    正文保持原始 markdown 不变，交给 LazyPager(md=True) 做富文本渲染
    （标题分级 / 粗斜体 / 行内代码 / 代码块 / 列表 / 引用 / 表格 /
    分隔线 / 链接）；这里只额外提取书名。"""
    with open(path, "rb") as f:
        raw = f.read()
    text = decode_text(raw)
    chapters = scan_chapters(text)
    return text, chapters, md_title(text)

# ============ PDF 版式解析（保留标题/粗斜体/段落/列表/代码/表格） ============
_PDF_MONO_HINTS = ("mono", "courier", "consola", "typewriter", "menlo", "code", "fixed")
_PDF_BULLET_RE = re.compile(r"^\s*([\u2022\u00b7\u25aa\u25e6\u25cf\u25cb\u25c6\u25a0\u25a1\u203b\u2023\u2219]|[-\u2013\u2014*])\s+")
_PDF_NUMBER_RE = re.compile(r"^\s*(\d{1,3}|[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e]{1,3})[\.\u3001)\uff09]\s*")
_PDF_PAGENO_RE = re.compile(r"^[\s\-\u2013\u2014\u00b7\.\u00b7|]*([ivxlcdmIVXLCDM]{1,7}|\d{1,4})[\s\-\u2013\u2014\u00b7\.\u00b7|]*$")
_PDF_SECTION_RE = re.compile(r"^\s*[\u25a0\u25aa\u25ae\u25cf\u25c6\u25b6\u25b8\u25ba]\S")   # ■INTRODUCTION 之类节标题
_PDF_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0x3000, 0x303F),
                   (0x3040, 0x30FF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))

def _is_cjk(ch):
    if not ch:
        return False
    o = ord(ch)
    return any(a <= o <= b for a, b in _PDF_CJK_RANGES)

def _pdf_span_bits(font, flags):
    fn = (font or "").lower()
    fl = int(flags or 0)
    bits = 0
    if (fl & 16) or any(k in fn for k in ("bold", "semibold", "-bd", "black", "heavy")):
        bits |= MD_BOLD
    if (fl & 2) or any(k in fn for k in ("italic", "oblique", "-it")):
        bits |= MD_ITALIC
    if (fl & 8) or any(k in fn for k in _PDF_MONO_HINTS):
        bits |= MD_CODE
    return bits

def _pdf_line_text(ln):
    return "".join(sp[0] for sp in ln["spans"])

def _pdf_line_spans(ln):
    return [(t.replace("\xad", ""), _pdf_span_bits(f, fl))
            for (t, _sz, f, fl, _bb) in ln["spans"] if t]

def _pdf_line_size(ln):
    best, bn = 0.0, 0
    for (t, sz, _f, _fl, _bb) in ln["spans"]:
        n = len(t.strip())
        if n > bn:
            bn, best = n, sz
    return best or (ln["spans"][0][1] if ln["spans"] else 0.0)

def _pdf_line_mono(ln):
    m = a = 0
    for (t, _sz, f, fl, _bb) in ln["spans"]:
        n = len(t.strip())
        if not n:
            continue
        a += n
        if _pdf_span_bits(f, fl) & MD_CODE:
            m += n
    return a > 0 and m >= a * 0.6

def _pdf_drop_prefix(spans, n):
    out = []
    for (t, bits) in spans:
        if n >= len(t):
            n -= len(t)
            continue
        out.append((t[n:], bits))
        n = 0
    return out

_PDF_TABLE_BUDGET = 6.0   # 表格识别的总时间预算（秒），避免大部头 PDF 打开过慢
_PDF_IMG_BUDGET = 12.0    # 插图提取总时间预算（秒）
_PDF_IMG_MAX = 400        # 最多提取的插图数量
_PDF_IMG_MIN = 24         # 最小边长（点），小于视为图标/线条
_PDF_IMG_MAX_AREA = 0.85  # 超过页面面积此比例的图（整页背景/扫描）不提取
_PDF_IMG_ZOOM_MAX = 2.0
_PDF_IMG_TARGET_W = 1400  # 插图渲染目标宽度（像素）
_PDF_VEC_MIN_ITEMS = 2    # 矢量插图最少绘图元素数（过滤单条线/单个框）
_PDF_SCAN_TARGET_W = 1400 # 扫描版整页渲染目标宽度（像素）
_PDF_SCAN_ZOOM_MAX = 2.5
_PDF_SCAN_JPEG_Q = 80     # 扫描版整页 JPEG 质量（灰度页，体积小）

def _pdf_rects_intersect(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def _pdf_merge_boxes(boxes, gap=6.0):
    """把重叠/相邻的矢量绘图框聚成簇，返回 [(x0,y0,x1,y1,weight)]。"""
    n = len(boxes)
    if n <= 1:
        return [(b[0], b[1], b[2], b[3], b[4]) for b in boxes]
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for i in range(n):
        x0, y0, x1, y1, _ = boxes[i]
        gx0, gy0, gx1, gy1 = x0 - gap, y0 - gap, x1 + gap, y1 + gap
        for j in range(i + 1, n):
            jx0, jy0, jx1, jy1, _ = boxes[j]
            if gx0 < jx1 and jx0 < gx1 and gy0 < jy1 and jy0 < gy1:
                union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(boxes[i])
    out = []
    for grp in groups.values():
        x0 = min(b[0] for b in grp); y0 = min(b[1] for b in grp)
        x1 = max(b[2] for b in grp); y1 = max(b[3] for b in grp)
        wsum = sum(b[4] for b in grp)
        out.append((x0, y0, x1, y1, wsum))
    return out

def _pdf_vector_figures(draws, pw, ph, page_area, table_rects, image_rects):
    """从矢量绘图里挑出像“插图/示意图”的簇（过滤表格线、页眉页脚线、边框等）。
    返回 [(x0,y0,x1,y1)...]，其中的文字随图一起渲染，正文里会跳过。"""
    if not draws or len(draws) > 1500:
        return []
    boxes = []
    for d in draws:
        r = d.get("rect")
        if not r:
            continue
        x0, y0, x1, y1 = [float(v) for v in r]
        if x1 <= x0 and y1 <= y0:      # 只跳过“点”，保留零厚度的连接线
            continue
        w, h = x1 - x0, y1 - y0
        # 高亮/下划线等“纯填充细长条”：跟随文字行，不是图
        if d.get("type") == "f" and min(w, h) < 14.0 and max(w, h) > min(w, h) * 3.0:
            continue
        if any(_pdf_rects_intersect((x0, y0, x1, y1), tb) for tb in table_rects):
            continue
        weight = max(1, len(d.get("items", []) or []))
        boxes.append((x0, y0, x1, y1, weight))
    if not boxes:
        return []
    out = []
    for (x0, y0, x1, y1, wsum) in _pdf_merge_boxes(boxes):
        w, h = x1 - x0, y1 - y0
        if w < _PDF_IMG_MIN or h < _PDF_IMG_MIN:
            continue
        if w * h > page_area * _PDF_IMG_MAX_AREA:
            continue
        if wsum < _PDF_VEC_MIN_ITEMS:      # 单条线/单个框基本不是图
            continue
        if min(w, h) < 6.0:                # 过扁/过窄的线状簇（分隔线、下划线）
            continue
        if h < 12.0 and w > pw * 0.7:      # 通栏横线
            continue
        if w < 12.0 and h > ph * 0.7:      # 通栏竖线
            continue
        if any(_pdf_rects_intersect((x0, y0, x1, y1), ir) for ir in image_rects):
            continue                        # 已被位图插图覆盖
        out.append((x0, y0, x1, y1))
    return out

def _pdf_in_fig(bbox, fig_rects):
    """判断文本行是否位于矢量插图内部（其中心点落在插图框内）。"""
    if not fig_rects:
        return False
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return any(fx0 <= cx <= fx2 and fy0 <= cy <= fy3 for (fx0, fy0, fx2, fy3) in fig_rects)

def _pdf_page_is_grayscale(page):
    """判断扫描页是否灰度（无 RGB/CMYK 图）。"""
    try:
        for info in page.get_image_info(xrefs=True):
            if info.get("colorspace") in (3, 4):   # RGB / CMYK
                return False
    except Exception:
        pass
    return True

def _pdf_render_scan_pages(path, n_pages, progress=None):
    """扫描版（无文字层）：把每页整页渲染成 JPEG，返回 (pages_spec, images)。"""
    import pymupdf
    doc = pymupdf.open(path)
    try:
        pages, images = [], {}
        for pno in range(n_pages):
            if progress and (pno % 10 == 0 or pno == n_pages - 1):
                progress(15 + int(80 * pno / max(1, n_pages)),
                         "渲染扫描页… %d/%d 页" % (pno + 1, n_pages))
            page = doc[pno]
            pw = max(1.0, float(page.rect.width))
            zoom = max(1.0, min(_PDF_SCAN_ZOOM_MAX, _PDF_SCAN_TARGET_W / pw))
            cs = pymupdf.csGRAY if _pdf_page_is_grayscale(page) else pymupdf.csRGB
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=cs, alpha=False)
            images[pno] = pix.tobytes("jpeg", jpg_quality=_PDF_SCAN_JPEG_Q)
            pages.append({"img_id": pno, "w": pix.width, "h": pix.height})
        return pages, images
    finally:
        doc.close()

def _pdf_extract(path, progress=None):
    import warnings
    import pymupdf
    warnings.filterwarnings("ignore", message=".*pymupdf_layout.*")
    try:
        pymupdf.set_messages(stream=io.StringIO())   # 静默 pymupdf 的提示信息
        pymupdf.no_recommend_layout()                # 不再推荐安装 pymupdf_layout
    except Exception:
        pass
    doc = pymupdf.open(path)
    try:
        title = ((doc.metadata or {}).get("title") or "").strip()
        try:
            toc = doc.get_toc() or []
        except Exception:
            toc = []
        pages = []
        images = {}
        img_seq = 0
        n = len(doc)
        tbl_deadline = time.time() + _PDF_TABLE_BUDGET
        img_deadline = time.time() + _PDF_IMG_BUDGET
        for pno in range(n):
            if progress and (pno % 20 == 0 or pno == n - 1):
                progress(15 + int(55 * pno / max(1, n)), "解析 PDF 版式… %d/%d 页" % (pno + 1, n))
            page = doc[pno]
            raw = page.get_text("dict")
            lines = []
            for b in raw.get("blocks", []):
                if b.get("type") != 0:
                    continue
                for ln in b.get("lines", []):
                    spans = []
                    for s in ln.get("spans", []):
                        t = s.get("text", "")
                        if not t:
                            continue
                        spans.append((t, round(float(s.get("size", 0.0)), 1),
                                      s.get("font", ""), int(s.get("flags", 0)),
                                      tuple(s.get("bbox", (0, 0, 0, 0)))))
                    if spans and any(t.strip() for (t, *_r) in spans):
                        lines.append({"bbox": tuple(ln.get("bbox", (0, 0, 0, 0))), "spans": spans})
            try:
                draws = page.get_drawings()
            except Exception:
                draws = []
            tables = []
            # 表格识别很贵：仅当页面有较多线条、且在时间预算内才做
            if time.time() < tbl_deadline and len(draws) >= 12:
                try:
                    for t in page.find_tables().tables:
                        data = t.extract()
                        if data and any(any((c or "").strip() for c in row) for row in data):
                            tables.append((tuple(t.bbox), [[(c or "").strip() for c in row] for row in data]))
                except Exception:
                    pass
            table_rects = [bb for bb, _rows in tables]
            # 插图：先位图、再矢量图（按位置框渲染成 PNG）
            page_imgs = []
            image_rects = []
            if time.time() < img_deadline and img_seq < _PDF_IMG_MAX:
                try:
                    pw, ph = float(page.rect.width), float(page.rect.height)
                    page_area = max(1.0, pw * ph)
                    for info in page.get_image_info(xrefs=True):
                        bb = info.get("bbox")
                        if not bb:
                            continue
                        x0, y0, x1, y1 = [float(v) for v in bb]
                        w, h = x1 - x0, y1 - y0
                        if w < _PDF_IMG_MIN or h < _PDF_IMG_MIN:
                            continue
                        if w * h > page_area * _PDF_IMG_MAX_AREA:
                            continue
                        zoom = max(1.0, min(_PDF_IMG_ZOOM_MAX, _PDF_IMG_TARGET_W / max(1.0, w)))
                        pix = page.get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1),
                                              matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                        if pix.width < 8 or pix.height < 8:
                            continue
                        images[img_seq] = pix.tobytes("png")
                        page_imgs.append((y0, img_seq, pix.width, pix.height))
                        image_rects.append((x0, y0, x1, y1))
                        img_seq += 1
                        if img_seq >= _PDF_IMG_MAX or time.time() >= img_deadline:
                            break
                except Exception:
                    pass
            fig_rects = []
            if time.time() < img_deadline and img_seq < _PDF_IMG_MAX:
                try:
                    pw, ph = float(page.rect.width), float(page.rect.height)
                    page_area = max(1.0, pw * ph)
                    for (x0, y0, x1, y1) in _pdf_vector_figures(draws, pw, ph, page_area,
                                                                 table_rects, image_rects):
                        w, h = x1 - x0, y1 - y0
                        zoom = max(1.0, min(_PDF_IMG_ZOOM_MAX, _PDF_IMG_TARGET_W / max(1.0, w)))
                        pix = page.get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1),
                                              matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                        if pix.width < 8 or pix.height < 8:
                            continue
                        images[img_seq] = pix.tobytes("png")
                        page_imgs.append((y0, img_seq, pix.width, pix.height))
                        fig_rects.append((x0, y0, x1, y1))
                        img_seq += 1
                        if img_seq >= _PDF_IMG_MAX or time.time() >= img_deadline:
                            break
                except Exception:
                    pass
            pages.append({"lines": lines, "tables": tables, "images": page_imgs,
                          "fig_rects": fig_rects, "height": float(page.rect.height)})
    finally:
        doc.close()
    return title, toc, pages, images, any(pg["lines"] for pg in pages)

def _pdf_body_size(pages):
    hist = {}
    for pg in pages:
        for ln in pg["lines"]:
            for (t, sz, _f, _fl, _bb) in ln["spans"]:
                w = len(t.strip())
                if w:
                    hist[sz] = hist.get(sz, 0) + w
    return max(hist.items(), key=lambda kv: kv[1])[0] if hist else 12.0

def _pdf_running_keys(pages):
    from collections import Counter
    cnt = Counter()
    for pg in pages:
        seen = set()
        for ln in pg["lines"]:
            t = _pdf_line_text(ln).strip()
            if not t or len(t) > 70:
                continue
            k = re.sub(r"\d+", "#", t)
            if k not in seen:
                seen.add(k)
                cnt[k] += 1
    thr = max(3, int(len(pages) * 0.3))
    return {k for k, v in cnt.items() if v >= thr}

def _pdf_head_level(sz, body):
    r = sz / max(1.0, body)
    if r >= 1.85:
        return 1
    if r >= 1.42:
        return 2
    if r >= 1.26:
        return 3
    if r >= 1.14:
        return 4
    if r >= 1.07:
        return 5
    return 0

def _pdf_build(pages, body, running):
    blocks = []
    para, prev = None, None
    for pi, pg in enumerate(pages):
        ph = pg.get("height", 0.0)
        elems = []
        tables = [{"bbox": bb, "rows": data, "inserted": False} for bb, data in pg.get("tables", [])]
        for ln in pg["lines"]:
            txt = _pdf_line_text(ln).strip()
            if not txt:
                continue
            if re.sub(r"\d+", "#", txt) in running:
                continue
            y0 = ln["bbox"][1]
            if _PDF_PAGENO_RE.match(txt) and ph and (y0 < ph * 0.08 or y0 > ph * 0.9):
                continue
            x0, y0, x1, y1 = ln["bbox"]
            if _pdf_in_fig((x0, y0, x1, y1), pg.get("fig_rects", [])):
                continue   # 矢量插图内部的文字，随图一起渲染，避免重复
            table = next((item for item in tables
                          if x1 > item["bbox"][0] and x0 < item["bbox"][2]
                          and item["bbox"][1] <= (y0 + y1) / 2 <= item["bbox"][3]), None)
            if table is not None:
                if not table["inserted"]:
                    elems.append(("table", y0, table["rows"]))
                    table["inserted"] = True
                continue
            elems.append(("line", y0, ln))
        for table in tables:
            if not table["inserted"]:
                elems.append(("table", table["bbox"][1], table["rows"]))
        for (iy, iid, iw, ih) in pg.get("images", []):
            elems.append(("image", iy, (iid, iw, ih)))
        elems.sort(key=lambda e: e[1])
        # 页内正文左边距与行距中位数（用于段落切分）
        body_xs, body_x1s, gaps, prev_bottom = [], [], [], None
        for (k, _y, ln) in elems:
            if k != "line":
                continue
            if _pdf_head_level(_pdf_line_size(ln), body) == 0 and not _pdf_line_mono(ln):
                body_xs.append(ln["bbox"][0])
                body_x1s.append(ln["bbox"][2])
            if prev_bottom is not None:
                gg = ln["bbox"][1] - prev_bottom
                if 0 <= gg < body * 3:
                    gaps.append(gg)
            prev_bottom = ln["bbox"][3]
        left = min(body_xs) if body_xs else 0.0
        gaps.sort()
        med_gap = gaps[len(gaps) // 2] if gaps else body * 0.2
        para_thr = max(med_gap * 1.8, body * 0.5)
        indent_x = left + max(6.0, body * 1.1)
        right = max(body_x1s) if body_x1s else 0.0

        for (k, _y, obj) in elems:
            if k == "image":
                if para is not None and para["spans"]:
                    blocks.append(para)
                para, prev = None, None
                iid, iw, ih = obj
                blocks.append({"type": "image", "img_id": iid, "img_w": iw,
                               "img_h": ih, "page": pi})
                continue
            if k == "table":
                if para is not None and para["spans"]:
                    blocks.append(para)
                para, prev = None, None
                rows = [[c for c in r] for r in obj]
                blocks.append({"type": "table", "page": pi, "rows": rows,
                               "align": ["left"] * max((len(r) for r in rows), default=1)})
                continue
            ln = obj
            ltxt = _pdf_line_text(ln)
            stripped = ltxt.strip()
            if not stripped:
                continue
            soft_break = stripped.endswith("\xad")
            spans = _pdf_line_spans(ln)
            lsz = _pdf_line_size(ln)
            bits_line = 0
            for (_t, _b) in spans:
                bits_line |= _b
            mono = _pdf_line_mono(ln)
            gap = (ln["bbox"][1] - prev["bottom"]) if prev and prev.get("page") == pi else None
            x0 = ln["bbox"][0]
            full = bool(right) and ln["bbox"][2] >= right - body * 1.5
            lev = _pdf_head_level(lsz, body)
            if not lev and (bits_line & MD_BOLD) and len(stripped) <= 64 \
               and gap is not None and gap > body * 0.8:
                lev = 3
            sect = bool(_PDF_SECTION_RE.match(ltxt) and len(stripped) <= 80)
            if sect and not lev:
                lev = 3   # ■INTRODUCTION / ■CONCLUSION 之类节标题（标记直接黏在文字上）
            m_bullet = _PDF_BULLET_RE.match(ltxt)
            m_num = None if m_bullet else _PDF_NUMBER_RE.match(ltxt)
            if lev:
                if para is not None and para["spans"]:
                    blocks.append(para)
                blocks.append({"type": "heading", "level": lev, "spans": spans, "page": pi, "sect": sect})
                para, prev = None, {"text": stripped, "bottom": ln["bbox"][3], "page": pi, "full": full}
                continue
            if m_bullet or m_num:
                if para is not None and para["spans"]:
                    blocks.append(para)
                mm = m_num or m_bullet
                blocks.append({"type": "list", "marker": mm.group(0).strip(),
                               "depth": 0 if x0 < indent_x else 1,
                               "spans": _pdf_drop_prefix(spans, mm.end()), "page": pi})
                para, prev = None, {"text": stripped, "bottom": ln["bbox"][3], "page": pi, "full": full}
                continue
            if mono:
                if para is not None and para["type"] == "code":
                    para["spans"].append(("\n", 0))
                    para["spans"].extend(spans)
                else:
                    if para is not None and para["spans"]:
                        blocks.append(para)
                    para = {"type": "code", "spans": list(spans), "page": pi}
                prev = {"text": stripped, "bottom": ln["bbox"][3], "page": pi, "full": full}
                continue
            # 正文段落
            if para is not None and para["type"] != "para":
                if para["spans"]:
                    blocks.append(para)
                para = None
            new = para is None
            if not new and prev is not None:
                if gap is not None:
                    if gap > para_thr or x0 > indent_x:
                        new = True
                elif not prev.get("full", True):   # 跨页：上一行非满行则新段，否则续段
                    new = True
            if new:
                if para is not None and para["spans"]:
                    blocks.append(para)
                para = {"type": "para", "spans": [], "page": pi}
            elif prev is not None:
                ptxt = prev["text"]
                cont = ((ptxt.endswith("-") and len(ptxt) >= 2 and ptxt[-2].isalnum()
                         and stripped[:1].isalnum())
                        or (prev.get("soft") and stripped[:1].isalnum()))
                if cont:
                    if para["spans"]:
                        lt, lb = para["spans"][-1]
                        if lt.endswith("-"):
                            para["spans"][-1] = (lt[:-1], lb)
                elif not (_is_cjk(ptxt[-1:]) or _is_cjk(stripped[:1])):
                    para["spans"].append((" ", 0))
            para["spans"].extend(spans)
            prev = {"text": stripped, "bottom": ln["bbox"][3], "soft": soft_break,
                    "page": pi, "full": full}
    if para is not None and para["spans"]:
        blocks.append(para)
    return blocks

def _pdf_serialize(blocks):
    buf, pos = [], 0
    for b in blocks:
        if buf:
            buf.append("\n\n")
            pos += 2
        b["start"] = pos
        if b["type"] == "table":
            cells = []
            for r in b.pop("rows", []):
                row = []
                for c in r:
                    row.append((c, pos))
                    buf.append(c)
                    pos += len(c)
                    buf.append("  ")
                    pos += 2
                buf.append("\n")
                pos += 1
                cells.append(row)
            b["cells"] = cells
        else:
            pieces = []
            for (t, bits) in b.pop("spans", []):
                s0 = pos
                buf.append(t)
                pos += len(t)
                pieces.append((t, bits, s0, pos))
            b["pieces"] = pieces
    return "".join(buf), blocks

def _pdf_page_offsets(blocks, npages):
    offs = [None] * npages
    for b in blocks:
        p = b.get("page")
        if p is not None and 0 <= p < npages and offs[p] is None:
            offs[p] = b["start"]
    last = 0
    for i in range(npages):
        if offs[i] is None:
            offs[i] = last
        else:
            last = offs[i]
    return offs

def _block_text(b):
    return "".join(p[0] for p in b.get("pieces", []))

def _norm_title(s):
    return re.sub(r"[\s\-\u2013\u2014_\u00b7\.\u3002\uff0c,\u3001:\uff1a;\uff1b!\uff01?\uff1f'\"\u201c\u201d\u2018\u2019()\uff08\uff09\[\]\u3010\u3011]+", "", (s or "").lower())

def _pdf_chapters(blocks, toc, text, page_offsets):
    if toc:
        by_page = {}
        for b in blocks:
            by_page.setdefault(b.get("page", -1), []).append(b)
        used, chapters = set(), []
        for item in toc:
            lvl, title, pno = item[0], item[1], item[2] if len(item) > 2 else 1
            key = _norm_title(title)
            off = None
            if key:
                for b in by_page.get(pno - 1, []):
                    if id(b) in used or b["type"] != "heading":
                        continue
                    bt = _norm_title(_block_text(b))
                    if bt and (bt == key or key in bt or bt in key):
                        off = b["start"]
                        used.add(id(b))
                        break
            if off is None and 0 <= pno - 1 < len(page_offsets):
                off = page_offsets[pno - 1]
            chapters.append((off if off is not None else 0, (title or "").strip() or "—"))
        chapters.sort(key=lambda x: x[0])
        ded = []
        for off, t in chapters:
            if ded and off <= ded[-1][0]:
                continue
            ded.append((off, t))
        if ded and ded[0][0] != 0:
            ded.insert(0, (0, "开篇"))
        if ded:
            return ded
    def _is_ch(b):
        return b["type"] == "heading" and (b.get("level", 9) <= 2 or b.get("sect"))
    # 合并“相邻且同级”的标题块（论文标题常折行成多行，避免拆成多个伪章节）
    ch, cur = [], None
    for b in blocks:
        if _is_ch(b):
            bt = re.sub(r"^[\u25a0\u25aa\u25ae\u25cf\u25c6\u25b6\u25b8\u25ba]+", "", _block_text(b).strip())
            bt = bt.strip()[:60]
            if cur is not None and cur["page"] == b.get("page") and cur["level"] == b.get("level"):
                cur["title"] = (cur["title"] + " " + bt).strip()[:60]
            else:
                if cur is not None:
                    ch.append((cur["start"], cur["title"]))
                cur = {"start": b["start"], "title": bt or "—", "page": b.get("page"), "level": b.get("level")}
        elif cur is not None:
            ch.append((cur["start"], cur["title"]))
            cur = None
    if cur is not None:
        ch.append((cur["start"], cur["title"]))
    if ch:
        if ch[0][0] != 0:
            ch.insert(0, (0, "开篇"))
        return ch
    return scan_chapters(text)

def _render_pdf_para(blk, font, max_w, line_h, para_gap):
    fm = QFontMetricsF(font)
    asc = fm.ascent()
    wrapped = _wrap_pieces(blk["pieces"], max_w, font)
    lines = [_line_from_pieces(w, (w[0][2] if w else blk["start"]),
                               (w[-1][3] if w else blk["start"]),
                               indent=0.0, height=line_h, ascent=asc) for w in wrapped]
    if lines:
        lines[-1].gap_after = para_gap
    return lines

def _render_pdf_heading(blk, font, max_w, line_spacing, line_h):
    level = max(1, min(6, blk.get("level", 3)))
    hf = _md_heading_font(font, level)
    hfm = QFontMetricsF(hf)
    asc = hfm.ascent()
    hh = hfm.height() * line_spacing * (1.15 if level <= 2 else 1.0)
    wrapped = _wrap_pieces(blk["pieces"], max_w, hf)
    lines = [_line_from_pieces(w, (w[0][2] if w else blk["start"]),
                               (w[-1][3] if w else blk["start"]),
                               style="h%d" % level, height=hh, ascent=asc, indent=0.0)
             for w in wrapped]
    if lines:
        lines[-1].gap_after = MD_H_GAP.get(level, 0.4) * line_h
    return lines

def _render_pdf_code(blk, font, max_w, line_spacing, line_h, para_gap):
    cf = _md_mono_font(font)
    cfm = QFontMetricsF(cf)
    asc = cfm.ascent()
    hh = cfm.height() * line_spacing
    rows, cur = [], []
    for (t, bits, s0, s1) in blk["pieces"]:
        start = 0
        for m in re.finditer("\n", t):
            if m.start() > start:
                cur.append((t[start:m.start()], bits | MD_CODE, s0 + start, s0 + m.start()))
            rows.append(cur)
            cur = []
            start = m.end()
        if start < len(t):
            cur.append((t[start:], bits | MD_CODE, s0 + start, s1))
    rows.append(cur)
    out = []
    for w in rows:
        if not w:
            out.append(Line("", blk["start"], blk["start"], indent=0.0, style="code",
                            height=hh, ascent=asc))
            continue
        for wl in _wrap_pieces(w, max_w, font, word_wrap=False):
            out.append(_line_from_pieces(wl, wl[0][2], wl[-1][3], indent=0.0,
                                         style="code", height=hh, ascent=asc))
    if out:
        out[-1].gap_after = para_gap
    return out

def _render_pdf_list(blk, font, max_w, line_h, para_gap):
    fm = QFontMetricsF(font)
    asc = fm.ascent()
    marker = blk.get("marker", "\u2022")
    content_indent = blk.get("depth", 0) * MD_LIST_INDENT + fm.horizontalAdvance(marker) + 8.0
    wrapped = _wrap_pieces(blk["pieces"], max_w - content_indent, font)
    lines = []
    for k, w in enumerate(wrapped):
        lines.append(_line_from_pieces(w, (w[0][2] if w else blk["start"]),
                                       (w[-1][3] if w else blk["start"]),
                                       indent=content_indent, marker=(marker if k == 0 else None),
                                       height=line_h, ascent=asc))
    if lines:
        lines[-1].gap_after = para_gap * 0.4
    return lines

def _render_pdf_table(blk, font, max_w, line_h, para_gap):
    cells = blk.get("cells") or []
    if not cells:
        return []

    def prep(row):
        out = []
        for (t, off) in row:
            pieces = [(t, 0, off, off + len(t))] if t else []
            out.append((t, pieces, off))
        return out

    hprep = prep(cells[0])
    rprep = [prep(r) for r in cells[1:]]
    return _table_lines(hprep, rprep, blk.get("align") or [], 0, blk["start"], blk["start"],
                        font, max_w, line_h, para_gap)

def _render_pdf_image(blk, max_w, max_h, line_h):
    iw, ih = blk.get("img_w", 0), blk.get("img_h", 0)
    if iw <= 0 or ih <= 0:
        return []
    dw = max_w
    dh = dw * ih / iw
    if max_h and dh > max_h:
        dh = max_h
        dw = dh * iw / ih
    if dw > max_w:
        dw = max_w
        dh = dw * ih / iw
    ln = Line("", blk["start"], blk["start"], indent=0.0, style="image",
              height=dh, ascent=0.0, image=(blk["img_id"], dw, dh))
    ln.gap_after = line_h * 0.6
    return [ln]

def render_pdf_lines(blocks, font, max_w, line_spacing, para_spacing, line_h, max_h=None):
    """把 PDF 版式块渲染成带样式与源偏移映射的 Line 列表（偏移为绝对）。"""
    fm = QFontMetricsF(font)
    para_gap = fm.height() * para_spacing
    out = []
    for b in blocks:
        t = b["type"]
        if t == "heading":
            out.extend(_render_pdf_heading(b, font, max_w, line_spacing, line_h))
        elif t == "code":
            out.extend(_render_pdf_code(b, font, max_w, line_spacing, line_h, para_gap))
        elif t == "list":
            out.extend(_render_pdf_list(b, font, max_w, line_h, para_gap))
        elif t == "table":
            out.extend(_render_pdf_table(b, font, max_w, line_h, para_gap))
        elif t == "image":
            out.extend(_render_pdf_image(b, max_w, max_h, line_h))
        else:
            out.extend(_render_pdf_para(b, font, max_w, line_h, para_gap))
    return out

def load_pdf(path, progress=None):
    """解析 PDF → (全文, 章节, 书名, 版式块)。
    有文字层：还原标题层级 / 粗体斜体 / 段落 / 列表 / 代码 / 表格，交给 LazyPager 富文本渲染。
    无文字层（扫描版/图片 PDF）：整页渲染成图，以“每页一章”的图片模式显示。"""
    try:
        import pymupdf  # noqa: F401
    except Exception as e:
        raise RuntimeError("未安装 PyMuPDF，请执行：pip install pymupdf") from e
    title, toc, pages, images, has_text = _pdf_extract(path, progress)
    if not has_text:
        # 扫描版 / 图片 PDF：无文字层，整页渲染成图（每页一章）
        spec, images = _pdf_render_scan_pages(path, len(pages), progress)
        n = len(spec)
        text = "\u3000" * n
        chapters = [(i, f"第 {i + 1} 页") for i in range(n)]
        if progress:
            progress(98, "完成")
        return text, chapters, title, {"mode": "scan", "pages": spec, "images": images}
    body = _pdf_body_size(pages)
    running = _pdf_running_keys(pages)
    blocks = _pdf_build(pages, body, running)
    text, blocks = _pdf_serialize(blocks)
    page_offsets = _pdf_page_offsets(blocks, len(pages))
    chapters = _pdf_chapters(blocks, toc, text, page_offsets)
    return text, chapters, title, {"blocks": blocks, "images": images}

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
    loaded = Signal(str, list, str, object)   # (全文, 章节, 书名, 版式块)
    failed = Signal(str)
    progress = Signal(int, str)       # (百分比, 说明)
    def __init__(self, path):
        super().__init__()
        self.path = path
    def run(self):
        extra = None
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
                text, chapters, title, extra = load_pdf(
                    self.path, progress=lambda p, m: self.progress.emit(p, m))
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
            self.loaded.emit(text, chapters, title, extra)
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
        self.pdf_images = {}      # img_id -> PNG/JPEG 字节（PDF 插图/扫描页）
        self._img_cache = {}      # img_id -> QPixmap（懒解码 + LRU）
        self.scan_mode = False
        self.scan_pages = []      # 扫描版：每页一张整页位图 [{img_id, w, h}]
        self.full_text = ""
        self.font = QFont(); self.font.setPointSize(16)
        self.line_spacing = LINE_SPACING
        self.margin_x = MARGIN_X; self.margin_y = MARGIN_Y
        self.outer = OUTER; self.gutter = GUTTER
        self.para_spacing = PARA_SPACING
        self.line_h = QFontMetricsF(self.font).height() * self.line_spacing
        self.spread = 0
        # 3D 翻页动画状态
        self._flip_anim = None          # QVariantAnimation（进行中）
        self._flip_dir = 0              # +1 向前 / -1 向后
        self._flip_t = 0.0              # 动画进度 0~1
        self._flip_from = 0             # 动画起始 spread
        self._flip_rect = None          # 翻动页的起始矩形
        self._flip_snaps = {}           # {'front': QPixmap, 'back': QPixmap}
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
        if self._flip_anim is not None:          # 动画进行中，忽略重复触发
            return
        total = (len(self.pages) + 1) // 2
        ns = self.spread + delta
        if ns < 0:
            self.needChapter.emit(-1, True)       # 去上一章末页
        elif ns >= total:
            self.needChapter.emit(1, False)       # 去下一章首页
        else:
            self._start_flip(ns, delta)

    # ---- 3D 翻页动画（Book Bazaar / Apple Books 式单页翻转） ----
    def cancel_flip(self):
        """停止并清理进行中的翻页动画（章节跳转 / 重排时调用）。"""
        if self._flip_anim is not None:
            self._flip_anim.stop()
            self._flip_anim.deleteLater()
            self._flip_anim = None
        self._flip_dir = 0
        self._flip_snaps = {}

    def _header_for(self, idx):
        """按页在书中的位置（奇偶）返回页眉：左页=书名，右页=章节。"""
        return self.book_title if idx % 2 == 0 else self.chapter_title

    def _spine_side(self, idx):
        """按页在书中的位置返回装订侧：左页书脊在右，右页书脊在左。"""
        return 'right' if idx % 2 == 0 else 'left'

    def _flip_snapshot(self, rect, page, header, spine_side):
        """把一页渲染成位图（翻页动画用，不含外部投影）。"""
        size = rect.size().toSize()
        if page is None or size.width() < 2 or size.height() < 2:
            return None
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        q.translate(-rect.left(), -rect.top())
        self._draw_page(q, rect, page, header, shadow=False, spine_side=spine_side)
        q.end()
        return pm

    def _prepare_flip(self, direction):
        """构建翻页所需的快照与几何状态（翻动页来自当前 spread，不改变 spread）。"""
        self._flip_from = self.spread
        self._flip_dir = 1 if direction > 0 else -1
        left, right = self.page_rects()
        f = self._flip_from
        if self._flip_dir > 0:
            # 向前翻：右页(2f+1) 绕书脊翻到左，翻过去后露出背面(2f+2)
            self._flip_rect = right
            front = self.pages[2 * f + 1] if 2 * f + 1 < len(self.pages) else None
            back = self.pages[2 * f + 2] if 2 * f + 2 < len(self.pages) else None
            self._flip_snaps['front'] = self._flip_snapshot(right, front, self._header_for(2 * f + 1), self._spine_side(2 * f + 1))
            self._flip_snaps['back'] = self._flip_snapshot(right, back, self._header_for(2 * f + 2), self._spine_side(2 * f + 2))
        else:
            # 向后翻：左页(2f) 绕书脊翻到右，翻过去后露出背面(2f-1)
            self._flip_rect = left
            front = self.pages[2 * f] if 2 * f < len(self.pages) else None
            back = self.pages[2 * f - 1] if 2 * f - 1 >= 0 else None
            self._flip_snaps['front'] = self._flip_snapshot(left, front, self._header_for(2 * f), self._spine_side(2 * f))
            self._flip_snaps['back'] = self._flip_snapshot(left, back, self._header_for(2 * f - 1), self._spine_side(2 * f - 1))

    def _start_flip(self, target, direction):
        self._prepare_flip(direction)
        self._flip_t = 0.0
        self._flip_anim = QVariantAnimation(self)
        self._flip_anim.setStartValue(0.0)
        self._flip_anim.setEndValue(1.0)
        self._flip_anim.setDuration(FLIP_MS)
        self._flip_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._flip_anim.valueChanged.connect(self._on_flip_tick)
        self._flip_anim.finished.connect(lambda: self._finish_flip(target))
        self._flip_anim.start()
        self.update()

    def _on_flip_tick(self, v):
        self._flip_t = float(v)
        self.update()

    def _finish_flip(self, target):
        if self._flip_anim is not None:
            self._flip_anim.deleteLater()
            self._flip_anim = None
        self._flip_dir = 0
        self._flip_snaps = {}
        self.spread = target
        self.pageChanged.emit(self.left_page_offset())
        self.update()

    def _warp_page(self, pm, mirror, pivot_x, cy, fx, hs, W, H):
        """用 Pillow 做单次透视扭曲（整页一次性采样，无条带接缝）。
        返回 (QPixmap, bbox_x, bbox_y)；失败返回 (None, 0, 0)。"""
        try:
            img = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            arr = np.frombuffer(img.constBits(), np.uint8).reshape(
                img.height(), img.bytesPerLine())[:, :img.width() * 4].reshape(img.height(), img.width(), 4)
            pil = Image.fromarray(arr, 'RGBA')
            if mirror:
                pil = pil.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            quad = QPolygonF([QPointF(pivot_x, cy - H / 2.0), QPointF(fx, cy - hs / 2.0),
                              QPointF(fx, cy + hs / 2.0), QPointF(pivot_x, cy + H / 2.0)])
            unit = QTransform()
            QTransform.squareToQuad(quad, unit)
            S = QTransform(); S.scale(1.0 / W, 1.0 / H)
            inv = (unit * S).inverted()[0]
            xs = [quad[0].x(), quad[1].x(), quad[2].x(), quad[3].x()]
            ys = [quad[0].y(), quad[1].y(), quad[2].y(), quad[3].y()]
            x0 = int(min(xs)); x1 = int(max(xs)); y0 = int(min(ys)); y1 = int(max(ys))
            if x1 - x0 < 2 or y1 - y0 < 2:
                return None, 0, 0
            m11, m12, m13, m21, m22, m23, m31, m32, m33 = (
                inv.m11(), inv.m12(), inv.m13(), inv.m21(), inv.m22(),
                inv.m23(), inv.m31(), inv.m32(), inv.m33())
            dc = m13 * x0 + m23 * y0 + m33
            a = m11 / dc; b = m21 / dc; c = (m11 * x0 + m21 * y0 + m31) / dc
            d_ = m12 / dc; e = m22 / dc; f = (m12 * x0 + m22 * y0 + m32) / dc
            g = m13 / dc; h = m23 / dc
            warped = pil.transform((x1 - x0 + 1, y1 - y0 + 1), Image.Transform.PERSPECTIVE,
                                   (a, b, c, d_, e, f, g, h), Image.Resampling.BILINEAR,
                                   fillcolor=(0, 0, 0, 0))
            wa = np.ascontiguousarray(warped)
            data = wa.tobytes()
            qimg = QImage(data, wa.shape[1], wa.shape[0], wa.shape[1] * 4, QImage.Format.Format_RGBA8888)
            return QPixmap.fromImage(qimg), x0, y0
        except Exception:
            return None, 0, 0

    def _draw_turning_page(self, p):
        """绘制带透视的翻动页：整页用 Pillow 一次性透视扭曲（无条带接缝、文字更清晰）。"""
        t = self._flip_t
        front = t < 0.5
        pm = self._flip_snaps.get('front' if front else 'back')
        if pm is None or pm.isNull():
            return
        mirror = (front and self._flip_dir < 0) or (not front and self._flip_dir > 0)

        rect = self._flip_rect
        W = rect.width(); H = rect.height()
        if W < 2 or H < 2:
            return
        gutter = self.gutter
        if self._flip_dir > 0:        # 右页 → 左页
            pivot0 = rect.left(); pivot1 = rect.left() - gutter; d = 1.0
        else:                         # 左页 → 右页
            pivot0 = rect.right(); pivot1 = rect.right() + gutter; d = -1.0
        theta = t * math.pi
        pivot_x = pivot0 + (pivot1 - pivot0) * t     # 书脊随翻页滑过书缝
        cy = rect.top() + H / 2.0
        c = math.cos(theta); s = math.sin(theta)
        D = max(1.0, FLIP_PERSPECTIVE * W)
        den = D - W * s
        if den <= 0.0:
            den = 0.001
        fx = pivot_x + d * W * c * (D / den)         # 自由边屏幕 x
        if abs(fx - pivot_x) < 1.0:                  # 页面侧立，几乎不可见
            return
        hs = H * D / den                             # 自由边投影高度

        warped, bx, by = self._warp_page(pm, mirror, pivot_x, cy, fx, hs, W, H)
        if warped is not None:
            p.drawPixmap(QPointF(bx, by), warped)

        # 弧面光影：书脊侧偏暗，整页单条平滑渐变
        yt_spine = cy - H / 2.0
        yb_spine = cy + H / 2.0
        yt_free = cy - hs / 2.0
        yb_free = cy + hs / 2.0
        path = QPainterPath()
        path.moveTo(pivot_x, yt_spine)
        path.lineTo(fx, yt_free)
        path.lineTo(fx, yb_free)
        path.lineTo(pivot_x, yb_spine)
        path.closeSubpath()
        g = QLinearGradient(pivot_x, 0, fx, 0)
        g.setColorAt(0.0, QColor(0, 0, 0, 52))
        g.setColorAt(0.4, QColor(0, 0, 0, 16))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(path)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._draw_background(p)
        left, right = self.page_rects()
        if self._flip_anim is not None:
            self._draw_flip(p, left, right)
        else:
            self._draw_book_stack(p, left, right)
            self._draw_gutter_shadow(p, left, right)
            li, ri = self.spread * 2, self.spread * 2 + 1
            if li < len(self.pages):
                self._draw_page(p, left, self.pages[li], self.book_title, spine_side=self._spine_side(li))
            if ri < len(self.pages):
                self._draw_page(p, right, self.pages[ri], self.chapter_title, spine_side=self._spine_side(ri))
        p.end()

    def _draw_background(self, p):
        """桌面背景 + 中心向四周的柔光渐暗，增强立体景深。"""
        p.fillRect(self.rect(), QColor(self.bg))
        g = QRadialGradient(self.width() / 2.0, self.height() / 2.0,
                            max(self.width(), self.height()) * 0.8)
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(1.0, QColor(0, 0, 0, 78))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(self.rect())

    def _draw_book_stack(self, p, left, right):
        """开卷书页下方的层叠纸边（纸厚 + 前切口），营造真实书的厚度。"""
        base = QColor(self.page_color)
        for k in range(6, 0, -1):        # 从外到内，最内层最后画（贴近书页）
            off = k * 1.4
            col = base.darker(105 + k * 7)     # 越靠外越暗
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            # 左页：纸边向左、向下外扩（书脊侧不扩）
            p.drawRect(QRectF(left.left() - off, left.top(), left.width() + off, left.height() + off * 0.6))
            # 右页：纸边向右、向下外扩
            p.drawRect(QRectF(right.left(), right.top(), right.width() + off, right.height() + off * 0.6))

    def _draw_flip(self, p, left, right):
        f = self._flip_from
        self._draw_book_stack(p, left, right)
        # 书脊阴影随翻页增强（页面侧立时最强）
        self._draw_gutter_shadow(p, left, right, boost=math.sin(self._flip_t * math.pi))
        if self._flip_dir > 0:
            # 向前：左页静止，右侧露出新页(2f+3)，翻动页绕书脊翻到左
            if 2 * f < len(self.pages):
                self._draw_page(p, left, self.pages[2 * f], self.book_title, spine_side=self._spine_side(2 * f))
            if 2 * f + 3 < len(self.pages):
                self._draw_page(p, right, self.pages[2 * f + 3], self.chapter_title, spine_side=self._spine_side(2 * f + 3))
        else:
            # 向后：右页静止，左侧露出新页(2f-2)，翻动页绕书脊翻到右
            if 2 * f + 1 < len(self.pages):
                self._draw_page(p, right, self.pages[2 * f + 1], self.chapter_title, spine_side=self._spine_side(2 * f + 1))
            if 2 * f - 2 >= 0:
                self._draw_page(p, left, self.pages[2 * f - 2], self.book_title, spine_side=self._spine_side(2 * f - 2))
        self._draw_turning_page(p)

    def _draw_gutter_shadow(self, p, left, right, boost=0.0):
        """书脊折页谷：中缝填充纸张渐暗的渐变，衔接两侧页面的折痕。
        之前中缝露出深色背景，形成一道“黑缝”断开折页；改成纸色压暗后更连续。
        boost 翻页时增强（页面侧立、折痕更深）。"""
        base = QColor(self.page_color)
        d0 = 128 + int(16 * boost)      # 中缝两缘：与页面折痕衔接的轻度压暗
        d1 = 150 + int(30 * boost)      # 谷底更深
        g = QLinearGradient(left.right(), 0, right.left(), 0)
        g.setColorAt(0.0, base.darker(d0))
        g.setColorAt(0.5, base.darker(d1))
        g.setColorAt(1.0, base.darker(d0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(QRectF(left.right(), left.top(), right.left() - left.right(), left.height()))

    def _line_font(self, ln):
        if ln.style:
            if ln.style.startswith("h") and ln.style[1:].isdigit():
                return _md_heading_font(self.font, int(ln.style[1:]))
            if ln.style == "code":
                return _md_mono_font(self.font)
        return self.font

    def _draw_highlights(self, p, ln, line_x, y, lh, lfm):
        def dpos(src):
            return lfm.horizontalAdvance(ln.text[:_line_src_to_disp(ln, src)])
        rs, re_ = max(ln.start, self.read_start), min(ln.end, self.read_end)
        if rs < re_:
            p.fillRect(QRectF(line_x + dpos(rs), y, dpos(re_) - dpos(rs), lh), QColor("#ffe08a"))
        if self.search_starts and self.search_len > 0:
            lo = bisect.bisect_left(self.search_starts, ln.start)
            hi = bisect.bisect_left(self.search_starts, ln.end)
            for k in range(lo, hi):
                ms = self.search_starts[k]
                me = min(ms + self.search_len, ln.end)
                col = QColor("#ffb340") if ms == self.search_cur else QColor("#ffe9a8")
                p.fillRect(QRectF(line_x + dpos(ms), y, dpos(me) - dpos(ms), lh), col)
        s, e = max(ln.start, self.sel_start), min(ln.end, self.sel_end)
        if s < e:
            p.fillRect(QRectF(line_x + dpos(s), y, dpos(e) - dpos(s), lh), QColor("#cfe3ff"))

    def _draw_runs(self, p, ln, lfont, line_x, y, asc):
        base = QColor(MD_QUOTE_COLOR) if ln.style == "quote" else QColor(self.text_color)
        x = line_x
        for (d0, dl, bits) in ln.runs:
            seg = ln.text[d0:d0 + dl]
            if not seg:
                continue
            f = _md_style_font(lfont, bits)
            p.setFont(f)
            fmm = QFontMetricsF(f)
            w = fmm.horizontalAdvance(seg)
            if bits & MD_CODE:
                p.fillRect(QRectF(x, y, w, fmm.height()), QColor(MD_INLINE_CODE_BG))
            if bits & MD_LINK:
                col = QColor(MD_LINK_COLOR)
            elif bits & MD_CODE:
                col = QColor(MD_INLINE_CODE_COLOR)
            else:
                col = base
            p.setPen(col)
            p.drawText(QPointF(x, y + asc), seg)
            if bits & MD_STRIKE:
                p.setPen(QPen(base, 1))
                p.drawLine(QPointF(x, y + asc - fmm.ascent() / 2),
                           QPointF(x + w, y + asc - fmm.ascent() / 2))
            if bits & MD_LINK:
                p.setPen(QPen(QColor(MD_LINK_COLOR), 1))
                p.drawLine(QPointF(x, y + asc + fmm.descent() * 0.4),
                           QPointF(x + w, y + asc + fmm.descent() * 0.4))
            x += w

    def _pixmap_for(self, iid):
        pm = self._img_cache.get(iid)
        if pm is not None:
            return pm
        data = self.pdf_images.get(iid)
        if not data:
            return None
        pm = QPixmap()
        if not pm.loadFromData(data):
            return None
        self._img_cache[iid] = pm
        if len(self._img_cache) > 48:      # LRU：超限淘汰最旧的
            for k in list(self._img_cache.keys())[:-32]:
                self._img_cache.pop(k, None)
        return pm

    def _draw_image(self, p, ln, tx, max_w, y):
        iid, dw, dh = ln.image
        pm = self._pixmap_for(iid)
        if pm is None or pm.isNull():
            p.setPen(QPen(QColor(MD_HR_COLOR), 1))
            p.drawRect(QRectF(tx, y, max_w, dh))
            return
        ix = tx + max(0.0, (max_w - dw) / 2.0)      # 居中
        p.drawPixmap(QRectF(ix, y, dw, dh), pm, QRectF(pm.rect()))

    def _draw_scan_content(self, p, rect, page):
        """扫描版：书页上居中画整页位图，底部显示页码。"""
        ln = page.lines[0] if page.lines else None
        if ln and ln.image:
            iid, dw, dh = ln.image
            pm = self._pixmap_for(iid)
            if pm and not pm.isNull():
                ix = rect.left() + (rect.width() - dw) / 2.0
                iy = rect.top() + (rect.height() - dh) / 2.0
                p.drawPixmap(QRectF(ix, iy, dw, dh), pm, QRectF(pm.rect()))
        total = len(self.scan_pages)
        pf = QFont(self.font); pf.setPointSize(max(9, self.font.pointSize() - 4))
        p.setFont(pf); p.setPen(QColor(self.pageno_color))
        p.drawText(QRectF(rect.left(), rect.bottom() - 24, rect.width(), 18),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   f"{page.start + 1} / {total}")
        p.setFont(self.font)

    def _draw_page(self, p, rect, page, header_text, shadow=True, spine_side=None):
        # 四周轻微投影，模拟书页浮在深色桌面上
        if shadow:
            for i in (6, 4, 2):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, int(22 / i)))
                p.drawRect(rect.adjusted(-i, -i, i, i))
        # 书页
        p.setPen(QPen(QColor(self.page_border), 1))
        p.setBrush(QColor(self.page_color))
        p.drawRect(rect)
        # 页面光照：中心亮、四周微暗，去除“纯白平片”感
        rad = QRadialGradient(rect.center().x(), rect.center().y() - rect.height() * 0.08,
                              rect.width() * 0.85)
        rad.setColorAt(0.0, QColor(0, 0, 0, 0))
        rad.setColorAt(0.72, QColor(0, 0, 0, 0))
        rad.setColorAt(1.0, QColor(0, 0, 0, 20))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(rad))
        p.drawRect(rect)
        # 书脊折痕：靠近装订一侧纸张弯曲的柔和阴影
        if spine_side:
            cw = rect.width() * 0.18
            if spine_side == 'right':
                g = QLinearGradient(rect.right(), 0, rect.right() - cw, 0)
                band = QRectF(rect.right() - cw, rect.top(), cw, rect.height())
            else:
                g = QLinearGradient(rect.left(), 0, rect.left() + cw, 0)
                band = QRectF(rect.left(), rect.top(), cw, rect.height())
            g.setColorAt(0.0, QColor(0, 0, 0, 58))
            g.setColorAt(0.3, QColor(0, 0, 0, 16))
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawRect(band)

        if self.scan_mode:
            self._draw_scan_content(p, rect, page)
            return

        fm = QFontMetricsF(self.font)
        base_asc = fm.ascent()
        tx = rect.left() + self.margin_x
        max_w = rect.width() - 2 * self.margin_x
        y = rect.top() + self.margin_y          # 每行的“顶端”
        for ln in page.lines:
            lh = ln.height if ln.height else self.line_h
            asc = ln.ascent if ln.ascent else base_asc
            lfont = self._line_font(ln)
            lfm = fm if lfont is self.font else QFontMetricsF(lfont)
            ix = ln.indent if ln.indent else 0.0
            line_x = tx + ix
            # 块级装饰：代码块底色 / 引用竖线 / 分隔线
            if ln.style == "code":
                p.fillRect(QRectF(tx - 6, y, max_w + 12, lh), QColor(MD_CODE_BG))
            if ln.style == "quote":
                p.fillRect(QRectF(tx + ix - MD_BAR_W - 6, y, MD_BAR_W, lh), QColor(MD_BAR_COLOR))
            if ln.style == "hr":
                p.setPen(QPen(QColor(MD_HR_COLOR), 1))
                p.drawLine(QPointF(tx, y + lh / 2), QPointF(tx + max_w, y + lh / 2))
            # 高亮（朗读 / 搜索 / 选择）：源偏移 → 显示位置
            self._draw_highlights(p, ln, line_x, y, lh, lfm)
            # 列表标记
            if ln.marker:
                p.setFont(lfont)
                p.setPen(QColor(self.text_color))
                p.drawText(QPointF(line_x - lfm.horizontalAdvance(ln.marker) - 8.0, y + asc), ln.marker)
            # 插图
            if ln.style == "image" and ln.image:
                self._draw_image(p, ln, tx, max_w, y)
            # 正文
            elif ln.text:
                if ln.runs:
                    self._draw_runs(p, ln, lfont, line_x, y, asc)
                else:
                    p.setFont(lfont)
                    p.setPen(QColor(MD_QUOTE_COLOR) if ln.style == "quote" else QColor(self.text_color))
                    p.drawText(QPointF(line_x, y + asc), ln.text)
            y += lh + ln.gap_after

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
                y = rect.top() + self.margin_y
                ln = page.lines[-1] if page.lines else None
                for cand in page.lines:                 # 按累计高度定位行
                    h = (cand.height if cand.height else self.line_h) + cand.gap_after
                    if pos.y() < y + h:
                        ln = cand
                        break
                    y += h
                if ln is None:
                    return None
                fm = QFontMetricsF(self._line_font(ln))
                ix = ln.indent if ln.indent else 0.0
                x = pos.x() - (rect.left() + self.margin_x + ix)
                lo, hi = 0, len(ln.text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fm.horizontalAdvance(ln.text[:mid]) <= x:
                        lo = mid
                    else:
                        hi = mid - 1
                return _line_disp_to_src(ln, lo)
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position()
            self._dragged = False

    def mouseMoveEvent(self, e):
        moved = self._press_pos is not None and \
                (e.position() - self._press_pos).manhattanLength() > 6
        if not self.selecting and moved:
            # 拖动才开始选字（避免长按/单击闪烁高亮）
            off = self._hit(self._press_pos)
            if off is not None:
                self.selecting = True
                self.sel_start = off
                self.sel_end = off
                self._dragged = True
        if self.selecting:
            if moved:
                self._dragged = True          # 拖动 = 选字
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
        self._press_pos = None
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

    def _on_book_loaded(self, text, chapters, title="", extra=None):
        self.load_bar.hide()
        self.full_text = text
        self.view.set_layout(self._make_layout())
        extra = extra if isinstance(extra, dict) else {}
        if extra.get("mode") == "scan":
            self.view.scan_mode = True
            self.view.scan_pages = extra.get("pages") or []
            self.view.pdf_images = extra.get("images") or {}
            self.view._img_cache.clear()
            self.pager = LazyPager(text, chapters, md=False, pdf_blocks=None,
                                   scan_pages=self.view.scan_pages)
        else:
            self.view.scan_mode = False
            self.view.scan_pages = []
            is_md = os.path.splitext(self.book_path)[1].lower() in MD_EXTS
            if is_md:
                chapters = md_chapters(text)
            pdf_blocks = extra.get("blocks")
            self.view.pdf_images = extra.get("images") or {}
            self.view._img_cache.clear()
            self.pager = LazyPager(text, chapters, md=is_md, pdf_blocks=pdf_blocks)
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
        self.view.cancel_flip()
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
        if self.pager and self.pager.md:
            items = [(m.start(), m.group(2).strip(), len(m.group(1)))
                     for m in MD_HEAD_RE.finditer(self.full_text)]
            if not items:
                items = [(c.start, c.title, 1) for c in self.pager.chapters]
        else:
            items = [(c.start, c.title, 1) for c in self.pager.chapters]
        for off, title, level in items:
            it = QListWidgetItem("      " * (level - 1) + title)
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
        if self.view.scan_mode:
            self.statusBar().showMessage("扫描版 PDF 无文字层，暂不支持朗读")
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
            unit = "页" if self.view.scan_mode else "章"
            self.statusBar().showMessage(
                f"第 {self.cur_chapter + 1}/{len(self.pager.chapters)} {unit} · {pct:.1f}%")
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
