"""漫画压缩包：纯逻辑（comic.py）在临时 zip 上验证；ComicView 用 offscreen Qt 验证打开、翻页、跨页、缓存与关闭。
真实的绘制外观与鼠标手感无法在此验证。"""
import os
import threading
import time
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pyreader import comic as cm  # noqa: E402


def make_zip(path, entries):
    """entries: {名字: bytes}；名字以 / 结尾的写成目录条目。"""
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return str(path)


def png_bytes(w=10, h=20, color=0xff3366cc):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(color)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


FAKE = b"not-really-an-image"       # 纯逻辑测试不解码，字节内容无所谓


# ------------------------------------------------------------------ 列页 / 排序
def test_natural_order_numbers(tmp_path):
    p = make_zip(tmp_path / "a.cbz", {f"page{n}.jpg": FAKE for n in (10, 2, 1, 11, 3)})
    assert cm.list_pages(p) == ["page1.jpg", "page2.jpg", "page3.jpg", "page10.jpg", "page11.jpg"]


def test_full_path_sort_nested(tmp_path):
    p = make_zip(tmp_path / "a.zip", {
        "ch10/1.png": FAKE, "ch2/10.png": FAKE, "ch2/2.png": FAKE, "ch1/a.jpg": FAKE, "cover.jpg": FAKE})
    # 逐级自然排序：目录按路径段比较，同目录的页挨在一起
    assert cm.list_pages(p) == ["ch1/a.jpg", "ch2/2.png", "ch2/10.png", "ch10/1.png", "cover.jpg"]


def test_ch2_before_ch10_even_when_basenames_collide(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"ch10/001.jpg": FAKE, "ch2/001.jpg": FAKE, "ch2/002.jpg": FAKE})
    assert cm.list_pages(p) == ["ch2/001.jpg", "ch2/002.jpg", "ch10/001.jpg"]


def test_skips_junk_entries(tmp_path):
    p = make_zip(tmp_path / "a.zip", {
        "dir/": b"", "__MACOSX/dir/._1.jpg": FAKE, ".DS_Store": FAKE, "dir/.hidden.png": FAKE,
        ".cache/2.jpg": FAKE, "readme.txt": FAKE, "dir/Thumbs.db": FAKE, "dir/ok.JPG": FAKE,
        "x.webp": FAKE, "y.gif": FAKE, "z.bmp": FAKE, "w.jpeg": FAKE, "noext": FAKE, "a.tiff": FAKE})
    assert cm.list_pages(p) == ["dir/ok.JPG", "w.jpeg", "x.webp", "y.gif", "z.bmp"]


def test_corrupt_zip_raises_comic_error(tmp_path):
    f = tmp_path / "bad.cbz"
    f.write_bytes(b"PK\x03\x04 this is not a zip at all")
    with pytest.raises(cm.ComicError):
        cm.list_pages(str(f))
    with pytest.raises(cm.ComicError):
        cm.list_pages(str(tmp_path / "missing.zip"))


def test_non_image_only_zip_is_empty(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"a.txt": b"x", "b/c.pdf": b"y"})
    assert cm.list_pages(p) == []


def test_read_page_bytes_and_unknown_entry(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"1.png": b"AAA", "2.png": b"BBB"})
    assert cm.read_page_bytes(p, "2.png") == b"BBB"
    with pytest.raises(cm.ComicError):
        cm.read_page_bytes(p, "../../etc/passwd")
    with pytest.raises(cm.ComicError):
        cm.read_page_bytes(p, "nope.png")


def test_entry_size_cap(tmp_path, monkeypatch):
    p = make_zip(tmp_path / "a.zip", {"1.png": b"x" * 100, "2.png": b"y"})
    monkeypatch.setattr(cm, "MAX_ENTRY_SIZE", 50)
    with cm.ComicArchive(p) as a:
        with pytest.raises(cm.ComicError):
            a.read("1.png")
        assert a.read("2.png") == b"y"


def test_too_many_entries(tmp_path, monkeypatch):
    p = make_zip(tmp_path / "a.zip", {f"{i}.jpg": FAKE for i in range(5)})
    monkeypatch.setattr(cm, "MAX_ENTRIES", 3)
    with pytest.raises(cm.ComicError):
        cm.ComicArchive(p)


