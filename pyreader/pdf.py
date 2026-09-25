import io
import os
import re
import time
from PySide6.QtGui import QFontMetricsF
from .model import Line, Page
from .markdown import (_md_heading_font, _md_mono_font, _table_lines, _wrap_pieces,
                       _line_from_pieces, MD_BOLD, MD_ITALIC, MD_CODE, MD_H_GAP, MD_LIST_INDENT)
from .textio import scan_chapters, load_epub, load_html_file

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
    if not wrapped:
        # 空段落（docx 里的空行）→ 渲染成单个空行，保留段落间距
        ln = Line("", blk["start"], blk["start"], indent=0.0, height=line_h, ascent=asc)
        ln.gap_after = para_gap
        return [ln]
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
