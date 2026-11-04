"""阶段 2 / 3 / 4：书架界面（阶段 3 起云端书库并入：后台列 library/、角标、带 token 的下载后打开；
阶段 4 起：设备文件的进度摘要写入与读取、进度条 / 最近阅读 / 在读 / 已读完、「继续阅读」条、手动上传对话框；
阶段 5 起：收藏 / 隐藏（右键菜单、智能书架「收藏」）、合并其它设备的覆盖层、自愈推送、只读实例、重置、关闭时的有界 flush）。
设计见 c:\\tmp\\pyreader-bookshelf-design.md §6、§7 阶段 2。

结构：
  - ScanWorker  后台线程：scan_local + build_books（纯逻辑，不碰 Qt 对象），结果经 Signal 回主线程
  - BookModel   QAbstractTableModel：网格视图用第 0 列，列表视图用全部列
  - BookDelegate 网格卡片（色块封面 + 书名 + 进度）
  - ShelfView   侧栏（智能书架 + 本机文件夹）、搜索 / 排序 / 视图切换、状态条、index.json 缓存

线程规则：Qt 对象只在主线程创建 / 访问；扫描线程只拿到快照（路径列表、书签快照、进度 / 打开记录副本）。
所有来自磁盘的字符串都按纯文本绘制（QLabel 一律 PlainText；delegate 用 drawText）。
"""
import html
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import (QAbstractTableModel, QCoreApplication, QModelIndex, QObject, QRect, QSize, Qt, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget, QStyle, QStyledItemDelegate, QToolButton,
                               QTreeView, QVBoxLayout, QWidget)

from . import shelf as sh
from . import shelfsync as ss
from .webdav import client_from_cfg, library_sources

SMART = (("all", "全部"), ("recent", "最近阅读"), ("reading", "在读"), ("done", "已读完"), ("fav", "收藏"))   # 智能书架
SORT_LABELS = (("last_read", "最近阅读"), ("title", "书名"), ("added", "最近添加"), ("progress", "进度"), ("size", "大小"))
COLUMNS = ("书名", "格式", "进度", "最近阅读", "大小", "位置")
CLOUD_SMART = (("uncategorized", "未分类"), ("removed", "云端已移除"), ("hidden", "我隐藏的"))     # 有书时才出现在侧栏
CLOUD_TIMER_MS = 10 * 60 * 1000      # 书架可见期间每 10 分钟刷新一次云端列表
CLOUD_STALE_SECONDS = 60             # 打开书架视图时，距上次云端刷新不足这个秒数则不重复
BADGES = {"cloud": "仅云端", "synced": "已下载", "local": "仅本地", "removed": "云端已移除"}
BADGE_COLORS = {"cloud": "#2f7fd8", "synced": "#2e9a5b", "local": "#6b7280", "removed": "#d9822b"}
CARD_W, CARD_H = 144, 240
COVER_W, COVER_H = 116, 160
PUSH_DEBOUNCE_MS = 5000              # 进度摘要：阅读事件之后等这么久再推送（合并连续的事件）
PUSH_MIN_INTERVAL_MS = 60 * 1000     # 两次推送设备文件之间至少隔这么久（设备文件最多约 300KB，读书时不必每 30 秒推一次）
OVERLAY_SAVE_MS = 5000               # overlay.json 防抖
SKEW_NOTICE_MS = 2 * 60 * 1000       # 本机时钟与服务器 Date 头相差超过这么久才提示 / 才修正时间戳（Date 只有秒级精度）
CONFIRM_TEXT = ("要把你的书架状态同步到云端吗？\n\n"
                "会上传到你的云端用户目录（users/<用户名>/shelf/）的内容：\n"
                "  · 书名、收藏、隐藏标记\n"
                "  · 最近读过的书及其阅读进度（百分比、时间）\n"
                "  · 本设备的名称\n"
                "不会上传：本机文件路径、账号密码、任何设置。\n\n"
                "之后你的修改和读书进度会自动同步（不会上传任何书）。")


# ================================================================== 小工具（纯函数，便于测试）
def fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError, OverflowError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_ago(ts: float, now: Optional[float] = None) -> str:
    """最近阅读时间的口语化显示。小于 1e9 的是 recent 菜单的排名种子（不是真实时间）→「最近」。"""
    if not ts or ts <= 0:
        return ""
    if ts < 1e9:
        return "最近"
    d = (now if now is not None else time.time()) - ts
    if d < 0:
        return "刚刚"
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d // 86400)} 天前"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def progress_text(b: sh.Book) -> str:
    """进度列 / 卡片上的进度文字：已知百分比 → 'NN%'（读完 → 已读完）；只知道有本地进度偏移 → 在读。"""
    if b.external:                       # 有声书 / 漫画压缩包：不读、不记进度，进度列留空
        return ""
    if b.percent is not None:
        if b.status == "done":
            return "已读完"
        return f"{b.percent:.0f}%"
    return "在读" if b.has_progress else ""


def plain_tip(text: str) -> str:
    """工具提示按纯文本显示：Qt 的 ToolTip 会自动识别富文本，来自磁盘 / 服务器的名字不能被当成 HTML 解释。"""
    return "<p style='white-space:pre-wrap'>" + html.escape(str(text), quote=False) + "</p>"


def badge_text(b: sh.Book, busy: bool = False) -> str:
    """卡片 / 列表「位置」列的角标文字（design §4.9）：下载中 > 仅云端 / 已下载 / 仅本地 / 云端已移除；占位卡片没有。"""
    if busy:
        return "下载中…"
    return BADGES.get(b.where, "")


def flags_text(b: sh.Book, cloud: bool = True) -> str:
    """列表「位置」列里的小记号：★ 收藏；● 有还没推送到云端的本地修改（仅启用云同步时）。"""
    return (" ★" if b.fav else "") + (" ●" if (b.unpushed and cloud) else "")


def continue_text(b: sh.Book, local_names=(), now: Optional[float] = None) -> str:
    """「继续阅读」条的一行文字：继续阅读：《三体》  在 gary-phone 上读到 37%（3 小时前）。纯文本（调用方用 PlainText 显示）。"""
    dev = (b.progress_device or "").strip()
    where = "本机" if dev and dev in set(local_names) else dev
    s = f"继续阅读：《{b.title}》"
    if b.percent is not None:
        s += f"  在 {where} 上读到 {b.percent:.0f}%" if where else f"  读到 {b.percent:.0f}%"
    ago = fmt_ago(b.last_read, now)
    return s + (f"（{ago}）" if ago else "")


def suggest_name(name: str) -> str:
    """撞名时给用户的改名建议：三体.epub -> 三体 (2).epub。"""
    stem, ext = os.path.splitext(name)
    return f"{stem or name} (2){ext}"


def count_shelves(books) -> Dict[str, int]:
    """一次遍历得到侧栏所有计数：智能书架 + 每个文件夹书架（与 filter_books 的可见性规则一致）。
    removed（云端已移除）含默认隐藏的；uncategorized 是云端根目录的书。"""
    out: Dict[str, int] = {"all": 0, "recent": 0, "reading": 0, "done": 0, "removed": 0, "uncategorized": 0,
                           "fav": 0, "hidden": 0}
    for b in (books.values() if isinstance(books, dict) else books):
        if b.removed and not b.hidden:
            out["removed"] += 1
        if b.hidden:
            out["hidden"] += 1
        if b.hidden or b.auto_hidden:
            continue
        out["all"] += 1
        if b.fav:
            out["fav"] += 1
        if b.shelf == sh.CLOUD:
            out["uncategorized"] += 1
        if b.last_read > 0:
            out["recent"] += 1
        st = b.status
        if st == "reading":
            out["reading"] += 1
        elif st == "done":
            out["done"] += 1
        out[b.shelf] = out.get(b.shelf, 0) + 1
    return out


class ScanResult:
    def __init__(self, books: Dict[str, sh.Book], truncated: bool, nfiles: int, files=None, scanned: bool = True):
        self.books, self.truncated, self.nfiles = books, truncated, nfiles
        self.files, self.scanned = files, scanned       # scanned=False：只用上一轮的本地文件重建（云端列表变了）


# ================================================================== 后台扫描
class ScanWorker(QObject):
    """后台线程里跑 scan_local + build_books。只处理传入的快照；结果经 Signal 回主线程（QueuedConnection）。"""
    done = Signal(int, object)        # (token, ScanResult)
    failed = Signal(int, str)

    def __init__(self, token: int, roots: List[str], bookmarks: dict, recent: List[str], progress: dict, opened: dict,
                 remote_files=None, removed=(), removed_files=(), files=None, entries=None):
        """remote_files 为 None = 云端未启用 / 列表未知。files 不为 None 时复用上一轮的本地文件（不再遍历磁盘）。
        entries：覆盖层合并视图的条目（fav / hidden / meta），主线程算好后的快照。"""
        super().__init__()
        self.token = token
        self.roots, self.bookmarks, self.recent = roots, bookmarks, recent
        self.progress, self.opened = progress, opened
        self.remote_files, self.removed, self.removed_files = remote_files, set(removed or ()), list(removed_files or ())
        self.files = files
        self.entries = entries or {}
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            scanned = self.files is None
            files = sh.scan_local(self.roots, should_stop=self._stop.is_set) if scanned else self.files
            if self._stop.is_set():
                return
            books = sh.build_books(files, self.remote_files, sh.progress_overlay(self.progress, self.entries), bookmarks=self.bookmarks,
                                   recent=self.recent, opened=self.opened, removed=self.removed,
                                   removed_files=self.removed_files)
            if self._stop.is_set():
                return
            self.done.emit(self.token, ScanResult(books, bool(getattr(files, "truncated", False)), len(files),
                                                  files, scanned))
        except Exception as e:      # 扫描失败不能抛进 UI
            self.failed.emit(self.token, str(e))


