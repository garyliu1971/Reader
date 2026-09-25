"""DOCX 解析：用 python-docx 提取段落 / 标题 / 粗斜体 / 列表 / 表格 / 图片，
转成与 PDF 一致的富文本块结构（pieces + images），复用 render_pdf_lines 渲染。"""
import io
import os
import re

from .markdown import MD_BOLD, MD_ITALIC


def _run_images(run, doc, images, img_counter):
    """从 run 里提取内嵌图片（a:blip → r:embed → relationship → 图片字节）。

    返回 (图片列表, 新计数器)。图片列表元素为 (img_id, w, h)。"""
    from docx.oxml.ns import qn
    from PIL import Image as PILImage

    out = []
    for blip in run._element.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        part = doc.part.related_parts.get(rid)
        if part is None:
            continue
        blob = part.blob
        img_id = f"docx_img_{img_counter}"
        images[img_id] = blob
        img_counter += 1
        w = h = 0
        try:
            with PILImage.open(io.BytesIO(blob)) as im:
                w, h = im.size
        except Exception:
            pass
        out.append((img_id, w, h))
    return out, img_counter


def _heading_level(paragraph):
    """返回标题级别 1~6，非标题返回 0。"""
    from docx.oxml.ns import qn

    st = (paragraph.style.name or "") if paragraph.style is not None else ""
    if st.startswith("Heading") or st.startswith("标题"):
        digits = "".join(ch for ch in st if ch.isdigit())
        return int(digits) if digits else 1
    pPr = paragraph._p.pPr
    if pPr is not None:
        lvl = pPr.find(qn("w:outlineLvl"))
        if lvl is not None:
            try:
                return int(lvl.get(qn("w:val"))) + 1
            except Exception:
                pass
    return 0


def _alpha(n):
    """1→a, 26→z, 27→aa…"""
    s = ""
    while n > 0:
        n -= 1
        s = chr(ord("a") + n % 26) + s
        n //= 26
    return s


