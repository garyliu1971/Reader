from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QComboBox, QLineEdit, QCheckBox, QDoubleSpinBox,
                               QSpinBox, QFormLayout, QDialogButtonBox, QLabel,
                               QVBoxLayout, QHBoxLayout, QScrollArea, QWidget, QFrame, QTabWidget,
                               QListWidget, QListWidgetItem, QPushButton, QMessageBox)
from PySide6.QtGui import QGuiApplication
from .config import AI_PRESETS, THEMES, DEFAULT_THEME, FLIP_EFFECTS, DEFAULT_FLIP_EFFECT
from .tts import TTS_VOICES, TTS_DEFAULT_VOICE, TTS_RATES, winrt_voices, sapi_voices
from .shelfview import SORT_LABELS as SHELF_SORT_LABELS


# ============ 额外书库源编辑对话框 ============
class _DavSourceDialog(QDialog):
    """编辑一个额外的只读 WebDAV 书库（name 兼作书架目录前缀 / 命名空间）。"""

    def __init__(self, src=None, parent=None):
        super().__init__(parent)
        src = src if isinstance(src, dict) else {}
        self.setWindowTitle("编辑额外书库" if src else "添加额外书库")
        self.name = QLineEdit(src.get("name", ""))
        self.name.setPlaceholderText("书架里显示的目录名，如 夸克网盘（不能含 / \\ :，不能以 . 或 ~$ 开头）")
        self.url = QLineEdit(src.get("url", ""))
        self.url.setPlaceholderText("如 http://192.168.1.5:5244/dav（alist 的 WebDAV 端点）")
        self.user = QLineEdit(src.get("user", ""))
        self.user.setPlaceholderText("匿名服务器可留空")
        self.password = QLineEdit(src.get("password", ""))
        self.password.setEchoMode(QLineEdit.Password)
        self.allow_insecure = QCheckBox("允许明文 http 向公网主机发送密码（不安全，建议用 https）")
        self.allow_insecure.setChecked(bool(src.get("allow_insecure", False)))
        self.enabled = QCheckBox("启用该书库")
        self.enabled.setChecked(bool(src.get("enabled", True)))
        form = QFormLayout()
        form.addRow("名称", self.name)
        form.addRow("WebDAV 地址", self.url)
        form.addRow("用户名", self.user)
        form.addRow("密码", self.password)
        form.addRow("", self.allow_insecure)
        form.addRow("", self.enabled)
        btn = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn.accepted.connect(self.accept)
        btn.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(btn)
        self.resize(560, self.sizeHint().height())

    def result_source(self) -> dict:
        return {"name": self.name.text().strip(),
                "url": self.url.text().strip(),
                "user": self.user.text().strip(),
                "password": self.password.text(),
                "allow_insecure": self.allow_insecure.isChecked(),
                "enabled": self.enabled.isChecked()}


