"""漫画 / 扫描书压缩包（.zip / .cbz）阅读控件。纯逻辑在 comic.py；这里只管解码、缓存、绘制与输入。
线程规则：Qt 控件只在主线程访问；后台线程只做「读字节 + QImage.fromData」（QImage 可在非 GUI 线程创建），结果经 Signal 回主线程。
当前屏的页缓存未命中时在主线程同步解码（保证能画出来），后台只预取前后几页。
"""
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPointF, QRect, QRectF, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QKeyEvent, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QInputDialog, QMenu, QWidget

from . import comic as cm
from .config import DEFAULT_THEME, THEMES

CACHE_PAGES = 5                      # 解码后页图的 LRU 容量（当前屏 ≤2 + 后 2 + 前 1）
PREFETCH_AHEAD = 2
WHEEL_STEP = 0.15                    # 一格滚轮（120）滚动视口高度的比例
KEY_STEP = 60                        # 方向键上下滚动的像素
SPACE_STEP = 0.9                     # 空格滚动视口高度的比例
WHEEL_FLIP_COOLDOWN = 0.15           # 滚到边缘后翻页的最小间隔（触摸板会连发很多小事件）
DRAG_THRESHOLD = 4                   # 鼠标位移小于这个像素才算「点击」
CLOSE_WAIT_MS = 3000                 # close_comic 等后台任务结束的上限


class _Signals(QObject):
    decoded = Signal(int, int, QImage)       # (token, 页序号, 图；解码失败为空图)


class _DecodeTask(QRunnable):
    """后台读一页并解码。token 对不上（已关闭 / 已换书）的结果由主线程丢弃。"""

    def __init__(self, archive: cm.ComicArchive, entry: str, index: int, token: int, signals: _Signals):
        super().__init__()
        self.archive, self.entry, self.index, self.token, self.signals = archive, entry, index, token, signals

    def run(self):
        try:
            img = QImage.fromData(self.archive.read(self.entry))
        except cm.ComicError:
            img = QImage()
        try:
            self.signals.decoded.emit(self.token, self.index, img)
        except RuntimeError:                 # 控件已销毁
            pass