def test_archive_shared_across_threads_and_closed(tmp_path):
    p = make_zip(tmp_path / "a.zip", {f"{i}.jpg": bytes([i]) * 1000 for i in range(20)})
    a = cm.ComicArchive(p)
    errs = []

    def work():
        try:
            for _ in range(20):
                for name in a.pages:
                    assert a.read(name) == bytes([int(name.split(".")[0])]) * 1000
        except Exception as e:      # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    a.close()
    with pytest.raises(cm.ComicError):
        a.read("0.jpg")
    a.close()                        # 重复关闭无副作用


def test_chapters_grouping(tmp_path):
    p = make_zip(tmp_path / "a.zip", {"a_cover.jpg": FAKE, "ch1/1.jpg": FAKE, "ch1/2.jpg": FAKE, "ch2/1.jpg": FAKE})
    with cm.ComicArchive(p) as a:
        ch = a.chapters()
    assert ch == [cm.Chapter("", 0, 1), cm.Chapter("ch1", 1, 2), cm.Chapter("ch2", 3, 1)]
    assert cm.group_chapters([]) == []


def test_display_name_gbk_repair():
    name = "第一话/001.jpg"
    garbled = name.encode("gbk").decode("cp437")        # zipfile 对未标 UTF-8 的条目就是这样解的
    assert cm.display_name(garbled) == name
    assert cm.display_name(name) == name                # 已是正确中文：不动
    assert cm.display_name("plain/1.jpg") == "plain/1.jpg"


# ------------------------------------------------------------------ 跨页 / 方向 / 缩放（纯函数）
def test_is_portrait():
    assert cm.is_portrait(10, 20) and not cm.is_portrait(20, 10) and not cm.is_portrait(10, 10)
    assert not cm.is_portrait(0, 0)


def test_spread_pages_rules():
    P = lambda i: True                                   # noqa: E731
    assert cm.spread_pages(0, 10, P) == (0,)             # 封面单独
    assert cm.spread_pages(0, 10, P, cover_alone=False) == (0, 1)
    assert cm.spread_pages(1, 10, P) == (1, 2)
    assert cm.spread_pages(9, 10, P) == (9,)             # 最后一页没有搭档
    assert cm.spread_pages(1, 10, P, spread=False) == (1,)
    land = lambda i: i != 2                              # noqa: E731  第 2 页是横页
    assert cm.spread_pages(1, 10, land) == (1,)
    assert cm.spread_pages(2, 10, land) == (2,)
    assert cm.spread_pages(3, 10, land) == (3, 4)


def test_next_prev_walk_is_consistent():
    port = {0: True, 1: True, 2: True, 3: False, 4: True, 5: True, 6: True}
    P = lambda i: port[i]                                # noqa: E731
    n = 7
    seen, i = [], 0
    while i is not None:
        seen.append(i)
        i = cm.next_start(i, n, P)
    assert seen == [0, 1, 3, 4, 6]                       # (0)(1,2)(3)(4,5)(6)
    back, i = [], seen[-1]
    while i is not None:
        back.append(i)
        i = cm.prev_start(i, n, P)
    assert back == [6, 4, 3, 1, 0]                       # 倒着走经过同样的屏
    assert cm.next_start(6, n, P) is None and cm.prev_start(0, n, P) is None


def test_prev_start_never_pairs_cover():
    P = lambda i: True                                   # noqa: E731
    assert cm.prev_start(2, 10, P) == 1                  # 退到 (1) 而不是与封面合并
    assert cm.prev_start(3, 10, P) == 1                  # 退成跨页 (1,2)
    assert cm.prev_start(2, 10, P, spread=False) == 1


def test_display_order_and_nav():
    assert cm.display_order((4, 5), rtl=False) == (4, 5)
    assert cm.display_order((4, 5), rtl=True) == (5, 4)
    assert cm.display_order((4,), rtl=True) == (4,)
    assert cm.nav_delta("right", False) == 1 and cm.nav_delta("left", False) == -1
    assert cm.nav_delta("right", True) == -1 and cm.nav_delta("left", True) == 1
    assert cm.nav_delta("middle", True) == 0


def test_click_side():
    assert cm.click_side(10, 300) == "left"
    assert cm.click_side(150, 300) == "middle"
    assert cm.click_side(290, 300) == "right"
    assert cm.click_side(5, 0) == "middle"


