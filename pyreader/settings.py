from PySide6.QtWidgets import (QDialog, QComboBox, QLineEdit, QCheckBox, QDoubleSpinBox,
                               QSpinBox, QFormLayout, QDialogButtonBox, QLabel)
from .config import AI_PRESETS, THEMES, DEFAULT_THEME
from .tts import TTS_VOICES, TTS_DEFAULT_VOICE, TTS_RATES

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
        # 语音朗读（edge-tts）
        self.tts_voice = QComboBox()
        for label, vid in TTS_VOICES:
            self.tts_voice.addItem(label, vid)
        vi = self.tts_voice.findData(cfg.get("tts_voice", TTS_DEFAULT_VOICE))
        self.tts_voice.setCurrentIndex(vi if vi >= 0 else 0)
        self.tts_rate = QComboBox()
        self.tts_rate.addItems(TTS_RATES)
        ri = self.tts_rate.findText(cfg.get("tts_rate", "+0%"))
        self.tts_rate.setCurrentIndex(ri if ri >= 0 else 3)
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
        form = QFormLayout(self)
        form.addRow("服务商预设", self.preset)
        form.addRow("API Key", self.key)
        form.addRow("API Base", self.base)
        form.addRow("模型", self.model)
        form.addRow("排版", QLabel("（调整后立即生效）"))
        form.addRow("字体", self.font_family)
        form.addRow("主题", self.theme)
        form.addRow("字号", self.font_size)
        form.addRow("行距", self.line_spacing)
        form.addRow("段落间距", self.para_spacing)
        form.addRow("左右边距", self.margin_x)
        form.addRow("上下边距", self.margin_y)
        form.addRow("页边距(外)", self.outer)
        form.addRow("书脊", self.gutter)
        form.addRow("语音朗读", QLabel("（edge-tts，需联网）"))
        form.addRow("朗读音色", self.tts_voice)
        form.addRow("朗读语速", self.tts_rate)
        form.addRow("双语朗读", QLabel("（朗读时 AI 翻译，需 API Key）"))
        form.addRow("双语翻译", self.bilingual)
        form.addRow("翻译目标", self.bilingual_target)
        btn = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn.accepted.connect(self.accept); btn.rejected.connect(self.reject)
        form.addRow(btn)

    def _apply(self, name):
        b, m = AI_PRESETS.get(name, ("", ""))
        if b: self.base.setText(b)
        if m: self.model.setText(m)
