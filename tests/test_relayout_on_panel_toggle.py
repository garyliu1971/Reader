"""目录/书签等 dock 开关只改变阅读页宽度、不改变主窗口大小：必须照样触发重排；重排不改变阅读锚点。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
import pytest
from pyreader.pager import LazyPager

TEXT = "\n".join(f"第{i}段 " + "春江潮水连海平，海上明月共潮生。" * 6 for i in range(400))


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win():
    from pyreader.mainwin import MainWindow
    w = MainWindow()
    w.resize(1100, 700)
    w.show()
    QApplication.processEvents()
    yield w
    w._progress_timer.stop()
    w._resize_timer.stop()
    w.close()
    w.deleteLater()


def _open(win):
    win.show_reader()
    win.pager = LazyPager(TEXT, [(0, "t")])
    win.pager.set_params(win.view.pagination_params())
    win.book_path = "t.txt"
    win.cur_chapter = 0
    win.load_chapter(0)
    win.goto_offset(3000)
    QApplication.processEvents()
    win._resize_timer.stop()


def test_dock_toggle_triggers_and_applies_relayout(win):
    _open(win)
    win.toc_dock.hide()
    QApplication.processEvents()
    win._resize_timer.stop()
    win.reload_current()
    before = win.pager._params["page_size"].width()
    win.toc_dock.show()
    QApplication.processEvents()
    assert win._resize_timer.isActive()                      # 只有 dock 变了，主窗口大小没变，也要排队重排
    win._resize_timer.stop()
    win._relayout_if_page_size_changed()
    after = win.pager._params["page_size"].width()
    assert after < before                                    # 页宽缩小后确实按新宽度重新分页
    assert after == win.view.pagination_params()["page_size"].width()


def test_unchanged_page_size_skips_relayout(win, monkeypatch):
    _open(win)
    calls = []
    monkeypatch.setattr(win, "reload_current", lambda *a, **k: calls.append(1))
    win._relayout_if_page_size_changed()
    assert calls == []


def test_repeated_relayout_does_not_drift_anchor(win):
    _open(win)
    anchor = win._anchor_off
    assert anchor == 3000
    for w in (900, 1200, 1000, 1300, 1100) * 2:
        win.resize(w, 700)
        QApplication.processEvents()
        win._resize_timer.stop()
        win.reload_current()
        assert win._anchor_off == anchor
        p = win.view.pages[win.view.spread * win.view.ppv] if not win.view.single_page else win.view.pages[win.view.spread]
        assert p.start <= anchor
