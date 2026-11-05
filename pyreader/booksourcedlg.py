"""在线书源对话框：选书源 -> 搜索 / 浏览目录 -> 下载（epub 优先）-> 直接打开。
所有网络请求都在后台线程；结果经 Signal 回主线程，并带请求序号（用户连点搜索时丢弃过期结果）。"""
import os
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QComboBox, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QHBoxLayout, QLabel, QHeaderView)

from . import sync as syncmod
from .booksource import (BookPage, BookSourceError, book_filename, enabled_sources, source_from_cfg)
from .config import _log_net


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


class BookSourceDialog(QDialog):
    bookReady = Signal(str)                  # 用户点「打开」：已下载到本机的文件路径

    _pageReady = Signal(int, object, bool)   # (序号, BookPage, 是否追加)
    _failed = Signal(int, str)
    _progress = Signal(int, int, int)        # (序号, 已收, 总数)
    _downloaded = Signal(int, str)           # (序号, 本地路径)

    def __init__(self, cfg: dict, parent=None, dest_root=None):
        super().__init__(parent)
        self.setWindowTitle("在线书源")
        self.cfg = cfg
        self._dest_root = dest_root or syncmod.REMOTE_BOOKS_DIR
        self._specs = enabled_sources(cfg)
        self._sources = {}                   # name -> OpdsSource（缓存，内含搜索模板）
        self._seq = 0                        # 最新请求序号；回调序号不等于它的一律丢弃
        self._closing = False
        self._history = []                   # 目录导航栈：[(BookPage)]，用于「返回」
        self._page = None                    # 当前显示的 BookPage
        self._last_path = ""

        self.source_box = QComboBox()
        for sp in self._specs:
            self.source_box.addItem(sp["name"], sp["name"])
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索书名 / 作者，如 pride and prejudice（Gutenberg 可加 lang:en）")
        self.query.setClearButtonEnabled(True)
        self.search_btn = QPushButton("搜索")
        self.root_btn = QPushButton("目录")
        self.back_btn = QPushButton("返回")
        self.back_btn.setEnabled(False)
        top = QHBoxLayout()
        for w in (self.source_box, self.query, self.search_btn, self.root_btn, self.back_btn):
            top.addWidget(w)
        top.setStretch(1, 1)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["书名", "作者 / 说明", "可下载格式"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.more_btn = QPushButton("加载更多")
        self.more_btn.setEnabled(False)
        self.dl_btn = QPushButton("下载")
        self.open_btn = QPushButton("打开已下载的书")
        self.open_btn.setEnabled(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        for w in (self.more_btn, self.dl_btn, self.open_btn):
            bottom.addWidget(w)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tree, 1)
        lay.addLayout(bottom)
        self.resize(760, 520)

        self.search_btn.clicked.connect(self.do_search)
        self.query.returnPressed.connect(self.do_search)
        self.root_btn.clicked.connect(self.do_root)
        self.back_btn.clicked.connect(self.go_back)
        self.more_btn.clicked.connect(self.load_more)
        self.dl_btn.clicked.connect(self.download_selected)
        self.open_btn.clicked.connect(self._open_last)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.source_box.currentIndexChanged.connect(lambda _i: self.do_root())
        self._pageReady.connect(self._on_page)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)

        if not self._specs:
            self.status.setText("没有启用的书源：在 设置 → 同步与网络 → 在线书源 里添加。")
            for w in (self.search_btn, self.root_btn, self.dl_btn, self.query):
                w.setEnabled(False)
        else:
            self.do_root()

    # ---- 公共 ----
    def current_source(self):
        name = self.source_box.currentData()
        if not name:
            return None
        if name not in self._sources:
            spec = next((s for s in self._specs if s["name"] == name), None)
            self._sources[name] = source_from_cfg(spec) if spec else None
            if self._sources[name] is not None:
                self._sources[name].name = name
        return self._sources[name]

    def done(self, r):
        self._closing = True                 # 通知后台下载中止
        super().done(r)

    def _spawn(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        return t

    def _begin(self, msg: str) -> int:
        self._seq += 1                       # 同时让在途的下载 / 请求作废（它们的回调序号对不上）
        self.status.setText(msg)
        self.dl_btn.setEnabled(bool(self._specs))
        return self._seq

    # ---- 浏览 / 搜索 ----
    def _fetch_page(self, msg, call, append=False):
        src = self.current_source()
        if src is None:
            return
        seq = self._begin(msg)

        def run():
            try:
                self._pageReady.emit(seq, call(src), append)
            except BookSourceError as e:
                self._failed.emit(seq, str(e))
            except Exception as e:
                _log_net(f"[书源] 意外错误: {e!r}")
                self._failed.emit(seq, f"出错了：{e}")
        self._spawn(run)

    def do_root(self):
        self._history.clear()
        self.back_btn.setEnabled(False)
        self._fetch_page("正在读取目录…", lambda s: s.browse())

    def do_search(self):
        q = self.query.text().strip()
        if not q:
            self.status.setText("请输入搜索词")
            return
        if self._page is not None:
            self._history.append(self._page)
        self.back_btn.setEnabled(bool(self._history))
        self._fetch_page(f"正在搜索：{q} …", lambda s: s.search(q))

    def load_more(self):
        if self._page is None or not self._page.next_href:
            return
        href = self._page.next_href
        self._fetch_page("正在加载下一页…", lambda s: s.browse(href), append=True)

    def go_back(self):
        if not self._history:
            return
        self._begin("")                      # 丢弃在途请求
        self._show(self._history.pop())
        self.back_btn.setEnabled(bool(self._history))

    def _on_double_click(self, item, _col):
        ent = item.data(0, Qt.ItemDataRole.UserRole)
        if ent is None:
            return
        if ent.kind == "book":
            self.download_selected()
        elif ent.href:
            if self._page is not None:
                self._history.append(self._page)
            self.back_btn.setEnabled(True)
            href = ent.href
            self._fetch_page(f"正在打开：{ent.title} …", lambda s: s.browse(href))

    def _show(self, page: BookPage, append=False):
        if append and self._page is not None:
            self._page.entries.extend(page.entries)
            self._page.next_href = page.next_href
        else:
            self._page = page
            self.tree.clear()
        start = self.tree.topLevelItemCount()
        for ent in page.entries:
            fmts = ", ".join(e.lstrip(".") for e in ent.formats())
            item = QTreeWidgetItem([ent.title, ent.author or ent.summary,
                                    fmts if ent.kind == "book" else "（点开查看 / 下载）"])
            item.setData(0, Qt.ItemDataRole.UserRole, ent)
            item.setToolTip(0, ent.summary or ent.title)
            self.tree.addTopLevelItem(item)
        self.more_btn.setEnabled(bool(self._page.next_href))
        n = self.tree.topLevelItemCount()
        self.status.setText(f"{self._page.title or '目录'}：{n} 项" + ("，还有下一页" if self._page.next_href else "")
                            if n else (self._page.title or "没有结果"))
        if start == 0 and n:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _on_page(self, seq, page, append):
        if seq != self._seq or self._closing:
            return
        self._show(page, append)

    def _on_failed(self, seq, msg):
        if seq != self._seq or self._closing:
            return
        self.status.setText(msg)

    # ---- 下载 ----
    def download_selected(self):
        item = self.tree.currentItem()
        ent = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        src = self.current_source()
        if ent is None or src is None:
            self.status.setText("请先在列表里选一本书")
            return
        seq = self._begin(f"正在准备下载：{ent.title} …")
        self.dl_btn.setEnabled(False)          # 下载中不重复点；换页 / 再搜索会作废并取消它
        lim = syncmod.max_book_bytes(self.cfg)
        dest_dir = os.path.join(self._dest_root, src.name)

        def run():
            try:
                acq = src.acquire(ent)           # 搜索结果只有详情页链接时，这里会多取一次详情
                dest = os.path.join(dest_dir, book_filename(ent.title, acq.ext))
                last = [0.0]

                def progress(done, total):       # 节流到约 5 次 / 秒
                    import time
                    now = time.monotonic()
                    if now - last[0] > 0.2 or done == total:
                        last[0] = now
                        self._progress.emit(seq, done, total)
                path = syncmod.download_url(acq.href, dest, size=acq.size, max_bytes=lim,
                                            headers=src.fetch_headers(acq.href),
                                            should_cancel=lambda: self._closing or seq != self._seq,
                                            progress=progress, allow_insecure=src.allow_insecure)
                self._downloaded.emit(seq, path)
            except BookSourceError as e:
                self._failed.emit(seq, str(e))
            except Exception as e:
                _log_net(f"[书源] 下载失败 {ent.title}: {e!r}")
                self._failed.emit(seq, f"下载失败：{e}")
        self._spawn(run)

    def _on_progress(self, seq, done, total):
        if seq != self._seq or self._closing:
            return
        self.status.setText(f"正在下载… {_mb(done)}" + (f" / {_mb(total)}（{min(100, done * 100 // total)}%）" if total else ""))

    def _on_downloaded(self, seq, path):
        if seq != self._seq or self._closing:
            return
        self._last_path = path
        self.dl_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        self.status.setText(f"已下载：{os.path.basename(path)}（点「打开已下载的书」阅读）")

    def _open_last(self):
        if self._last_path and os.path.isfile(self._last_path):
            self.bookReady.emit(self._last_path)
            self.accept()
