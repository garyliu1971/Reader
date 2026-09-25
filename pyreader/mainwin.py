import os
import re
import threading
from typing import Optional
from PySide6.QtCore import Qt, QTimer, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QMainWindow, QWidget, QListWidget, QListWidgetItem,
                               QToolBar, QMenu, QFontDialog, QTextEdit, QVBoxLayout, QLabel,
                               QFileDialog, QMessageBox, QProgressBar, QSlider, QDockWidget, QLineEdit)
from .config import (CONFIG_PATH, BOOKMARKS_PATH, OUTER, MARGIN_X, MARGIN_Y, GUTTER,
                     LINE_SPACING, PARA_SPACING, _HAS_MULTIMEDIA, QMediaPlayer, QAudioOutput,
                     load_json, save_json, default_cjk_font, THEMES, DEFAULT_THEME, DEFAULT_CONFIG)
from .markdown import md_chapters, MD_HEAD_RE
from .textio import MD_EXTS
from .pager import LazyPager
from .loader import BookLoader
from .view import PageView
from .settings import SettingsDialog
from .ai import translate, lookup, AiWorker
from .tts import TtsWorker, TranslateWorker, split_sentences, TTS_DEFAULT_VOICE, _log_tts

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
