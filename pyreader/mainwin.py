import os
import re
import threading
import time as _time
from typing import Optional
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QPixmap, QPainter, QColor, QIcon, QDesktopServices, QKeySequence, QCursor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
                               QToolBar, QMenu, QFontDialog, QTextEdit, QVBoxLayout, QLabel,
                               QFileDialog, QMessageBox, QProgressBar, QSlider, QDockWidget,
                               QLineEdit, QToolButton, QWidgetAction, QHBoxLayout, QFrame,
                               QTabWidget, QPushButton, QSizePolicy, QSplitter, QStackedWidget)
from .config import (view_count, view_of_page, CONFIG_PATH, BOOKMARKS_PATH, OUTER, MARGIN_X, MARGIN_Y, GUTTER,
                     load_env_defaults,
                     LINE_SPACING, PARA_SPACING, _HAS_MULTIMEDIA,
                     load_json, save_json, default_cjk_font, THEMES, DEFAULT_THEME, DEFAULT_CONFIG,
                     FLIP_EFFECTS, DEFAULT_FLIP_EFFECT, set_net_proxy)
from .ttsplayer import TtsPlayer
from .comicview import ComicView
from . import mediautil as mu
if _HAS_MULTIMEDIA:
    from .audioplayer import AudioPlayerView, audio_available
from .markdown import md_chapters, MD_HEAD_RE
from .textio import MD_EXTS
from .pager import LazyPager
from .loader import BookLoader
from . import sync as syncmod
from .sync import SyncWorker
from .view import PageView
from .settings import SettingsDialog
from . import shelf as shelfmod
from .shelfview import ShelfView
from .ai import (translate, lookup, AiWorker,
                  summarize_chapter, extract_mindmap, ai_qa,
                  continue_story, analyze_sentiment, explain_background,
                  extract_knowledge_graph)
from .graph import build_graph_html
from .tts import TtsWorker, TranslateWorker, split_sentences, synthesize_tts, TTS_VOICES, TTS_DEFAULT_VOICE, _log_tts, set_azure_config, sherpa_model_ready, kokoro_model_ready, reset_loudness_state

# ============ 主窗口 ============

# 朗读预取深度（句）。Melo 合成约 0.87× 实时、且按句长波动很大（短句 0.7s、长句 7s），
# 只预取 1 句时缓冲没有余量：短句后面跟长句必然露白 5 秒以上。维持 3 句缓冲可吸收这种波动。
TTS_PREFETCH = 3