# ================================================================== 模型 / 委托
class BookModel(QAbstractTableModel):
    BookRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._books: List[sh.Book] = []
        self.busy: Set[str] = set()          # 正在下载的 book_id
        self.show_badges = False             # 云端未启用时没有任何云相关角标

    def set_busy(self, busy):
        self.busy = set(busy)
        if self._books:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._books) - 1, len(COLUMNS) - 1))

    def badge(self, b: sh.Book) -> str:
        return badge_text(b, b.id in self.busy) if self.show_badges else ""

    def set_books(self, books: List[sh.Book]):
        self.beginResetModel()
        self._books = list(books)
        self.endResetModel()

    def books(self) -> List[sh.Book]:
        return list(self._books)

    def book_at(self, row: int) -> Optional[sh.Book]:
        return self._books[row] if 0 <= row < len(self._books) else None

    def row_of(self, bid: str) -> int:
        for i, b in enumerate(self._books):
            if b.id == bid:
                return i
        return -1

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._books)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(COLUMNS):
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._books)):
            return None
        b = self._books[index.row()]
        if role == self.BookRole:
            return b
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return b.title
            if c == 1:
                return b.fmt_label
            if c == 2:
                return progress_text(b)
            if c == 3:
                return fmt_ago(b.last_read)
            if c == 4:
                return fmt_size(b.size)
            if c == 5:
                return (self.badge(b) + flags_text(b, self.show_badges)).strip()
        elif role == Qt.ItemDataRole.ToolTipRole:
            if b.local_paths:
                return plain_tip(b.local_paths[0])
            return plain_tip(("云端：" + b.remote_rel) if b.remote_rel else b.title)
        elif role == Qt.ItemDataRole.TextAlignmentRole and c in (2, 4):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class BookDelegate(QStyledItemDelegate):
    """网格卡片：fp 哈希取色的色块封面 + 大字 + 进度条；封面下是书名（最多两行）和进度文字。"""

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    def paint(self, p: QPainter, option, index):
        b = index.data(BookModel.BookRole)
        if not isinstance(b, sh.Book):
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if b.removed:
            p.setOpacity(0.55)                            # 云端已移除：灰显
        r = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            p.fillRect(r.adjusted(2, 2, -2, -2), option.palette.highlight())
            txt = option.palette.highlightedText().color()
        else:
            txt = option.palette.text().color()
        cover = QRect(r.left() + (r.width() - COVER_W) // 2, r.top() + 8, COVER_W, COVER_H)
        h, s, l = sh.cover_hsl(b.fp, b.title)
        col = QColor.fromHsl(h, s, l)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(cover, 6, 6)
        p.setBrush(col.lighter(125))                      # 书脊
        p.drawRoundedRect(QRect(cover.left(), cover.top(), 8, cover.height()), 3, 3)
        glyph = sh.cover_glyph(b.title)
        f = QFont(p.font())
        f.setBold(True)
        f.setPixelSize(40 if len(glyph) <= 2 else 28)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(cover.adjusted(12, 6, -6, -6), int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWrapAnywhere), glyph)
        m = index.model()
        bt = m.badge(b) if hasattr(m, "badge") else ""
        if bt:                                            # 角标（纯文字小药丸，drawText 按纯文本绘制）
            bf = QFont(option.font)
            bf.setBold(False)
            bf.setPixelSize(11)
            bfm = QFontMetrics(bf)
            pill = QRect(0, cover.top() + 4, bfm.horizontalAdvance(bt) + 10, bfm.height() + 2)
            pill.moveRight(cover.right() - 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#8b5cf6" if bt.startswith("下载中") else BADGE_COLORS.get(b.where, "#6b7280")))
            p.drawRoundedRect(pill, 4, 4)
            p.setFont(bf)
            p.setPen(QColor("#ffffff"))
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), bt)
        if b.fav or b.unpushed:                           # 左上角：★ 收藏；● 橙点 = 有未推送的本地修改
            x = cover.left() + 12
            if b.fav:
                sf = QFont(option.font)
                sf.setPixelSize(16)
                p.setFont(sf)
                p.setPen(QColor("#ffd54a"))
                p.drawText(QRect(x, cover.top() + 4, 18, 18), int(Qt.AlignmentFlag.AlignCenter), "★")
                x += 20
            if b.unpushed and getattr(m, "show_badges", False):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#ff8c1a"))
                p.drawEllipse(QRect(x, cover.top() + 8, 9, 9))
        has_bar = b.percent is not None and not b.external
        if b.external or b.inapp_media:                   # 媒体（有声书 / 漫画压缩包）：封面左下角标格式；外部格式没有进度条
            ef = QFont(option.font)
            ef.setBold(True)
            ef.setPixelSize(11)
            efm = QFontMetrics(ef)
            et = b.fmt_label[:6]
            if et:                                        # 占位卡 fmt 为空时回退到文件名扩展名；仍为空就不画空药丸
                epill = QRect(cover.left() + 12, cover.bottom() - (30 if has_bar else 22), efm.horizontalAdvance(et) + 10, efm.height() + 2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 120))
                p.drawRoundedRect(epill, 4, 4)
                p.setFont(ef)
                p.setPen(QColor("#ffffff"))
                p.drawText(epill, int(Qt.AlignmentFlag.AlignCenter), et)
        if has_bar:                                       # 封面底部的进度条
            track = QRect(cover.left() + 12, cover.bottom() - 10, cover.width() - 20, 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRoundedRect(track, 2, 2)
            p.setBrush(QColor("#ffffff"))
            w = int(track.width() * max(0.0, min(100.0, b.percent)) / 100.0)
            if w > 0:
                p.drawRoundedRect(QRect(track.left(), track.top(), w, track.height()), 2, 2)
        base = QFont(option.font)
        base.setPointSize(max(8, base.pointSize()))
        p.setFont(base)
        fm = QFontMetrics(base)
        p.setPen(QPen(txt))
        tr = QRect(r.left() + 6, cover.bottom() + 6, r.width() - 12, fm.height() * 2 + 2)
        p.setClipRect(tr)
        title = fm.elidedText(b.title, Qt.TextElideMode.ElideRight, tr.width() * 2 - fm.horizontalAdvance("W"))
        p.drawText(tr, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWrapAnywhere), title)
        p.setClipping(False)
        pt = progress_text(b)
        if pt:
            small = QFont(base)
            small.setPointSize(max(7, base.pointSize() - 1))
            p.setFont(small)
            c2 = QColor(txt)
            c2.setAlpha(170)
            p.setPen(c2)
            p.drawText(QRect(r.left() + 6, tr.bottom() + 2, r.width() - 12, fm.height()),
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), pt)
        p.restore()


# ================================================================== 手动上传对话框（design §4.10）
class UploadDialog(QDialog):
    """上传前的确认对话框：选目标文件夹（已有的云端书架或新建）、（单个文件时）可改名、明确说明不会覆盖已有文件。
    result_value()：(folder, [(本机路径, 云端文件名)])。名字 / 文件夹不合法时「上传」按钮不可用。"""
    NEW = "__new__"

    def __init__(self, items: List[Tuple[str, str]], folders: List[str], parent=None, default_folder: str = ""):
        super().__init__(parent)
        self.setWindowTitle("上传到云端书库")
        self.items = list(items)
        self.name_edit: Optional[QLineEdit] = None
        lay = QVBoxLayout(self)
        info = QLabel("将把下面的文件上传到云端书库（library/）。上传后你的其它设备和家人都能看到它。\n"
                      "云端已有同名文件时不会覆盖，会让你改名。")
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setWordWrap(True)
        lay.addWidget(info)
        if len(self.items) == 1:
            row = QHBoxLayout()
            row.addWidget(QLabel("文件名"))
            self.name_edit = QLineEdit(self.items[0][1])
            self.name_edit.textChanged.connect(lambda _t: self._validate())
            row.addWidget(self.name_edit, 1)
            lay.addLayout(row)
            src = QLabel("来自：" + self.items[0][0])
            src.setTextFormat(Qt.TextFormat.PlainText)
            src.setWordWrap(True)
            lay.addWidget(src)
        else:
            self.file_list = QListWidget()
            for path, name in self.items:
                try:
                    sz = fmt_size(os.path.getsize(path))
                except OSError:
                    sz = "?"
                self.file_list.addItem(QListWidgetItem(f"{name}    {sz}"))      # QListWidgetItem 文本按纯文本显示
            lay.addWidget(self.file_list)
        row = QHBoxLayout()
        row.addWidget(QLabel("目标文件夹"))
        self.folder_box = QComboBox()
        self.folder_box.addItem("（根目录 / 未分类）", "")
        for f in folders:
            self.folder_box.addItem(f, f)
        self.folder_box.addItem("新建文件夹…", self.NEW)
        i = self.folder_box.findData(default_folder)
        self.folder_box.setCurrentIndex(i if i >= 0 else 0)
        self.folder_box.currentIndexChanged.connect(self._on_folder_changed)
        row.addWidget(self.folder_box, 1)
        lay.addLayout(row)
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("新文件夹名（可用 / 分隔多层，如 小说/科幻）")
        self.new_edit.textChanged.connect(lambda _t: self._validate())
        self.new_edit.hide()
        lay.addWidget(self.new_edit)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("上传")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._validate()

    def _on_folder_changed(self, _i):
        self.new_edit.setVisible(self.folder_box.currentData() == self.NEW)
        self._validate()

    def folder(self) -> Optional[str]:
        d = self.folder_box.currentData()
        return ss.normalize_folder(self.new_edit.text()) if d == self.NEW else d

    def names(self) -> List[str]:
        if self.name_edit is not None:
            return [self.name_edit.text()]
        return [n for _p, n in self.items]

    def problem(self) -> str:
        if self.folder() is None:
            return "文件夹名不合法（不能含 \\ : 等字符，不能以 . 开头）"
        for n in self.names():
            if ss.upload_rel(self.folder(), n) is None:
                return f"文件名不合法或不是支持的电子书 / 有声书 / 漫画压缩包格式：{n}"
        if len({n.casefold() for n in self.names()}) != len(self.names()):
            return "有重名的文件"
        return ""

    def _validate(self):
        msg = self.problem()
        self.error.setText(msg)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not msg)

    def result_value(self):
        if self.problem():
            return None
        return self.folder(), [(p, n) for (p, _n), n in zip(self.items, self.names())]


class BatchUploadDialog(QDialog):
    """批量上传前的确认对话框：按类别（电子书 / 有声书 / 压缩包·漫画）汇总数量和大小，类别可勾选——电子书默认选中，
    音频和压缩包默认不选（书库目录里常有不相干的音乐 / 备份压缩包，云端又没有删除入口）；外部格式列出清单；
    显示跳过明细，可给云端文件夹加一个统一前缀。
    result_value()：前缀（'' = 直接用本地目录结构）；selected_kinds()：勾选的类别；前缀不合法或什么都没选时「开始上传」不可用。"""

    KIND_LABELS = (("book", "电子书"), ("audio", "有声书 / 音频"), ("archive", "压缩包 / 漫画"))
    LIST_MAX = 200

    def __init__(self, plan: "ss.BatchPlan", parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量上传到云端书库")
        self.resize(560, 480)
        lay = QVBoxLayout(self)
        self._stats: Dict[str, List[int]] = {}                        # 类别 -> [个数, 字节]
        listed: List[str] = []
        for path, folder, name in plan.items:
            try:
                sz = os.path.getsize(path)
            except OSError:
                sz = 0
            k = sh.media_kind(name) or "book"
            st = self._stats.setdefault(k, [0, 0])
            st[0] += 1
            st[1] += sz
            if k != "book":
                listed.append(f"{folder + '/' if folder else ''}{name}（{fmt_size(sz)}）")
        self.head = QLabel()
        self.head.setTextFormat(Qt.TextFormat.PlainText)
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.kind_boxes: Dict[str, QCheckBox] = {}
        for k, label in self.KIND_LABELS:
            if k not in self._stats:
                continue
            n, sz = self._stats[k]
            cb = QCheckBox(f"{label}：{n} 个，{fmt_size(sz)}" + ("" if k == "book" else "（默认不选，勾选后才会上传）"))
            cb.setChecked(k == "book")
            cb.toggled.connect(lambda _c: self._validate())
            self.kind_boxes[k] = cb
            lay.addWidget(cb)
        if listed:
            lay.addWidget(QLabel("音频 / 压缩包清单（勾选对应类别才会上传）："))
            lst = QPlainTextEdit()
            lst.setReadOnly(True)
            lst.setPlainText("\n".join(listed[:self.LIST_MAX]) +
                             (f"\n…… 还有 {len(listed) - self.LIST_MAX} 个" if len(listed) > self.LIST_MAX else ""))
            lay.addWidget(lst, 1)
        nskip = sum(len(v) for v in plan.skipped.values())
        if nskip:
            lay.addWidget(QLabel(f"已自动跳过 {nskip} 个文件："))
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setPlainText("\n".join(f"【{why}】{len(names)} 个\n    " + "\n    ".join(names[:30]) +
                                       (f"\n    …… 还有 {len(names) - 30} 个" if len(names) > 30 else "")
                                       for why, names in plan.skipped.items()))
            lay.addWidget(box, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("统一前缀文件夹（可选）"))
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("如 我的书库；留空 = 直接用本地目录结构")
        self.prefix.textChanged.connect(lambda _t: self._validate())
        row.addWidget(self.prefix, 1)
        lay.addLayout(row)
        self.error = QLabel()
        lay.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始上传")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._validate()

    def selected_kinds(self) -> Set[str]:
        return {k for k, cb in self.kind_boxes.items() if cb.isChecked()}

    def _validate(self):
        bad = ss.normalize_folder(self.prefix.text()) is None
        kinds = self.selected_kinds()
        n = sum(self._stats[k][0] for k in kinds)
        sz = sum(self._stats[k][1] for k in kinds)
        self.head.setText(f"将上传 {n} 个文件，共 {fmt_size(sz)}。\n"
                          "云端文件夹沿用本地书库的子文件夹结构；云端已有同名文件的自动跳过，不会覆盖；"
                          "失败的会最后集中列出，之后可以再点一次，只会补传没传上的。")
        self.error.setText("前缀文件夹名不合法（不能含 \\ : 等字符，不能以 . 开头）" if bad else ("请至少勾选一个类别" if not n else ""))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not bad and n > 0)

    def result_value(self) -> Optional[str]:
        return ss.normalize_folder(self.prefix.text())