def test_spread_size_and_zoom():
    assert cm.spread_size([(100, 200)]) == (100, 200)
    assert cm.spread_size([(100, 200), (50, 100)]) == (200, 200)       # 矮的半尺寸页放大到同高
    assert cm.spread_size([(100, 200), (100, 100)]) == (300, 200)      # 宽扁的那页放大到 200 高
    assert cm.compute_zoom("width", 400, 300, 200, 100) == 2.0
    assert cm.compute_zoom("height", 400, 300, 200, 100) == 3.0
    assert cm.compute_zoom("window", 400, 300, 200, 100) == 2.0
    assert cm.compute_zoom("window", 0, 0, 200, 100) == cm.MIN_ZOOM
    assert cm.compute_zoom("width", 10 ** 6, 10, 1, 1) == cm.MAX_ZOOM


# ------------------------------------------------------------------ ComicView（offscreen）
@pytest.fixture(scope="module", autouse=True)
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_for(cond, timeout=10.0):
    from PySide6.QtWidgets import QApplication
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def comic_zip(tmp_path, n=8, name="c.cbz", sizes=None):
    """页尺寸默认都是竖页 (10, 20)；sizes 可以覆盖：{序号: (w, h)}。"""
    sizes = sizes or {}
    return make_zip(tmp_path / name, {f"{i + 1:03d}.png": png_bytes(*sizes.get(i, (10, 20)), color=0xff000000 + i * 20)
                                      for i in range(n)})


def test_view_open_and_navigate(tmp_path):
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(300, 400)
    got = []
    v.pageChanged.connect(lambda a, b: got.append((a, b)))
    assert v.open_comic(comic_zip(tmp_path), page=2)
    assert (v.current_page(), v.page_count()) == (2, 8) and got == [(2, 8)]
    assert v.next_screen() and v.current_page() == 3
    assert v.prev_screen() and v.prev_screen() and v.current_page() == 1
    v.go_to(99)
    assert v.current_page() == 7 and not v.next_screen()
    v.go_to(0)
    assert not v.prev_screen()
    assert got[-1] == (0, 8)
    v.grab()                                  # 真正走一遍 paintEvent
    v.close_comic()
    assert not v.is_open() and v.page_count() == 0


def test_view_open_failures(tmp_path):
    from pyreader.comicview import ComicView
    v = ComicView()
    bad = tmp_path / "bad.cbz"
    bad.write_bytes(b"garbage")
    assert not v.open_comic(str(bad)) and v.last_error()
    assert not v.open_comic(make_zip(tmp_path / "t.zip", {"a.txt": b"x"})) and "没有图片" in v.last_error()
    assert not v.open_comic(str(tmp_path / "nope.zip"))
    assert not v.is_open()
    # 失败后仍能正常打开
    assert v.open_comic(comic_zip(tmp_path, n=2)) and v.last_error() == ""
    v.close_comic()


def test_view_spread_rtl_and_keys(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(400, 300)
    v.open_comic(comic_zip(tmp_path, n=8, sizes={3: (30, 10)}))      # 第 3 页是横页：不参与跨页
    changed = []
    v.optionsChanged.connect(lambda: changed.append(1))
    v.set_spread(True)
    assert v.spread() and changed
    v.go_to(1)
    v.grab()
    v.next_screen()
    assert v.current_page() == 3                          # (1,2) 之后是 3（横页，单独）
    v.next_screen()
    assert v.current_page() == 4                          # (4,5)
    v.prev_screen()
    assert v.current_page() == 3
    # 方向键：LTR 右 = 下一屏；RTL 右 = 上一屏
    v.set_spread(False)
    v.go_to(3)
    v.show()
    QTest.keyClick(v, Qt.Key_Right)
    assert v.current_page() == 4
    v.set_rtl(True)
    QTest.keyClick(v, Qt.Key_Right)
    assert v.current_page() == 3
    QTest.keyClick(v, Qt.Key_Left)
    assert v.current_page() == 4
    QTest.keyClick(v, Qt.Key_Home)
    assert v.current_page() == 0
    QTest.keyClick(v, Qt.Key_End)
    assert v.current_page() == 7
    QTest.keyClick(v, Qt.Key_PageUp)
    assert v.current_page() == 6
    QTest.keyClick(v, Qt.Key_PageDown)
    assert v.current_page() == 7
    QTest.keyClick(v, Qt.Key_W)
    assert v.fit_mode() == "width"
    QTest.keyClick(v, Qt.Key_D)
    assert v.spread()
    QTest.keyClick(v, Qt.Key_R)
    assert not v.rtl()
    back = []
    v.backRequested.connect(lambda: back.append(1))
    QTest.keyClick(v, Qt.Key_Escape)
    assert back == [1]
    v.close_comic()
    v.hide()


def test_view_click_zones(tmp_path):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(300, 300)
    v.open_comic(comic_zip(tmp_path, n=5), page=2)
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(280, 150))
    assert v.current_page() == 3
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(150, 150))      # 中间：无动作
    assert v.current_page() == 3
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(10, 150))
    assert v.current_page() == 2
    v.set_rtl(True)
    QTest.mouseClick(v, Qt.LeftButton, pos=QPoint(10, 150))       # RTL：左 = 下一页
    assert v.current_page() == 3
    v.close_comic()