def _roman(n):
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
            (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
            (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, sym in vals:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)


def _chinese_num(n):
    digits = "零一二三四五六七八九"
    if n <= 0:
        return "零"
    if n < 10:
        return digits[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + digits[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    return str(n)


def _fmt_number(fmt, n):
    """把计数 n 按 numFmt 格式化成字符串。"""
    fmt = (fmt or "decimal").lower()
    if fmt == "bullet":
        return "\u2022"
    if fmt in ("decimal", "decimalzero", "cardinaltext", "ordinaltext", "ordinal"):
        return str(n)
    if fmt == "lowerletter":
        return _alpha(n)
    if fmt == "upperletter":
        return _alpha(n).upper()
    if fmt == "lowerroman":
        return _roman(n).lower()
    if fmt == "upperroman":
        return _roman(n).upper()
    if fmt in ("chinesecounting", "chinesecountingthousand"):
        return _chinese_num(n)
    return str(n)


class _Numbering:
    """解析 numbering.xml：numId → 各级编号定义，样式(pStyle) → (numId, ilvl)。"""

    def __init__(self, doc):
        from docx.oxml.ns import qn
        self.schemes = {}    # numId -> {ilvl: {"fmt","text","start","pstyle"}}
        self.by_style = {}   # pStyle val -> (numId, ilvl)
        try:
            numbering = doc.part.numbering_part
        except Exception:
            numbering = None
        if numbering is None:
            return
        root = numbering.element
        abstracts = {}        # abstractNumId -> {ilvl: level}
        for a in root.findall(qn("w:abstractNum")):
            aid = a.get(qn("w:abstractNumId"))
            levels = {}
            for lvl in a.findall(qn("w:lvl")):
                try:
                    ilvl = int(lvl.get(qn("w:ilvl")) or 0)
                except Exception:
                    continue
                fmt_el = lvl.find(qn("w:numFmt"))
                txt_el = lvl.find(qn("w:lvlText"))
                start_el = lvl.find(qn("w:start"))
                ps_el = lvl.find(qn("w:pStyle"))
                start = 1
                if start_el is not None:
                    try:
                        start = int(start_el.get(qn("w:val")) or 1)
                    except Exception:
                        start = 1
                levels[ilvl] = {
                    "fmt": (fmt_el.get(qn("w:val")) or "decimal") if fmt_el is not None else "decimal",
                    "text": (txt_el.get(qn("w:val")) or "") if txt_el is not None else "",
                    "start": start,
                    "pstyle": ps_el.get(qn("w:val")) if ps_el is not None else None,
                }
            abstracts[aid] = levels
        for num in root.findall(qn("w:num")):
            nid = num.get(qn("w:numId"))
            a = num.find(qn("w:abstractNumId"))
            aid = a.get(qn("w:val")) if a is not None else None
            if aid in abstracts:
                self.schemes[nid] = abstracts[aid]
                for ilvl, lvl in abstracts[aid].items():
                    if lvl.get("pstyle"):
                        self.by_style[lvl["pstyle"]] = (nid, ilvl)


def _list_ref(paragraph, numbering):
    """返回 (depth, numId) 或 None（非列表）。覆盖直接 <w:numPr> 与 pStyle 两种写法。"""
    from docx.oxml.ns import qn

    pPr = paragraph._p.pPr
    if pPr is None:
        return None
    numPr = pPr.find(qn("w:numPr"))
    if numPr is not None:
        depth = 0
        ilvl = numPr.find(qn("w:ilvl"))
        if ilvl is not None:
            try:
                depth = int(ilvl.get(qn("w:val")) or 0)
            except Exception:
                depth = 0
        numId = numPr.find(qn("w:numId"))
        num_id = numId.get(qn("w:val")) if numId is not None else None
        return depth, num_id
    pstyle = pPr.find(qn("w:pStyle"))
    if pstyle is None:
        return None
    hit = numbering.by_style.get(pstyle.get(qn("w:val")))
    if hit is None:
        return None
    num_id, ilvl = hit
    return ilvl, num_id


def _bump_counter(counters, num_id, depth, numbering):
    """列表项计数：本层 +1（首次用 start），更深层清零。返回该 numId 的计数器 dict。"""
    c = counters.setdefault(num_id, {})
    for k in [k for k in c if k > depth]:
        del c[k]
    scheme = numbering.schemes.get(num_id) or {}
    lvl = scheme.get(depth) or {}
    start = lvl.get("start", 1) or 1
    if depth in c:
        c[depth] += 1
    else:
        c[depth] = start
    return c


def _list_marker(numbering, num_id, depth, counters):
    """按 lvlText 模板（%1.%2.）与各级计数器生成 marker 文本。"""
    scheme = numbering.schemes.get(num_id) if num_id is not None else None
    lvl = scheme.get(depth) if scheme else None
    if lvl is None:
        return "\u2022" if depth == 0 else "\u25e6"
    if (lvl["fmt"] or "").lower() == "bullet":
        return "\u2022" if depth == 0 else "\u25e6"

    def rep(m):
        k = int(m.group(1)) - 1          # lvlText 里 %1 → ilvl 0
        c = counters.get(k, 1)
        sub = scheme.get(k) if scheme else None
        return _fmt_number((sub or {}).get("fmt", "decimal"), c)

    out = re.sub(r"%(\d+)", rep, lvl["text"] or "")
    return out or _fmt_number(lvl["fmt"], counters.get(depth, 1))


def _iter_block_items(doc):
    """按文档顺序遍历段落与表格（python-docx 默认的 paragraphs 会丢失表格顺序）。"""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def load_docx(path, progress=None):
    """解析 DOCX → (全文, 章节, 书名, 富文本块)。

    返回的 extra = {"blocks": [...], "images": {...}}，与 PDF 一致，
    直接交给 LazyPager(pdf_blocks=blocks) + view.pdf_images 渲染。"""
    try:
        import docx
    except Exception as e:
        raise RuntimeError("未安装 python-docx，请执行：pip install python-docx") from e

    if progress:
        progress(5, "解析 docx…")

    doc = docx.Document(path)
    title = doc.core_properties.title or ""

    blocks = []
    images = {}
    chapters = []
    text_parts = []          # 全文（用于搜索 / 进度 / 目录）
    offset = 0               # 当前在全文中的字符偏移
    img_counter = 0
    numbering = _Numbering(doc)
    list_counters = {}       # numId -> {ilvl: 当前计数}，用于多级有序列表编号

    def emit_text(ttype, pieces, **kw):
        nonlocal offset
        start = offset
        txt = "".join(p[0] for p in pieces)
        text_parts.append(txt)
        text_parts.append("\n")
        blk = {"type": ttype, "start": start, "pieces": pieces}
        blk.update(kw)
        blocks.append(blk)
        offset += len(txt) + 1
        return start, txt

    def emit_image(img_id, w, h):
        nonlocal offset
        blk = {"type": "image", "start": offset, "img_id": img_id, "img_w": w, "img_h": h}
        blocks.append(blk)
        return offset

    def emit_table(table):
        nonlocal offset
        start = offset
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        txt = "\n".join("\t".join(r) for r in rows)
        text_parts.append(txt)
        text_parts.append("\n")
        cells, p = [], 0
        for r in rows:
            rc = []
            for c in r:
                rc.append((c, start + p))
                p += len(c) + 1
            cells.append(rc)
            p += 1
        blocks.append({"type": "table", "start": start, "cells": cells, "align": []})
        offset += len(txt) + 1

    for item in _iter_block_items(doc):
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        if isinstance(item, Table):
            list_counters = {}
            emit_table(item)
            continue

        # 段落
        paragraph = item
        level = _heading_level(paragraph)
        list_ref = _list_ref(paragraph, numbering)

        # 顺序收集文本 run 的 pieces（含粗斜体样式位）
        pieces = []
        local = offset
        for run in paragraph.runs:
            t = run.text
            if not t:
                continue
            bits = 0
            if run.bold:
                bits |= MD_BOLD
            if run.italic:
                bits |= MD_ITALIC
            pieces.append((t, bits, local, local + len(t)))
            local += len(t)

        # 段落内嵌图片
        imgs = []
        for run in paragraph.runs:
            if not run.text:
                got, img_counter = _run_images(run, doc, images, img_counter)
                imgs.extend(got)

        if level:
            list_counters = {}
            start, txt = emit_text("heading", pieces, level=level)
            if txt.strip():
                chapters.append((start, txt.strip()))
            for iid, w, h in imgs:
                emit_image(iid, w, h)
        elif list_ref is not None:
            depth, num_id = list_ref
            counters = _bump_counter(list_counters, num_id, depth, numbering)
            marker = _list_marker(numbering, num_id, depth, counters)
            emit_text("list", pieces, marker=marker, depth=depth)
            for iid, w, h in imgs:
                emit_image(iid, w, h)
        elif pieces or imgs:
            list_counters = {}
            if pieces:
                emit_text("para", pieces)
            for iid, w, h in imgs:
                emit_image(iid, w, h)
        else:
            # 空段落 → 空行（保持段落间距）
            list_counters = {}
            emit_text("para", [("", 0, offset, offset)])

    text = "".join(text_parts)

    if not chapters:
        from .textio import scan_chapters
        chapters = scan_chapters(text)
    elif chapters[0][0] != 0:
        chapters.insert(0, (0, "开篇"))

    if progress:
        progress(95, "完成")
    return text, chapters, title, {"blocks": blocks, "images": images}
