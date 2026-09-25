import re
import math
import html
from typing import List, Tuple, Optional
from PySide6.QtGui import QFont, QFontMetricsF
from .model import Line, Page

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