def test_view_wheel_scroll_then_flip(tmp_path):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from pyreader.comicview import ComicView
    v = ComicView()
    v.resize(200, 100)
    v.open_comic(comic_zip(tmp_path, n=3))
    v.set_fit_mode("width")                                       # 10x20 页按宽适配 → 高 400，超出视口 100
    v.grab()
    assert v._pan_y == 0

    def wheel(dy):
        v.wheelEvent(QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, dy), Qt.NoButton,
                                 Qt.NoModifier, Qt.NoScrollPhase, False))
    wheel(-120)
    assert v._pan_y > 0 and v.current_page() == 0                 # 先在页内滚动
    for _ in range(40):
        v._last_wheel_flip = 0
        wheel(-120)
        if v.current_page() == 1:
            break
    assert v.current_page() == 1                                  # 滚到底再滚才翻页
    v._last_wheel_flip = 0
    wheel(120)                                                    # 往回翻：落在上一页底部
    assert v.current_page() == 0
    v.grab()
    assert v._pan_y > 0
    v.close_comic()


def test_view_prefetch_cache_and_safe_close(tmp_path):
    from pyreader import comicview as cv
    v = cv.ComicView()
    v.resize(200, 200)
    assert v.open_comic(comic_zip(tmp_path, n=12), page=3)
    v.grab()
    assert wait_for(lambda: {2, 4, 5} <= set(v._cache))           # 后台预取后两页和前一页
    for p in range(4, 12):
        v.go_to(p)
        v.grab()
        assert len(v._cache) <= cv.CACHE_PAGES
        assert p in v._cache
    v.go_to(0)                                                    # 关闭时可能还有任务在途：不能崩，迟到的结果被丢弃
    v.close_comic()
    wait_for(lambda: False, 0.1)                                  # 让迟到的 queued 信号走完
    assert not v._cache and not v._pending
    assert not v.open_comic(str(tmp_path / "nope.zip"))


def test_view_undecodable_page_does_not_loop(tmp_path):
    from pyreader.comicview import ComicView
    p = make_zip(tmp_path / "x.zip", {"1.png": png_bytes(), "2.png": b"this is not a png", "3.png": png_bytes()})
    v = ComicView()
    v.resize(200, 200)
    assert v.open_comic(p, page=1)
    v.grab()
    assert 1 in v._bad and v.page_count() == 3
    v.next_screen()
    v.grab()
    assert v.current_page() == 2
    v.close_comic()


def test_view_theme_and_chapters(tmp_path):
    from pyreader.comicview import ComicView
    from pyreader.config import THEMES
    v = ComicView()
    v.set_theme(THEMES["纯白"])
    assert v._bg == THEMES["纯白"]["bg"]
    p = make_zip(tmp_path / "c.zip", {"a/1.png": png_bytes(), "b/1.png": png_bytes()})
    v.open_comic(p)
    assert [c.title for c in v.chapters()] == ["a", "b"]
    v.close_comic()


def test_natural_key_survives_non_decimal_digit_characters(tmp_path):
    assert cm.natural_key("①") == ["①"]                                       # str.isdigit() 为真、int() 会抛 ValueError
    p = make_zip(tmp_path / "w.cbz", {"②/001.jpg": FAKE, "①/001.jpg": FAKE, "²/10.jpg": FAKE})
    assert cm.list_pages(p) and len(cm.list_pages(p)) == 3
    assert cm.natural_key("p٣") == ["p", 3, ""]                                 # 十进制数字（含非 ASCII）照常转数字