class ComicView(QWidget):
    pageChanged = Signal(int, int)           # (当前屏第一页的序号 0 起, 总页数)；翻页 / 打开时发
    backRequested = Signal()                 # Esc：请求回到书架
    optionsChanged = Signal()                # 适配 / 跨页 / 方向改变（便于外部持久化）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self._bg = THEMES[DEFAULT_THEME]["bg"]
        self._archive: Optional[cm.ComicArchive] = None
        self._pages: List[str] = []
        self._index = 0
        self._fit = "window"
        self._spread = False
        self._rtl = False
        self._error = ""
        self._cache: "OrderedDict[int, QImage]" = OrderedDict()
        self._sizes: Dict[int, Tuple[int, int]] = {}
        self._bad: Set[int] = set()
        self._pending: Set[int] = set()
        self._screen: Tuple[int, ...] = ()
        self._token = 0
        self._signals = _Signals()
        self._signals.decoded.connect(self._on_decoded)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._pan_x: Optional[float] = None      # None = 水平居中；否则是左边缘滚出视口的像素
        self._pan_y = 0.0
        self._press: Optional[QPointF] = None
        self._dragged = False
        self._last_wheel_flip = 0.0

    # ------------------------------------------------------------------ 公共接口
    def open_comic(self, path: str, page: int = 0) -> bool:
        """打开 .zip / .cbz / 图片型 .epub。失败（损坏 / 没有图片）返回 False，原因见 last_error()。page 超界会被夹到范围内。"""
        self.close_comic()
        try:
            arc = cm.ComicArchive(path)
        except cm.ComicError as e:
            self._error = str(e)
            return False
        if not arc.pages:
            arc.close()
            self._error = "压缩包里没有图片"
            return False
        self._archive, self._pages, self._error = arc, arc.pages, ""
        self._index = min(max(int(page), 0), len(self._pages) - 1)
        self._reset_pan()
        self._prefetch()
        self.update()
        self.pageChanged.emit(self._index, len(self._pages))
        return True

    def close_comic(self):
        """释放压缩包与缓存；等后台任务收尾（有上限），之后迟到的结果因 token 失效被丢弃。"""
        self._token += 1
        self._pool.clear()
        self._pool.waitForDone(CLOSE_WAIT_MS)
        if self._archive is not None:
            self._archive.close()
        self._archive, self._pages, self._index, self._screen = None, [], 0, ()
        self._cache.clear()
        self._sizes.clear()
        self._bad.clear()
        self._pending.clear()
        self.update()

    def is_open(self) -> bool:
        return self._archive is not None

    def last_error(self) -> str:
        return self._error

    def current_page(self) -> int:
        return self._index

    def screen_last(self, index: int) -> int:
        """以 index 起头的那一屏的最后一页（跨页时是第二页）；没打开时就是 index。"""
        if not self._pages:
            return index
        return max(cm.spread_pages(index, len(self._pages), self._portrait, self._spread))

    def page_count(self) -> int:
        return len(self._pages)

    def chapters(self) -> List[cm.Chapter]:
        return self._archive.chapters() if self._archive else []

    def go_to(self, page: int):
        if not self._pages:
            return
        self._set_index(min(max(int(page), 0), len(self._pages) - 1))

    def next_screen(self) -> bool:
        nxt = cm.next_start(self._index, len(self._pages), self._portrait, self._spread) if self._pages else None
        if nxt is None:
            return False
        self._set_index(nxt)
        return True

    def prev_screen(self) -> bool:
        prv = cm.prev_start(self._index, len(self._pages), self._portrait, self._spread) if self._pages else None
        if prv is None:
            return False
        self._set_index(prv, to_bottom=True)
        return True

    def fit_mode(self) -> str:
        return self._fit

    def set_fit_mode(self, mode: str):
        if mode in cm.FIT_MODES and mode != self._fit:
            self._fit = mode
            self._reset_pan()
            self.update()
            self.optionsChanged.emit()

    def spread(self) -> bool:
        return self._spread

    def set_spread(self, on: bool):
        if bool(on) != self._spread:
            self._spread = bool(on)
            self._reset_pan()
            self._prefetch()
            self.update()
            self.optionsChanged.emit()

    def rtl(self) -> bool:
        return self._rtl

    def set_rtl(self, on: bool):
        if bool(on) != self._rtl:
            self._rtl = bool(on)
            self.update()
            self.optionsChanged.emit()

    def opf_rtl(self) -> Optional[bool]:
        """这本书自己声明的阅读方向（epub 的 page-progression-direction）：True 从右往左 / False 从左往右 / None 没声明（zip / cbz 恒为 None）。"""
        return self._archive.rtl if self._archive is not None else None

    def set_theme(self, colors):
        self._bg = colors["bg"]
        self.update()

    # ------------------------------------------------------------------ 页图 / 缓存
    def _set_index(self, index: int, to_bottom: bool = False):
        changed = index != self._index
        self._index = index
        self._reset_pan(to_bottom)
        self._prefetch()
        self.update()
        if changed:
            self.pageChanged.emit(self._index, len(self._pages))

    def _reset_pan(self, to_bottom: bool = False):
        self._pan_x = None
        self._pan_y = float("inf") if to_bottom else 0.0      # 夹取在 _layout 里做

    def _image(self, i: int) -> Optional[QImage]:
        """第 i 页的解码图；缓存未命中则在本线程解码。解码失败返回 None（记入 _bad，不反复重试）。"""
        img = self._cache.get(i)
        if img is not None:
            self._cache.move_to_end(i)
            return img
        if i in self._bad or self._archive is None:
            return None
        try:
            img = QImage.fromData(self._archive.read(self._pages[i]))
        except cm.ComicError:
            img = QImage()
        if img.isNull():
            self._bad.add(i)
            return None
        self._put(i, img)
        return img

    def _put(self, i: int, img: QImage):
        self._cache[i] = img
        self._cache.move_to_end(i)
        self._sizes[i] = (img.width(), img.height())
        for k in list(self._cache):          # 淘汰最久没用的，但不动当前屏
            if len(self._cache) <= CACHE_PAGES:
                break
            if k not in self._screen and k != i:
                del self._cache[k]

    def _portrait(self, i: int) -> bool:
        """第 i 页是否竖页。尺寸没缓存时只读图片头（QImageReader），不整页解码。"""
        sz = self._sizes.get(i)
        if sz is None:
            sz = (0, 0)
            if self._archive is not None and i not in self._bad:
                try:
                    buf = QBuffer()
                    buf.setData(QByteArray(self._archive.read(self._pages[i])))
                    buf.open(QIODevice.ReadOnly)
                    s = QImageReader(buf).size()
                    sz = (s.width(), s.height())
                except cm.ComicError:
                    pass
            if sz[0] <= 0:
                self._bad.add(i)
            else:
                self._sizes[i] = sz
        return cm.is_portrait(*sz)

    def _prefetch(self):
        """后台预取当前屏之后的 PREFETCH_AHEAD 页和之前 1 页。"""
        if not self._pages or self._archive is None:
            return
        n = len(self._pages)
        shown = cm.spread_pages(self._index, n, self._portrait, self._spread)
        want = list(range(shown[-1] + 1, min(shown[-1] + 1 + PREFETCH_AHEAD, n)))
        if shown[0] > 0:
            want.append(shown[0] - 1)
        for i in want:
            if i in self._cache or i in self._pending or i in self._bad:
                continue
            self._pending.add(i)
            self._pool.start(_DecodeTask(self._archive, self._pages[i], i, self._token, self._signals))

    def _on_decoded(self, token: int, i: int, img: QImage):
        if token != self._token:
            return
        self._pending.discard(i)
        if img.isNull():
            self._bad.add(i)
        elif i not in self._cache:
            self._put(i, img)

    # ------------------------------------------------------------------ 布局 / 绘制
    def _layout(self):
        """当前屏的排版：[(绘制顺序的页序号, 图, 目标矩形)]，以及内容总尺寸。无页返回 None。"""
        if not self._pages:
            return None
        n = len(self._pages)
        self._screen = cm.spread_pages(self._index, n, self._portrait, self._spread)
        order = cm.display_order(self._screen, self._rtl)
        imgs = {i: self._image(i) for i in order}
        ok = [i for i in order if imgs[i] is not None]
        if not ok:
            return order, imgs, [], 0.0, 0.0
        sizes = [(imgs[i].width(), imgs[i].height()) for i in ok]
        tw, th = cm.spread_size(sizes)
        z = cm.compute_zoom(self._fit, self.width(), self.height(), tw, th)
        dw, dh = tw * z, th * z
        self._pan_y = min(max(self._pan_y, 0.0), max(dh - self.height(), 0.0))
        if self._pan_x is None:
            self._pan_x = max(dw - self.width(), 0.0) / 2
        self._pan_x = min(max(self._pan_x, 0.0), max(dw - self.width(), 0.0))
        x = (self.width() - dw) / 2 if dw <= self.width() else -self._pan_x
        y = (self.height() - dh) / 2 if dh <= self.height() else -self._pan_y
        rects = []
        for i in ok:
            w = imgs[i].width() * th / imgs[i].height() * z
            rects.append((i, QRectF(x, y, w, dh)))
            x += w
        return order, imgs, rects, dw, dh

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self._bg))
        lay = self._layout()
        if lay is None:
            return
        order, imgs, rects, _, _ = lay
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for i, r in rects:
            p.drawImage(r, imgs[i])
        p.setPen(QColor("#303030" if QColor(self._bg).lightness() > 128 else "#d0d0d0"))      # 跟主题底色反差
        for i in order:
            if imgs[i] is None:                         # 解码失败的页：在窗口里提示，不留空白
                p.drawText(self.rect(), Qt.AlignCenter, f"无法显示：{cm.display_name(self._pages[i])}")
        self._draw_hud(p, order)

    def _draw_hud(self, p: QPainter, order):
        first, last = min(order) + 1, max(order) + 1
        text = f"{first}" if first == last else f"{first}-{last}"
        text += f" / {len(self._pages)}"
        f = QFont(self.font())
        f.setPointSize(10)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(text) + 16
        h = p.fontMetrics().height() + 6
        r = QRect((self.width() - w) // 2, self.height() - h - 8, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawRoundedRect(r, 6, 6)
        p.setPen(QColor("#ffffff"))
        p.drawText(r, Qt.AlignCenter, text)

    # ------------------------------------------------------------------ 滚动 / 输入
    def _overflow(self) -> Tuple[float, float]:
        lay = self._layout()
        if lay is None or not lay[2]:
            return 0.0, 0.0
        return max(lay[3] - self.width(), 0.0), max(lay[4] - self.height(), 0.0)

    def _scroll_y(self, dy: float) -> bool:
        """竖向滚动 dy 像素；已在边缘（无法再动）返回 False。"""
        _, oy = self._overflow()
        new = min(max(self._pan_y + dy, 0.0), oy)
        if new == self._pan_y:
            return False
        self._pan_y = new
        self.update()
        return True

    def _flip(self, delta: int):
        if delta > 0:
            self.next_screen()
        elif delta < 0:
            self.prev_screen()

    def wheelEvent(self, ev: QWheelEvent):
        dy = ev.angleDelta().y()
        if dy == 0:
            return
        if not self._scroll_y(-dy / 120 * WHEEL_STEP * self.height()):
            now = time.monotonic()
            if now - self._last_wheel_flip >= WHEEL_FLIP_COOLDOWN:
                self._last_wheel_flip = now
                self._flip(1 if dy < 0 else -1)
        ev.accept()

    def mousePressEvent(self, ev: QMouseEvent):
        self.setFocus()
        if ev.button() == Qt.LeftButton:
            self._press = ev.position()
            self._dragged = False

    def mouseMoveEvent(self, ev: QMouseEvent):
        if self._press is None or not (ev.buttons() & Qt.LeftButton):
            return
        d = ev.position() - self._press
        if not self._dragged and abs(d.x()) + abs(d.y()) < DRAG_THRESHOLD:
            return
        self._dragged = True
        self._press = ev.position()
        ox, oy = self._overflow()
        if self._pan_x is not None:
            self._pan_x = min(max(self._pan_x - d.x(), 0.0), ox)
        self._pan_y = min(max(self._pan_y - d.y(), 0.0), oy)
        self.update()

    def mouseReleaseEvent(self, ev: QMouseEvent):
        if ev.button() != Qt.LeftButton or self._press is None:
            return
        self._press = None
        if not self._dragged:
            self._flip(cm.nav_delta(cm.click_side(ev.position().x(), self.width()), self._rtl))

    def contextMenuEvent(self, ev):
        self._build_menu().exec(ev.globalPos())
        ev.accept()

    def _build_menu(self) -> QMenu:
        """右键菜单：快捷键之外的鼠标入口——适配 / 跨页 / 方向、跳页、章节。"""
        menu = QMenu(self)
        for mode, label in (("width", "适应宽度（W）"), ("height", "适应高度（H）"), ("window", "适应窗口（F）")):
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self._fit == mode)
            a.triggered.connect(lambda _=False, m=mode: self.set_fit_mode(m))
        menu.addSeparator()
        a = menu.addAction("双页跨页（D）")
        a.setCheckable(True)
        a.setChecked(self._spread)
        a.triggered.connect(lambda _=False: self.set_spread(not self._spread))
        a = menu.addAction("从右向左（R）")
        a.setCheckable(True)
        a.setChecked(self._rtl)
        a.triggered.connect(lambda _=False: self.set_rtl(not self._rtl))
        if self._pages:
            menu.addSeparator()
            menu.addAction("跳到页码…").triggered.connect(self._ask_page)
            chs = self.chapters()
            if len(chs) > 1:
                sub = menu.addMenu("章节")
                for ch in chs:
                    sub.addAction(f"{ch.title or '（根目录）'}（{ch.count} 页）").triggered.connect(
                        lambda _=False, s=ch.start: self.go_to(s))
        return menu

    def _ask_page(self):
        n = len(self._pages)
        v, ok = QInputDialog.getInt(self, "跳到页码", f"页码（1–{n}）", self._index + 1, 1, n)
        if ok:
            self.go_to(v - 1)

    def keyPressEvent(self, ev: QKeyEvent):
        k, shift = ev.key(), bool(ev.modifiers() & Qt.ShiftModifier)
        if k == Qt.Key_Left:
            self._flip(cm.nav_delta("left", self._rtl))
        elif k == Qt.Key_Right:
            self._flip(cm.nav_delta("right", self._rtl))
        elif k in (Qt.Key_Down, Qt.Key_Up):
            d = KEY_STEP if k == Qt.Key_Down else -KEY_STEP
            if not self._scroll_y(d):
                self._flip(1 if d > 0 else -1)
        elif k == Qt.Key_Space:
            d = SPACE_STEP * self.height() * (-1 if shift else 1)
            if not self._scroll_y(d):
                self._flip(-1 if shift else 1)
        elif k == Qt.Key_PageDown:
            self._flip(1)
        elif k == Qt.Key_PageUp:
            self._flip(-1)
        elif k == Qt.Key_Home:
            self.go_to(0)
        elif k == Qt.Key_End:
            self.go_to(len(self._pages) - 1)
        elif k == Qt.Key_W:
            self.set_fit_mode("width")
        elif k == Qt.Key_H:
            self.set_fit_mode("height")
        elif k == Qt.Key_F:
            self.set_fit_mode("window")
        elif k == Qt.Key_D:
            self.set_spread(not self._spread)
        elif k == Qt.Key_R:
            self.set_rtl(not self._rtl)
        elif k == Qt.Key_Escape:
            self.backRequested.emit()
        else:
            super().keyPressEvent(ev)
            return
        ev.accept()

    def closeEvent(self, ev):
        self.close_comic()
        super().closeEvent(ev)