def _set_reading_priority(high: bool):
    """朗读期间把进程优先级提到 ABOVE_NORMAL，结束后恢复。

    Melo 合成是纯 CPU 计算（VITS 解码）。后台服务（SearchIndexer / Defender / node）
    抢核时实测 rtf 从 0.87 掉到 1.4~1.6，而 rtf>1 时任何预取缓冲都补不回来（只能出静音）。
    提优先级不改变模型开销，但能让合成线程稳定拿到 CPU。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ABOVE_NORMAL, NORMAL = 0x8000, 0x20
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), ABOVE_NORMAL if high else NORMAL)
    except Exception:
        pass


class MainWindow(QMainWindow):
    # 合成线程 → 主线程的结果通道
    # （Qt 对象只能在主线程碰：QAudioSink / QTimer / UI 都必须在主线程，
    #   合成完成时从后台线程 emit，靠 QueuedConnection 排回主线程）
    ttsSynthReady = Signal(int, int, bytes)   # (epoch, seq, wav)
    ttsSynthFailed = Signal(int, int, str)    # (epoch, seq, 错误信息)

    def __init__(self, book_path=None):
        super().__init__()
        # 优先级：DEFAULT_CONFIG < coding-agent/.env < 用户已保存的 config.json
        _saved = load_json(CONFIG_PATH, {})
        _env   = load_env_defaults() if not _saved.get("api_key") else {}
        self.cfg = {**DEFAULT_CONFIG, **_env, **_saved}
        # Reader 用自己的代理策略（不读 Windows 系统代理），见 config.net_urlopen
        set_net_proxy(self.cfg.get("proxy") or os.environ.get("PYREADER_PROXY", ""))
        set_azure_config(self.cfg["azure_tts_key"], self.cfg["azure_tts_region"],
                         self.cfg["azure_tts_key2"], self.cfg["azure_tts_region2"])
        if self.cfg.get("tts_voice") not in {v for _, v in TTS_VOICES}:   # 已下线的 edge-tts 音色 → 默认音色
            self.cfg["tts_voice"] = TTS_DEFAULT_VOICE
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

        # ── AI 面板（Tab 式）──────────────────────────────────────
        self._qa_history = []          # [(role, content), ...]  多轮对话记录
        self._qa_context_title = ""    # 当前对话绑定的章节标题
        self._qa_context_text  = ""    # 当前对话绑定的章节全文

        # Tab 1：文字输出（摘要 / 大纲 / 问答 / 续写 / 情感）
        self.ai_out = QTextEdit(); self.ai_out.setReadOnly(True)
        self.ai_out.setStyleSheet("font-size:13px; line-height:1.6;")

        # 问答输入行（带历史清除按钮）
        qa_row = QWidget()
        qa_hl  = QHBoxLayout(qa_row); qa_hl.setContentsMargins(0, 0, 0, 0)
        self.ai_qa_input = QLineEdit()
        self.ai_qa_input.setPlaceholderText("向书童提问，按 Enter 发送（支持多轮对话）…")
        self.ai_qa_input.returnPressed.connect(self._run_ai_qa)
        self._qa_clear_btn = QPushButton("清除对话")
        self._qa_clear_btn.setFixedWidth(72)
        self._qa_clear_btn.clicked.connect(self._clear_qa_history)
        qa_hl.addWidget(self.ai_qa_input)
        qa_hl.addWidget(self._qa_clear_btn)

        tab1 = QWidget(); t1v = QVBoxLayout(tab1); t1v.setContentsMargins(4, 4, 4, 4)
        t1v.addWidget(self.ai_out)
        t1v.addWidget(qa_row)

        # Tab 2：知识图谱（生成后在系统默认浏览器中打开，避免捆绑 QtWebEngine）
        self.graph_view = QLabel(
            "点击工具栏 🤖AI功能 → 知识图谱 生成图谱\n\n"
            "（图谱将用系统默认浏览器打开）")
        self.graph_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.graph_view.setStyleSheet("color:#888; font-size:13px;")
        tab2 = QWidget(); t2v = QVBoxLayout(tab2); t2v.setContentsMargins(0, 0, 0, 0)
        t2v.addWidget(self.graph_view)

        self.ai_tabs = QTabWidget()
        self.ai_tabs.addTab(tab1, "💬 问答")
        self.ai_tabs.addTab(tab2, "🕸️ 知识图谱")

        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.ai_tabs)

        # 中央控件：QStackedWidget。第 0 页书架（ShelfView），第 1 页现有阅读页（self.view，属性名不变）。
        # 单实例锁（design §4.5）：~/.pyreader/shelf/.lock 的 OS 级独占锁。取不到（另一个 pyreader 进程在用书架）就以只读模式
        # 打开书架：可浏览，但不写 overlay.json / state.json / 设备文件，状态条提示。锁随进程退出自动释放。
        try:
            self._shelf_lock = shelfmod.acquire_lock()
        except Exception:
            self._shelf_lock = None
        self.shelf = ShelfView(self._shelf_context,
                               default_view=self.cfg.get("shelf_default_view", "grid"),
                               default_sort=self.cfg.get("shelf_default_sort", "last_read"),
                               read_only=self._shelf_lock is None)
        self.shelf.openRequested.connect(self._open_from_shelf)
        self.shelf.addLibraryRequested.connect(self._add_library_dir)
        self.shelf.uploadStatus.connect(lambda text, ms: self.statusBar().showMessage(text, ms))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.shelf)                 # 第 0 页：书架
        self.stack.addWidget(self.view)                  # 第 1 页：阅读页
        # 应用内媒体：第 2 页漫画（.zip / .cbz），第 3 页音频（没有 QtMultimedia 时为 None，打开时交给系统应用）
        self.comic = ComicView()
        self.comic.set_theme(THEMES.get(self.cfg.get("theme"), THEMES[DEFAULT_THEME]))
        self.comic.backRequested.connect(self.show_shelf)
        self.comic.pageChanged.connect(self._on_comic_page)
        self.comic.optionsChanged.connect(self._save_comic_options)
        self.stack.addWidget(self.comic)
        self.audio = AudioPlayerView() if _HAS_MULTIMEDIA else None
        if self.audio:
            self.audio.backRequested.connect(self.show_shelf)
            self.audio.positionChanged.connect(self._on_audio_position)
            self.audio.playbackStateChanged.connect(self._on_audio_state)
            self.audio.seeked.connect(self._on_audio_seeked)
            self.audio.failed.connect(lambda m: self.statusBar().showMessage("无法播放：" + m, 8000))
            self.stack.addWidget(self.audio)
        self._media = None                # 当前打开的应用内媒体（见 _open_media）；None = 没有
        self._text_active = True          # 阅读页上的书是不是「当前书」：打开媒体后为 False（书架里再点勾选不回到阅读页）
        self.setCentralWidget(self.stack)                # 沉浸阅读：默认全宽
        self._resized_on_shelf = False    # 书架可见期间窗口变过大小 / 书加载完成：回阅读页时要重排
        self._shelf_open_pending = False  # 从书架双击打开：加载失败时回书架
        self._reader_title = ""
        self._reader_acts = []            # 依赖阅读页的工具栏动作（书架可见时禁用）
        self._reader_widgets = []
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

        # ---- 语音朗读 ----
        # 用 QAudioSink 持续流播放（TtsPlayer）而不是 QMediaPlayer 每句重开媒体源：
        # 后者每次切换音频源都要重新打开/配置音频设备管线，会产生可闻的哒哒声（"du"音）。
        self.player = TtsPlayer(self) if _HAS_MULTIMEDIA else None
        if self.player:
            self.player.sentenceFinished.connect(self._on_tts_sentence_finished)
        self.tts_on = False
        self.tts_paused = False
        self.tts_chapter = 0
        self.tts_epoch = 0            # 章节代际，用于忽略过期合成结果
        self.tts_sentences = []       # [(start, end, text)]
        self.tts_idx = 0
        self.tts_cache = {}           # seq -> 音频字节（wav）
        self.tts_inflight = set()
        self.tts_workers = {}         # seq -> TtsWorker（防止信号前被 GC）
        self.tts_failed_seqs = set()  # 本章内合成失败的句子（跳过，不重试）
        self.tts_pending = []         # 待合成序号 FIFO（由单一线程消费，保证先播的先合成）
        self.tts_pending_lock = threading.Lock()
        self.tts_pending_event = threading.Event()   # 有新任务时唤醒合成线程
        self.tts_dispatcher = None    # 合成线程
        self.ttsSynthReady.connect(self._tts_on_ready, Qt.ConnectionType.QueuedConnection)
        self.ttsSynthFailed.connect(self._tts_on_failed, Qt.ConnectionType.QueuedConnection)
        self.bilingual_on = bool(self.cfg.get("bilingual", False))
        self.tr_cache = {}            # seq -> 译文
        self.tr_inflight = set()
        self.tr_workers = {}

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True); self._resize_timer.setInterval(200)
        self._resize_timer.timeout.connect(self.reload_current)

        # WebDAV 进度同步：所有网络都在后台线程，结果经 Signal 回主线程
        self.sync = SyncWorker(self)
        self.sync.pulled.connect(self._on_remote_progress)
        self.sync.status.connect(lambda m: self.statusBar().showMessage(m, 5000))
        self.sync.downloaded.connect(self.load_book)
        self.sync.remoteList.connect(self._fill_remote_menu)
        self._loaded_path = None       # 已完成加载的书（远端进度到达时用来判断能否立刻应用）
        self._pending_remote = None    # 在书加载完成前到达的远端进度
        self._seed_push = None         # 首次打开云端缓存副本时从本地副本种入了进度：等远端确认没有记录后推送一次
        self._seed_remote_none = False
        self._last_saved_offset = None
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(30000)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        self._progress_timer.start()

        self._build_toolbar(); self._build_menus()
        self._rebuild_recent_menu()
        self._update_bilingual_act()
        # 翻页方式写入 view
        self.view._flip_effect = self.cfg.get("flip_effect", DEFAULT_FLIP_EFFECT)
        self.view.single_page = bool(self.cfg.get("single_page", False))
        self.single_act.setChecked(self.view.single_page)
        if self.bilingual_on:
            self.bilingual_dock.show()

        # 底部状态栏：加载进度条 + 阅读进度（可拖动跳转）
        self.load_bar = QProgressBar()
        self.load_bar.setRange(0, 100); self.load_bar.setFixedWidth(180)
        self.load_bar.hide()
        # 进度标签点击循环切换显示模式
        self._progress_mode = 0   # 0=全书%  1=本章页码  2=预估剩余分钟
        self.pos_label = QLabel("0.0%"); self.pos_label.setFixedWidth(72)
        self.pos_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pos_label.setToolTip("点击切换进度显示模式")
        self.pos_label.mousePressEvent = lambda e: self._cycle_progress_mode()
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
        # 启动页：指定了书 → 阅读页（load_book 会切页）；否则「启动时显示书架」或没有可用的最近阅读 → 书架
        has_recent = any(isinstance(p, str) and os.path.exists(p) and shelfmod.is_readable(p)
                         for p in self.cfg.get("recent", []))
        start_shelf = (not book_path or not shelfmod.is_readable(book_path)) and (bool(self.cfg.get("shelf_start_in_shelf", False)) or not has_recent)
        self.stack.setCurrentIndex(0 if start_shelf else 1)
        self._sync_page_state()
        QTimer.singleShot(300, self.shelf.rescan_if_stale)   # 先用 index.json 缓存画界面，稍后后台增量扫描
        QTimer.singleShot(3000, self.shelf.sync_cloud_if_stale)   # 云端书库：延迟几秒再读（未启用 WebDAV 时是空操作）
        if book_path:
            self.load_book(book_path)

    def _build_toolbar(self):
        tb = QToolBar(); self.addToolBar(tb)
        self._toolbar = tb

        def R(act):                       # 登记「依赖阅读页」的动作：书架可见时禁用
            self._reader_acts.append(act)
            return act
        tb.addAction("打开", self.open_book)
        tb.addAction("字体", self.choose_font)
        tb.addAction("设置", self.open_settings)
        tb.addAction("⛶ 全屏", self.toggle_fullscreen).setToolTip("全屏阅读 (F11)；全屏时鼠标移到顶部显示工具栏")
        tb.addSeparator()
        R(tb.addAction("◀ 上一页", lambda: self.view.flip(-1)))
        R(tb.addAction("下一页 ▶", lambda: self.view.flip(1)))
        R(tb.addAction("添加书签", self.add_bookmark))
        self.single_act = R(tb.addAction("📄 单页", lambda: self.set_single_page(self.single_act.isChecked())))
        self.single_act.setCheckable(True)
        self.single_act.setToolTip("单页 / 双页跨页 切换")
        tb.addSeparator()
        self.tts_act = R(tb.addAction("🔊 朗读", self.toggle_reading))
        self.stop_act = R(tb.addAction("⏹ 停止", self.stop_reading))
        self.stop_act.setEnabled(False)
        self.bilingual_act = R(tb.addAction("🌐 双语", self.toggle_bilingual))
        self.bilingual_act.setCheckable(True)
        self.sleep_act = R(tb.addAction("⏰ 定时"))
        self.sleep_act.setMenu(self._build_sleep_menu())
        tb.addSeparator()
        tb.addAction(self.toc_dock.toggleViewAction())   # 目录 显示/隐藏
        tb.addAction(self.ai_dock.toggleViewAction())    # AI 工具 显示/隐藏
        # 主题快速切换色板
        theme_btn = QToolButton(self)
        theme_btn.setText("🎨")
        theme_btn.setToolTip("快速切换主题")
        theme_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.theme_menu = QMenu(self)
        self._rebuild_theme_menu()
        theme_btn.setMenu(self.theme_menu)
        tb.addWidget(theme_btn)
        tb.addSeparator()
        # 最近打开（下拉菜单）
        self.recent_menu = QMenu("最近打开", self)
        self.recent_act = tb.addAction("🕘 最近")
        self.recent_act.setMenu(self.recent_menu)
        # 书库目录（如移动云盘同步文件夹）：每次展开菜单时重新扫描，新同步的书自动出现
        self.library_menu = QMenu("书库", self)
        self.library_menu.aboutToShow.connect(self._rebuild_library_menu)
        self.library_act = tb.addAction("📚 书库")
        self.library_act.setMenu(self.library_menu)
        # 远程书库（WebDAV <base>/library）：展开时后台列目录
        self.remote_menu = QMenu("远程书库", self)
        self.remote_menu.aboutToShow.connect(self._rebuild_remote_menu)
        self.remote_act = tb.addAction("☁️ 远程书库")
        self.remote_act.setMenu(self.remote_menu)
        # 书架（可勾选，Ctrl+B；阅读页按 Esc 也回书架）
        self.shelf_act = tb.addAction("📖 书架")
        self.shelf_act.setCheckable(True)
        self.shelf_act.setShortcut(QKeySequence("Ctrl+B"))
        self.shelf_act.setToolTip("书架 (Ctrl+B)")
        self.shelf_act.triggered.connect(self._on_shelf_act)
        tb.addSeparator()
        # AI 辅助功能组
        ai_menu = QMenu("AI 功能", self)
        ai_menu.addAction("📝 章节摘要", self._ai_summarize)
        ai_menu.addAction("🗺️ 思维导图", self._ai_mindmap)
        ai_menu.addAction("✍️ 续写建议", self._ai_continue)
        ai_menu.addSeparator()
        ai_menu.addAction("🕸️ 知识图谱", self._ai_knowledge_graph)
        ai_menu.addSeparator()
        ai_menu.addAction("😊 情感分析（选中文字）", self._ai_sentiment_selection)
        ai_menu.addAction("🔍 背景解读（选中文字）", self._ai_background_selection)
        self.ai_menu_act = R(tb.addAction("🤖 AI功能"))
        self.ai_menu_act.setMenu(ai_menu)
        # 全文搜索
        tb.addSeparator()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索 Ctrl+F")
        self.search_box.setFixedWidth(150)
        self.search_box.textChanged.connect(self._on_search_changed)
        self.search_box.returnPressed.connect(self._search_next)
        tb.addWidget(self.search_box)
        self._reader_widgets.append(self.search_box)
        R(tb.addAction("↑", self._search_prev))
        R(tb.addAction("↓", self._search_next))

    # ---- 书架 / 阅读页切换 ----
    def _shelf_context(self):
        """书架后台扫描需要的活数据（在主线程取；书架自己拷贝快照再交给线程）。"""
        return {"library_dirs": self.cfg.get("library_dirs", []), "recent": self.cfg.get("recent", []),
                "bookmarks": self.bookmarks, "cfg": self.cfg}      # cfg：webdav_* 决定云端书库是否并入书架

    def _shelf_visible(self):
        return self.stack.currentWidget() is self.shelf

    def _reader_visible(self):
        return self.stack.currentWidget() is self.view

    def _sync_page_state(self):
        """按当前页刷新：书架按钮勾选、依赖阅读页的动作 / 搜索框可用性、窗口标题（媒体页同书架：没有文字阅读动作可用）。"""
        on_shelf = self._shelf_visible()
        on_reader = self._reader_visible()
        self.shelf_act.setChecked(on_shelf)
        for a in self._reader_acts:
            a.setEnabled(on_reader)
        for w in self._reader_widgets:
            w.setEnabled(on_reader)
        if on_reader:
            self._update_tts_actions()    # stop_act 的可用性取决于朗读状态，不能一律启用
        title = self.windowTitle()
        if on_shelf:
            if title != "书架 — PyReader":
                self._reader_title = title
            self.setWindowTitle("书架 — PyReader")
        elif title == "书架 — PyReader":
            self.setWindowTitle(self._reader_title)

    def show_shelf(self):
        if self._shelf_visible():
            return
        was_media = self._media is not None
        if was_media:
            self._close_media()           # 媒体不留在后台：停播并放开文件（云端缓存副本才能被替换 / 删除）
        else:
            self._save_progress(force=False)  # 离开阅读页前存一次进度：书架上看到的进度是最新的
        self.stack.setCurrentIndex(0)
        if was_media:                     # 媒体页的标题不是阅读页的标题：回阅读页时按文字书恢复
            self.setWindowTitle("书架 — PyReader")
            self._reader_title = os.path.basename(self.book_path) + " — PyReader" if self.book_path else ""
        self._sync_page_state()

    def show_reader(self):
        """切到阅读页。必须发生在启动 loader 之前：pagination_params 用阅读页的 height()，
        QStackedLayout 只给当前页设置几何，隐藏的页尺寸可能是旧的（或从未显示过的默认值）。"""
        if self._reader_visible():
            return
        self.stack.setCurrentIndex(1)
        self._sync_page_state()
        if self._resized_on_shelf and self.pager:
            self._resized_on_shelf = False
            self._resize_timer.start()    # 书架期间窗口大小变过：按新尺寸重排

    def _on_shelf_act(self, checked):
        if checked:
            self.show_shelf()
        elif self.book_path and self._text_active:
            self.show_reader()
        else:
            self.shelf_act.setChecked(True)   # 没有可返回的书：留在书架

    def _open_from_shelf(self, path):
        """书架双击 / 回车：先切页再加载（load_book 内部保证）；加载失败则回书架。"""
        if shelfmod.is_external(path):         # 外部格式不进阅读器：交给系统应用，不设「失败回书架」标记
            self.shelf.launch_file(path)
            return
        self.load_book(path)
        self._shelf_open_pending = not shelfmod.is_inapp_media(path)     # 媒体同步打开，失败时 _open_media 自己兜底

    # ---- 应用内媒体（音频 / 漫画）：打开、进度、离开 ----
    MEDIA_SAVE_S = 5.0        # 本地进度（bookmarks.json / 书架）至多每 5 秒存一次；暂停 / 离开页面 / 退出时立即存
    MEDIA_PUSH_S = 30.0       # 云端逐书进度至多每 30 秒推一次；30 秒计时器兜底，安静下来前的最后一次也会推出去

    def _media_widget(self, kind):
        return self.comic if kind == "comic" else self.audio

    def _media_bookmark(self, path):
        return self.bookmarks.get(path) or shelfmod.bookmark_for(self.bookmarks, path) or {}

    @staticmethod
    def _int_field(ent, key):
        v = ent.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    def _open_media(self, path, kind):
        """音频 / .zip / .cbz 在应用内打开：停朗读、存文字书进度、放开上一个媒体，再切到对应页并从断点继续。
        打不开（没有音频设备 / 没有 QtMultimedia / 不是漫画的 .zip）时退回系统默认应用，行为同旧版。"""
        self._tts_stop()
        self._save_progress(force=False)
        self._close_media()
        self._shelf_open_pending = False
        ent = self._media_bookmark(path)
        start = self._int_field(ent, "_progress_ms" if kind == "audio" else "_progress_page")
        if kind == "audio":
            if self.audio is None:
                ok, err = False, "没有 QtMultimedia"
            elif not audio_available():
                ok, err = False, "没有可用的音频输出设备"
            else:
                if "audio_speed" in self.cfg:
                    self.audio.set_speed(self.cfg["audio_speed"])
                if "audio_volume" in self.cfg:
                    self.audio.set_volume(self.cfg["audio_volume"])
                ok, err = self.audio.open_audio(path, start), "找不到文件"
        else:
            self._apply_comic_options()
            ok = self.comic.open_comic(path, start)
            err = self.comic.last_error()
        if not ok:
            self.statusBar().showMessage(f"无法在应用内打开 {os.path.basename(path)}（{err}），改用系统应用", 8000)
            if os.path.isfile(path):
                self.shelf.open_external(path)
            if self.stack.currentWidget() in (self.comic, self.audio):
                self.show_shelf()
            return
        self.shelf.note_opened(path)
        self._media = {"path": path, "kind": kind, "bid": syncmod.book_id(path), "start": start, "pos": start, "dur": 0,
                       "page": start, "count": self.comic.page_count() if kind == "comic" else 0,
                       "saved_at": None, "pushed_at": None, "dirty": False, "push_due": False,
                       "closing": False, "touched": False, "applying": False}
        self._text_active = False
        self.stack.setCurrentWidget(self._media_widget(kind))
        self._sync_page_state()
        self.setWindowTitle(os.path.basename(path) + " — PyReader")
        self._media_widget(kind).setFocus()
        self.sync.pull(self.cfg, path)       # 未启用时是 no-op；别的设备读得更远则在没动过之前跳过去
        self.statusBar().showMessage(os.path.basename(path), 4000)

    def _apply_comic_options(self):
        self._applying_opts = True
        try:
            if self.cfg.get("comic_fit"):
                self.comic.set_fit_mode(self.cfg["comic_fit"])
            if "comic_spread" in self.cfg:
                self.comic.set_spread(bool(self.cfg["comic_spread"]))
            if "comic_rtl" in self.cfg:
                self.comic.set_rtl(bool(self.cfg["comic_rtl"]))
        finally:
            self._applying_opts = False

    def _save_comic_options(self):
        if getattr(self, "_applying_opts", False):
            return
        self.cfg.update({"comic_fit": self.comic.fit_mode(), "comic_spread": self.comic.spread(), "comic_rtl": self.comic.rtl()})
        save_json(CONFIG_PATH, self.cfg)

    def _on_audio_position(self, pos, dur):
        m = self._media
        if m is None or m["kind"] != "audio":
            return
        m["pos"], m["dur"] = pos, dur
        if m["applying"] or not m["touched"]:     # 载入后自动续播到断点、别的设备的位置：不是本机的阅读事件，不存也不回推（否则会盖上新时间戳，挡住更新的远端进度）
            return
        m["dirty"] = True
        if not m["closing"]:
            self._save_media_progress(force=False)

    def _on_audio_seeked(self):
        if self._media is not None and self._media["kind"] == "audio" and not self._media["closing"]:
            self._media["touched"] = True

    def _on_audio_state(self, playing):
        m = self._media
        if m is None or m["kind"] != "audio" or m["closing"]:
            return
        if playing:
            m["touched"] = True
        elif m["touched"]:
            self._save_media_progress(force=True, push_gap=self.MEDIA_SAVE_S)     # 暂停：立刻存，云端至多每 5 秒推一次

    def _on_comic_page(self, page, count):
        m = self._media
        if m is None or m["kind"] != "comic":
            return
        m["page"], m["count"] = page, count
        if m["applying"]:
            return
        m["dirty"] = True
        m["touched"] = m["touched"] or page != m["start"]
        if not m["closing"]:
            self._save_media_progress(force=False)

    def _media_snapshot(self):
        """当前要记的进度 {"ms"|"page", "frac"（0..1）}；音频位置不值得记（开头 / 缺位置）时返回 None。"""
        m = self._media
        if m["kind"] == "audio":
            pos, dur = m["pos"], m["dur"]
            if dur > 0 and pos > mu.RESUME_MARGIN_MS and pos >= dur - mu.RESUME_MARGIN_MS:
                return {"ms": 0, "frac": 1.0}                      # 听完了：下次从头，书架上记 100%
            if not mu.should_save_resume(pos, dur):
                return None
            return {"ms": pos, "frac": min(1.0, pos / dur) if dur > 0 else 0.0}
        page, count = m["page"], m["count"]
        last = max(page, self.comic.screen_last(page))      # 跨页时一屏有两页：按这屏的最后一页算，翻到底才是 100%
        return {"page": page, "frac": min(1.0, last / (count - 1)) if count > 1 else 0.0}

    def _save_media_progress(self, force=True, push_gap=None):
        """存媒体进度到 bookmarks.json（_progress_ms / _progress_page + 0..1 的 _progress_pct_）并通知书架；
        force=False 时至多每 MEDIA_SAVE_S 秒一次（没存的留着，下次事件或 30 秒计时器再存）。返回推送线程。"""
        m = self._media
        if m is None or not (force or m["dirty"]):
            return None
        now = _time.monotonic()
        if not force and m["saved_at"] is not None and now - m["saved_at"] < self.MEDIA_SAVE_S:
            return None
        snap = self._media_snapshot()
        m["dirty"] = False
        if snap is None:
            return None
        m["saved_at"] = now
        updated = _time.time()
        device = self.cfg.get("device_name") or syncmod.default_device_name()
        ent = self.bookmarks.setdefault(m["path"], {})
        if m["kind"] == "audio":
            ent["_progress_ms"] = snap["ms"]
        else:
            ent["_progress_page"] = snap["page"]
        ent["_progress_pct_"] = round(snap["frac"], 6)
        ent["_progress_ts_"] = updated
        try:
            save_json(BOOKMARKS_PATH, self.bookmarks)
        except Exception:
            pass
        self.shelf.note_progress(m["path"], snap["frac"] * 100.0, updated, device)
        m["rec"] = syncmod.make_media_record(m["bid"], m["kind"], snap["frac"] * 100.0, device,
                                             pos_ms=snap.get("ms"), page=snap.get("page"), updated=updated)
        m["push_due"] = True
        return self._push_media(force=push_gap is None and force, gap=push_gap)

    def _push_media(self, force=False, gap=None):
        """把最近一次存下的媒体进度推到云端（force 立即；否则至多每 gap 秒，默认 MEDIA_PUSH_S）。"""
        m = self._media
        if m is None or not m["push_due"]:
            return None
        now = _time.monotonic()
        gap = 0.0 if force else (self.MEDIA_PUSH_S if gap is None else gap)
        if m["pushed_at"] is not None and now - m["pushed_at"] < gap:
            return None
        m["push_due"], m["pushed_at"] = False, now
        return self.sync.push(self.cfg, m["rec"])

    def _close_media(self):
        """离开媒体：存最后的进度并推送，停播 / 关档案放开文件，记住音量 / 倍速。没有打开的媒体是空操作。返回推送线程。"""
        m = self._media
        if m is None:
            return None
        m["closing"] = True
        if m["kind"] == "audio":
            self.audio.close_audio()         # 先让播放器补发最后一次位置，再存
            t = self._flush_media()
            vol, spd = self.audio.volume(), self.audio.speed()
            if (self.cfg.get("audio_volume"), self.cfg.get("audio_speed")) != (vol, spd):
                self.cfg.update({"audio_volume": vol, "audio_speed": spd})
                save_json(CONFIG_PATH, self.cfg)
        else:
            t = self._flush_media()
            self.comic.close_comic()
        self._media = None
        return t

    def _flush_media(self):
        """关闭时：有没存的变化才存（只是打开看看不重写时间戳、不回推，免得盖掉别的设备的进度）；已存未推的补推。"""
        t = self._save_media_progress(force=True) if self._media["dirty"] else None
        return t or self._push_media(force=True)

    def _apply_remote_media(self, remote):
        """别的设备记的位置比本机新、而且本机还没动过（没播放 / 没翻页）：跳过去。远端数据不可信，逐项校验。"""
        m = self._media
        if not isinstance(remote, dict) or m["closing"] or m["touched"] or remote.get("kind") != m["kind"]:
            return
        val = syncmod.media_position(remote, m["kind"])
        local_ts = shelfmod._num(self._media_bookmark(m["path"]).get("_progress_ts_"), 0.0)
        rts = shelfmod._num(remote.get("updated"))
        if val is None or rts is None or rts <= local_ts:
            return
        if m["kind"] == "comic":
            val = min(val, max(0, self.comic.page_count() - 1))      # 远端页码可能超出本机这份压缩包
        if val != m["start"]:
            m["applying"] = True
            try:
                if m["kind"] == "audio":
                    self.audio.open_audio(m["path"], val)     # 重开比在载入中途 seek 可靠（见 AudioPlayerView 的续播说明）
                else:
                    self.comic.go_to(val)
            finally:
                m["applying"] = False
        m["start"], m["pos"], m["page"] = val, val, val
        m["touched"] = False
        ent = self.bookmarks.setdefault(m["path"], {})
        ent["_progress_ms" if m["kind"] == "audio" else "_progress_page"] = val
        pct = shelfmod._num(remote.get("percent"), 0.0)
        ent["_progress_pct_"] = round(min(100.0, max(0.0, pct)) / 100.0, 6)
        ent["_progress_ts_"] = rts
        try:
            save_json(BOOKMARKS_PATH, self.bookmarks)
        except Exception:
            pass
        self.shelf.note_progress(m["path"], pct, rts, str(remote.get("device", ""))[:64], local=False)
        self.statusBar().showMessage(f"☁️ 已同步进度（来自 {remote.get('device', '?')}，{pct:.1f}%）", 6000)

    def _on_progress_tick(self):
        """30 秒进度计时器：媒体页把攒着的本地进度存下并推到云端；书架可见且阅读位置没变时什么都不做。"""
        if self._media is not None:
            self._save_media_progress(force=False)
            self._push_media(force=False)
        if not self._text_active:
            return
        if self._shelf_visible() and self.view.left_page_offset() == self._last_saved_offset:
            return
        self._save_progress(force=False)

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

    BOOK_EXTS = shelfmod.READABLE_EXTS      # Library / 云端书库菜单只列阅读器能打开的格式（有声书 / 漫画压缩包只在书架）
    LIBRARY_MAX_FILES = 300

    def _rebuild_library_menu(self):
        m = self.library_menu
        m.clear()
        dirs = [d for d in self.cfg.get("library_dirs", []) if os.path.isdir(d)]
        for d in dirs:
            sub = m.addMenu(os.path.basename(d.rstrip("\\/")) or d)
            sub.setToolTipsVisible(True)
            files = []
            for root, _dn, fns in os.walk(d):
                for fn in fns:
                    if fn.lower().endswith(self.BOOK_EXTS) and not fn.startswith((".", "~$")):
                        p = os.path.join(root, fn)
                        try:
                            files.append((os.path.getmtime(p), p))
                        except OSError:
                            pass
            files.sort(reverse=True)  # 最近同步/修改的在前
            for _t, p in files[:self.LIBRARY_MAX_FILES]:
                a = sub.addAction(os.path.relpath(p, d), lambda p=p: self.load_book(p))
                a.setToolTip(p)
            if not files:
                sub.addAction("（没有找到电子书）").setEnabled(False)
            elif len(files) > self.LIBRARY_MAX_FILES:
                sub.addAction(f"…仅显示最新 {self.LIBRARY_MAX_FILES} 本").setEnabled(False)
        if dirs:
            m.addSeparator()
        m.addAction("➕ 添加书库目录…", self._add_library_dir)
        if self.cfg.get("library_dirs"):
            rm = m.addMenu("➖ 移除书库目录")
            for d in self.cfg["library_dirs"]:
                rm.addAction(d, lambda d=d: self._remove_library_dir(d))

    # ---- WebDAV：远程书库 / 进度同步 ----
    def _sync_on(self):
        return syncmod.client_from_cfg(self.cfg) is not None

    def _rebuild_remote_menu(self):
        m = self.remote_menu
        m.clear()
        if not self._sync_on():
            m.addAction("（未启用 WebDAV：设置中填写地址并勾选启用）").setEnabled(False)
            return
        m.addAction("正在读取远程书库…").setEnabled(False)
        self.sync.list_library(self.cfg, self.BOOK_EXTS)

    def _fill_remote_menu(self, files):
        m = self.remote_menu
        m.clear()
        for rel, size in files[:self.LIBRARY_MAX_FILES]:
            m.addAction(rel, lambda rel=rel, size=size: self._open_remote_book(rel, size))
        if not files:
            m.addAction("（远程书库为空）").setEnabled(False)
        m.addSeparator()
        m.addAction("⬆️ 上传当前书到云端书库…", self._upload_current_book)

    def _open_remote_book(self, rel, size):
        self.statusBar().showMessage(f"正在下载 {rel} …")
        self.sync.download(self.cfg, rel, size)

    def _upload_current_book(self):
        if not self._sync_on():
            self.statusBar().showMessage("未启用 WebDAV 同步")
            return
        if not self.book_path or not os.path.isfile(self.book_path):
            self.statusBar().showMessage("请先打开一本书")
            return
        self.shelf.start_upload([self.book_path])     # 确认对话框 -> 选文件夹 / 改名 -> 后台上传；云端已有同名文件时不覆盖

    def _current_progress_record(self):
        off = self.view.left_page_offset()
        n = max(1, len(self.full_text))
        ch = ""
        try:
            ch = self.pager.chapters[self.cur_chapter].title
        except Exception:
            pass
        return off, syncmod.make_record(syncmod.book_id(self.book_path), off, off * 100.0 / n, ch,
                                        self.cfg.get("device_name") or syncmod.default_device_name())

    def _save_progress(self, force=True):
        """保存本地进度（bookmarks.json）并（若启用）推送远端。
        force=False 时仅在偏移变化后才保存（定时器用）。返回推送线程（便于 closeEvent 等待）。"""
        if not (self.book_path and self.pager and self._loaded_path == self.book_path):
            return None
        off, rec = self._current_progress_record()
        if not force and off == self._last_saved_offset:
            return None
        self._last_saved_offset = off
        ent = self.bookmarks.setdefault(self.book_path, {})
        ent["_progress_"] = off
        ent["_progress_ts_"] = rec["updated"]
        try:
            save_json(BOOKMARKS_PATH, self.bookmarks)
        except Exception:
            pass
        self.shelf.note_progress(self.book_path, rec["percent"], rec["updated"], rec["device"])
        return self.sync.push(self.cfg, rec)

    def _maybe_push_seed(self):
        """首次打开云端缓存副本、进度是从本地副本种入的：书已加载且远端确认没有记录时，推送一次 remote: 进度。"""
        if (self._seed_push and self._seed_push == self.book_path and self._seed_remote_none
                and self._loaded_path == self.book_path and self.pager):
            self._seed_push = None
            self._save_progress(force=True)

    def _on_remote_progress(self, book_path, remote):
        if self._media is not None and book_path == self._media["path"]:
            self._apply_remote_media(remote)
            return
        if remote is None and book_path == self.book_path and self._seed_push == book_path:
            self._seed_remote_none = True        # 远端没有这本书的进度：种入的本地进度要推上去
            self._maybe_push_seed()
            return
        if remote is None or book_path != self.book_path:
            return
        if self._loaded_path != book_path or not self.pager:
            self._pending_remote = (book_path, remote)   # 书还没加载完，加载后再应用
            return
        self._apply_remote_progress(remote)

    def _apply_remote_progress(self, remote):
        ent = self.bookmarks.get(self.book_path, {})
        local = {"updated": ent.get("_progress_ts_", 0)}
        if syncmod.merge_progress(local, remote) is not remote:
            return
        off = syncmod.resolve_offset(remote, len(self.full_text))
        if off == self.view.left_page_offset():
            return
        self.goto_offset(off)
        self._update_status()
        self._last_saved_offset = off
        ent = self.bookmarks.setdefault(self.book_path, {})
        ent["_progress_"] = off
        ent["_progress_ts_"] = float(remote.get("updated", 0))
        try:
            save_json(BOOKMARKS_PATH, self.bookmarks)
        except Exception:
            pass
        self.shelf.note_progress(self.book_path, remote.get("percent", 0), remote.get("updated"), remote.get("device", ""),
                                 local=False)       # 别的设备读的：不写进本设备的进度摘要
        self.statusBar().showMessage(
            f"☁️ 已同步进度（来自 {remote.get('device', '?')}，{remote.get('percent', 0):.1f}%）", 6000)

    def _add_library_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择书库目录（如移动云盘同步文件夹）")
        if not d:
            return
        d = os.path.normpath(d)
        dirs = list(self.cfg.get("library_dirs", []))
        if d not in dirs:
            dirs.append(d)
            self.cfg["library_dirs"] = dirs
            save_json(CONFIG_PATH, self.cfg)
            self.shelf.rescan(force=True)

    def _remove_library_dir(self, d):
        self.cfg["library_dirs"] = [x for x in self.cfg.get("library_dirs", []) if x != d]
        save_json(CONFIG_PATH, self.cfg)
        self.shelf.rescan(force=True)

    def open_book(self):
        libs = self.cfg.get("library_dirs", [])
        start = libs[0] if libs and os.path.isdir(libs[0]) else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "打开小说", start,
            "全部支持 (" + " ".join("*" + e for e in shelfmod.READABLE_EXTS + shelfmod.INAPP_MEDIA_EXTS) + ");;"
            "电子书 (" + " ".join("*" + e for e in shelfmod.READABLE_EXTS) + ");;文本 (*.txt *.md *.markdown);;EPUB (*.epub);;Kindle (*.mobi *.azw *.azw3 *.prc);;网页 (*.html *.htm);;PDF (*.pdf);;Word (*.docx);;PowerPoint (*.pptx);;"
            "有声书 (" + " ".join("*" + e for e in shelfmod.AUDIO_EXTS) + ");;漫画 (" + " ".join("*" + e for e in shelfmod.COMIC_EXTS) + ")")
        if path:
            self.load_book(path)

    def load_book(self, path):
        kind = shelfmod.inapp_kind(path)
        if kind:                             # 音频 / .zip / .cbz：应用内播放器 / 漫画页，不进 loader
            self._open_media(path, kind)
            return
        if shelfmod.is_external(path):       # .cbr / .rar / .7z：没有内置解码器，必须在切页、记进度、起 loader 之前拦下
            self.shelf.launch_file(path)
            return
        self._close_media()                  # 从媒体页转去读文字书：先存断点、放开文件
        self._text_active = True
        self.show_reader()                   # 先切到阅读页，再启动 loader（几何见 show_reader 的说明）
        self._shelf_open_pending = False
        self.shelf.note_opened(path)
        self._tts_stop()
        self._save_progress(force=True)      # 切书前先存当前书的进度
        self._loaded_path = None
        self._pending_remote = None
        self._seed_push, self._seed_remote_none = None, False
        try:
            seed = shelfmod.progress_seed(self.bookmarks, path)    # 云端缓存副本首次打开：带上本机另一路径上读到的进度
        except Exception:
            seed = None                      # 种入只是锦上添花：任何意外都不能挡住打开
        if seed:
            ent = self.bookmarks.setdefault(path, {})
            ent["_progress_"], ent["_progress_ts_"] = seed["_progress_"], seed["_progress_ts_"]
            try:
                save_json(BOOKMARKS_PATH, self.bookmarks)
            except Exception:
                pass
            self._seed_push = path
        self.book_path = path
        self.sync.pull(self.cfg, path)       # 未启用时是 no-op
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
        if self._shelf_open_pending:         # 从书架打开却失败：回书架，而不是停在空白阅读页
            self._shelf_open_pending = False
            self.show_shelf()

    def _on_book_loaded(self, text, chapters, title="", extra=None):
        self.load_bar.hide()
        self._shelf_open_pending = False
        if not self._reader_visible():       # 加载期间用户切回了书架 / 去了媒体页：阅读页尺寸可能是旧的，回来时重排
            self._resized_on_shelf = True
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
        # 版式块文档（PDF/DOCX/PPTX）用真实页数计页码；纯文本/EPUB 仍按字数估算
        self.view.page_info = self.pager.page_info if (not self.view.scan_mode and extra.get("blocks") is not None and not is_md) else None
        self.view.full_text = text
        self.view.book_title = title or os.path.splitext(os.path.basename(self.book_path))[0]
        self._build_toc()
        self._load_bookmarks()
        self.setWindowTitle(os.path.basename(self.book_path) + " — PyReader")
        prog = self.bookmarks.get(self.book_path, {}).get("_progress_", 0)
        self.goto_offset(prog)
        self._update_status()
        self._add_recent(self.book_path)
        self._loaded_path = self.book_path
        self._last_saved_offset = self.view.left_page_offset()
        if self._pending_remote and self._pending_remote[0] == self.book_path:
            _, remote = self._pending_remote
            self._pending_remote = None
            self._apply_remote_progress(remote)
        self._maybe_push_seed()

    # ---- 章节导航（惰性分页的入口） ----
    def load_chapter(self, ci, goto_end=False):
        if not self.pager or not self.pager.chapters:
            return
        ci = max(0, min(len(self.pager.chapters) - 1, ci))
        self.cur_chapter = ci
        self.view.chapter_index = ci
        self.view.cancel_flip()
        self.view.pages = self.pager.pages_of(ci)      # 只分这一章，毫秒级
        self.view.chapter_title = self.pager.chapters[ci].title
        pages = self.pager.pages_of(ci)
        ch = self.pager.chapters[ci]
        self.view.chars_per_page = max(1, (ch.end - ch.start) / max(1, len(pages))) if pages else 400.0
        self.view.spread = (view_count(len(self.view.pages), self.view.ppv) - 1) if goto_end else 0
        self.view.update()

    def goto_offset(self, offset):
        if not self.pager:
            return
        ci = self.pager.chapter_index_at(offset)
        self.load_chapter(ci)
        for p in self.pager.pages_of(ci):
            if p.start <= offset < p.end:
                self.view.spread = view_of_page(p.index, self.view.ppv)
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

    def set_single_page(self, single):
        """切换单页/双页：取消翻页动画，按字符偏移重排并恢复阅读位置。"""
        single = bool(single)
        self.single_act.setChecked(single)
        if single == self.view.single_page:
            return
        off = self.view.left_page_offset()     # 必须在切换前取（spread 含义随每屏页数变化）
        self.view.set_single_page(single)
        self.cfg["single_page"] = single
        save_json(CONFIG_PATH, self.cfg)
        self.reload_current(off)

    def reload_current(self, off=None):
        if not self.pager:
            return
        if off is None:
            off = self.view.left_page_offset()
        self.view.set_layout(self._make_layout())
        self.pager.set_params(self.view.pagination_params())
        self.load_chapter(self.cur_chapter)
        self.goto_offset(off)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.pager:
            if not self._reader_visible():
                self._resized_on_shelf = True   # 隐藏的阅读页拿不到新尺寸：回到阅读页再重排
            else:
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

    # 每本书最多保留的书签数量
    _BM_MAX = 10

    def add_bookmark(self):
        if not self.book_path or not self.pager:
            return
        off = self.view.left_page_offset()
        chapter = next((c.title for c in reversed(self.pager.chapters)
                        if c.start <= off), "未知章节")
        # 优先用选中文字作预览，否则取首行文字（最多 20 字）
        selected = self.view.selected_text().strip().replace("\n", " ")
        preview = (selected[:20] if selected else
                   self.full_text[off:off + 20].replace("\n", " ").strip())

        bms = self.bookmarks.setdefault(self.book_path, {})
        items: list = bms.get("items", [])

        # 同一偏移已有书签 → 给出提示，不重复添加
        if any(b["offset"] == off for b in items):
            self.statusBar().showMessage("⚠️ 当前位置已有书签")
            return

        items.insert(0, {
            "chapter": chapter,
            "offset": off,
            "preview": preview,
        })
        # 超出上限时删除最旧的
        if len(items) > self._BM_MAX:
            items = items[:self._BM_MAX]
        bms["items"] = items
        save_json(BOOKMARKS_PATH, self.bookmarks)
        self._load_bookmarks()
        self.statusBar().showMessage(
            f"🔖 已添加书签（{len(items)}/{self._BM_MAX}）：{chapter}")

    def _load_bookmarks(self):
        self.bm_list.clear()
        bms = self.bookmarks.get(self.book_path, {})
        # 兼容旧格式（dict of bm_xxx keys）
        items = bms.get("items")
        if items is None:
            items = [
                {"chapter": v["chapter"], "offset": v["offset"], "preview": ""}
                for k, v in bms.items()
                if k not in ("_progress_", "items") and isinstance(v, dict)
            ]
        for i, b in enumerate(items):
            label = f"#{i+1}  {b['chapter']}"
            if b.get("preview"):
                label += f"  「{b['preview'][:15]}…」" if len(b.get("preview", "")) > 15 else f"  「{b['preview']}」"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, b["offset"])
            it.setToolTip(f"章节：{b['chapter']}\n偏移：{b['offset']}\n预览：{b.get('preview', '')}")
            self.bm_list.addItem(it)
        # 右键删除单条书签（只连接一次，更新内容时不重复连接）
        if not getattr(self, "_bm_ctx_connected", False):
            self.bm_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.bm_list.customContextMenuRequested.connect(self._bm_ctx_menu)
            self._bm_ctx_connected = True

    def _bm_ctx_menu(self, pos):
        it = self.bm_list.itemAt(pos)
        if not it:
            return
        idx = self.bm_list.row(it)
        menu = QMenu(self)
        menu.addAction("📍 跳转到此书签",
                       lambda: self.goto_offset(it.data(Qt.ItemDataRole.UserRole)))
        menu.addAction("🗑️ 删除此书签", lambda: self._delete_bookmark(idx))
        if self.bm_list.count() > 1:
            menu.addAction("🗑️ 清空所有书签", self._clear_bookmarks)
        menu.exec(self.bm_list.mapToGlobal(pos))

    def _delete_bookmark(self, idx):
        bms = self.bookmarks.get(self.book_path, {})
        items: list = bms.get("items", [])
        if 0 <= idx < len(items):
            removed = items.pop(idx)
            bms["items"] = items
            save_json(BOOKMARKS_PATH, self.bookmarks)
            self._load_bookmarks()
            self.statusBar().showMessage(f"🗑️ 已删除书签：{removed['chapter']}")

    def _clear_bookmarks(self):
        bms = self.bookmarks.get(self.book_path, {})
        bms["items"] = []
        save_json(BOOKMARKS_PATH, self.bookmarks)
        self._load_bookmarks()
        self.statusBar().showMessage("🗑️ 已清空所有书签")

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            old_shelf = (self.cfg.get("shelf_default_view"), self.cfg.get("shelf_default_sort"),
                         bool(self.cfg.get("shelf_start_in_shelf", False)))
            self.set_single_page(dlg.single_page.isChecked())
            self.cfg.update({
                "api_key": dlg.key.text().strip(), "api_base": dlg.base.text().strip(),
                "model": dlg.model.text().strip(), "api_version": dlg.api_version.text().strip(),
                "font_family": dlg.font_family.currentData(),
                "font_size": dlg.font_size.value(), "line_spacing": dlg.line_spacing.value(),
                "para_spacing": dlg.para_spacing.value(),
                "margin_x": dlg.margin_x.value(), "margin_y": dlg.margin_y.value(),
                "outer": dlg.outer.value(), "gutter": dlg.gutter.value(),
                "tts_voice": dlg.tts_voice.currentData(),
                "tts_rate": dlg.tts_rate.currentText(),
                "proxy": dlg.proxy.text().strip(),
                "azure_tts_key": dlg.azure_key.text().strip(),
                "azure_tts_region": dlg.azure_region.text().strip(),
                "azure_tts_key2": dlg.azure_key2.text().strip(),
                "azure_tts_region2": dlg.azure_region2.text().strip(),
                "theme": dlg.theme.currentText(),
                "flip_effect": dlg.flip_effect.currentText(),
                "bilingual": dlg.bilingual.isChecked(),
                "bilingual_target": dlg.bilingual_target.currentText(),
                "webdav_enabled": dlg.webdav_enabled.isChecked(),
                "webdav_url": dlg.webdav_url.text().strip(),
                "webdav_user": dlg.webdav_user.text().strip(),
                "webdav_password": dlg.webdav_password.text(),
                "webdav_allow_insecure": dlg.webdav_allow_insecure.isChecked(),
                "webdav_max_book_mb": dlg.webdav_max_book.currentData(),
                "sync_user_name": dlg.sync_user_name.text().strip() or "default",
                "device_name": dlg.device_name.text().strip() or syncmod.default_device_name(),
                "shelf_default_view": dlg.shelf_view.currentData(),
                "shelf_default_sort": dlg.shelf_sort.currentData(),
                "shelf_start_in_shelf": dlg.shelf_start.isChecked(),
            })
            save_json(CONFIG_PATH, self.cfg)
            new_shelf = (self.cfg["shelf_default_view"], self.cfg["shelf_default_sort"], self.cfg["shelf_start_in_shelf"])
            if dlg.shelf_reset.isChecked() and QMessageBox.question(
                    self, "重置书架状态",
                    "将重置书架的界面偏好，清除索引缓存（含本机记录的阅读百分比和打开时间），\n"
                    "并清空本设备的收藏 / 隐藏 / 进度摘要（本机 overlay.json 与其它设备文件的缓存；\n"
                    "启用云同步时，云端本设备的设备文件也会被清空）。其它设备的状态不受影响。\n"
                    "不会删除书，也不会动阅读书签 / 进度。继续吗？") == QMessageBox.StandardButton.Yes:
                self.shelf.reset_state(*new_shelf)
            elif new_shelf != old_shelf:
                self.shelf.apply_defaults(*new_shelf)
            set_net_proxy(self.cfg["proxy"])
            set_azure_config(self.cfg["azure_tts_key"], self.cfg["azure_tts_region"],
                         self.cfg["azure_tts_key2"], self.cfg["azure_tts_region2"])
            self._apply_theme(self.cfg["theme"])
            self.view._flip_effect = self.cfg["flip_effect"]
            self.bilingual_on = bool(self.cfg["bilingual"])
            if self.bilingual_on:
                self.bilingual_dock.show()
            else:
                self.bilingual_dock.hide()
            self._update_bilingual_act()
            self.reload_current()

    def _show_ctx_menu(self, pos):
        text = self.view.selected_text().strip()
        menu = QMenu(self)
        if text:
            menu.addAction("🌐 翻译选中内容", lambda: self._run_ai(translate, text))
            menu.addAction("📖 查词典", lambda: self._run_ai(lookup, text))
            menu.addSeparator()
            menu.addAction("😊 情感分析", lambda: self._run_ai(analyze_sentiment, text, self.cfg))
            menu.addAction("🔍 背景解读", lambda: self._run_ai(explain_background, text, self.cfg))
            menu.addSeparator()
        menu.addAction("📝 章节摘要", self._ai_summarize)
        menu.addAction("🗺️ 思维导图", self._ai_mindmap)
        menu.addAction("🕸️ 知识图谱", self._ai_knowledge_graph)
        menu.addAction("✍️ 续写建议", self._ai_continue)
        menu.addSeparator()
        menu.addAction("🔖 加入书签", self.add_bookmark)
        menu.exec(self.view.mapToGlobal(pos))

    def _run_ai(self, fn, *args):
        """通用 AI 调用：显示问答 Tab 并异步执行。"""
        self.ai_dock.show()
        self.ai_tabs.setCurrentIndex(0)
        self.ai_out.setPlainText("处理中……")
        self.worker = AiWorker(fn, *args)
        self.worker.done.connect(self.ai_out.setPlainText)
        self.worker.failed.connect(lambda m: self.ai_out.setPlainText("出错：" + m))
        threading.Thread(target=self.worker.run, daemon=True).start()

    # ---- 获取当前章节文本与标题 ----
    def _current_chapter_text(self):
        """返回 (chapter_title, chapter_text)，无书时返回空字符串。"""
        if not self.pager or not self.full_text:
            return "", ""
        ch = self.pager.chapters[self.cur_chapter]
        return ch.title, self.full_text[ch.start:ch.end]

    # ---- 📝 章节摘要 ----
    def _ai_summarize(self):
        if not self._check_api():
            return
        title, text = self._current_chapter_text()
        if not text:
            self.statusBar().showMessage("请先打开一本书")
            return
        self._run_ai(summarize_chapter, text, title, self.cfg)
        self.statusBar().showMessage(f"📝 AI 正在生成《{title}》的摘要…")

    # ---- 🗺️ 思维导图（大纲提取）----
    def _ai_mindmap(self):
        if not self._check_api():
            return
        title, text = self._current_chapter_text()
        if not text:
            self.statusBar().showMessage("请先打开一本书")
            return
        self._run_ai(extract_mindmap, text, title, self.cfg)
        self.statusBar().showMessage(f"🗺️ AI 正在提取《{title}》的思维导图…")

    # ---- ❓ AI 问答（多轮对话）----
    def _run_ai_qa(self):
        if not self._check_api():
            return
        question = self.ai_qa_input.text().strip()
        if not question:
            return
        title, text = self._current_chapter_text()
        if not text:
            self.statusBar().showMessage("请先打开一本书")
            return
        # 章节切换时自动重置对话上下文
        if title != self._qa_context_title:
            self._qa_history.clear()
            self._qa_context_title = title
            self._qa_context_text  = text
        self.ai_qa_input.clear()
        self.ai_dock.show()
        self.ai_tabs.setCurrentIndex(0)
        # 在输出区展示对话历史 + 当前问题
        self._append_qa_bubble("user", question)
        self.ai_out.append("🤖 AI 思考中…")
        history_snapshot = list(self._qa_history)
        ctx_text  = self._qa_context_text
        ctx_title = self._qa_context_title

        def _do_qa():
            return ai_qa(question, ctx_text, ctx_title, self.cfg, history=history_snapshot)

        self.worker = AiWorker(_do_qa)
        self.worker.done.connect(lambda ans: self._on_qa_done(question, ans))
        self.worker.failed.connect(lambda m: self.ai_out.append(f"❌ 出错：{m}"))
        threading.Thread(target=self.worker.run, daemon=True).start()
        self.statusBar().showMessage("❓ AI 书童回答中…")

    def _on_qa_done(self, question: str, answer: str):
        """问答完成：更新对话历史并显示回答。"""
        # 将本轮记入历史（保留最近20轮，避免 token 爆算）
        self._qa_history.append(("user",      question))
        self._qa_history.append(("assistant", answer))
        if len(self._qa_history) > 40:
            self._qa_history = self._qa_history[-40:]
        # 删掉“AI 思考中…”那行
        cur = self.ai_out.toPlainText()
        if cur.endswith("🤖 AI 思考中…"):
            self.ai_out.setPlainText(cur[: -len("🤖 AI 思考中…")].rstrip())
        self._append_qa_bubble("assistant", answer)

    def _append_qa_bubble(self, role: str, text: str):
        """在 ai_out 里追加对话气泡（纯文本模拟）。"""
        prefix = "💬 你" if role == "user" else "🤖 书童"
        separator = "─" * 30
        self.ai_out.append(f"\n{prefix}\n{text}\n{separator}")
        # 滚动到底部
        sb = self.ai_out.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _clear_qa_history(self):
        """\u6e05除对话历史并重置上下文。"""
        self._qa_history.clear()
        self._qa_context_title = ""
        self._qa_context_text  = ""
        self.ai_out.clear()
        self.statusBar().showMessage("🗑️ 对话历史已清除")

    # ---- 🕸️ 知识图谱 ----
    def _ai_knowledge_graph(self):
        if not self._check_api():
            return
        title, text = self._current_chapter_text()
        if not text:
            self.statusBar().showMessage("请先打开一本书")
            return
        self.ai_dock.show()
        self.ai_tabs.setCurrentIndex(1)          # 切换到图谱 Tab
        self.graph_view.setText("⚙️ AI 正在提取知识图谱…\n\n完成后将在系统默认浏览器中打开")
        self.statusBar().showMessage(f"🕸️ AI 正在构建《{title}》的知识图谱…")

        def _build():
            return extract_knowledge_graph(text, title, self.cfg)

        def _on_done(graph_json: str):
            html = build_graph_html(graph_json, title)
            import tempfile, os as _os
            _tmp = _os.path.join(tempfile.gettempdir(), "pyreader_graph.html")
            with open(_tmp, "w", encoding="utf-8") as _f:
                _f.write(html)
            QDesktopServices.openUrl(QUrl.fromLocalFile(_tmp))
            self.graph_view.setText(f"✅ 知识图谱已生成：《{title}》\n\n已用系统默认浏览器打开。")
            self.statusBar().showMessage(f"🕸️ 知识图谱已生成：《{title}》")

        def _on_fail(msg: str):
            self.graph_view.setText(f"❌ 知识图谱生成失败：\n{msg[:200]}")
            self.statusBar().showMessage(f"❌ 知识图谱失败：{msg[:60]}")

        self.worker = AiWorker(_build)
        self.worker.done.connect(_on_done)
        self.worker.failed.connect(_on_fail)
        threading.Thread(target=self.worker.run, daemon=True).start()

    # ---- ✍️ 续写建议 ----
    def _ai_continue(self):
        if not self._check_api():
            return
        _, text = self._current_chapter_text()
        if not text:
            self.statusBar().showMessage("请先打开一本书")
            return
        # 取当前页附近的文字作为续写上文
        off = self.view.left_page_offset()
        snippet = self.full_text[max(0, off - 500):off + 500]
        self._run_ai(continue_story, snippet, self.cfg)
        self.statusBar().showMessage("✍️ AI 正在生成续写建议…")

    # ---- 😊 情感分析（选中文字）----
    def _ai_sentiment_selection(self):
        if not self._check_api():
            return
        text = self.view.selected_text().strip()
        if not text:
            self.statusBar().showMessage("请先选中一段文字")
            return
        self._run_ai(analyze_sentiment, text, self.cfg)
        self.statusBar().showMessage("😊 AI 情感分析中…")

    # ---- 🔍 背景解读（选中文字）----
    def _ai_background_selection(self):
        if not self._check_api():
            return
        text = self.view.selected_text().strip()
        if not text:
            self.statusBar().showMessage("请先选中一段文字")
            return
        self._run_ai(explain_background, text, self.cfg)
        self.statusBar().showMessage("🔍 AI 背景解读中…")

    # ---- 通用：检查 API Key ----
    def _check_api(self):
        if not self.cfg.get("api_key"):
            self.statusBar().showMessage("该功能需要先配置 API Key（工具栏→设置）")
            return False
        return True

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

    # ---- 语音朗读 ----
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
            if self.player.is_paused():
                self.player.resume()
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
        _set_reading_priority(True)
        reset_loudness_state()   # 新一次朗读清空跨句增益平滑状态，避免上一次朗读的增益残留影响这次开头几句
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
        # 过滤纯标点/无语义内容的句子（TTS 无法合成，会报 No audio）
        sents = [(s, e, t) for s, e, t in sents if re.search(r"[0-9A-Za-z\u4e00-\u9fff]", t)]
        sents = [(s, e, t) for s, e, t in sents if re.sub(r"\s+", " ", t).strip()]
        self.tts_chapter = ci
        self.tts_sentences = sents
        self.tts_epoch += 1            # 换章：旧缓存/在途结果全部作废
        self.tts_cache.clear(); self.tts_inflight.clear(); self.tts_workers.clear()
        self.tts_failed_seqs.clear()
        with self.tts_pending_lock:
            self.tts_pending.clear()
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
        voice = self.cfg.get("tts_voice", TTS_DEFAULT_VOICE)
        if seq == 0 and voice.startswith("kokoro:") and not kokoro_model_ready():
            self.statusBar().showMessage("⏬ 首次使用，正在下载离线语音模型（约 140MB，需 1~3 分钟），完成后自动开始朗读…")
        elif seq == 0 and voice.startswith("sherpa:") and not sherpa_model_ready():
            self.statusBar().showMessage("⏬ 首次使用，正在下载离线语音模型（约 190MB，需 1~3 分钟），完成后自动开始朗读…")
        else:
            self.statusBar().showMessage(
                f"🔊 朗读中 · 第{self.tts_chapter + 1}章 · {seq + 1}/{len(self.tts_sentences)}句")
        if self.bilingual_on:
            self._update_bilingual(seq)
            self._tr_kick(seq)
        data = self.tts_cache.pop(seq, None)
        if data is not None:
            self._tts_play_data(seq, data)
        else:
            # 还没合成好：先把后续几句排进队列（避免出现"等合成"的空白）
            self._tts_kick(seq)
            self.statusBar().showMessage(
                f"⚙️ 合成中… 第{self.tts_chapter + 1}章 · {seq + 1}/{len(self.tts_sentences)}句")
        self._tts_fill()
        if self.bilingual_on:
            nxt = seq + 1
            if nxt < len(self.tts_sentences):
                self._tr_kick(nxt)

    def _tts_fill(self):
        """把「当前句之后 TTS_PREFETCH 句」排进合成队列。

        原来只预取下一句（nxt = seq + 1），而 Melo 合成速度约 0.87×实时、且按句长波动很大：
        短句（如“他走了。”0.8s）后面跟一个长句（需 6s 合成）时，露白 = 6 - 0.8 ≈ 5.2 秒。
        改成维持 3 句的缓冲后，队列天然吸收这种波动。"""
        if not self.tts_on or not self.tts_sentences:
            return
        end = min(len(self.tts_sentences), self.tts_idx + TTS_PREFETCH + 1)
        for seq in range(self.tts_idx, end):
            self._tts_kick(seq)

    def _tts_kick(self, seq):
        """把某一句排进合成队列（实际合成由单一后台线程按 FIFO 消费）。"""
        if seq in self.tts_inflight or seq in self.tts_cache or seq in self.tts_failed_seqs:
            return
        if seq >= len(self.tts_sentences):
            return
        with self.tts_pending_lock:
            if seq in self.tts_pending:
                return
            self.tts_pending.append(seq)
        self.tts_pending_event.set()             # 唤醒合成线程
        self._tts_start_dispatcher()

    def _tts_start_dispatcher(self):
        """只创建一个常驻合成线程（daemon），开销为 0（空闲时阻塞在 Event 上）。

        不用"队列空了就退出、有活再重建"的写法：那样在"线程即将退出"与"主线程刚好入队"之间
        有竞争窗口，漏掉一次唤醒就是几百毫秒到几秒的停顿。"""
        if self.tts_dispatcher is not None and self.tts_dispatcher.is_alive():
            return
        self.tts_dispatcher = threading.Thread(target=self._tts_dispatch_loop, daemon=True)
        self.tts_dispatcher.start()

    def _tts_claim(self):
        """从待合成队列里取下一个（最小序号），标记为 inflight；队列空返回 None。"""
        with self.tts_pending_lock:
            while self.tts_pending:
                s = min(self.tts_pending)
                self.tts_pending.remove(s)
                if s in self.tts_inflight or s in self.tts_cache or s in self.tts_failed_seqs:
                    continue                      # 已不需要（已合成/已失败/在途）
                if s >= len(self.tts_sentences):
                    continue
                clean = re.sub(r"\s+", " ", self.tts_sentences[s][2]).strip()
                if not clean:
                    continue
                self.tts_inflight.add(s)
                return s, self.tts_epoch, clean
        return None

    def _tts_dispatch_loop(self):
        """单一常驻合成线程：按序号 FIFO 合成，保证"先播的句子先合成"。

        以前是每句起一个线程，但 Melo/Kokoro 的模型调用本来就被 _sherpa_lock 串行化，
        多线程只会让排队顺序不可控（恰好当前句被排到最后 → 又变成停顿）。"""
        while True:
            self.tts_pending_event.wait()        # 无任务时阻塞，不占 CPU
            self.tts_pending_event.clear()
            while True:
                job = self._tts_claim()
                if job is None:
                    break
                seq, epoch, clean = job
                try:
                    data = synthesize_tts(clean,
                                          self.cfg.get("tts_voice", TTS_DEFAULT_VOICE),
                                          self.cfg.get("tts_rate", "+0%"))
                except Exception as e:
                    # 信号是 QueuedConnection：回到主线程后才改 inflight/缓存/播放
                    self.ttsSynthFailed.emit(epoch, seq, str(e))
                    continue
                self.ttsSynthReady.emit(epoch, seq, data)

    def _tts_on_ready(self, epoch, seq, data):
        if not self.tts_on or epoch != self.tts_epoch:
            return
        self.tts_workers.pop((epoch, seq), None)
        self.tts_inflight.discard(seq)
        if seq == self.tts_idx and not self.tts_paused and not self.player.is_playing():
            self._tts_play_data(seq, data)     # 当前句：直接播，不入缓存
        else:
            self.tts_cache[seq] = data         # 预取句：先缓存
        self._tts_fill()                        # 存好一句就顺手把窗口补满

    def _tts_play_data(self, seq, data):
        if not self.tts_on or seq != self.tts_idx:
            return
        if not data:
            self._tts_advance()
            return
        self.player.play_wav(data)

    def _tts_advance(self):
        nxt = self.tts_idx + 1
        while nxt < len(self.tts_sentences) and nxt in self.tts_failed_seqs:
            nxt += 1
        if nxt < len(self.tts_sentences):
            self._tts_start_sentence(nxt)
        else:
            self._tts_next_chapter()

    def _on_tts_sentence_finished(self):
        # 延迟到下一轮事件循环，避免在播放完成回调里重入引发下一句合成/播放导致卡死
        QTimer.singleShot(0, self._tts_advance)

    def _tts_on_failed(self, epoch, seq, msg):
        """单句合成失败：跳过该句继续朗读，而不是中断整章。"""
        if not self.tts_on or epoch != self.tts_epoch:
            return
        self.tts_inflight.discard(seq)
        self.tts_workers.pop((epoch, seq), None)
        self.tts_failed_seqs.add(seq)
        _log_tts(f"[跳过] 第{seq + 1}句合成失败：{msg}")
        if seq == self.tts_idx:      # 当前句失败 → 直接读下一句
            self.statusBar().showMessage(f"跳过无法朗读的句子（{seq + 1}/{len(self.tts_sentences)}）")
            self._tts_advance()
        else:
            self._tts_fill()         # 预取句失败 → 把窗口补满

    def _tts_stop(self, finished=False):
        was_on = self.tts_on
        self.tts_on = False; self.tts_paused = False
        _set_reading_priority(False)
        self.tts_idx = 0
        self.tts_chapter = self.cur_chapter
        self.tts_epoch += 1
        self.tts_sentences = []
        self.tts_cache = {}; self.tts_inflight = set(); self.tts_workers = {}
        self.tts_failed_seqs = set()
        with self.tts_pending_lock:
            self.tts_pending.clear()
        self.tr_cache = {}; self.tr_inflight = set(); self.tr_workers = {}
        if self.player:
            self.player.stop()
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
        if self._shelf_visible():
            self.stop_act.setEnabled(False)     # 书架可见：依赖阅读页的动作一律禁用

    def _reveal_offset(self, off):
        if not self.pager:
            return
        pages = self.view.pages
        vis = [pages[i] for i in self.view.visible_page_indices() if i < len(pages)]
        if vis and vis[0].start <= off < vis[-1].end:
            self.view.update()
            return
        self.goto_offset(off)

    def _cycle_progress_mode(self):
        """pos_label 点击循环：全书% → 本章页码 → 预估剩余分钟 → 循环"""
        self._progress_mode = (self._progress_mode + 1) % 3
        self._update_status()

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
            # 页碉/页码数据
            ch = self.pager.chapters[self.cur_chapter]
            self.view.chapter_title = ch.title
            pages = self.pager.pages_of(self.cur_chapter)
            self.view.chars_per_page = max(1, (ch.end - ch.start) / max(1, len(pages))) if pages else 400.0
            # 根据模式更新进度标签
            mode = getattr(self, "_progress_mode", 0)
            if mode == 1:
                # 本章页码
                cur_pg  = self.view.spread + 1
                tot_pg  = max(1, view_count(len(pages), self.view.ppv))
                label   = f"{cur_pg}/{tot_pg}页"
                tip     = "本章页码（点击切换）"
            elif mode == 2:
                # 预估剩余分钟（按平均阅读速度 400字/分钟计算）
                chars_left = max(0, len(self.full_text) - off)
                mins = chars_left / 400
                label = (f"{int(mins)}分钟" if mins >= 1 else "<1分钟")
                tip   = "预估剩余阅读时间（点击切换）"
            else:
                label = f"{pct:.1f}%"
                tip   = "全书阅读进度（点击切换）"
            self.pos_label.setText(label)
            self.pos_label.setToolTip(tip)
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
            if os.path.exists(p) and shelfmod.is_readable(p):     # 最近打开只列文字书（媒体的断点在书架 / 进度里）
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

    # ---- 主题快速切换 ----
    def _rebuild_theme_menu(self):
        """(重新)构建主题色板菜单，每个条目左侧匹配一个彩色小色块。"""
        self.theme_menu.clear()
        cur = self.cfg.get("theme", DEFAULT_THEME)
        for name, colors in THEMES.items():
            action = self.theme_menu.addAction(name, lambda n=name: self._apply_theme(n))
            # 用页面和文字颜色渲染小色块图标
            pm = QPixmap(16, 16)
            pm.fill(QColor(colors["page"]))
            _p = QPainter(pm)
            _p.fillRect(2, 6, 12, 4, QColor(colors["text"]))
            _p.end()
            action.setIcon(QIcon(pm))
            if name == cur:
                action.setCheckable(True)
                action.setChecked(True)

    def _apply_theme(self, name: str):
        """立即切换主题，保存配置，刷新工具栏菜单勾选状态。"""
        self.cfg["theme"] = name
        save_json(CONFIG_PATH, self.cfg)
        self.view.set_theme(THEMES.get(name, THEMES[DEFAULT_THEME]))
        self.comic.set_theme(THEMES.get(name, THEMES[DEFAULT_THEME]))
        self._rebuild_theme_menu()
        self.view.update()
        self.statusBar().showMessage(f"🎨 已切换主题：{name}")

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

    # ---- 全屏：工具栏收起，鼠标移到窗口顶部才浮出（悬浮覆盖，不挤占阅读区，所以不会触发重排）----
    _FS_TOP_BAND = 40       # 顶部悬停触发区高度（像素）

    def toggle_fullscreen(self):
        tb = self._toolbar
        if not self.isFullScreen():
            self._fs_was_max = self.isMaximized()
            self.removeToolBar(tb)           # 脱离主窗口布局，作为子控件浮在阅读区上方
            tb.setParent(self)
            tb.hide()
            self.statusBar().hide()
            self.showFullScreen()
            if getattr(self, "_fs_timer", None) is None:
                self._fs_timer = QTimer(self)
                self._fs_timer.setInterval(120)
                self._fs_timer.timeout.connect(self._fs_tick)
            self._fs_timer.start()
        else:
            self._fs_timer.stop()
            tb.hide()
            self.addToolBar(tb)              # 回到原来的停靠位置
            tb.show()
            self.statusBar().show()
            if getattr(self, "_fs_was_max", False):
                self.showMaximized()
            else:
                self.showNormal()

    def _fs_tick(self):
        if not self.isFullScreen():
            return
        tb = self._toolbar
        y = self.mapFromGlobal(QCursor.pos()).y()
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        popup = QApplication.activePopupWidget() is not None   # 工具栏的菜单（如「定时」）展开时别收起
        if tb.isVisible():
            if popup or (inside and y <= tb.height() + 10):
                return
            tb.hide()
        elif inside and y <= self._FS_TOP_BAND and self.isActiveWindow():
            tb.setGeometry(0, 0, self.width(), tb.sizeHint().height())
            tb.show()
            tb.raise_()

    def keyPressEvent(self, e):
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if e.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            return
        if self._shelf_visible():
            # 书架可见：翻页键 / F3 绝不传给隐藏的阅读页（否则会翻页、触发 pageChanged 与进度推送）；Ctrl+F 聚焦书架自己的搜索框
            if e.key() == Qt.Key.Key_F and ctrl:
                self.shelf.focus_search()
                return
            if e.key() == Qt.Key.Key_Escape and self.shelf.handle_escape():
                return
            if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                           Qt.Key.Key_Space, Qt.Key.Key_F3):
                e.accept()
                return
            super().keyPressEvent(e)
            return
        if e.key() == Qt.Key.Key_Escape and not (e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier):
            self.show_shelf()                # 阅读页 / 媒体页 Esc → 书架
            return
        if not self._reader_visible():       # 媒体页：翻页 / 播放键由页面自己处理，不能漏给隐藏的阅读页
            if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                           Qt.Key.Key_Space, Qt.Key.Key_F3):
                e.accept()
                return
            super().keyPressEvent(e)
            return
        if e.key() == Qt.Key.Key_F and ctrl:
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
        _t0 = _time.monotonic()
        self.tts_on = False
        if self.player:
            self.player.stop()
        t = self._close_media()
        if self.book_path and self.pager and self._text_active:
            if self._loaded_path == self.book_path:
                t = self._save_progress(force=True) or t
            else:   # 还在加载中：保持旧行为
                self.bookmarks.setdefault(self.book_path, {})["_progress_"] = self.view.left_page_offset()
                save_json(BOOKMARKS_PATH, self.bookmarks)
        if t is not None:
            t.join(3)   # 给远端推送一点时间（daemon 线程不会阻止退出）
        # 有未推送的收藏 / 隐藏 / 进度摘要就有界地推一次：与上面的阅读进度推送合计最多等 3 秒（各只 join 一次）
        self.shelf.flush_cloud(max(0.3, 3.0 - (_time.monotonic() - _t0)))
        self.shelf.flush()
        if self._shelf_lock is not None:
            self._shelf_lock.release()
        super().closeEvent(e)
