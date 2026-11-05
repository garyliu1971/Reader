"""在线书源对话框：后台线程 + Signal；用桩服务器走一遍 搜索 -> 下载 -> 打开（offscreen，无真实网络）。"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import fakeopds  # noqa: E402
from pyreader.booksourcedlg import BookSourceDialog  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def srv():
    s, h, base = fakeopds.start()
    yield h, base
    s.shutdown()
    s.server_close()


def wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def make(base, tmp_path):
    cfg = {"book_sources": [{"name": "Fake", "url": base + "/opds/", "user": "", "password": "",
                             "enabled": True, "allow_insecure": True}]}
    return BookSourceDialog(cfg, dest_root=str(tmp_path))


def test_search_download_open(srv, tmp_path):
    h, base = srv
    d = make(base, tmp_path)
    assert wait(lambda: d.tree.topLevelItemCount() == 2)            # 根目录两个导航条目
    d.query.setText("pride and prejudice")
    d.do_search()
    assert wait(lambda: d.tree.topLevelItemCount() == 1 and d.tree.topLevelItem(0).text(0) == "Pride and Prejudice")
    assert d.tree.topLevelItem(0).text(1) == "Jane Austen"
    d.tree.setCurrentItem(d.tree.topLevelItem(0))
    d.download_selected()
    dest = tmp_path / "Fake" / "Pride and Prejudice.epub"
    assert wait(lambda: d.open_btn.isEnabled())
    assert dest.read_bytes() == fakeopds.EPUB_SMALL                  # 详情页里挑的是最小的 epub
    assert not os.path.exists(str(dest) + ".part")
    got = []
    d.bookReady.connect(got.append)
    d._open_last()
    assert got == [str(dest)]


def test_browse_pagination_back_and_stale_results(srv, tmp_path):
    h, base = srv
    d = make(base, tmp_path)
    assert wait(lambda: d.tree.topLevelItemCount() == 2)
    d._on_double_click(d.tree.topLevelItem(0), 0)                    # 进入 Fiction
    assert wait(lambda: d.tree.topLevelItemCount() == 3 and d.more_btn.isEnabled())
    d.load_more()
    assert wait(lambda: d.tree.topLevelItemCount() == 4 and not d.more_btn.isEnabled())
    assert d.tree.topLevelItem(3).text(0) == "Persuasion" and d.tree.topLevelItem(0).text(2) == "epub"
    d._on_page(d._seq - 1, d._page, False)                           # 过期序号的结果被丢弃
    assert d.tree.topLevelItemCount() == 4
    d.go_back()
    assert d.tree.topLevelItemCount() == 2 and not d.back_btn.isEnabled()


def test_error_is_shown_not_raised(srv, tmp_path):
    h, base = srv
    d = make(base, tmp_path)
    assert wait(lambda: d.tree.topLevelItemCount() == 2)
    d.query.setText("x")
    h.routes["/opds/"] = (200, fakeopds.ATOM_CT, fakeopds.feed("Root"), {})        # 无搜索链接
    d._sources.clear()
    d.do_search()
    assert wait(lambda: "不支持搜索" in d.status.text())


def test_no_enabled_sources(tmp_path):
    d = BookSourceDialog({"book_sources": []}, dest_root=str(tmp_path))
    assert not d.search_btn.isEnabled() and "没有启用的书源" in d.status.text()