def test_view_error_text_readable_on_light_theme(tmp_path):
    from pyreader.comicview import ComicView
    from pyreader.config import THEMES
    p = make_zip(tmp_path / "bad.zip", {"1.png": b"this is not a png"})
    v = ComicView()
    v.resize(300, 200)
    v.set_theme(THEMES["纯白"])
    assert v.open_comic(p)
    v.grab()
    img = v.grab().toImage()
    bg = img.pixelColor(2, 2).lightness()
    dark = sum(1 for x in range(0, 300) for y in range(40, 120) if img.pixelColor(x, y).lightness() < bg - 100)
    assert dark > 20                                                            # 浅底上的提示字要够深（原来 #d0d0d0 几乎看不见）
    v.close_comic()


def test_view_context_menu_has_mouse_entry_points(tmp_path):
    from pyreader.comicview import ComicView
    p = make_zip(tmp_path / "m.zip", {"a/1.png": png_bytes(), "b/1.png": png_bytes(), "b/2.png": png_bytes()})
    v = ComicView()
    v.resize(200, 200)
    assert v.open_comic(p)
    acts = {x.text(): x for x in v._build_menu().actions()}
    acts["双页跨页（D）"].trigger()
    acts["适应宽度（W）"].trigger()
    acts["从右向左（R）"].trigger()
    acts["章节"].menu().actions()[1].trigger()
    assert "跳到页码…" in acts and v.spread() and v.fit_mode() == "width" and v.rtl() and v.current_page() == 1
    v.close_comic()


# ================================================================== epub 漫画：页序（spine）/ 图片型判定
from epubfix import FAKE as EFAKE, comic_epub, img_body, make_epub, svg_body, text_epub  # noqa: E402


def test_epub_page_order_follows_spine_cover_first_flat_chapters(tmp_path):
    p = comic_epub(tmp_path / "c.epub", n=150)
    pages, rtl = cm.epub_page_order(p)
    assert pages[0] == "OEBPS/images/cover.jpg" and rtl is None                    # OPF 没写方向
    assert pages[1:] == [f"OEBPS/{i}.jpeg" for i in range(150)]                    # 0..149 按 spine 顺序，不是字符串序
    assert sorted(pages, key=cm.path_key)[-1] == "OEBPS/images/cover.jpg"          # 对照：按文件名排，封面会排到最后
    with cm.ComicArchive(p) as a:
        assert a.pages == pages and a.rtl is None
        assert a.chapters() == [cm.Chapter("", 0, 151)]                            # 一本一章，不拆成 OEBPS / OEBPS/images
        assert a.read(pages[3]) == EFAKE


def test_epub_page_order_direction_from_opf(tmp_path):
    assert cm.epub_page_order(comic_epub(tmp_path / "r.epub", n=3, ppd="rtl"))[1] is True
    assert cm.epub_page_order(comic_epub(tmp_path / "l.epub", n=3, ppd="ltr"))[1] is False
    assert cm.epub_page_order(comic_epub(tmp_path / "n.epub", n=3))[1] is None
    with cm.ComicArchive(comic_epub(tmp_path / "r2.epub", n=3, ppd="rtl")) as a:
        assert a.rtl is True


def test_epub_page_order_resolves_refs_dedupes_and_drops_outside_spine(tmp_path):
    pages = {
        "a.xhtml": img_body("img/one%20two.jpg"),                                   # %20
        "sub/b.xhtml": img_body("../img/three.jpg#frag"),                           # ../ 与 #片段
        "c.xhtml": svg_body("img/svg.png"),                                         # <svg><image xlink:href>
        "d.xhtml": '<svg xmlns="http://www.w3.org/2000/svg"><image href="img/plain.png"/></svg>',   # <image href>
        "e.xhtml": img_body("img/one%20two.jpg") + img_body("http://example.com/x.jpg"),             # 重复 + 外链
        "f.xhtml": img_body("IMG/UPPER.JPG"),                                       # 大小写不一致
    }
    images = {n: EFAKE for n in ("img/one two.jpg", "img/three.jpg", "img/svg.png", "img/plain.png", "img/upper.jpg", "img/orphan.jpg")}
    p = make_epub(tmp_path / "r.epub", pages, images, spine=["a.xhtml", "sub/b.xhtml", "c.xhtml", "d.xhtml", "e.xhtml", "f.xhtml"])
    got, _ = cm.epub_page_order(p)
    assert got == ["OEBPS/img/one two.jpg", "OEBPS/img/three.jpg", "OEBPS/img/svg.png", "OEBPS/img/plain.png", "OEBPS/img/upper.jpg"]
    assert "OEBPS/img/orphan.jpg" not in got                                        # spine 之外的图不算