# ============ 设置对话框 ============
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.preset = QComboBox()
        self.preset.addItems(list(AI_PRESETS.keys()))
        self.key = QLineEdit(cfg.get("api_key", "")); self.key.setEchoMode(QLineEdit.Password)
        self.base = QLineEdit(cfg.get("api_base", ""))
        self.model = QLineEdit(cfg.get("model", ""))
        self.api_version = QLineEdit(cfg.get("api_version", ""))
        self.api_version.setPlaceholderText("仅旧式 Azure 部署路径需要，如 2024-10-21")
        # 排版参数
        self.font_family = QComboBox()
        self.font_family.addItem("系统默认", "")
        try:
            from PySide6.QtGui import QFontDatabase
            avail = set(QFontDatabase.families())
            for name in ("微软雅黑", "Microsoft YaHei", "宋体", "SimSun", "楷体", "KaiTi",
                         "黑体", "SimHei", "等线", "DengXian", "仿宋", "FangSong",
                         "思源宋体", "Source Han Serif SC", "Noto Serif CJK SC",
                         "思源黑体", "Source Han Sans SC", "PingFang SC"):
                if name in avail:
                    self.font_family.addItem(name, name)
        except Exception:
            pass
        i = self.font_family.findData(cfg.get("font_family", ""))
        self.font_family.setCurrentIndex(i if i >= 0 else 0)
        self.theme = QComboBox()
        self.theme.addItems(list(THEMES.keys()))
        ti = self.theme.findText(cfg.get("theme", DEFAULT_THEME))
        self.theme.setCurrentIndex(ti if ti >= 0 else 0)
        self.flip_effect = QComboBox()
        self.flip_effect.addItems(FLIP_EFFECTS)
        fi = self.flip_effect.findText(cfg.get("flip_effect", DEFAULT_FLIP_EFFECT))
        self.flip_effect.setCurrentIndex(fi if fi >= 0 else 0)
        self.single_page = QCheckBox("单页显示（默认双页跨页）")
        self.single_page.setChecked(bool(cfg.get("single_page", False)))
        self.font_size = QSpinBox(); self.font_size.setRange(10, 48)
        self.font_size.setValue(cfg.get("font_size", 16))
        self.line_spacing = QDoubleSpinBox(); self.line_spacing.setRange(0.8, 3.0)
        self.line_spacing.setSingleStep(0.05); self.line_spacing.setValue(cfg.get("line_spacing", 1.25))
        self.para_spacing = QDoubleSpinBox(); self.para_spacing.setRange(0.0, 3.0)
        self.para_spacing.setSingleStep(0.1); self.para_spacing.setValue(cfg.get("para_spacing", 0.5))
        self.margin_x = QSpinBox(); self.margin_x.setRange(0, 120)
        self.margin_x.setValue(cfg.get("margin_x", 24))
        self.margin_y = QSpinBox(); self.margin_y.setRange(0, 120)
        self.margin_y.setValue(cfg.get("margin_y", 44))
        self.outer = QSpinBox(); self.outer.setRange(0, 80)
        self.outer.setValue(cfg.get("outer", 16))
        self.gutter = QSpinBox(); self.gutter.setRange(0, 160)
        self.gutter.setValue(cfg.get("gutter", 32))
        # 语音朗读
        self.tts_voice = QComboBox()
        for label, vid in TTS_VOICES + winrt_voices() + sapi_voices():
            self.tts_voice.addItem(label, vid)
        vi = self.tts_voice.findData(cfg.get("tts_voice", TTS_DEFAULT_VOICE))
        self.tts_voice.setCurrentIndex(vi if vi >= 0 else 0)
        self.azure_key = QLineEdit(cfg.get("azure_tts_key", "")); self.azure_key.setEchoMode(QLineEdit.Password)
        self.azure_region = QLineEdit(cfg.get("azure_tts_region", ""))
        self.azure_region.setPlaceholderText("如 eastus；仅选用 Azure 音色时需要")
        self.azure_key2 = QLineEdit(cfg.get("azure_tts_key2", "")); self.azure_key2.setEchoMode(QLineEdit.Password)
        self.azure_region2 = QLineEdit(cfg.get("azure_tts_region2", ""))
        self.azure_region2.setPlaceholderText("备用账号区域，如 eastus；主账号失败/额度用尽时自动切换")
        self.tts_rate = QComboBox()
        self.tts_rate.addItems(TTS_RATES)
        ri = self.tts_rate.findText(cfg.get("tts_rate", "+0%"))
        self.tts_rate.setCurrentIndex(ri if ri >= 0 else 3)
        # 网络代理：Reader 只认这里（不读 Windows 系统代理/注册表），留空 = 直连
        self.proxy = QLineEdit(cfg.get("proxy", ""))
        self.proxy.setPlaceholderText("如 http://127.0.0.1:1081（sing-box mixed 入站）；留空 = 直连")
        # WebDAV 进度同步
        self.webdav_enabled = QCheckBox("启用 WebDAV 阅读进度同步")
        self.webdav_enabled.setChecked(bool(cfg.get("webdav_enabled", False)))
        self.webdav_url = QLineEdit(cfg.get("webdav_url", ""))
        self.webdav_url.setPlaceholderText("如 http://127.0.0.1:8080/reader")
        self.webdav_user = QLineEdit(cfg.get("webdav_user", ""))
        self.webdav_user.setPlaceholderText("匿名服务器可留空（用户名和密码都空则不发认证头）")
        self.webdav_password = QLineEdit(cfg.get("webdav_password", ""))
        self.webdav_password.setEchoMode(QLineEdit.Password)
        self.webdav_allow_insecure = QCheckBox("允许明文 http 向公网主机发送密码（不安全，建议用 https）")
        self.webdav_allow_insecure.setChecked(bool(cfg.get("webdav_allow_insecure", False)))
        self.webdav_max_book = QComboBox()
        for mb, label in ((1024, "1 GB"), (4096, "4 GB"), (10240, "10 GB（有声书 / 漫画 ZIP）"), (-1, "不限制")):
            self.webdav_max_book.addItem(label, mb)
        mi = self.webdav_max_book.findData(cfg.get("webdav_max_book_mb", 1024))
        self.webdav_max_book.setCurrentIndex(mi if mi >= 0 else 0)
        self.sync_user_name = QLineEdit(cfg.get("sync_user_name", "default"))
        self.device_name = QLineEdit(cfg.get("device_name", ""))
        # 额外的只读 WebDAV 书库（多数据源：只列书目 + 下载，不记进度、不同步；主书库见上面的 WebDAV 同步）
        self.webdav_libraries = [dict(s) for s in (cfg.get("webdav_libraries") or []) if isinstance(s, dict)]
        self.webdav_sources_list = QListWidget()
        self.webdav_sources_list.setMinimumHeight(96)
        self.webdav_sources_list.setToolTip("额外的只读 WebDAV 书库（数据源），如 alist 挂载的夸克网盘；"
                                            "只列书目、可下载，不记录进度、不做跨设备同步。名称会成为书架里的目录前缀。")
        self._reload_sources_list()
        self._ws_add = QPushButton("添加…")
        self._ws_edit = QPushButton("编辑…")
        self._ws_remove = QPushButton("删除")
        self._ws_add.clicked.connect(self._add_source)
        self._ws_edit.clicked.connect(self._edit_source)
        self._ws_remove.clicked.connect(self._remove_source)
        ws_row = QWidget()
        ws_h = QHBoxLayout(ws_row)
        ws_h.setContentsMargins(0, 0, 0, 0)
        for b in (self._ws_add, self._ws_edit, self._ws_remove):
            ws_h.addWidget(b)
        ws_h.addStretch(1)
        # 书架：默认视图 / 默认排序 / 启动时显示书架 / 重置
        self.shelf_view = QComboBox()
        self.shelf_view.addItem("网格", "grid")
        self.shelf_view.addItem("列表", "list")
        vi2 = self.shelf_view.findData(cfg.get("shelf_default_view", "grid"))
        self.shelf_view.setCurrentIndex(vi2 if vi2 >= 0 else 0)
        self.shelf_sort = QComboBox()
        for key, label in SHELF_SORT_LABELS:
            self.shelf_sort.addItem(label, key)
        si2 = self.shelf_sort.findData(cfg.get("shelf_default_sort", "last_read"))
        self.shelf_sort.setCurrentIndex(si2 if si2 >= 0 else 0)
        self.shelf_start = QCheckBox("启动时显示书架")
        self.shelf_start.setChecked(bool(cfg.get("shelf_start_in_shelf", False)))
        self.shelf_start.setToolTip("不勾选时：有最近阅读的书则进入阅读页，否则显示书架")
        self.shelf_reset = QCheckBox("点「确定」时重置书架状态")
        self.shelf_reset.setChecked(False)
        self.shelf_reset.setToolTip("重置书架的界面偏好与索引缓存（含本机记录的阅读百分比和打开时间），并清空本设备的收藏 / 隐藏 / 进度摘要"
                                    "（启用云同步时，云端本设备的设备文件也会被清空；其它设备的状态不受影响）。"
                                    "不删除书，也不动阅读书签 / 进度。")
        # 双语朗读（朗读时 AI 翻译）
        self.bilingual = QCheckBox("朗读时自动翻译并显示")
        self.bilingual.setChecked(bool(cfg.get("bilingual", False)))
        self.bilingual_target = QComboBox()
        self.bilingual_target.addItems(["中文", "英文"])
        bi = self.bilingual_target.findText(cfg.get("bilingual_target", "中文"))
        self.bilingual_target.setCurrentIndex(bi if bi >= 0 else 0)
        # 根据当前 base 反推预设（先设值，后连信号，避免误触发覆盖用户自定义模型）
        cur = cfg.get("api_base", "").rstrip("/")
        matched = False
        for name, (b, m) in AI_PRESETS.items():
            if b and b.rstrip("/") == cur:
                self.preset.setCurrentText(name); matched = True; break
        if not matched:
            self.preset.setCurrentText("自定义")
        self.preset.currentTextChanged.connect(self._apply)
        # 按功能分页签；每页放进滚动区，按钮固定在底部。顶部搜索框按「名称 / 提示 / 占位符」过滤各页的行
        self._rows = []                      # [(form, row, tab_index, 搜索文本)]
        self._tab_titles = []
        self.tabs = QTabWidget()
        self._pages = []

        def page(title, hint=""):
            content = QWidget()
            form = QFormLayout(content)
            if hint:
                note = QLabel(hint)
                note.setWordWrap(True)
                note.setStyleSheet("color: gray;")
                form.addRow(note)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setWidget(content)
            idx = self.tabs.addTab(sc, title)
            self._tab_titles.append(title)
            self._pages.append(content)
            return form, idx

        def add(form_idx, label, widget):
            form, idx = form_idx
            form.addRow(label, widget)
            text = [label, widget.toolTip()]
            if isinstance(widget, QLineEdit):
                text.append(widget.placeholderText())
            if isinstance(widget, QCheckBox):
                text.append(widget.text())
            if isinstance(widget, QComboBox):
                text += [widget.itemText(i) for i in range(widget.count())]
            self._rows.append((form, form.rowCount() - 1, idx, " ".join(t for t in text if t).lower()))

        ai = page("AI", "用于 AI 工具、翻译、双语朗读。")
        add(ai, "服务商预设", self.preset)
        add(ai, "API Key", self.key)
        add(ai, "API Base", self.base)
        add(ai, "模型 / 部署名", self.model)
        add(ai, "API Version", self.api_version)
        add(ai, "双语翻译", self.bilingual)
        add(ai, "翻译目标", self.bilingual_target)

        lay = page("排版", "调整后立即生效。")
        add(lay, "字体", self.font_family)
        add(lay, "主题", self.theme)
        add(lay, "翻页方式", self.flip_effect)
        add(lay, "单页模式", self.single_page)
        add(lay, "字号", self.font_size)
        add(lay, "行距", self.line_spacing)
        add(lay, "段落间距", self.para_spacing)
        add(lay, "左右边距", self.margin_x)
        add(lay, "上下边距", self.margin_y)
        add(lay, "页边距(外)", self.outer)
        add(lay, "书脊", self.gutter)

        tts = page("朗读", "Melo/Kokoro/Piper/系统语音 为离线音色；Azure 音色需联网和 Speech Key。")
        add(tts, "朗读音色", self.tts_voice)
        add(tts, "朗读语速", self.tts_rate)
        add(tts, "Azure Speech Key", self.azure_key)
        add(tts, "Azure 区域", self.azure_region)
        add(tts, "Azure 备用 Key", self.azure_key2)
        add(tts, "Azure 备用区域", self.azure_region2)

        net = page("同步与网络", "WebDAV 同步阅读进度、书架和云端书库；网络代理对翻译、朗读和同步都生效。")
        add(net, "网络代理", self.proxy)
        add(net, "WebDAV 同步", self.webdav_enabled)
        add(net, "WebDAV 地址", self.webdav_url)
        add(net, "WebDAV 用户名", self.webdav_user)
        add(net, "WebDAV 密码", self.webdav_password)
        add(net, "", self.webdav_allow_insecure)
        add(net, "额外书库（只读）", self.webdav_sources_list)
        add(net, "", ws_row)
        add(net, "单个文件上限", self.webdav_max_book)
        add(net, "同步用户名", self.sync_user_name)
        add(net, "设备名", self.device_name)

        shelf = page("书架", "默认视图 / 排序在改动后立即应用。")
        add(shelf, "默认视图", self.shelf_view)
        add(shelf, "默认排序", self.shelf_sort)
        add(shelf, "启动", self.shelf_start)
        add(shelf, "重置", self.shelf_reset)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索设置（如 key、代理、字号、WebDAV）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        btn = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn.accepted.connect(self.accept); btn.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addWidget(self.search)
        root.addWidget(self.tabs)
        root.addWidget(btn)
        # 以最高的一页为准；高度不超过当前屏幕可用高度的 85%
        want_h = max(c.sizeHint().height() for c in self._pages) + btn.sizeHint().height()             + self.search.sizeHint().height() + self.tabs.tabBar().sizeHint().height() + 48
        want_w = max(max(c.sizeHint().width() for c in self._pages) + 40, self.tabs.tabBar().sizeHint().width() + 24, 560)
        scr = QGuiApplication.primaryScreen()
        max_h = int(scr.availableGeometry().height() * 0.85) if scr else want_h
        self.resize(want_w, min(want_h, max_h))

    # ---- 额外书库源（多数据源）的列表编辑 ----
    def _reload_sources_list(self):
        self.webdav_sources_list.clear()
        for s in self.webdav_libraries:
            label = (s.get("name") or "?").strip()
            url = (s.get("url") or "").strip()
            tail = f"  —  {url}" if url else ""
            if not s.get("enabled", True):
                label += "（停用）"
            item = QListWidgetItem(label + tail)
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.webdav_sources_list.addItem(item)

    def _add_source(self):
        dlg = _DavSourceDialog(None, self)
        if dlg.exec():
            src = dlg.result_source()
            if self._validate_source(src):
                self.webdav_libraries.append(src)
                self._reload_sources_list()

    def _edit_source(self):
        row = self.webdav_sources_list.currentRow()
        if row < 0:
            return
        dlg = _DavSourceDialog(self.webdav_libraries[row], self)
        if dlg.exec():
            src = dlg.result_source()
            if self._validate_source(src):
                self.webdav_libraries[row] = src
                self._reload_sources_list()

    def _remove_source(self):
        row = self.webdav_sources_list.currentRow()
        if row >= 0:
            del self.webdav_libraries[row]
            self._reload_sources_list()

    def _validate_source(self, src: dict) -> bool:
        from .store import safe_segment, is_hidden_name
        name = src.get("name", "")
        if not name or safe_segment(name) is None or is_hidden_name(name):
            QMessageBox.warning(self, "名称不合法",
                                "名称不能为空，不能含 / \\ : 或控制字符，不能以 . 或 ~$ 开头，"
                                "不能是 Windows 保留名（如 CON、NUL）。")
            return False
        if not src.get("url", "").strip():
            QMessageBox.warning(self, "地址为空", "请填写 WebDAV 地址。")
            return False
        return True

    def _filter(self, text):
        """按空格分词，所有词都出现在该行的搜索文本里才显示；页签标题带上匹配行数，并跳到第一个有匹配的页。"""
        words = [w for w in (text or "").lower().split() if w]
        hits = [0] * len(self._tab_titles)
        for form, row, idx, hay in self._rows:
            ok = all(w in hay for w in words)
            form.setRowVisible(row, ok)
            hits[idx] += 1 if ok else 0
        for i, title in enumerate(self._tab_titles):
            self.tabs.setTabText(i, f"{title} ({hits[i]})" if words else title)
            self.tabs.setTabEnabled(i, not words or hits[i] > 0)
        if words and not self.tabs.isTabEnabled(self.tabs.currentIndex()):
            first = next((i for i, n in enumerate(hits) if n), None)
            if first is not None:
                self.tabs.setCurrentIndex(first)

    def _apply(self, name):
        b, m = AI_PRESETS.get(name, ("", ""))
        if b: self.base.setText(b)
        if m: self.model.setText(m)
