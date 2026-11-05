from typing import List, Tuple, Optional
from PySide6.QtCore import QSizeF
from PySide6.QtGui import QFont, QFontMetricsF
from .config import LINE_SPACING, MARGIN_X, MARGIN_Y, PARA_SPACING, INDENT_SPACES

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

def _can_break(text: str, i: int) -> bool:
    """能否在 text[i-1] 与 text[i] 之间断行：空格之后、连字符 / 破折号之后、任一侧是 CJK 时可断；
    拉丁词内部（含撇号 Taylor’s、数字）不可断。"""
    a, b = text[i - 1], text[i]
    if a in " \t" or a == "—":
        return True
    if a == "-" and i >= 2 and text[i - 2].isalnum() and b.isalnum():
        return True
    return a >= "⺀" or b >= "⺀"

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
    if text[lo] == " ":                 # 恰好断在词尾：把空格留在行尾（不可见），下一行不以空格开头
        lo += 1
    elif not _can_break(text, lo):      # 会把拉丁词劈开：回退到最近的可断点；整行都是同一个词才强制断词
        k = lo
        while k > 1 and not _can_break(text, k):
            k -= 1
        if k > 1:
            lo = k
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