def test_epub_page_order_image_items_directly_in_spine(tmp_path):
    p = make_epub(tmp_path / "i.epub", {"a.xhtml": img_body("1.jpg")}, {"1.jpg": EFAKE, "2.jpg": EFAKE}, spine=["a.xhtml"], image_types=["2.jpg"])
    assert cm.epub_page_order(p)[0] == ["OEBPS/1.jpg", "OEBPS/2.jpg"]


def test_epub_page_order_other_opf_dir_and_linear_no_still_listed(tmp_path):
    p = comic_epub(tmp_path / "d.epub", n=3, opf_dir="OPS/x", linear_no={"CoverPage.xhtml"})
    pages, _ = cm.epub_page_order(p)
    assert pages == ["OPS/x/images/cover.jpg"] + [f"OPS/x/{i}.jpeg" for i in range(3)]      # 页序含 linear="no" 的封面


def test_epub_page_order_unreadable_gives_empty_and_archive_falls_back_to_natural_sort(tmp_path):
    assert cm.epub_page_order(str(tmp_path / "missing.epub")) == ([], None)
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    assert cm.epub_page_order(str(bad)) == ([], None)
    nocont = make_epub(tmp_path / "nc.epub", {"a.xhtml": img_body("1.jpg")}, {"1.jpg": EFAKE}, container=False)
    assert cm.epub_page_order(nocont) == ([], None)
    # spine 引用到的图不到压缩包里图片的一半：引用关系读不出来，宁可按文件名排也不丢页
    imgs = {f"{i}.jpg": EFAKE for i in range(10)}
    p = make_epub(tmp_path / "few.epub", {"a.xhtml": img_body("3.jpg")}, imgs)
    assert cm.epub_page_order(p)[0] == []
    with cm.ComicArchive(p) as a:
        assert a.pages == [f"OEBPS/{i}.jpg" for i in range(10)] and a.chapters() == [cm.Chapter("OEBPS", 0, 10)]


def test_zip_and_cbz_are_unaffected_by_epub_logic(tmp_path):
    p = make_zip(tmp_path / "a.cbz", {"ch2/1.jpg": FAKE, "ch10/1.jpg": FAKE})
    with cm.ComicArchive(p) as a:
        assert a.pages == ["ch2/1.jpg", "ch10/1.jpg"] and a.rtl is None
        assert [c.title for c in a.chapters()] == ["ch2", "ch10"]
    z = make_zip(tmp_path / "a.zip", {"1.jpg": FAKE})                              # 就算是个 zip 里放了 epub 的壳，也不碰 .zip
    assert cm.list_pages(z) == ["1.jpg"]


# ---- is_image_epub ----
def _pictures(n):
    pages = {f"p{i}.xhtml": img_body(f"{i}.jpg") for i in range(n)}
    return pages, {f"{i}.jpg": EFAKE for i in range(n)}


def _comic(tmp_path, n=100, **kw):
    pages, images = _pictures(n)
    return make_epub(tmp_path / "x.epub", pages, images, **kw)


def _with_text(tmp_path, n, text_pages, text, **kw):
    """n 页图片，其中 text_pages 里的页改成「图 + text」。"""
    pages, images = _pictures(n)
    for i in text_pages:
        pages[f"p{i}.xhtml"] = img_body(f"{i}.jpg", text)
    return make_epub(tmp_path / "x.epub", pages, images, **kw)


def _novel_with_plates(tmp_path, frac, n=40, **kw):
    pages, images = {}, {}
    plates = {i for i in range(n) if i % 10 < round(frac * 10)}              # 每 10 页里有 round(frac * 10) 页是整页插图
    for i in range(n):
        if i in plates:
            pages[f"c{i}.xhtml"] = img_body(f"pl{i}.jpg")
            images[f"pl{i}.jpg"] = EFAKE
        else:
            pages[f"c{i}.xhtml"] = "<p>" + "字" * 600 + "</p>"
    return make_epub(tmp_path / "x.epub", pages, images, **kw)


TEXTY_THIRD = cm._sample(list(range(1, 29)), cm.EPUB_SAMPLE_PAGES)[::3]       # 30 页书会被抽查的 12 页里的 4 页：图片页占 8/12，介于 0.5 与 0.8 之间