# ================================================================== 书架页
class ShelfView(QWidget):
    openRequested = Signal(str)          # 要打开的本地文件路径（由 MainWindow 调 load_book）
    addLibraryRequested = Signal()       # 空状态里的「添加书库目录…」
    scanFinished = Signal(int)           # 本轮扫描得到的书数（测试 / 状态用）
    cloudSynced = Signal(int)            # 一轮云端列表已并入书架（参数：云端书数）；测试 / 状态用
    progressSynced = Signal(int)         # 一轮进度同步已并入（参数：得到的进度条数）；测试 / 状态用
    uploadFinished = Signal(object)      # 一次上传结束（shelfsync.UploadResult）
    uploadStatus = Signal(str, int)      # 上传状态文字 + 显示毫秒数（0 = 一直显示）：主窗口状态栏用，阅读页也看得到

    STALE_SECONDS = 60                   # 书架显示时，距上次扫描超过这个秒数才重新扫描
    INDEX_SAVE_MS = 5000                 # index.json 防抖

    def __init__(self, context: Optional[Callable[[], dict]] = None, root: Optional[str] = None,
                 default_view: str = "grid", default_sort: str = "last_read", parent=None,
                 cloud: Optional[ss.ShelfSyncWorker] = None, read_only: bool = False):
        """context() 在主线程调用，返回 {"library_dirs": [...], "recent": [...], "bookmarks": {...}, "cfg": {...}}
        （引用 MainWindow 的活数据；cfg 里的 webdav_* 决定云端是否启用）。
        root：书架数据根（默认 ~/.pyreader，测试传临时目录）。cloud：云端后台任务（测试可注入）。
        read_only：没拿到单实例锁（另一个 pyreader 进程在用书架）——可浏览，但不写 overlay.json / state.json、
        不写设备文件、不能改收藏 / 隐藏（design §4.5）。"""
        super().__init__(parent)
        self._context = context or (lambda: {})
        self._root = root
        self._read_only = bool(read_only)
        self._books: Dict[str, sh.Book] = {}
        self._opened: Dict[str, float] = {}
        self._progress: Dict[str, dict] = {}
        self._token = 0
        self._scanner: Optional[ScanWorker] = None
        self._last_scan: Optional[float] = None       # time.monotonic()
        self._truncated = False
        self._scan_error = ""
        self._notice = ""                             # 一次性提示（如「找不到文件」），下一轮扫描完成后清除
        self._index_dirty = False
        # ---- 云端（阶段 3）
        if not self._read_only:                       # 清掉上次异常退出留下的下载残片（只读实例不碰：另一个进程可能正在下载）
            from . import sync as _syncmod
            _syncmod.sweep_stale_parts()
        self._cloud = cloud or ss.ShelfSyncWorker(self)
        self._cloud.listed.connect(self._on_cloud_listed, Qt.ConnectionType.QueuedConnection)
        self._cloud.listFailed.connect(self._on_cloud_failed, Qt.ConnectionType.QueuedConnection)
        self._cloud.downloadedFor.connect(self._on_downloaded_for, Qt.ConnectionType.QueuedConnection)
        self._cloud.downloadFailed.connect(self._on_download_failed, Qt.ConnectionType.QueuedConnection)
        self._cloud.downloadProgress.connect(self._on_download_progress, Qt.ConnectionType.QueuedConnection)
        self._cloud.progressPulled.connect(self._on_progress_pulled, Qt.ConnectionType.QueuedConnection)
        self._cloud.progressFailed.connect(self._on_progress_failed, Qt.ConnectionType.QueuedConnection)
        self._cloud.pushed.connect(self._on_pushed, Qt.ConnectionType.QueuedConnection)   # (rev, hash, etag, 戳)
        self._cloud.pushFailed.connect(self._on_push_failed, Qt.ConnectionType.QueuedConnection)
        self._cloud.uploadDone.connect(self._on_upload_done, Qt.ConnectionType.QueuedConnection)
        self._cloud.uploadProgress.connect(self._on_upload_progress, Qt.ConnectionType.QueuedConnection)
        # ---- 阶段 4：进度摘要 / 推送 / 上传。下面三个可调用对象供测试替换（默认弹真实对话框）
        self.confirm_push: Callable[[], Optional[bool]] = self._default_confirm_push     # True 同意 / False 拒绝 / None 现在不问
        self.upload_chooser: Callable = self._default_upload_chooser                     # (items, folders) -> (folder, [(path, name)]) | None
        self.external_opener: Callable[[str], bool] = self._default_external_opener      # (路径) -> 是否交给了系统应用（测试替换，避免真启动程序）
        self.rename_chooser: Callable = self._default_rename_chooser                     # (name, folder, message) -> 新文件名 | None
        self.run_menu: Callable = lambda menu, global_pos: menu.exec(global_pos)         # 右键菜单怎么弹出（测试替换成直接选动作）
        self.batch_chooser: Callable = self._default_batch_chooser                       # (plan) -> 前缀文件夹 str | None（取消）
        self.batch_reporter: Callable = self._default_batch_reporter                     # (摘要, 失败明细 list) 批量结束后的汇报
        self._batch: Optional[dict] = None            # 批量上传进行中的统计；None = 没有批量任务
        self._overlay: Optional[dict] = None          # 本设备的覆盖层（fav / hidden / meta + progress 摘要）；主线程独占，可写
        self._disk_ov: Optional[dict] = None          # 只用来显示的覆盖层（读盘一次，不创建文件）；_overlay 建立后不再用
        self._merged: Optional[dict] = None           # 本设备 + 其它设备合并后的视图缓存（编辑 / peers 变化后置 None）
        self._unpushed: Dict[str, int] = {}           # 本设备改过、还没推送的 book_id -> 修改时的 rev（橙点）
        self._heal = False                            # 自愈：云端自己的设备文件落后 / 缺失 / 损坏，需要重推（即使内容哈希没变）
        self._reset_floor_rev = 0                     # 重置后产生的 rev 下限：更早发出的推送完成不能清掉 reset_pending
        self._clock_note = ""                         # 本机时钟与服务器相差较大时的提示
        self._overlay_dirty = False
        self._push_declined = False                   # 本次会话用户拒绝了首次上传确认
        self._push_inflight: Optional[tuple] = None   # (rev, hash)
        self._push_error = ""
        self._last_push: Optional[float] = None       # time.monotonic()
        self._progress_due = False                    # 云端列表刚并入：扫描完成后做一轮进度同步
        self._progress_busy = False
        self._progress_error = ""
        self._peers: Dict[str, dict] = {}             # 其它设备文件的校验视图（缓存 / 最近一轮）
        self._upload_queue: List[tuple] = []          # [(本机路径, 文件夹, 云端文件名)]
        self._upload_cur: Optional[tuple] = None      # (token, 路径, 文件夹, 名字)
        self._upload_seq = 0
        self._cloud_busy = False                      # 列表读取中
        self._cloud_error = ""                        # 上次读取失败的原因（离线时显示本地缓存）
        self._cloud_ok_at: Optional[float] = None     # 上次成功的时间（time.time()）
        self._cloud_tick: Optional[float] = None      # 上次发起的时间（time.monotonic()，60 秒防重复）
        self._listing: Optional[ss.Listing] = None
        self._local_files = None                      # 上一轮扫描的本地文件（云端列表变化时复用，不再遍历磁盘）
        self._local_roots: List[str] = []
        self._dl: Dict[int, tuple] = {}               # token -> (book_id, rel, open_when_done)
        self._dl_seq = 0
        self._open_token: Optional[int] = None        # 只有最新的「下载后打开」请求有资格打开；其它都是迟到 / 过期

        # ---- 本机 UI 状态（state.json）：首次（无 state.json）用 config 里的默认视图 / 排序
        fresh = not os.path.exists(sh.state_path(root))
        self._state = sh.load_state(root)
        ui = self._state["ui"]
        if fresh:
            if default_view in ("grid", "list"):
                ui["view"] = default_view
            if default_sort in sh.SORT_KEYS:
                ui["sort"] = default_sort
            try:
                if not self._read_only:
                    sh.save_state(self._state, root)      # 立刻固定下来：以后 config 默认值只在设置页改动时才再应用
            except OSError:
                pass
        self._want_shelf = ui.get("last_shelf") or "all"
        self._shelf = "all"

        # ---- index.json 缓存：先显示上次的结果，后台扫描再刷新
        self._index_readonly = sh.index_readonly(root)
        cached = sh.load_index(root)
        if cached:
            self._books, self._opened, self._progress = cached

        self._catalog = ss.CloudCatalog(ss.known_from_books(self._books.values()), self._state["cloud"]["missing"])
        self._peers = ss.load_cached_peers(root)           # 离线冷启动也能显示其它设备读到哪了
        self._merge_progress(ss.digest_progress(self._peers))
        self._unpushed = {bid: 0 for bid in self._state.get("unpushed", [])}
        self._decorate_books()                             # 缓存的书先套上收藏 / 隐藏 / 橙点（扫描完成后由 build_books 再算一遍）
        self._build_ui()
        self._cloud_timer = QTimer(self)
        self._cloud_timer.setInterval(CLOUD_TIMER_MS)
        self._cloud_timer.timeout.connect(self._on_cloud_timer)
        self._cloud_timer.start()
        self._index_timer = QTimer(self)
        self._index_timer.setSingleShot(True)
        self._index_timer.setInterval(self.INDEX_SAVE_MS)
        self._index_timer.timeout.connect(self._save_index)
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setSingleShot(True)
        self._overlay_timer.setInterval(OVERLAY_SAVE_MS)
        self._overlay_timer.timeout.connect(self._save_overlay)
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.timeout.connect(lambda: self.maybe_push())
        self.set_view_mode(ui["view"], save=False)
        i = self.sort_box.findData(ui["sort"])
        self.sort_box.setCurrentIndex(i if i >= 0 else 0)
        self.refresh()

    # ------------------------------------------------------------ 界面
    def _build_ui(self):
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索书名 / 作者 / 文件名  Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._apply_filter())
        self.sort_box = QComboBox()
        for key, label in SORT_LABELS:
            self.sort_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(self._on_sort_changed)
        self.btn_grid = QToolButton(); self.btn_grid.setText("▦"); self.btn_grid.setToolTip("网格")
        self.btn_list = QToolButton(); self.btn_list.setText("☰"); self.btn_list.setToolTip("列表")
        for b in (self.btn_grid, self.btn_list):
            b.setCheckable(True)
            b.setAutoExclusive(True)
        self.btn_grid.clicked.connect(lambda: self.set_view_mode("grid"))
        self.btn_list.clicked.connect(lambda: self.set_view_mode("list"))
        self.btn_refresh = QToolButton(); self.btn_refresh.setText("⟳"); self.btn_refresh.setToolTip("重新扫描书库并刷新云端列表")
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        top.addWidget(self.search, 1)
        top.addWidget(QLabel("排序"))
        top.addWidget(self.sort_box)
        top.addWidget(self.btn_grid)
        top.addWidget(self.btn_list)
        top.addWidget(self.btn_refresh)
        self.btn_batch_upload = QToolButton(); self.btn_batch_upload.setText("⬆"); self.btn_batch_upload.setToolTip(
            "批量上传本地书到云端（只上传云端还没有的，已有同名的自动跳过）")
        self.btn_batch_upload.clicked.connect(lambda: self.start_batch_upload())
        self.btn_batch_stop = QToolButton(); self.btn_batch_stop.setText("⏹"); self.btn_batch_stop.setToolTip("停止批量上传（当前这本传完后停止）")
        self.btn_batch_stop.clicked.connect(self.cancel_batch)
        self.btn_batch_stop.hide()
        top.addWidget(self.btn_batch_upload)
        top.addWidget(self.btn_batch_stop)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        self.sidebar.currentItemChanged.connect(self._on_sidebar_changed)

        self.model = BookModel(self)
        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setGridSize(QSize(CARD_W + 8, CARD_H + 8))
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setItemDelegate(BookDelegate(self.grid))
        self.table = QTreeView()
        self.table.setRootIsDecorated(False)
        self.table.setItemsExpandable(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        for v in (self.grid, self.table):
            v.setModel(self.model)
            v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)     # 右键菜单支持多选
            v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            v.customContextMenuRequested.connect(lambda pos, v=v: self._show_menu(v, pos))
            v.activated.connect(self._on_activated)       # 双击 / 回车
        hdr = self.table.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)

        self.empty_label = QLabel()
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_btn = QPushButton("添加书库目录…")
        self.empty_btn.clicked.connect(self.addLibraryRequested.emit)
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch(1)
        ev.addWidget(self.empty_label)
        ev.addWidget(self.empty_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        ev.addStretch(1)
        self.content = QStackedWidget()
        self.content.addWidget(self.grid)       # 0
        self.content.addWidget(self.table)      # 1
        self.content.addWidget(empty)           # 2

        body = QHBoxLayout()
        body.addWidget(self.sidebar)
        body.addWidget(self.content, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.model.show_badges = False
        self.continue_label = QLabel()
        self.continue_label.setTextFormat(Qt.TextFormat.PlainText)
        self.continue_btn = QPushButton("继续")
        self.continue_btn.clicked.connect(self._on_continue_clicked)
        self.continue_bar = QWidget()
        cb = QHBoxLayout(self.continue_bar)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.addWidget(QLabel("▶"))
        cb.addWidget(self.continue_label, 1)
        cb.addWidget(self.continue_btn)
        self.continue_bar.hide()
        self._continue_book: Optional[sh.Book] = None
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.continue_bar)
        root.addLayout(body, 1)
        root.addWidget(self.status)

    # ------------------------------------------------------------ 状态 / 偏好
    @property
    def books(self) -> Dict[str, sh.Book]:
        return self._books

    @property
    def view_mode(self) -> str:
        return self._state["ui"]["view"]

    @property
    def sort_key(self) -> str:
        return self.sort_box.currentData()

    @property
    def current_shelf(self) -> str:
        return self._shelf

    @property
    def scanning(self) -> bool:
        return self._scanner is not None

    def visible_books(self) -> List[sh.Book]:
        return self.model.books()

    def _save_ui(self):
        if self._read_only:
            return                                # 没有锁的实例不写 state.json（避免和持锁实例互相覆盖）
        self._state["unpushed"] = sorted(self._unpushed)[:sh.MAX_UNPUSHED]
        try:
            sh.save_state(self._state, self._root)
        except OSError:
            pass

    def set_view_mode(self, mode: str, save: bool = True):
        mode = mode if mode in ("grid", "list") else "grid"
        self._state["ui"]["view"] = mode
        self.btn_grid.setChecked(mode == "grid")
        self.btn_list.setChecked(mode == "list")
        self._update_content_page()
        if save:
            self._save_ui()

    def set_sort(self, key: str, save: bool = True):
        i = self.sort_box.findData(key)
        if i < 0:
            return
        self._state["ui"]["sort"] = key
        if self.sort_box.currentIndex() != i:
            self.sort_box.blockSignals(True)
            self.sort_box.setCurrentIndex(i)
            self.sort_box.blockSignals(False)
        self._apply_filter()
        if save:
            self._save_ui()

    def _on_sort_changed(self, _i):
        self.set_sort(self.sort_box.currentData())

    def set_shelf(self, key: str, save: bool = True):
        """选择侧栏条目（智能书架 key 或 local:<路径>）。不存在的 key 退回「全部」，但保留用户的选择（书架重新出现时恢复）。"""
        self._want_shelf = key
        if save:
            self._state["ui"]["last_shelf"] = key
            self._save_ui()
        self._select_sidebar(key)

    def set_query(self, text: str):
        self.search.setText(text)

    def focus_search(self):
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def handle_escape(self) -> bool:
        """Esc 在书架上：搜索框非空则清空并返回 True；否则返回 False（交给调用者）。"""
        if self.search.text():
            self.search.clear()
            return True
        return False

    def apply_defaults(self, view: str, sort: str, start_in_shelf: bool):
        """设置页改了「默认视图 / 默认排序 / 启动时显示书架」：立即应用并写入 state.json。"""
        self._state["ui"]["start_in_shelf"] = bool(start_in_shelf)
        self.set_view_mode(view, save=False)
        self.set_sort(sort, save=False)
        self._save_ui()

    def reset_cloud_state(self):
        """「重置书架状态」的覆盖层部分（design §4.7）：截断本地 overlay.json、清空 peers/ 与游标 / 推送记录，
        并（曾经向云端写过的话）登记 reset_pending —— 之后第一件事是把空的设备文件 PUT 上去覆盖云端旧内容。
        reset_pending 期间不会采纳云端自己的旧文件（见 _check_own），所以旧状态 / 旧缓存不会被「自愈」复活。
        只读实例不能重置。device_id 保持不变。"""
        if self._read_only:
            self._notice = "只读：另一个 pyreader 窗口正在使用书架，不能重置书架状态"
            return False
        self._overlay_timer.stop()
        self._push_timer.stop()
        self._index_timer.stop()
        ov_file = os.path.exists(sh.overlay_path(self._root))
        had_remote = bool(self._state.get("confirmed_upload"))
        ov = self._ensure_overlay() if (ov_file or self._overlay is not None or had_remote) else None
        if ov is not None:
            sh.reset_overlay(ov)
            self._reset_floor_rev = int(ov.get("_rev", 0))
            self._overlay_dirty = True
        else:
            self._overlay = None
            self._disk_ov = None
            self._reset_floor_rev = 0
        self._save_overlay()
        sh.clear_peers(self._root)
        self._peers = {}
        self._unpushed.clear()
        self._heal = False
        self._push_inflight = None
        st = self._state
        st["cursors"] = {"peers": {}, "rounds_since_full": 0}
        st["remote_stamp"], st["pushed_hash"], st["pushed_rev"], st["unpushed"] = None, "", 0, []
        st["reset_pending"] = had_remote
        self._cloud.forget_cursors()
        self._invalidate_merged()
        self._decorate_books()
        return True

    def reset_state(self, default_view: str, default_sort: str, start_in_shelf: bool = False):
        """设置页「重置书架状态」：本机界面偏好 + index.json 缓存 + 本设备覆盖层（overlay.json、peers/、游标，
        并把空设备文件推上云端，见 reset_cloud_state）。device_id 保持不变。"""
        ui = dict(sh.DEFAULT_UI, collapsed=[])
        if default_view in ("grid", "list"):
            ui["view"] = default_view
        if default_sort in sh.SORT_KEYS:
            ui["sort"] = default_sort
        ui["start_in_shelf"] = bool(start_in_shelf)
        self._state["ui"] = ui
        self.reset_cloud_state()                  # 先清覆盖层 / peers / 游标并登记 reset_pending，再统一落盘 state.json
        self._save_ui()
        if not self._index_readonly:
            sh.reset_index(self._root)
        self._index_timer.stop()
        self._index_dirty = False
        self._opened.clear()
        self._progress.clear()
        self._books = {}
        self._catalog = ss.CloudCatalog({}, self._state["cloud"]["missing"])     # 云端缓存是派生的；「缺失」计数保留
        self._want_shelf = "all"
        self.search.clear()
        self.set_view_mode(ui["view"], save=False)
        self.set_sort(ui["sort"], save=False)
        self.refresh()
        self.rescan(force=True)
        self.sync_cloud(force=True)
        self.maybe_push()                         # 把空设备文件推上去（云端不可达就留着 reset_pending，联网后第一件事就是推）

    # ------------------------------------------------------------ 过滤 / 刷新
    def refresh(self):
        """重建侧栏（含计数）并按当前书架 / 搜索 / 排序刷新主区。"""
        self.model.show_badges = self.cloud_enabled()
        self._rebuild_sidebar()
        self._apply_filter()
        self._update_continue()

    def _roots(self) -> List[str]:
        try:
            dirs = self._context().get("library_dirs") or []
        except Exception:
            dirs = []
        return [d for d in dirs if isinstance(d, str)]

    def _shelf_label(self, key: str) -> str:
        """local:<根>/<子文件夹> → 显示名：子文件夹相对书库根（缩进表示层级）；根目录显示根的末段。"""
        path = key[len(sh.LOCAL):]
        best = ""
        for r in self._roots():
            rr = r.replace("\\", "/").rstrip("/") or r
            if (path == rr or path.startswith(rr + "/")) and len(rr) > len(best):
                best = rr
        if not best:
            return path
        sub = path[len(best):].strip("/")
        base = best.rsplit("/", 1)[-1] or best
        if not sub:
            return base
        return "　" * sub.count("/") + "└ " + sub.rsplit("/", 1)[-1]

    @staticmethod
    def _cloud_label(key: str, counts: dict) -> str:
        """cloud:<文件夹路径> → 显示名：父文件夹也在侧栏里时缩进显示最后一段，否则显示完整相对路径。"""
        path = key[len(sh.CLOUD):]
        if "/" in path and (sh.CLOUD + path.rsplit("/", 1)[0]) in counts:
            return "　" * path.count("/") + "└ " + path.rsplit("/", 1)[-1]
        return path

    def _rebuild_sidebar(self):
        counts = count_shelves(self._books)
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        keys = []
        for key, label in SMART + tuple((k, l) for k, l in CLOUD_SMART if counts.get(k, 0) > 0):
            it = QListWidgetItem(f"{label}    {counts.get(key, 0)}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.sidebar.addItem(it)
            keys.append(key)
        cloud = sorted((k for k in counts if k.startswith(sh.CLOUD) and k != sh.CLOUD), key=lambda k: k.casefold())
        if cloud:
            hdr = QListWidgetItem("云端书库")
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            self.sidebar.addItem(hdr)
            for k in cloud:
                it = QListWidgetItem(f"{self._cloud_label(k, counts)}    {counts[k]}")
                it.setData(Qt.ItemDataRole.UserRole, k)
                it.setToolTip(plain_tip("library/" + k[len(sh.CLOUD):]))
                self.sidebar.addItem(it)
                keys.append(k)
        local = sorted((k for k in counts if k.startswith(sh.LOCAL)), key=lambda k: k.casefold())
        if local:
            hdr = QListWidgetItem("本机书库")
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            self.sidebar.addItem(hdr)
            for k in local:
                it = QListWidgetItem(f"{self._shelf_label(k)}    {counts[k]}")
                it.setData(Qt.ItemDataRole.UserRole, k)
                it.setToolTip(plain_tip(k[len(sh.LOCAL):]))
                self.sidebar.addItem(it)
                keys.append(k)
        self._shelf = self._want_shelf if self._want_shelf in keys else "all"
        for i in range(self.sidebar.count()):
            if self.sidebar.item(i).data(Qt.ItemDataRole.UserRole) == self._shelf:
                self.sidebar.setCurrentRow(i)
                break
        self.sidebar.blockSignals(False)

    def _select_sidebar(self, key: str):
        for i in range(self.sidebar.count()):
            if self.sidebar.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.sidebar.setCurrentRow(i)      # 触发 _on_sidebar_changed
                return
        self.refresh()                            # 目标书架当前不存在：按「退回全部」重建侧栏

    def _on_sidebar_changed(self, cur, _prev):
        key = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        if not key or key == self._shelf and self._want_shelf == key:
            return
        self._shelf = key
        self._want_shelf = key
        self._state["ui"]["last_shelf"] = key
        self._save_ui()
        self._apply_filter()

    def _current_view(self):
        return self.table if self.view_mode == "list" else self.grid

    def _apply_filter(self):
        shown = sh.sort_books(sh.filter_books(self._books, self._shelf, self.search.text()), self.sort_key or "last_read")
        v = self._current_view()
        cur = v.currentIndex()
        cur_bid = cur.data(BookModel.BookRole).id if cur.isValid() and isinstance(cur.data(BookModel.BookRole), sh.Book) else None
        scroll = v.verticalScrollBar().value()
        self.model.set_books(shown)
        if cur_bid:
            row = self.model.row_of(cur_bid)
            if row >= 0:
                v.setCurrentIndex(self.model.index(row, 0))
        v.verticalScrollBar().setValue(scroll)
        self._update_content_page()
        self._update_status()

    def _update_content_page(self):
        if not hasattr(self, "content"):
            return
        if self.model.rowCount() == 0:
            self.content.setCurrentIndex(2)
            self._set_empty_text()
        else:
            self.content.setCurrentIndex(1 if self.view_mode == "list" else 0)

    def _set_empty_text(self):
        if not self._books:
            if self.scanning or self._cloud_busy:
                self.empty_label.setText("正在扫描书库…" if self.scanning else "正在读取云端书库…")
                self.empty_btn.hide()
            elif self.cloud_enabled() and not self._roots():
                self.empty_label.setText("云端 library/ 里还没有书，也没有添加本机书库目录。")
                self.empty_btn.show()
            elif not self._roots():
                self.empty_label.setText("尚未添加书库目录。\n选择存放电子书的文件夹后，书会出现在这里。")
                self.empty_btn.show()
            else:
                self.empty_label.setText("书库目录中没有找到电子书。")
                self.empty_btn.hide()
        else:
            self.empty_label.setText("没有匹配的书。")
            self.empty_btn.hide()

    def _update_status(self):
        n_all = sum(1 for b in self._books.values() if not b.hidden and not b.auto_hidden)
        cloud = self.cloud_enabled()
        parts = [f"{'共' if cloud else '本机'} {n_all} 本", f"当前显示 {self.model.rowCount()} 本"]
        if cloud:
            parts.append(self.cloud_status_text())
        if self.scanning:
            parts.append("扫描中…")
        if self._truncated:
            parts.append(f"书库超过 {sh.SCAN_LIMIT} 本，只显示前 {sh.SCAN_LIMIT} 本")
        if self._scan_error:
            parts.append("扫描失败：" + self._scan_error)
        if self._read_only:
            parts.append("只读：另一个 pyreader 窗口正在使用书架（本窗口不保存收藏 / 隐藏，也不同步）")
        if self._clock_note:
            parts.append("⚠ " + self._clock_note)
        bad = self.clock_anomalies()
        if bad:
            parts.append("⚠ 设备 " + "、".join(bad[:3]) + " 的时钟异常（超前一天以上），它的收藏 / 隐藏改动已按最旧处理")
        if self._notice:
            parts.append(self._notice)
        self.status.setText(" · ".join(parts))

    # ------------------------------------------------------------ 扫描（后台线程）
    def rescan_if_stale(self) -> bool:
        if self._scanner is None and (self._last_scan is None or time.monotonic() - self._last_scan > self.STALE_SECONDS):
            return self.rescan()
        return False

    def rescan(self, force: bool = False, reuse: bool = False) -> bool:
        """后台扫描本机书库并与云端目录归并。已有扫描在跑时（且非 force）忽略；force 会作废旧扫描的结果。
        reuse=True：云端列表变了但磁盘没变，复用上一轮的本地文件，只重建（不再遍历磁盘）。"""
        if self._scanner is not None and not force:
            return False
        try:
            ctx = self._context() or {}
        except Exception:
            ctx = {}
        roots = [d for d in (ctx.get("library_dirs") or []) if isinstance(d, str)]
        remote_files, removed, removed_files = None, (), ()
        if self.cloud_enabled():
            from . import sync as syncmod
            rb = syncmod.REMOTE_BOOKS_DIR              # 缓存副本也要扫：云端移除后本机副本仍要留在书架上
            if not any(sh.norm_path(r) == sh.norm_path(rb) for r in roots):
                roots.append(rb)
            remote_files, removed, removed_files = (self._catalog.display_files(), self._catalog.removed_ids(),
                                                    self._catalog.removed_files())
        files = self._local_files if (reuse and self._local_files is not None and self._local_roots == roots) else None
        recent = [p for p in (ctx.get("recent") or []) if isinstance(p, str)]
        snap = sh.bookmark_snapshot(ctx.get("bookmarks"))
        if self._scanner is not None:
            self._scanner.stop()
        self._token += 1
        w = ScanWorker(self._token, roots, snap, recent, dict(self._progress), dict(self._opened),
                       remote_files, removed, removed_files, files, self.merged_view().get("entries"))
        w.done.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        w.failed.connect(self._on_scan_failed, Qt.ConnectionType.QueuedConnection)
        self._scanner = w
        self._scan_error = ""
        threading.Thread(target=w.run, daemon=True).start()
        self._update_status()
        if not self._books:
            self._update_content_page()
        return True

    def _on_scan_done(self, token: int, res: ScanResult):
        if token != self._token:
            return                                # 被更新的扫描作废
        if res.scanned:
            self._local_roots = list(self._scanner.roots) if self._scanner is not None else []
            self._local_files = res.files
            self._last_scan = time.monotonic()
        self._scanner = None
        self._notice = ""
        self._books = res.books
        self._truncated = res.truncated
        for bid, ts in self._opened.items():      # 扫描期间发生的打开 / 进度记录不能丢
            b = self._books.get(bid)
            if b:
                sh.note_opened(b, ts)
        for bid, rec in self._progress.items():
            b = self._books.get(bid)
            if b:
                sh.note_progress(b, rec["percent"], rec["updated"], rec.get("device", ""))
        self._decorate_books()
        self.refresh()
        self._schedule_index_save()
        self.scanFinished.emit(len(self._books))
        self._run_progress_if_due()

    def _on_scan_failed(self, token: int, msg: str):
        if token != self._token:
            return
        self._scanner = None
        self._scan_error = msg[:200]
        self._last_scan = time.monotonic()
        self._update_status()
        self._update_content_page()
        self._run_progress_if_due()

    def showEvent(self, e):
        super().showEvent(e)
        self.rescan_if_stale()
        self.sync_cloud_if_stale()
        self.maybe_push()                          # 之前因为「不在书架上不弹确认」而推迟的进度推送

    # ------------------------------------------------------------ 云端（阶段 3）：全部网络在 ShelfSyncWorker 的后台线程
    def _cfg(self) -> dict:
        try:
            cfg = (self._context() or {}).get("cfg")
        except Exception:
            cfg = None
        return cfg if isinstance(cfg, dict) else {}

    def cloud_enabled(self) -> bool:
        """有至少一个可用的书库源（主源或额外只读源）。未启用时整个云端流程都是空操作，界面上也没有云相关角标 / 提示。"""
        return bool(library_sources(self._cfg()))

    def cloud_status_text(self) -> str:
        if self._cloud_busy:
            return "云端：同步中…"
        if self._cloud_error:
            return f"云端：离线（显示本地缓存）· 上次同步失败：{self._cloud_error}（点 ⟳ 重试）"
        if self._cloud_ok_at is None:
            return "云端：尚未同步"
        s = "云端：已同步 " + time.strftime("%H:%M", time.localtime(self._cloud_ok_at))
        lst = self._listing
        if lst is not None:
            if not lst.complete:
                s += f" · ⚠ 云端列表不完整（{lst.problem()}），暂不判断「云端已移除」"
            if lst.unstable:
                s += f" · {len(lst.unstable)} 本正在上传，暂不显示"
        if self._progress_busy:
            s += " · 正在读取其它设备的进度…"
        elif self._progress_error:
            s += f" · 进度同步失败：{self._progress_error}"
        n = self.pending_count()
        if n:
            s += (f" · {n} 项待同步" if n > 0 else " · 有改动待同步") + (f"（{self._push_error}）" if self._push_error else "")
        return s

    def sync_cloud_if_stale(self) -> bool:
        return self.sync_cloud(force=False)

    def sync_cloud(self, force: bool = False) -> bool:
        """后台读取所有书库源的 library/ 并并入书架。没有任何可用源是空操作；非 force 时 60 秒内不重复。"""
        cfg = self._cfg()
        if not library_sources(cfg):
            return False
        now = time.monotonic()
        if not force and self._cloud_tick is not None and now - self._cloud_tick < CLOUD_STALE_SECONDS:
            return False
        self._cloud_tick = now
        self._cloud_busy = True
        self._progress_due = True                  # 云端可达时顺带做一轮进度同步（列表并入、扫描完成之后）
        self._cloud.refresh(cfg)
        self._update_status()
        return True

    def _on_refresh_clicked(self):
        self.rescan(force=True)
        self.sync_cloud(force=True)

    def _on_cloud_timer(self):
        if self.isVisible():
            self.sync_cloud(force=True)

    def _on_cloud_listed(self, lst):
        self._cloud_busy = False
        if not self.cloud_enabled():
            return
        self._cloud_error = ""
        self._listing = lst
        self._cloud_ok_at = time.time()
        self._catalog.apply(lst, ss.extra_candidates(self._books.values()))
        self._state["cloud"] = self._catalog.to_state()
        self._save_ui()
        # 刚扫过磁盘（几秒内）就只重建；否则连磁盘一起重扫（后台线程，成本很小），以免错过本机的新缓存副本
        recent = self._last_scan is not None and time.monotonic() - self._last_scan < 5
        self.rescan(force=True, reuse=recent)
        self.cloudSynced.emit(len(lst.files))

    def _on_cloud_failed(self, msg: str):
        """离线 / 认证失败 / 服务器错误：保留本地缓存（书、索引、已移除计数都不动），只在状态条报告。"""
        self._cloud_busy = False
        self._progress_due = False
        self._cloud_error = (msg or "未知错误")[:120]
        self._update_status()
        self._update_content_page()

    def request_download(self, b: sh.Book, open_when_done: bool = False) -> bool:
        """后台下载云端书到 remote_books/。open_when_done：完成后打开（仅当这个请求仍是最新的打开意图、
        书架仍在前台时才会打开：迟到 / 过期的完成事件只缓存文件，不会劫持当前窗口）。"""
        cfg = self._cfg()
        if not library_sources(cfg):
            self._notice = "未启用云同步，无法下载"
            self._update_status()
            return False
        if not b.remote_rel or b.removed:
            self._notice = f"云端已移除，无法下载：{b.title}" if b.removed else f"这本书不在云端：{b.title}"
            self._update_status()
            return False
        from . import sync as syncmod
        lim = syncmod.max_book_bytes(cfg)
        if b.size > lim:                              # 先在本地拦下：别发请求再拿一条以字节计的 WebDavError
            self._notice = f"{b.title}（{fmt_size(b.size)}）{ss.limit_text(lim)}"
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        self._dl_seq += 1
        tok = self._dl_seq
        self._dl[tok] = (b.id, b.remote_rel, bool(open_when_done))
        if open_when_done:
            self._open_token = tok
        self.model.set_busy({v[0] for v in self._dl.values()})
        self._notice = f"正在下载：{b.title}"
        self._update_status()
        self._cloud.download(cfg, b.remote_rel, b.size, open_when_done, tok)
        return True

    def _on_download_progress(self, tok, done, total):
        ent = self._dl.get(tok)
        if ent is None or total <= 0:
            return                                # 迟到的进度（该次下载已结束）直接丢弃
        b = self._books.get(ent[0])
        name = b.title if b is not None else ent[1].rsplit("/", 1)[-1]
        mb = 1024 * 1024
        self._notice = f"正在下载：{name} {min(100, done * 100 // total)}%（{done / mb:.1f}/{total / mb:.1f} MB）"
        self._update_status()

    def _on_downloaded_for(self, tok, path: str):
        ent = self._dl.pop(tok, None)
        if ent is None:
            return                                # 不是本视图发起的请求
        self.model.set_busy({v[0] for v in self._dl.values()})
        ext = sh.is_external(path)
        # 外部格式交给系统应用：不切页、不抢阅读窗口，所以不要求「仍是最新意图 / 书架在前台」——GB 级文件下几分钟，
        # 用户多半已经切走，不能下完了什么都不发生。可读格式仍只让最新的请求打开（别劫持当前窗口）。
        want = ent[2] and (ext or tok == self._open_token)
        if tok == self._open_token:
            self._open_token = None
        self._notice = ""
        self.rescan(force=True)                   # 新文件落地：重新归并（仅云端 → 已下载）
        if want and (ext or self.isVisible()) and os.path.isfile(path):
            self.launch_file(path)
        elif ent[2]:                              # 想打开但没打开（过期 / 不在前台）：告诉用户文件已经在本机
            self.uploadStatus.emit(f"已下载：{os.path.basename(path)}（在书架里双击打开）", 8000)

    def _default_external_opener(self, path: str) -> bool:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))

    def launch_file(self, path: str) -> bool:
        """打开一个本地文件的唯一出口：阅读器能读的、应用内媒体（音频 / .zip / .cbz）发 openRequested（MainWindow 按类别分发）；
        外部格式（.cbr / .rar / .7z）交给系统默认应用，不切页、不 note_opened、不记进度。"""
        if not sh.is_external(path):
            self.openRequested.emit(path)
            return True
        return self.open_external(path)

    def open_external(self, path: str) -> bool:
        """交给系统默认应用。只启动媒体扩展名（MEDIA_EXTS）：这里不能变成任意文件启动器。
        应用内媒体打不开（没有音频设备 / 不是漫画的 .zip）时 MainWindow 也走这里作兜底。"""
        if not path.lower().endswith(sh.MEDIA_EXTS):
            return False
        name = os.path.basename(path)
        try:
            ok = bool(self.external_opener(path))
        except Exception:
            ok = False
        self._notice = "" if ok else f"系统里没有能打开 {os.path.splitext(name)[1]} 的应用，文件在：{path}"
        self._update_status()
        return ok

    def _on_download_failed(self, tok, msg: str):
        ent = self._dl.pop(tok, None)
        if ent is None:
            return
        self.model.set_busy({v[0] for v in self._dl.values()})
        if tok == self._open_token:
            self._open_token = None
        self._notice = msg
        self._update_status()

    # ------------------------------------------------------------ 来自阅读页的记录（主线程）
    @staticmethod
    def _book_id(path: str) -> Optional[str]:
        try:
            from . import sync
            return sync.book_id(path)
        except Exception:
            return None

    def note_opened(self, path: str, ts: Optional[float] = None):
        """阅读页打开了一本书：记 last_opened（只存本机索引，不创建覆盖层条目）。"""
        self._open_token = None                   # 用户已经打开了别的书：还没完成的「下载后打开」不得再抢窗口
        if sh.is_external(path):
            return                                # 外部格式不进 recent / 索引（正常路径到不了这里，防御）
        bid = self._book_id(path)
        if not bid or not sh.valid_book_id(bid):
            return
        ts = float(ts if ts is not None else time.time())
        self._opened[bid] = max(self._opened.get(bid, 0.0), ts)
        b = self._books.get(bid)
        if b:
            sh.note_opened(b, ts)
            self.refresh()
        self._schedule_index_save()

    def note_progress(self, path: str, percent: float, updated: Optional[float] = None, device: str = "",
                      local: bool = True):
        """阅读页保存了进度：记百分比（书签里只有字符偏移，没有百分比；已读完 / 进度条靠这条）。
        local=True（默认）：这是本设备上的阅读事件，同时写进本设备设备文件的进度摘要（启用云同步时）；
        local=False：只是把别的设备的进度应用到本机，不当作本设备读过的记录写进摘要。"""
        if sh.is_external(path):
            return                                # 外部格式不记进度（防御）
        bid = self._book_id(path)
        if not bid or not sh.valid_book_id(bid):
            return
        u = float(updated if updated is not None else time.time())
        p = sh._num(percent)
        if p is None or p < 0:
            return
        old = self._progress.get(bid)
        if old and old["updated"] > u:
            return
        self._progress[bid] = {"percent": min(100.0, p), "updated": u, "device": str(device or "")[:64]}
        b = self._books.get(bid)
        if b and sh.note_progress(b, p, u, device):
            self.refresh()
        self._schedule_index_save()
        if local:
            self._record_digest(bid, p, u)

    # ------------------------------------------------------------ index.json（主线程写，防抖）
    def _schedule_index_save(self):
        self._index_dirty = True
        self._index_timer.start()

    def _save_index(self):
        self._index_timer.stop()
        if not self._index_dirty or self._index_readonly:
            return
        self._index_dirty = False
        try:
            sh.save_index(self._books, self._opened, self._progress, self._root)
        except OSError:
            self._index_dirty = True

    def flush(self):
        """关闭窗口 / 退出前：立刻落盘未保存的索引、覆盖层与 UI 状态。"""
        self._save_index()
        self._save_overlay()
        self._save_ui()

    def flush_cloud(self, timeout: float = 3.0) -> bool:
        """关闭窗口时的有界 flush（design §4.2）：本设备覆盖层有未推送的改动、且用户之前已确认过上传，就在后台线程推一次，
        最多等 timeout 秒（只 join 一次）。这里绝不弹确认框；没确认过 / 只读 / 未启用云同步 / 没有待推送内容都是空操作。
        返回是否已无待推送内容。"""
        if self._read_only or not self.cloud_enabled() or not self._state.get("confirmed_upload"):
            return True
        self._overlay_timer.stop()
        self._push_timer.stop()
        if self.push_pending():
            self.maybe_push()
            self._cloud.join_push(timeout)
            QCoreApplication.processEvents()      # 投递排队中的 pushed 信号：把 pushed_hash / remote_stamp 记下来
        self.flush()
        return not self.push_pending()

    # ------------------------------------------------------------ 进度摘要（阶段 4）：本设备写、其它设备读
    def _sync_user(self) -> str:
        return self._cfg().get("sync_user_name") or "default"

    def _ensure_overlay(self) -> Optional[dict]:
        """本设备覆盖层（懒加载，主线程独占，可写）。只读实例 / 启用云同步但用户名不合法 / 更高主版本的只读文件 -> None。
        未启用云同步时也可用（收藏 / 隐藏只存本机 overlay.json，不发任何请求）。"""
        if self._read_only:
            return None
        if self.cloud_enabled() and ss.check_user(self._sync_user()) is None:
            return None
        if self._overlay is None:
            self._overlay = sh.load_overlay(self._root, self._state["device_id"], user=self._sync_user())
            self._disk_ov = None
        return None if self._overlay.get("_readonly") else self._overlay

    # ------------------------------------------------------------ 覆盖层视图（本设备 + 其它设备合并）
    def _own_view(self) -> dict:
        """用来显示的本设备覆盖层：可写的内存对象优先；还没有就读盘一次（不创建任何文件）。"""
        if self._overlay is not None:
            return self._overlay
        if self._disk_ov is None:
            try:
                self._disk_ov = sh.load_overlay(self._root, self._state["device_id"], user=self._sync_user())
            except Exception:
                self._disk_ov = sh.new_overlay(self._state["device_id"])
        return self._disk_ov

    def merged_view(self) -> dict:
        """本设备覆盖层 + peers/ 里其它设备的覆盖层，按 shelf.merge 合并（校验在 merge 里做）。"""
        if self._merged is None:
            try:
                self._merged = sh.merge(self._own_view(), *[v for _d, v in sorted(self._peers.items())])
            except Exception:
                self._merged = {"version": sh.WIRE_VERSION, "_view": True, "entries": {}, "progress": {}, "_clock": []}
        return self._merged

    def _invalidate_merged(self):
        self._merged = None

    def _decorate_books(self):
        """把合并视图里的 fav / hidden 与「未推送」标记就地套到当前的书上（不重建、不碰磁盘）。"""
        sh.apply_flags(self._books, self.merged_view())
        for b in self._books.values():
            b.unpushed = b.id in self._unpushed

    def clock_anomalies(self) -> List[str]:
        """合并时发现时间戳异常（超前 24 小时以上）的设备。"""
        return list(self.merged_view().get("_clock") or [])

    def _note_skew(self, ms: int):
        """可选增强（design §4.6）：服务器 Date 头与本机时钟相差超过 2 分钟才采信，用来修正写入的时间戳并提示用户。"""
        if abs(ms) >= SKEW_NOTICE_MS:
            ms = max(-sh.FUTURE_LIMIT_MS, min(sh.FUTURE_LIMIT_MS, int(ms)))
            self._clock_note = f"本机时钟与服务器相差约 {abs(ms) // 60000} 分钟，请开启系统自动对时"
        else:
            ms, self._clock_note = 0, ""
        if self._state.get("clock_skew_ms") != ms:
            self._state["clock_skew_ms"] = ms
            self._save_ui()
        self._update_status()

    # ------------------------------------------------------------ 收藏 / 隐藏（本设备编辑）
    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_flag(self, books, name: str, value: bool) -> int:
        """把 books 的 fav / hidden 设为 value（写本设备覆盖层，混合逻辑时钟；立即生效，防抖落盘、防抖推送）。
        返回实际改动的书数。只读实例 / 用户名不合法时不改，并在状态条提示。绝不创建覆盖层条目以外的东西、不读写书本身。"""
        if name not in sh.REG_FIELDS:
            raise ValueError(f"unknown field {name!r}")
        if self._read_only:
            self._notice = "只读：另一个 pyreader 窗口正在使用书架，不能修改收藏 / 隐藏"
            self._update_status()
            return 0
        ov = self._ensure_overlay()
        if ov is None:
            self._notice = "同步用户名不合法，收藏 / 隐藏无法保存" if self.cloud_enabled() else "书架状态文件不可写，收藏 / 隐藏无法保存"
            self._update_status()
            return 0
        merged = self.merged_view()
        skew = int(self._state.get("clock_skew_ms") or 0)
        n = 0
        cloud = self.cloud_enabled()
        for b in books:
            if not isinstance(b, sh.Book) or not sh.valid_book_id(b.id):
                continue
            if bool(sh.field_value(merged, b.id, name, False)) == bool(value):
                continue
            try:
                sh.set_field(ov, b.id, name, bool(value), merged=merged, skew_ms=skew, meta=self._meta_for(b))
            except ValueError:
                continue                                  # 条目数超限等：跳过这一本
            if cloud:
                self._unpushed[b.id] = int(ov.get("_rev", 0))
            n += 1
        if not n:
            return 0
        self._invalidate_merged()
        self._decorate_books()
        self._overlay_dirty = True
        self._overlay_timer.start()
        if cloud:
            self._schedule_push(urgent=True)
        self.refresh()
        self._update_status()
        return n

    @staticmethod
    def _meta_for(b: sh.Book) -> Optional[dict]:
        """随编辑一起写进覆盖层的 meta（让别的设备能给这本书画占位卡片）：书名取自文件名，而不是 b.title ——
        b.title 可能已被别的设备的 meta 覆盖，不能把对端给的字符串原样再当成自己的写出去。只有本地文件名可知的书才写。"""
        if b.placeholder and not b.remote_rel:
            return None
        return {"fp": b.fp, "title": sh._stem(b.filename)}

    def set_fav(self, books, value: bool = True) -> int:
        return self.set_flag(books, "fav", value)

    def set_hidden(self, books, value: bool = True) -> int:
        return self.set_flag(books, "hidden", value)

    def pending_count(self) -> int:
        """「N 项待同步」：本设备改过、还没推送的书数；有待推送内容但不知道是哪些书（重启后丢了标记 / 自愈重推）时返回 -1。"""
        if not self.push_pending():
            return 0
        return len(self._unpushed) or -1

    def _merge_progress(self, recs: Dict[str, dict]) -> bool:
        """把别的设备 / 逐书补缺得到的进度并入 self._progress：updated 大者胜（本设备更新的读数不会被旧记录盖掉）。"""
        changed = False
        for bid, rec in (recs or {}).items():
            if not sh.valid_book_id(bid) or not isinstance(rec, dict):
                continue
            p, u = sh._num(rec.get("percent")), sh._num(rec.get("updated"))
            if p is None or u is None or p < 0 or u <= 0:
                continue
            old = self._progress.get(bid)
            if old is None or u > old["updated"]:
                self._progress[bid] = {"percent": min(100.0, p), "updated": u, "device": str(rec.get("device") or "")[:64]}
                changed = True
        return changed

    def _record_digest(self, bid: str, percent: float, updated: float):
        """本设备读到了哪：写进覆盖层的 progress 摘要（内存，主线程）；防抖落盘 overlay.json、防抖推送设备文件。
        未启用云同步是空操作（不创建任何书架文件）。"""
        if not self.cloud_enabled() or self._read_only:
            return
        ov = self._ensure_overlay()
        if ov is None:
            self._notice = "同步用户名不合法，阅读进度不会同步到其它设备"
            self._update_status()
            return
        try:
            sh.set_progress(ov, bid, percent, updated)
            sh.prune_progress(ov)
        except ValueError:
            return
        self._overlay_dirty = True
        self._overlay_timer.start()
        self._schedule_push()

    def _save_overlay(self):
        """原子写 overlay.json（内容 = to_wire，同时就是上传内容）。"""
        self._overlay_timer.stop()
        if not self._overlay_dirty or self._overlay is None:
            return
        self._overlay_dirty = False
        try:
            if not sh.save_overlay(self._overlay, self._root):
                return
        except (OSError, ValueError):
            self._overlay_dirty = True

    def push_pending(self) -> bool:
        """本设备覆盖层有还没推送的内容：内容哈希与上次确认推送的不同；或重置后还没把空文件推上去（reset_pending）；
        或自愈发现云端自己的设备文件落后 / 缺失 / 损坏（_heal）。只读实例 / 未启用云同步永远是 False。"""
        if self._read_only or not self.cloud_enabled():
            return False
        if self._state.get("reset_pending") or self._heal:
            return self._ensure_overlay() is not None
        ov = self._overlay
        if ov is None or ov.get("_readonly"):
            return False
        if not ov.get("progress") and not ov.get("entries") and not self._state.get("pushed_hash"):
            return False                              # 从没写过任何东西：不需要（也不应该）创建空的设备文件
        try:
            return sh.wire_hash(ov) != self._state.get("pushed_hash", "")
        except ValueError:
            return False

    def _schedule_push(self, urgent: bool = False):
        """防抖推送。阅读进度事件之间至少隔 PUSH_MIN_INTERVAL_MS；用户亲手改的收藏 / 隐藏（urgent）只防抖 5 秒。"""
        if self._push_timer.isActive():
            return
        delay = PUSH_DEBOUNCE_MS
        if self._last_push is not None and not urgent:
            delay = max(delay, PUSH_MIN_INTERVAL_MS - int((time.monotonic() - self._last_push) * 1000))
        self._push_timer.start(delay)

    def _default_confirm_push(self) -> Optional[bool]:
        """首次向云端写书架数据前的确认（design §5）。书架不在前台时不打扰（返回 None：以后再问）。"""
        if not self.isVisible():
            return None
        r = QMessageBox.question(self, "同步书架状态", CONFIRM_TEXT, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.No)
        return r == QMessageBox.StandardButton.Yes

    def _push_allowed(self) -> bool:
        if self._state.get("confirmed_upload"):
            return True
        if self._push_declined:
            return False
        ans = self.confirm_push()
        if ans is None:
            return False                              # 现在不问：保持待推送
        if not ans:
            self._push_declined = True                # 本次会话不再问；进度仍只存本机
            return False
        self._state["confirmed_upload"] = True
        self._save_ui()
        return True

    def maybe_push(self, force: bool = False) -> bool:
        """有待上传的摘要就推送设备文件（后台线程）。未启用云同步 / 没有待推送内容 / 已有同内容在推送 / 用户没确认：返回 False。
        首次写入前必须经过确认对话框（confirmed_upload 记在 state.json）。"""
        self._push_timer.stop()
        if not self.cloud_enabled() or self._ensure_overlay() is None or not self.push_pending():
            return False
        ov = self._overlay
        h = sh.wire_hash(ov)
        if self._push_inflight is not None and self._push_inflight[1] == h:
            return False
        if not self._push_allowed():
            return False
        self._save_overlay()
        payload, rev = sh.wire_bytes(ov), int(ov.get("_rev", 0))
        self._push_inflight = (rev, h)
        self._last_push = time.monotonic()
        self._cloud.push_device(self._cfg(), self._state["device_id"], payload, rev, h)
        self._update_status()
        return True

    def _on_pushed(self, rev: int, digest: str, etag: str, stamp=None):
        """设备文件已推送（且读回比对通过）。pushed_hash / remote_stamp 记的是「服务器上现在是什么」，永远如实记录；
        而「本地还有没有待推送的内容」由当前内容哈希与 pushed_hash 的比较决定（pending 判定不依赖 rev）：
        推送期间发生的新编辑 / 新阅读事件使当前哈希 != 刚推送的哈希，所以仍是待推送，并再排一轮。
        pushed_rev 与「已推送的书」标记只覆盖 rev <= 发出时 rev 的修改（design §4.3 推送与自愈 2）。"""
        self._push_error = ""
        if self._push_inflight is not None and self._push_inflight[1] == digest:
            self._push_inflight = None                # 先到的旧内容的完成通知不能清掉更新内容的「在途」标记
        self._state["pushed_hash"] = digest
        if int((self._overlay or {}).get("_rev", 0)) == int(rev):
            self._state["pushed_rev"] = int(rev)      # 只有当前 rev 仍等于发出时的 rev，才说「到 rev 为止的都推上去了」
        if isinstance(stamp, dict):
            self._state["remote_stamp"] = sh._clean_stamp(stamp)
        for bid in [b for b, r in self._unpushed.items() if r <= int(rev)]:
            del self._unpushed[bid]                   # 推送期间又改的（rev 更大）保留橙点
        if int(rev) >= self._reset_floor_rev:
            self._state["reset_pending"] = False      # 重置之后产生的内容已经推上去了（之前发出的在途推送不算）
        self._heal = False
        self._decorate_books()
        self._save_ui()
        if self.push_pending():                       # 推送期间又有新的阅读事件：不能当作已清空，再推一轮
            self._schedule_push(urgent=bool(self._unpushed))
        self.refresh()
        self._update_status()

    def _on_push_failed(self, msg: str):
        self._push_inflight = None
        self._push_error = (msg or "未知错误")[:80]
        self._update_status()

    # ------------------------------------------------------------ 进度同步（阶段 4）：其它设备的摘要 + 逐书补缺
    def progress_wanted(self) -> List[str]:
        """还没有任何进度信息的书（不在任何设备摘要里）：先试最近读过的，再按书名；其它设备的占位 / 已移除且无副本的不探测。"""
        out = []
        for b in sorted(self._books.values(), key=lambda b: (-b.last_read, b.title.casefold(), b.id)):
            if b.percent is None and b.readable and not b.placeholder and not (b.removed and not b.local_paths) and sh.valid_book_id(b.id):
                out.append(b.id)
        return out

    def _run_progress_if_due(self):
        if self._progress_due:
            self._progress_due = False
            self.sync_progress()

    def sync_progress(self, force_full: bool = False) -> bool:
        """后台读其它设备的进度摘要（游标 + racy 规则）并给摘要里没有的书逐本补缺。未启用云同步是空操作。"""
        cfg = self._cfg()
        if client_from_cfg(cfg) is None:
            return False
        self._progress_busy = True
        own = None
        if not self._read_only:                       # 只读实例完全不碰自己的设备文件
            ov = self._ensure_overlay()
            if ov is not None:
                own = {"stamp": self._state.get("remote_stamp"), "has_content": sh.has_content(ov)}
        self._cloud.sync_progress(cfg, self._root, self._state["device_id"], self._state["cursors"],
                                  self.progress_wanted(), force_full, own=own)
        self._update_status()
        return True

    def _check_own(self, own):
        """自愈（design §4.3）：根据后台对「本设备自己的云端文件」的核对结果决定采纳 / 重推。主线程独占覆盖层，所以在这里做。
        重置之后（reset_pending）一律不采纳云端旧内容：先把空文件推上去覆盖，旧状态不会复活。"""
        ov = self._ensure_overlay()
        if ov is None or own.failed or self._state.get("reset_pending"):
            return
        has = sh.has_content(ov)
        if own.fetched and own.corrupt:
            self._heal = has                          # 损坏的云端文件：有内容就覆盖修复
        elif own.fetched and own.remote is not None:
            adopted, behind = sh.reconcile_own(ov, own.remote)
            if adopted is not None:                   # 云端有本地没有的更新（例如 device_id 被克隆）：并进本地
                ov["entries"], ov["progress"], ov["_clock"] = adopted["entries"], adopted["progress"], adopted["_clock"]
                ov["_rev"] = int(ov.get("_rev", 0)) + 1
                self._overlay_dirty = True
                self._overlay_timer.start()
                self._invalidate_merged()
                self._notice = "已把云端的更新并入本设备的书架状态（可能有另一台设备用了同一个设备 ID）"
            if behind:
                self._heal = True                     # 云端缺少 / 落后于本地：重推（服务器回滚、被覆盖的旧内容）
            else:
                self._heal = False
                if own.stamp:
                    self._state["remote_stamp"] = sh._clean_stamp(own.stamp)    # 已核对一致：下一轮戳没变就不必再 GET
        elif own.missing:
            self._heal = True                         # 自己的设备文件被删了而本地有内容：重推

    def _on_progress_pulled(self, res):
        self._progress_busy = False
        self._progress_error = ""
        if not self.cloud_enabled():
            return
        old_entries = self.merged_view().get("entries")
        self._peers = res.peers.views
        self._invalidate_merged()
        self._state["cursors"] = res.cursors
        if res.peers.skew_ms is not None and not self._read_only:
            self._note_skew(res.peers.skew_ms)
        if res.peers.own is not None and not self._read_only:
            self._check_own(res.peers.own)
        self._save_ui()
        changed = self._merge_progress(res.digest)
        changed = self._merge_progress(res.found) or changed
        new_entries = self.merged_view().get("entries")
        if new_entries != old_entries:                # 其它设备改了收藏 / 隐藏：就地更新、并让下一次重建带上新条目
            self._decorate_books()
            changed = True
        if changed:
            self._schedule_index_save()
            self.rescan(force=True, reuse=True)       # 只重建（不遍历磁盘）：本地书、云端书、local: 别名下的进度一并对上
        self.refresh()
        self.progressSynced.emit(len(res.digest) + len(res.found))
        self.maybe_push()                             # 云端刚确认可达：把之前（离线 / 重启前 / 自愈发现）没推上去的内容补推

    def _on_progress_failed(self, msg: str):
        self._progress_busy = False
        self._progress_error = (msg or "未知错误")[:80]
        self._update_status()

    # ------------------------------------------------------------ 「继续阅读」条
    def _local_device_names(self) -> Set[str]:
        from . import sync as syncmod
        return {n for n in (self._cfg().get("device_name"), syncmod.default_device_name()) if n}

    def _update_continue(self):
        b = sh.continue_candidate(self._books)
        self._continue_book = b
        if b is None:
            self.continue_bar.hide()
            return
        self.continue_label.setText(continue_text(b, self._local_device_names()))
        self.continue_bar.show()

    def _on_continue_clicked(self):
        b = self._continue_book
        if b is not None and b.id in self._books:
            self.open_book(b)             # 远端较新则静默跳转 + 状态栏提示：由 load_book 里已有的 sync.pull 流程负责

    # ------------------------------------------------------------ 手动上传（阶段 4，design §4.10）：只由用户操作触发
    def _cloud_folders(self) -> List[str]:
        return sorted({b.shelf[len(sh.CLOUD):] for b in self._books.values()
                       if b.shelf.startswith(sh.CLOUD) and b.shelf != sh.CLOUD}, key=lambda f: f.casefold())

    def _default_upload_chooser(self, items, folders):
        dlg = UploadDialog(items, folders, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.result_value()

    def _default_rename_chooser(self, name: str, folder: str, message: str):
        text = suggest_name(name)
        while True:
            text, ok = QInputDialog.getText(self, "云端已有同名文件", message + "\n不会覆盖它。请输入新的文件名：",
                                            QLineEdit.EchoMode.Normal, text)
            if not ok:
                return None
            if ss.upload_rel(folder, text) is not None:
                return text
            QMessageBox.warning(self, "文件名不合法", "文件名不合法或不是支持的电子书 / 有声书 / 漫画压缩包格式，请重新输入。")

    def start_upload(self, paths) -> bool:
        """上传本机的书到云端书库（弹确认对话框 -> 选文件夹 / 改名 -> 后台上传）。绝不自动调用、绝不覆盖云端已有文件。
        已经是云端缓存副本（remote_books/ 下）的文件、不存在的文件、不支持的格式 / 名字会被跳过并提示。"""
        if not self.cloud_enabled():
            self._notice = "未启用云同步，无法上传"
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        if self._batch is not None:
            self._notice = "批量上传进行中，请等它结束（或先点 ⏹ 停止）再单独上传"
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        from . import sync as syncmod
        items, skipped = [], []
        for p in paths or ():
            if not isinstance(p, str) or not os.path.isfile(p):
                continue
            name = os.path.basename(p)
            if syncmod._remote_rel(p) is not None:
                skipped.append(f"{name}（已经在云端）")
            elif ss.upload_rel("", name) is None:
                skipped.append(f"{name}（不是支持的电子书 / 有声书 / 漫画压缩包格式或文件名不合法）")
            elif os.path.getsize(p) > syncmod.max_book_bytes(self._cfg()):
                skipped.append(f"{name}（{fmt_size(os.path.getsize(p))}，{ss.limit_text(syncmod.max_book_bytes(self._cfg()))}）")
            else:
                items.append((p, name))
        if not items:
            self._notice = "没有可上传的文件：" + "；".join(skipped) if skipped else "没有可上传的文件"
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        choice = self.upload_chooser(items, self._cloud_folders())
        if not choice:
            return False
        folder, picked = choice
        if ss.normalize_folder(folder) is None:
            return False
        for path, name in picked:
            self._upload_queue.append((path, ss.normalize_folder(folder), name))
        self._notice = "；".join(skipped)
        self._pump_uploads()
        return True

    def _pump_uploads(self):
        if self._upload_cur is not None or not self._upload_queue:
            return
        path, folder, name = self._upload_queue.pop(0)
        self._upload_seq += 1
        tok = self._upload_seq
        self._upload_cur = (tok, path, folder, name)
        self._notice = f"{self._batch_prefix()}正在上传：{name}"
        self._update_status()
        self.uploadStatus.emit(self._notice, 0)
        if self._cloud.upload(self._cfg(), path, folder, name, tok) is None:
            self._upload_cur = None
            self._upload_queue.clear()
            self._notice = "未启用云同步，无法上传"
            if self._batch is not None:
                self._finish_batch()
                return
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)

    def _on_upload_progress(self, tok, sent, total):
        cur = self._upload_cur
        if cur is None or cur[0] != tok or total <= 0:
            return                                     # 迟到的进度（该次上传已结束）直接丢弃
        mb = 1024 * 1024
        queued = f"（队列里还有 {len(self._upload_queue)} 本）" if self._upload_queue else ""
        if self._batch is not None:
            queued = ""                                # 批量时前缀里已经有 第几本 / 共几本
        self._notice = (f"{self._batch_prefix()}正在上传：{cur[3]} {min(100, sent * 100 // total)}%"
                        f"（{sent / mb:.1f}/{total / mb:.1f} MB）{queued}")
        self._update_status()
        self.uploadStatus.emit(self._notice, 0)

    def _on_upload_done(self, tok, res):
        cur = self._upload_cur
        if cur is None or cur[0] != tok:
            return
        _t, path, folder, name = cur
        self._upload_cur = None
        if self._batch is not None:
            self._on_batch_item_done(path, name, res)
            return
        if res.status == "ok":
            self._notice = res.message + "（其它设备约 1 分钟后能看到）"
            self.sync_cloud(force=True)
        elif res.status == "exists":
            new = self.rename_chooser(name, folder, res.message)
            if new and new != name and ss.upload_rel(folder, new) is not None:
                self._upload_queue.insert(0, (path, folder, new))
                self._notice = f"云端已有同名文件，改名为 {new} 重试"
            else:
                self._notice = f"已跳过（云端已有同名文件，未覆盖）：{name}"
        else:
            self._notice = res.message or "上传失败"
        self._update_status()
        self.uploadStatus.emit(self._notice, 0 if self._upload_queue else 15000)   # 队列里还有书：马上被下一条「正在上传」盖掉
        self.uploadFinished.emit(res)
        self._pump_uploads()

    # ------------------------------------------------------------ 批量上传：同一条队列，但「云端已有」直接跳过、不弹改名
    BATCH_MAX_CONSECUTIVE_ERRORS = 5     # 连续失败这么多本就停（服务器挂了 / 断网时不要把上千本都试一遍）

    def _default_batch_chooser(self, plan):
        dlg = BatchUploadDialog(plan, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        prefix = dlg.result_value()
        return None if prefix is None else (prefix, dlg.selected_kinds())     # 带上勾选的类别；测试里的假 chooser 只返回前缀字符串也行

    def _default_batch_reporter(self, summary: str, failed: List[str]):
        box = QMessageBox(QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information, "批量上传结束", summary, parent=self)
        if failed:
            box.setDetailedText("\n".join(failed))
        box.exec()

    def _batch_prefix(self) -> str:
        b = self._batch
        return f"批量上传 {min(b['done'] + 1, b['total'])}/{b['total']} · " if b is not None else ""

    def start_batch_upload(self, paths=None) -> bool:
        """把本机书库里云端还没有的书批量上传到云端（保持子文件夹结构）。paths = 只上传这些文件（多选右键），None = 全部。
        只由用户操作触发；已有同名文件一律跳过，绝不覆盖；上传完成后只刷新一次云端列表。"""
        if not self.cloud_enabled():
            self._notice = "未启用云同步，无法上传"
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        if self._batch is not None or self._upload_cur is not None or self._upload_queue:
            self._notice = "已经有上传在进行，请等它结束再开始批量上传"
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        from . import sync as syncmod
        want = {sh.norm_path(p) for p in paths} if paths is not None else None
        files = [lf for lf in (self._local_files or []) if want is None or sh.norm_path(lf.path) in want]
        cloud_rels = [b.remote_rel for b in self._books.values() if b.remote_rel and not b.removed]
        max_bytes = syncmod.max_book_bytes(self._cfg())
        is_cached = lambda p: syncmod._remote_rel(p) is not None
        plan = ss.plan_batch_upload(files, cloud_rels, max_bytes, "", is_cached)
        if not plan.items:
            self._notice = ("没有需要上传的书：" + "；".join(f"{why} {len(n)} 个" for why, n in plan.skipped.items())
                            if plan.skipped else "没有需要上传的书（本机书库为空，或还没扫描完）")
            self._update_status()
            self.uploadStatus.emit(self._notice, 8000)
            return False
        choice = self.batch_chooser(plan)
        if choice is None:
            return False
        prefix, kinds = choice if isinstance(choice, tuple) else (choice, None)          # kinds None = 不限类别
        if kinds is not None:
            files = [lf for lf in files if (sh.media_kind(lf.name) or "book") in kinds]
        if prefix or kinds is not None:
            plan = ss.plan_batch_upload(files, cloud_rels, max_bytes, prefix, is_cached)   # 前缀 / 类别变了，计划要重算（「云端已有」按新位置）
            if not plan.items:
                return False
        self._batch = {"total": len(plan.items), "done": 0, "ok": 0, "exists": 0, "bytes": 0, "failed": [],
                       "errs": 0, "cancelled": False, "aborted": False, "skipped": sum(map(len, plan.skipped.values()))}
        self._upload_queue = list(plan.items)
        self.btn_batch_upload.setEnabled(False)
        self.btn_batch_stop.show()
        self._pump_uploads()
        return True

    def cancel_batch(self):
        """停止批量上传：队列清空，正在传的这一本传完（不中断，免得留下半截文件）。"""
        b = self._batch
        if b is None:
            return
        b["cancelled"] = True
        self._upload_queue.clear()
        if self._upload_cur is None:
            self._finish_batch()
        else:
            self._notice = "正在停止批量上传（等当前这本传完）…"
            self._update_status()
            self.uploadStatus.emit(self._notice, 0)

    def _on_batch_item_done(self, path: str, name: str, res):
        b = self._batch
        b["done"] += 1
        if res.status == "ok":
            b["ok"] += 1
            b["errs"] = 0
            try:
                b["bytes"] += os.path.getsize(path)
            except OSError:
                pass
        elif res.status == "exists":
            b["exists"] += 1
            b["errs"] = 0
        else:
            b["failed"].append(f"{res.rel or name}：{res.message or '上传失败'}")
            b["errs"] += 1
            if b["errs"] >= self.BATCH_MAX_CONSECUTIVE_ERRORS and self._upload_queue:
                b["aborted"] = True
                self._upload_queue.clear()
        self.uploadFinished.emit(res)
        if self._upload_queue:
            self._pump_uploads()
        else:
            self._finish_batch()

    def _finish_batch(self):
        b, self._batch = self._batch, None
        if b is None:
            return
        self.btn_batch_upload.setEnabled(True)
        self.btn_batch_stop.hide()
        left = b["total"] - b["done"]
        parts = [f"成功 {b['ok']} 本（{fmt_size(b['bytes'])}）"]
        if b["exists"]:
            parts.append(f"云端已有 {b['exists']} 本（已跳过）")
        if b["failed"]:
            parts.append(f"失败 {len(b['failed'])} 本")
        if left:
            parts.append(f"未处理 {left} 本")
        head = "批量上传已停止：" if b["cancelled"] else ("批量上传中止（连续失败 %d 本，请检查网络 / 服务器）：" % self.BATCH_MAX_CONSECUTIVE_ERRORS
                                                    if b["aborted"] else "批量上传完成：")
        summary = head + "，".join(parts) + "。" + ("再点 ⬆ 会只补传没传上的。" if (b["failed"] or left) else "")
        self._notice = summary
        self._update_status()
        self.uploadStatus.emit(summary, 20000)
        if b["failed"]:
            from .config import _log_net
            for line in b["failed"]:
                _log_net(f"[书架] 批量上传失败 {line}")
        if b["ok"]:
            self.sync_cloud(force=True)
        self.batch_reporter(summary, b["failed"])

    # ------------------------------------------------------------ 交互
    def _on_activated(self, index: QModelIndex):
        b = index.data(BookModel.BookRole)
        if isinstance(b, sh.Book):
            self.open_book(b)

    def open_book(self, b: sh.Book) -> bool:
        """打开一本书：取第一个仍存在的本地副本发 openRequested（MainWindow 先切页再 load_book）。
        没有可用文件（索引过期）时提示并重新扫描。"""
        for p in b.local_paths:
            if os.path.isfile(p):
                self._open_token = None
                self.launch_file(p)
                return True
        if b.remote_rel and b.where in ("cloud", "synced") and self.cloud_enabled():
            return self.request_download(b, open_when_done=True)      # 仅云端（或缓存副本被删了）：下载后再打开
        if b.removed and not b.local_paths:
            self._notice = f"云端已移除，本机没有副本：{b.title}"
            self._update_status()
            return False
        if b.remote_rel and not self.cloud_enabled():
            self._notice = f"未启用云同步，无法下载：{b.title}"
            self._update_status()
            return False
        self._notice = f"找不到文件：{b.title}（已重新扫描）"
        self.rescan(force=True)
        return False

    @staticmethod
    def selected_books(view) -> List[sh.Book]:
        """视图里选中的书（去重、按行序）。网格只有第 0 列被选，列表是整行，所以按行去重。"""
        rows = {}
        for ix in view.selectionModel().selectedIndexes():
            b = ix.data(BookModel.BookRole)
            if isinstance(b, sh.Book):
                rows.setdefault(ix.row(), b)
        return [rows[r] for r in sorted(rows)]

    def _show_menu(self, view, pos):
        idx = view.indexAt(pos)
        b = idx.data(BookModel.BookRole) if idx.isValid() else None
        if not isinstance(b, sh.Book):
            return
        if not view.selectionModel().isSelected(idx):
            view.setCurrentIndex(idx)                 # 右键点在选区之外：只作用于被点的那一本
        targets = self.selected_books(view) or [b]
        m = QMenu(self)
        m.addAction("用系统默认应用打开" if b.external else "打开", lambda: self.open_book(b))
        if not self._read_only:                       # 收藏 / 隐藏：支持多选；只读实例（没拿到单实例锁）不提供
            all_fav = all(x.fav for x in targets)
            all_hid = all(x.hidden for x in targets)
            m.addAction("取消收藏" if all_fav else "收藏", lambda: self.set_fav(targets, not all_fav))
            m.addAction("取消隐藏" if all_hid else "从我的书架隐藏（只隐藏，不删文件）",
                        lambda: self.set_hidden(targets, not all_hid))
            m.addSeparator()
        if b.remote_rel and not b.local_paths and not b.removed and self.cloud_enabled():
            m.addAction("下载到本机", lambda: self.request_download(b, open_when_done=False))
        if b.where == "local" and b.local_paths and self.cloud_enabled():
            locals_ = [x.local_paths[0] for x in targets if x.where == "local" and x.local_paths]
            if len(locals_) > 1:
                m.addAction(f"批量上传选中的 {len(locals_)} 本到云端…", lambda: self.start_batch_upload(locals_))
            else:
                m.addAction("上传到云端…（手动，需确认）", lambda: self.start_upload([b.local_paths[0]]))
        if b.remote_rel:
            m.addAction("复制云端路径", lambda: QGuiApplication.clipboard().setText("library/" + b.remote_rel))
        if b.local_paths:
            m.addAction("在文件夹中显示", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(os.path.dirname(b.local_paths[0]))))
        self.run_menu(m, view.viewport().mapToGlobal(pos))
