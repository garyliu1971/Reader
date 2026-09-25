import math
import bisect
import numpy as np
from PIL import Image
from typing import List, Tuple, Optional
from PySide6.QtCore import Qt, QRectF, QSizeF, QPointF, Signal, QObject, QTimer, QEvent, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QColor, QPen, QBrush, QLinearGradient, QPixmap, QTransform, QRadialGradient, QPainterPath, QPolygonF, QNativeGestureEvent, QImage
from PySide6.QtWidgets import QWidget, QSwipeGesture, QPanGesture
from .config import CLICK_ZONE, FLIP_MS, FLIP_PERSPECTIVE, GUTTER, LINE_SPACING, MARGIN_X, MARGIN_Y, OUTER, PARA_SPACING, SWIPE_THRESHOLD, WHEEL_THRESHOLD
from .model import Line, Page
from .markdown import (_line_disp_to_src, _line_src_to_disp, _md_heading_font, _md_mono_font,
                       _md_style_font, MD_BAR_COLOR, MD_BAR_W, MD_CODE, MD_CODE_BG, MD_HR_COLOR,
                       MD_INLINE_CODE_BG, MD_INLINE_CODE_COLOR, MD_LINK, MD_LINK_COLOR,
                       MD_QUOTE_COLOR, MD_STRIKE)

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
        hs = H                                       # 自由边高度保持不变（避免透视把文字明显放大）

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