POSITIVES = {
    "100 页纯图漫画": lambda d: _comic(d, 100),
    "封面包装页 + 真实形状": lambda d: comic_epub(d / "x.epub", n=150),
    "svg 包装的页": lambda d: make_epub(d / "x.epub", {f"p{i}.xhtml": svg_body(f"{i}.png") for i in range(30)},
                                      {f"{i}.png": EFAKE for i in range(30)}),
    "约 15% 的页带 80 字说明": lambda d: _with_text(d, 100, [3, 17, 31, 45, 59, 71, 83, 95, 8, 22, 36, 50, 64, 78, 90], "字" * 80),
    "一页 400 字译者注": lambda d: _with_text(d, 100, [50], "注" * 400),
    "book-type=manga 且抽样页里三分之一带 80 字": lambda d: _with_text(d, 30, TEXTY_THIRD, "字" * 80, book_type="manga"),
    "绘本：每页不超过 30 字": lambda d: _with_text(d, 24, range(24), "字" * 30),
    "单个 xhtml 的长条漫画（40 张图）": lambda d: make_epub(d / "x.epub", {"all.xhtml": "".join(img_body(f"{i}.jpg") for i in range(40))},
                                              {f"{i}.jpg": EFAKE for i in range(40)}),
    "全部页 linear=no": lambda d: _comic(d, 20, linear_no={f"p{i}.xhtml" for i in range(20)}),
    "只有 3 页": lambda d: _comic(d, 3),
}

NEGATIVES = {
    "纯文字小说": lambda d: text_epub(d / "x.epub", n=30),
    "三成整页插图的小说": lambda d: _novel_with_plates(d, 0.3),
    "五成整页插图的小说": lambda d: _novel_with_plates(d, 0.5),
    "七成整页图的写真类非虚构": lambda d: _novel_with_plates(d, 0.7),
    "固定版式：背景图 + 每页 300 字": lambda d: _with_text(d, 30, range(30), "字" * 300),
    "扫描书 + 隐藏的 OCR 文字层": lambda d: make_epub(d / "x.epub", {f"p{i}.xhtml": img_body(f"{i}.jpg") + '<div style="display:none">' + "字" * 300 + "</div>" for i in range(30)},
                                              {f"{i}.jpg": EFAKE for i in range(30)}),
    "单个 xhtml 的小说 + 3 张图": lambda d: make_epub(d / "x.epub", {"all.xhtml": "<p>" + "字" * 20000 + "</p>" + "".join(img_body(f"{i}.jpg") for i in range(3))},
                                              {f"{i}.jpg": EFAKE for i in range(3)}),
    "引用的都是外链图片": lambda d: make_epub(d / "x.epub", {f"p{i}.xhtml": img_body("http://example.com/a.jpg") for i in range(20)}, {}),
    "引用的图片不在压缩包里": lambda d: make_epub(d / "x.epub", {f"p{i}.xhtml": img_body(f"gone{i}.jpg") for i in range(20)}, {}),
    "没有 spine 页": lambda d: make_epub(d / "x.epub", {}, {}),
    "全部页 linear=no 的文字书": lambda d: text_epub(d / "x.epub", n=20, linear_no={f"c{i}.xhtml" for i in range(20)}),
}


@pytest.mark.parametrize("name", list(POSITIVES))
def test_is_image_epub_positives(tmp_path, name):
    assert cm.is_image_epub(POSITIVES[name](tmp_path)) is True


@pytest.mark.parametrize("name", list(NEGATIVES))
def test_is_image_epub_negatives(tmp_path, name):
    assert cm.is_image_epub(NEGATIVES[name](tmp_path)) is False


def test_is_image_epub_hint_only_relaxes_the_threshold(tmp_path):
    assert cm.is_image_epub(_with_text(tmp_path, 30, TEXTY_THIRD, "字" * 80)) is False                     # 图片页 0.67：不到 0.80
    assert cm.is_image_epub(_with_text(tmp_path, 30, TEXTY_THIRD, "字" * 80, book_type="MANGA")) is True   # 有提示放宽到 0.50（不区分大小写）
    assert cm.is_image_epub(text_epub(tmp_path / "t.epub", n=30, book_type="comic")) is False         # 标了 comic 的文字书：提示从不单独触发
    assert cm.is_image_epub(_with_text(tmp_path, 30, range(30), "字" * 300, book_type="comic")) is False


