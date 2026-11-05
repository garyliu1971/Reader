"""译文字号倍数：默认值、旧配置兼容、钳制、面板字号随倍数变化、设置页控件。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pyreader.config import (DEFAULT_CONFIG, TRANS_SCALE_DEFAULT, TRANS_SCALE_MAX,  # noqa: E402
                             TRANS_SCALE_MIN, bilingual_html, clamp_trans_scale,
                             translation_point_size)


def test_default_scale_not_smaller_than_body():
    assert DEFAULT_CONFIG["translation_font_scale"] == TRANS_SCALE_DEFAULT == 1.4
    assert translation_point_size(DEFAULT_CONFIG["font_size"], TRANS_SCALE_DEFAULT) > DEFAULT_CONFIG["font_size"]


def test_old_config_without_key_uses_default():
    cfg = {**DEFAULT_CONFIG, **{"font_size": 18}}           # 旧 config.json 无该键，合并后取默认
    assert clamp_trans_scale(cfg.get("translation_font_scale")) == TRANS_SCALE_DEFAULT
    assert clamp_trans_scale({}.get("translation_font_scale")) == TRANS_SCALE_DEFAULT


@pytest.mark.parametrize("raw,exp", [(0.1, TRANS_SCALE_MIN), (-3, TRANS_SCALE_MIN), (9, TRANS_SCALE_MAX),
                                     (1.5, 1.5), ("1.3", 1.3), ("abc", 1.4), (None, 1.4),
                                     (float("nan"), 1.4)])
def test_clamp(raw, exp):
    assert clamp_trans_scale(raw) == exp


def test_point_size_scales_with_scale_and_body():
    assert translation_point_size(16, 1.0) == 16
    assert translation_point_size(16, 1.5) == 24
    assert translation_point_size(20, 1.5) == 30
    assert translation_point_size(16, 99) == round(16 * TRANS_SCALE_MAX, 2)


def test_bilingual_html_uses_scale_and_escapes():
    h1 = bilingual_html("A <b> & B", "译文", 16, 1.0)
    h2 = bilingual_html("A <b> & B", "译文", 16, 1.5)
    assert "font-size:16pt" in h1 and "font-size:24pt" in h2
    assert "&lt;b&gt;" in h1 and "&amp;" in h1
    assert "（翻译中…）" in bilingual_html("x", None, 16, 1.1)


def test_rendered_panel_height_grows_with_scale():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QTextEdit
    app = QApplication.instance() or QApplication([])
    def height(scale):
        w = QTextEdit(); w.resize(300, 200)
        w.setHtml(bilingual_html("Her sister", "她的姐姐，虽因婚姻而与她相隔甚近，却住在伦敦" * 3, 15, scale))
        w.document().setTextWidth(300)
        return w.document().size().height()
    assert height(1.8) > height(1.0)


def test_settings_dialog_has_scale_control():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from pyreader.settings import SettingsDialog
    app = QApplication.instance() or QApplication([])
    d = SettingsDialog(dict(DEFAULT_CONFIG))
    assert d.trans_scale.value() == pytest.approx(1.4)
    assert d.trans_scale.minimum() == TRANS_SCALE_MIN and d.trans_scale.maximum() == TRANS_SCALE_MAX
    cfg = {k: v for k, v in DEFAULT_CONFIG.items() if k != "translation_font_scale"}
    assert SettingsDialog(cfg).trans_scale.value() == pytest.approx(1.4)
    cfg["translation_font_scale"] = 5
    assert SettingsDialog(cfg).trans_scale.value() == pytest.approx(TRANS_SCALE_MAX)