def test_is_image_epub_thresholds_are_named_constants():
    assert (cm.EPUB_SAMPLE_PAGES, cm.EPUB_PAGE_TEXT_MAX, cm.EPUB_COMIC_FRAC, cm.EPUB_COMIC_FRAC_HINTED) == (12, 50, 0.80, 0.50)
    assert {"comic", "comics", "manga"} == set(cm.EPUB_COMIC_BOOK_TYPES)


def test_is_image_epub_invalid_inputs_never_raise(tmp_path):
    assert cm.is_image_epub(str(tmp_path / "missing.epub")) is False
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"")
    assert cm.is_image_epub(str(bad)) is False
    bad.write_bytes(b"PK\x03\x04 truncated")
    assert cm.is_image_epub(str(bad)) is False
    assert cm.is_image_epub(make_zip(tmp_path / "z.zip", {"1.jpg": FAKE})) is False                       # 不是 .epub 不判
    nocont = make_epub(tmp_path / "nc.epub", {"a.xhtml": img_body("1.jpg")}, {"1.jpg": EFAKE}, container=False)
    assert cm.is_image_epub(nocont) is False
    assert cm.is_image_epub(None) is False and cm.is_image_epub("") is False


def test_is_image_epub_is_cheap_and_never_decompresses_images(tmp_path, monkeypatch):
    p = comic_epub(tmp_path / "big.epub", n=150)
    opened = []
    orig = zipfile.ZipFile.open

    def spy(self, name, *a, **k):
        opened.append(getattr(name, "filename", name))
        return orig(self, name, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    assert cm.is_image_epub(p) is True
    assert not any(n.lower().endswith((".jpg", ".jpeg", ".png")) for n in opened)                           # 一张图也没解压
    assert len(opened) <= 2 + cm.EPUB_SAMPLE_PAGES                                                          # container.xml + OPF + 抽样页


def test_is_image_epub_memoized_by_path_size_mtime(tmp_path, monkeypatch):
    p = comic_epub(tmp_path / "m.epub", n=10)
    calls = []
    real = cm._detect_image_epub
    monkeypatch.setattr(cm, "_detect_image_epub", lambda zf: calls.append(1) or real(zf))
    assert cm.is_image_epub(p) is True and cm.is_image_epub(p) is True
    assert len(calls) == 1                                                                                  # 第二次走记忆
    text_epub(p, n=30)                                                                                      # 同一路径换了内容（大小 / 时间都变）
    assert cm.is_image_epub(p) is False and len(calls) == 2


def test_is_image_epub_samples_at_most_twelve_pages_evenly(tmp_path):
    pages, images = _pictures(200)
    p = make_epub(tmp_path / "s.epub", pages, images)
    assert len(cm._sample(list(range(100)), 12)) == 12
    assert cm._sample(list(range(100)), 12)[0] == 0 and cm._sample(list(range(100)), 12)[-1] == 99
    assert cm._sample([1, 2, 3], 12) == [1, 2, 3]
    assert cm.is_image_epub(p) is True


def test_is_image_epub_visible_text_ignores_head_script_style_and_whitespace():
    assert cm._visible_chars('<html><head><title>很长很长的标题</title><style>a{}</style></head><body>'
                             '<script>var x = "abc";</script><!-- 注释 --><p> 你 好\n\t</p>&nbsp;<img src="a.jpg"/></body></html>') == 2


# ---- ComicView / ComicArchive 的阅读方向提示 ----
def test_view_opens_epub_comic_in_spine_order_and_reports_opf_direction(tmp_path):
    from pyreader.comicview import ComicView
    png = png_bytes()
    v = ComicView()
    assert v.opf_rtl() is None
    assert v.open_comic(comic_epub(tmp_path / "r.epub", n=6, png=png, ppd="rtl"), 0)
    assert v.page_count() == 7 and v.opf_rtl() is True and [c.count for c in v.chapters()] == [7]
    assert v.open_comic(comic_epub(tmp_path / "n.epub", n=6, png=png), 3) and v.opf_rtl() is None and v.current_page() == 3
    assert v.open_comic(make_zip(tmp_path / "z.cbz", {"1.png": png}), 0) and v.opf_rtl() is None
    v.close_comic()
    assert v.opf_rtl() is None
    v.close()
