"""音频 / 漫画并入应用：分发（shelf 常量 / ShelfView / MainWindow）、进度（书签字段 + 书架百分比 + 云端记录）、
离开与退出时释放文件、旧客户端兼容。MainWindow 用真实窗口（offscreen），音频用 wave 生成的 8 秒 WAV，漫画用 QImage 生成的 PNG。
音频相关测试在没有音频输出设备时跳过。"""
import json
import math
import os
import struct
import time
import wave
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader.webdav import WebDavClient  # noqa: E402

try:
    from pyreader.audioplayer import audio_available
    HAS_AUDIO = audio_available()
except Exception:                                    # 没有 QtMultimedia
    HAS_AUDIO = False
needs_audio = pytest.mark.skipif(not HAS_AUDIO, reason="no audio output device / backend")


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def png_bytes(w=10, h=20):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(0xff3366cc)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_cbz(path, pages=5):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(1, pages + 1):
            z.writestr(f"{i:03d}.png", png_bytes())
    return str(path)


def write_wav(path, seconds=8):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i * 0.2))) for i in range(8000 * seconds)))
    return str(path)


# ================================================================== shelf.py：应用内媒体 vs 外部
def test_book_flags_split_inapp_media_from_external():
    mp3 = sh.Book("remote:a.mp3", "a", "mp3", 1, "cloud:", "", "cloud", "a.mp3")
    cbz = sh.Book("remote:a.cbz", "a", "cbz", 1, "cloud:", "", "cloud", "a.cbz")
    cbr = sh.Book("remote:a.cbr", "a", "cbr", 1, "cloud:", "", "cloud", "a.cbr")
    epub = sh.Book("remote:a.epub", "a", "epub", 1, "cloud:", "", "cloud", "a.epub")
    assert [(b.inapp_media, b.external, b.readable) for b in (mp3, cbz, cbr, epub)] == [
        (True, False, True), (True, False, True), (False, True, False), (False, False, True)]


def lf(name, root="/lib"):
    return sh.LocalFile(f"{root}/{name}", root, "", name, 100, 1.0)


def test_media_progress_from_bookmarks_and_digest():
    local = [lf("a.mp3"), lf("b.cbz"), lf("c.zip"), lf("d.cbr")]
    ids = {f.name: sync.book_id(f.path) for f in local}
    bms = {local[0].path: {"_progress_ms": 90000, "_progress_pct_": 0.25, "_progress_ts_": 50.0},
           local[1].path: {"_progress_page": 7, "_progress_pct_": 0.5, "_progress_ts_": 60.0},
           local[2].path: {"_progress_page": 0, "_progress_pct_": 1.0, "_progress_ts_": 70.0},
           local[3].path: {"_progress_page": 7, "_progress_pct_": 0.5, "_progress_ts_": 80.0}}
    books = sh.build_books(local, None, None, bookmarks=sh.bookmark_snapshot(bms))
    a, b, c, d = (books[ids[n]] for n in ("a.mp3", "b.cbz", "c.zip", "d.cbr"))
    assert (a.percent, a.has_progress, a.status) == (25.0, True, "reading")      # 书签里的 0..1 比例兜底（还没有进度摘要）
    assert (b.percent, b.status) == (50.0, "reading")
    assert (c.percent, c.status, c.has_progress) == (100.0, "done", False)       # 听完 / 看完：位置归零但 100%
    assert d.percent is None and d.status == "unread" and not d.has_progress     # 外部格式：书签里的任何进度都不采信
    assert sh.continue_candidate(books).id in (ids["a.mp3"], ids["b.cbz"])
    assert {x.id for x in sh.filter_books(books, "reading")} == {ids["a.mp3"], ids["b.cbz"]}
    digest = {"entries": {}, "progress": {ids["a.mp3"]: {"percent": 40.0, "updated": 99.0, "device": "pc"}}}
    books = sh.build_books(local, None, digest, bookmarks=sh.bookmark_snapshot(bms))
    assert books[ids["a.mp3"]].percent == 40.0                                   # 进度摘要（含别的设备）优先
    assert sv.progress_text(books[ids["a.mp3"]]) == "40%" and sv.progress_text(books[ids["c.zip"]]) == "已读完"


def test_note_opened_and_progress_apply_to_inapp_media_not_external():
    a = sh.Book("remote:a.mp3", "a", "mp3", 1, "cloud:", "", "cloud", "a.mp3")
    sh.note_opened(a, 100.0)
    assert a.last_opened == 100.0 and sh.note_progress(a, 30.0, 120.0) is True and a.percent == 30.0
    r = sh.Book("remote:a.rar", "a", "rar", 1, "cloud:", "", "cloud", "a.rar")
    assert sh.note_progress(r, 30.0, 120.0) is False and r.percent is None


def test_index_roundtrip_keeps_media_percent():
    books = sh.build_books([], [("有声书/c.mp3", 10, 1.0)])
    wire = sh.index_to_wire(books, {"remote:有声书/c.mp3": 50.0},
                            {"remote:有声书/c.mp3": {"percent": 40.0, "updated": 70.0, "device": "d"}})
    back, opened, _prog = sh.index_from_wire(wire)
    b = back["remote:有声书/c.mp3"]
    assert (b.percent, b.last_opened, b.inapp_media) == (40.0, 50.0, True)


# ================================================================== sync.py：新记录字段 + 旧客户端
def old_client_pull(body: bytes, bid: str):
    """改版前 sync.pull_progress 的校验逻辑原样照抄（旧客户端对一条远端记录的全部要求）。"""
    d = json.loads(body.decode("utf-8"))
    if isinstance(d, dict) and "offset" in d and d.get("book_id") == bid:
        return d
    return None


def test_media_record_shape_and_validation():
    rec = sync.make_media_record("local:" + "a" * 40, "audio", 25.0, "pc", pos_ms=90000, updated=123.0)
    assert (rec["offset"], rec["chapter"], rec["kind"], rec["pos_ms"], rec["percent"]) == (0, "", "audio", 90000, 25.0)
    assert "page" not in rec
    rc = sync.make_media_record("local:" + "b" * 40, "comic", 50.0, "pc", page=7)
    assert rc["page"] == 7 and "pos_ms" not in rc
    assert sync.media_position(rec, "audio") == 90000 and sync.media_position(rec, "comic") is None
    assert sync.media_position(rc, "comic") == 7
    for bad in (True, -1, 1.5, "7", None, sync.MAX_PAGE + 1):                     # 来自服务器的数据不可信
        assert sync.media_position({"page": bad}, "comic") is None
    assert sync.media_position({"pos_ms": sync.MAX_POS_MS + 1}, "audio") is None and sync.media_position(None, "audio") is None


def test_new_format_record_is_accepted_by_old_client_logic(server):
    h, url = server
    c = WebDavClient(url)
    bid = "remote:有声书/三体.mp3"
    new = sync.make_media_record(bid, "audio", 33.3, "pc-new", pos_ms=123456, updated=time.time())
    sync.push_progress(c, "bob", new)
    body = c.get(sync.progress_path("bob", bid))
    assert "pos_ms" in json.loads(body.decode("utf-8"))                            # 线上确实带了新字段
    old = old_client_pull(body, bid)                                              # 旧版校验：只要 book_id / offset，无视多出来的字段
    assert old is not None and old["percent"] == 33.3 and old["offset"] == 0
    assert sync.pull_progress(c, "bob", bid) == old
    assert sync.merge_progress({"updated": 1.0}, old) is old                       # 旧版的合并 / 偏移换算也不受影响
    assert sync.resolve_offset(old, 5000) == 0
    got = ss.parse_progress_body(body, bid)                                       # 书架补缺的解析（旧版同一份代码）：只取三项
    assert got == {"percent": 33.3, "updated": new["updated"], "device": "pc-new"}
    assert ss.parse_progress_body(body, "remote:other.mp3") is None                # book_id 不符仍然拒绝


def test_old_client_overlay_validator_ignores_unknown_progress_fields():
    bid = "local:" + "c" * 40
    raw = {"version": 1, "progress": {bid: {"percent": 12.5, "updated": time.time() - 5, "pos_ms": 9000, "page": 3, "kind": "audio"}}}
    ov, issues = sh.validate_overlay(raw, device_id="pc")
    assert ov["progress"][bid]["percent"] == 12.5 and set(ov["progress"][bid]) == {"percent", "updated", "device"} and not issues


# ================================================================== ShelfView：打开路径
def make_view(tmp_path, lib):
    ctx = {"library_dirs": [str(lib)], "recent": [], "bookmarks": {}, "cfg": {}}
    v = sv.ShelfView(lambda: ctx, root=str(tmp_path / "home"))
    v.opened, v.launched = [], []
    v.openRequested.connect(v.opened.append)
    v.external_opener = lambda p: (v.launched.append(p), True)[1]
    return v


def test_shelfview_sends_media_to_reader_window_and_external_to_os(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    names = ("a.mp3", "b.cbz", "c.ZIP", "d.cbr", "e.7z")
    for n in names:
        (lib / n).write_bytes(b"1")
    v = make_view(tmp_path, lib)
    for n in names:
        v.launch_file(str(lib / n))
    assert [os.path.basename(p) for p in v.opened] == ["a.mp3", "b.cbz", "c.ZIP"]
    assert [os.path.basename(p) for p in v.launched] == ["d.cbr", "e.7z"]
    assert v.open_external(str(lib / "a.mp3")) is True and v.launched[-1] == str(lib / "a.mp3")      # 播放器打不开时的兜底
    assert v.open_external(str(tmp_path / "evil.exe")) is False and len(v.launched) == 3            # 仍然不是任意文件启动器
    v.note_opened(str(lib / "a.mp3"))
    assert sync.book_id(str(lib / "a.mp3")) in v._opened                                            # 媒体进「最近阅读」
    v.note_progress(str(lib / "b.cbz"), 50.0, 123.0)
    assert v._progress[sync.book_id(str(lib / "b.cbz"))]["percent"] == 50.0


# ================================================================== MainWindow（真实窗口，offscreen）
@pytest.fixture
def win(monkeypatch):
    from pyreader.mainwin import MainWindow
    w = MainWindow()
    w.resize(1000, 700)
    w.show()
    QApplication.processEvents()
    w.cfg["audio_autoplay"] = False        # 既有用例假设「打开不自动播」；自动播放见 test_audio_autoplay_*
    w.pushed, w.pulled, w.external = [], [], []
    monkeypatch.setattr(w.sync, "push", lambda cfg, rec: w.pushed.append(rec))
    monkeypatch.setattr(w.sync, "pull", lambda cfg, path: w.pulled.append(path))
    w.shelf.external_opener = lambda p: (w.external.append(p), True)[1]
    yield w
    w._progress_timer.stop()
    w._resize_timer.stop()
    w.close()
    w.deleteLater()


def key(win, k, mod=Qt.KeyboardModifier.NoModifier):
    win.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mod))


def test_window_has_media_pages(win):
    assert win.stack.widget(2) is win.comic
    assert win.audio is None or win.stack.widget(3) is win.audio
    assert win.stack.widget(0) is win.shelf and win.stack.widget(1) is win.view
    assert win._shelf_visible() and win._media is None


def test_comic_opens_in_app_saves_page_and_resumes(win, tmp_path, monkeypatch):
    p = write_cbz(tmp_path / "漫画.cbz", pages=5)
    stopped = []
    monkeypatch.setattr(win, "_tts_stop", lambda *a, **k: stopped.append(1))
    win.load_book(p)
    assert win.stack.currentWidget() is win.comic and win.comic.is_open() and win.comic.page_count() == 5
    assert stopped and win._media["kind"] == "comic" and not win._text_active
    assert win.pulled == [p] and win.external == []                                # 没有交给系统应用
    assert not win.shelf_act.isChecked() and not any(a.isEnabled() for a in win._reader_acts)
    assert win.windowTitle() == "漫画.cbz — PyReader"
    assert win.bookmarks.get(p, {}).get("_progress_page") is None                   # 刚打开不写进度
    win.comic.go_to(1)                                                              # 第一次翻页立刻存（5 秒节流从这次算起）
    ent = win.bookmarks[p]
    assert (ent["_progress_page"], ent["_progress_pct_"]) == (1, 0.25) and ent["_progress_ts_"] > 0
    assert [r["page"] for r in win.pushed] == [1]
    rec = win.pushed[0]
    assert (rec["kind"], rec["percent"], rec["offset"], rec["book_id"]) == ("comic", 25.0, 0, sync.book_id(p))
    bid = sync.book_id(p)
    assert win.shelf._progress[bid]["percent"] == 25.0                              # 书架进度条用的百分比
    win.comic.go_to(3)                                                              # 5 秒内的第二次：只记在内存，不落盘不推送
    assert win.bookmarks[p]["_progress_page"] == 1 and len(win.pushed) == 1 and win._media["dirty"]
    win._on_progress_tick()                                                         # 30 秒计时器：仍在节流窗口内 → 什么都不做
    assert win.bookmarks[p]["_progress_page"] == 1
    win._media["saved_at"] -= 6                                                     # 过了 5 秒：计时器把攒着的存下
    win._on_progress_tick()
    assert win.bookmarks[p]["_progress_page"] == 3 and len(win.pushed) == 1         # 本地存了；云端 30 秒内不重复推
    win._media["pushed_at"] -= 31
    win._on_progress_tick()
    assert [r["page"] for r in win.pushed] == [1, 3]                                # 30 秒到了：推最新的
    win.comic.go_to(4)
    key(win, Qt.Key.Key_Escape)                                                     # 回书架：存 + 立即推 + 放开文件
    assert win._shelf_visible() and win._media is None and not win.comic.is_open()
    assert win.bookmarks[p]["_progress_page"] == 4 and win.pushed[-1]["page"] == 4 and win.pushed[-1]["percent"] == 100.0
    os.remove(p)                                                                    # 文件已释放：Windows 上可以删除
    write_cbz(p, pages=5)
    win.load_book(p)                                                                # 再打开：回到上次的页
    assert win.comic.current_page() == 4 and win._media["start"] == 4
    win.show_shelf()


def test_comic_options_persist(win, tmp_path):
    p = write_cbz(tmp_path / "o.cbz", pages=3)
    win.load_book(p)
    win.comic.set_rtl(True)
    win.comic.set_fit_mode("width")
    assert win.cfg["comic_rtl"] is True and win.cfg["comic_fit"] == "width"
    win.show_shelf()
    win.comic.blockSignals(True)                                                    # 控件自己的状态被改回默认（不触发保存）
    win.comic.set_rtl(False)
    win.comic.set_fit_mode("window")
    win.comic.blockSignals(False)
    win.load_book(p)
    assert win.comic.rtl() is True and win.comic.fit_mode() == "width"              # 再打开按保存的设置


def test_bad_zip_and_no_audio_fall_back_to_os_app(win, tmp_path, monkeypatch):
    import pyreader.mainwin as mw
    z = tmp_path / "资料.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "no images here")
    win.load_book(str(z))
    assert win.external == [str(z)] and win._shelf_visible() and win._media is None and win._text_active
    assert "改用系统应用" in win.statusBar().currentMessage()
    bad = tmp_path / "坏.cbz"
    bad.write_bytes(b"this is not a zip")
    win._open_from_shelf(str(bad))
    assert win.external[-1] == str(bad) and win._shelf_visible() and not win._shelf_open_pending
    if win.audio is not None:
        w = write_wav(tmp_path / "a.wav", 1)
        monkeypatch.setattr(mw, "audio_available", lambda: False)
        win.load_book(w)
        assert win.external[-1] == w and win._shelf_visible() and win._media is None
    monkeypatch.setattr(win, "audio", None)                                         # 没有 QtMultimedia
    (tmp_path / "b.mp3").write_bytes(b"ID3")
    win.load_book(str(tmp_path / "b.mp3"))
    assert win.external[-1] == str(tmp_path / "b.mp3") and win._media is None


def test_external_formats_still_never_enter_app(win, tmp_path):
    for n in ("a.cbr", "b.rar", "c.7z"):
        win.load_book(str(tmp_path / n))
    assert [os.path.basename(p) for p in win.external] == ["a.cbr", "b.rar", "c.7z"]
    assert win._shelf_visible() and win._media is None and win.pulled == []


def test_text_book_and_media_switch_release_each_other(win, tmp_path, monkeypatch):
    import pyreader.mainwin as mw

    class StubLoader(QObject):
        loaded, failed, progress = Signal(str, list, str, object), Signal(str), Signal(int, str)

        def __init__(self, path):
            super().__init__()

        def run(self):
            pass
    monkeypatch.setattr(mw, "BookLoader", StubLoader)
    p = write_cbz(tmp_path / "c.cbz", pages=3)
    win.load_book(p)
    assert win.comic.is_open()
    t = tmp_path / "t.txt"
    t.write_text("hello")
    win.load_book(str(t))                                                           # 去读文字书：媒体先关掉（存断点、放开文件）
    assert win._media is None and not win.comic.is_open() and win._reader_visible() and win._text_active
    assert win.book_path == str(t) and win.single_act.isEnabled() and win.tts_act.isEnabled()
    os.remove(p)
    write_cbz(p, pages=3)
    win.load_book(p)                                                                # 文字书不关闭，但不再是「当前书」
    assert win._media is not None and not win._text_active and win.stack.currentWidget() is win.comic
    win.show_shelf()
    win.shelf_act.setChecked(False)
    win._on_shelf_act(False)
    assert win._shelf_visible()                                                     # Ctrl+B 再按不会把人带回刚才的文字书
    assert win._reader_title == os.path.basename(str(t)) + " — PyReader"


def test_remote_progress_applies_only_when_newer_and_untouched(win, tmp_path):
    p = write_cbz(tmp_path / "r.cbz", pages=6)
    win.load_book(p)
    bid = sync.book_id(p)

    def rec(pg, updated, kind="comic", **extra):
        d = sync.make_media_record(bid, kind, 50.0, "phone", page=pg, updated=updated)
        d.update(extra)
        return d
    now = time.time()
    win._on_remote_progress(p, rec(0, now, page=True))                              # 布尔页码：不采信
    win._on_remote_progress(p, rec(0, now, page=-3))
    win._on_remote_progress(p, rec(4, now, kind="audio"))                           # 类别不符
    assert win.comic.current_page() == 0
    win.comic.go_to(0)
    win._media["start"], win._media["touched"] = 0, False
    win.bookmarks[p] = {"_progress_ts_": time.time() + 100}
    win._on_remote_progress(p, rec(4, time.time()))                                 # 本机的更新
    assert win.comic.current_page() == 0
    win.bookmarks[p] = {"_progress_ts_": 1.0}
    win._on_remote_progress(p, rec(4, time.time()))                                 # 别的设备更新、本机没动过 → 跳过去
    assert win.comic.current_page() == 4 and win.bookmarks[p]["_progress_page"] == 4
    assert win.shelf._progress[bid]["percent"] == 50.0 and "已同步进度" in win.statusBar().currentMessage()
    assert win.pushed == [] and not win._media["dirty"] and not win._media["touched"]      # 别的设备的位置不会当成本机的阅读事件回推
    win.comic.go_to(1)                                                              # 用户翻过页以后，迟到的远端进度不再抢位置
    win._on_remote_progress(p, rec(5, time.time() + 50))
    assert win.comic.current_page() == 1


def test_close_event_saves_and_releases_media(win, tmp_path):
    p = write_cbz(tmp_path / "x.cbz", pages=4)
    win.load_book(p)
    win.comic.go_to(1)
    win.comic.go_to(2)                                                              # 第二次在节流窗口内：只有关闭才会存
    assert win.bookmarks[p]["_progress_page"] == 1
    win.close()
    assert win.bookmarks[p]["_progress_page"] == 2 and win.pushed[-1]["page"] == 2 and not win.comic.is_open()
    os.remove(p)


def test_media_snapshot_rules(win, tmp_path):
    p = write_cbz(tmp_path / "s.cbz", pages=2)
    win.load_book(p)
    m = win._media
    m.update(kind="audio", pos=2000, dur=60000)
    assert win._media_snapshot() is None                                            # 开头 3 秒内不记
    m.update(pos=30000)
    assert win._media_snapshot() == {"ms": 30000, "frac": 0.5}
    m.update(pos=58000)
    assert win._media_snapshot() == {"ms": 0, "frac": 1.0}                           # 结尾 3 秒内 = 听完：下次从头
    m.update(pos=30000, dur=0)
    assert win._media_snapshot() == {"ms": 30000, "frac": 0.0}                       # 总长未知：只记位置
    m.update(kind="comic", page=0, count=1)
    assert win._media_snapshot() == {"page": 0, "frac": 0.0}
    m.update(kind="comic", page=9, count=10)
    assert win._media_snapshot() == {"page": 9, "frac": 1.0}


@needs_audio
def test_audio_opens_saves_on_pause_and_resumes(win, tmp_path):
    p = write_wav(tmp_path / "章节.wav", 8)
    win.load_book(p)
    assert win.stack.currentWidget() is win.audio and win._media["kind"] == "audio" and win.external == []
    assert wait_for(lambda: win.audio.duration_ms() > 0)
    win.audio.seek(4000)
    assert wait_for(lambda: abs(win.audio.position_ms() - 4000) < 500)
    win.audio.play()
    assert wait_for(lambda: win.audio.is_playing())
    win.audio.pause()
    assert wait_for(lambda: not win.audio.is_playing())
    ent = win.bookmarks[p]                                                          # 暂停：立即存
    assert 3500 <= ent["_progress_ms"] < 5000 and 0.4 < ent["_progress_pct_"] < 0.65
    assert win.pushed and win.pushed[-1]["kind"] == "audio" and win.pushed[-1]["pos_ms"] == ent["_progress_ms"]
    bid = sync.book_id(p)
    assert 40 < win.shelf._progress[bid]["percent"] < 65
    saved = ent["_progress_ms"]
    win.show_shelf()                                                                # 离开：停播、放开文件
    assert win._shelf_visible() and win._media is None and win.audio.path() == ""
    renamed = str(tmp_path / "改名.wav")
    os.rename(p, renamed)                                                           # Windows 上文件仍被占用时会失败
    os.rename(renamed, p)
    win.load_book(p)
    assert wait_for(lambda: win.audio.duration_ms() > 0 and abs(win.audio.position_ms() - saved) < 600)
    win.show_shelf()


@needs_audio
def test_audio_speed_and_volume_remembered(win, tmp_path):
    p = write_wav(tmp_path / "v.wav", 3)
    win.load_book(p)
    win.audio.set_speed(1.5)
    win.audio.set_volume(40)
    win.show_shelf()
    assert win.cfg["audio_speed"] == 1.5 and win.cfg["audio_volume"] == 40
    win.audio.set_speed(1.0)
    win.load_book(p)
    assert win.audio.speed() == 1.5 and win.audio.volume() == 40
    win.show_shelf()


def settle_shelf(w):
    assert wait_for(lambda: not w.shelf.scanning and not w.shelf._cloud_busy), "书架扫描没有在时限内结束"


def test_shelf_open_progress_persist_and_restore_after_restart(tmp_path, monkeypatch):
    """走真实路径：书架双击 → openRequested → MainWindow 分发 → 翻页 / 播放 → Esc → 关窗；再开一个窗口，书架上有进度、再打开回到原位。"""
    from pyreader.mainwin import MainWindow
    lib = tmp_path / "lib"
    lib.mkdir()
    cbz = write_cbz(lib / "漫画.cbz", pages=5)
    wav = write_wav(lib / "有声书.wav", 8) if HAS_AUDIO else None

    def start():
        w = MainWindow()
        w.resize(900, 600)
        w.cfg["library_dirs"] = [str(lib)]
        w.show()
        w.shelf.rescan(force=True)
        settle_shelf(w)
        monkeypatch.setattr(w.sync, "push", lambda cfg, rec: None)
        w.shelf.external_opener = lambda p: pytest.fail("应用内媒体不能交给系统应用")
        return w

    def stop(w):
        w._progress_timer.stop()
        w.close()
        w.deleteLater()

    def book(w, name):
        return next(b for b in w.shelf.books.values() if b.filename == name)
    w = start()
    assert w.shelf.open_book(book(w, "漫画.cbz")) is True
    assert w.stack.currentWidget() is w.comic and w.comic.current_page() == 0
    w.comic.go_to(3)
    key(w, Qt.Key.Key_Escape)
    assert w._shelf_visible() and not w.comic.is_open()
    b = book(w, "漫画.cbz")
    assert b.percent == 75.0 and b.status == "reading" and sv.progress_text(b) == "75%"
    if HAS_AUDIO:
        assert w.shelf.open_book(book(w, "有声书.wav")) is True
        assert w.stack.currentWidget() is w.audio
        assert wait_for(lambda: w.audio.duration_ms() > 0)
        w.audio.seek(4000)
        assert wait_for(lambda: abs(w.audio.position_ms() - 4000) < 500)
        w.audio.play()
        assert wait_for(lambda: w.audio.is_playing())
        w.audio.pause()
        assert wait_for(lambda: not w.audio.is_playing())
        saved_ms = w.bookmarks[wav]["_progress_ms"]
        assert 3500 <= saved_ms < 5000
        w.close()                                                                   # 在媒体页直接退出：closeEvent 存档并放开文件
        assert w.audio.path() == ""
    stop(w)
    w2 = start()                                                                    # 「重启」：只靠磁盘上的 bookmarks.json / index.json
    b = book(w2, "漫画.cbz")
    assert b.percent == 75.0 and b.has_progress and b.status == "reading"
    assert w2.shelf.open_book(b) is True and w2.comic.current_page() == 3
    key(w2, Qt.Key.Key_Escape)
    if HAS_AUDIO:
        b = book(w2, "有声书.wav")
        assert b.has_progress and 40 < b.percent < 65
        assert w2.shelf.open_book(b) is True
        assert wait_for(lambda: w2.audio.duration_ms() > 0 and abs(w2.audio.position_ms() - saved_ms) < 600)
        w2.show_shelf()
    stop(w2)


@needs_audio
def test_audio_remote_position_applied_when_untouched(win, tmp_path):
    p = write_wav(tmp_path / "r.wav", 8)
    win.load_book(p)
    assert wait_for(lambda: win.audio.duration_ms() > 0)
    win._on_remote_progress(p, sync.make_media_record(sync.book_id(p), "audio", 50.0, "phone", pos_ms=4000, updated=time.time()))
    assert wait_for(lambda: win.audio.duration_ms() > 0 and abs(win.audio.position_ms() - 4000) < 600)
    assert win.bookmarks[p]["_progress_ms"] == 4000 and win.pushed == [] and not win.audio.is_playing()
    win.audio.play()                                                                # 播放过之后：迟到的远端位置不再抢
    assert wait_for(lambda: win.audio.is_playing())
    win._on_remote_progress(p, sync.make_media_record(sync.book_id(p), "audio", 10.0, "phone", pos_ms=1000, updated=time.time() + 60))
    assert win.audio.position_ms() > 3000
    win.show_shelf()


# ================================================================== 评审修复的回归测试
@needs_audio
def test_audio_open_without_playing_keeps_timestamp_and_accepts_newer_remote(win, tmp_path):
    p = write_wav(tmp_path / "o.wav", 12)
    win.bookmarks[p] = {"_progress_ms": 4000, "_progress_ts_": 1000.0}
    win.load_book(p)
    assert wait_for(lambda: win.audio.duration_ms() > 0 and abs(win.audio.position_ms() - 4000) < 600)
    wait_for(lambda: False, 1.5)                                                    # 续播到断点会发位置事件：不能当成本机的阅读
    assert win.bookmarks[p]["_progress_ts_"] == 1000.0 and win.pushed == [] and not win._media["dirty"]
    win._on_remote_progress(p, sync.make_media_record(sync.book_id(p), "audio", 80.0, "phone", pos_ms=6500, updated=time.time() - 30))
    assert wait_for(lambda: win.audio.duration_ms() > 0 and abs(win.audio.position_ms() - 6500) < 600)
    assert win.bookmarks[p]["_progress_ms"] == 6500
    win.show_shelf()
    assert win.pushed == []                                                         # 没动过：离开也不推


@needs_audio
def test_audio_autoplay_starts_playing_when_sync_disabled(win, tmp_path):
    p = write_wav(tmp_path / "ap.wav", 12)
    win.cfg["audio_autoplay"] = True
    win.load_book(p)                                                                # 同步没启用（pull 为空操作）：马上播
    assert wait_for(lambda: win.audio.is_playing())
    win.show_shelf()


@needs_audio
def test_audio_autoplay_waits_for_remote_position_then_plays(win, tmp_path, monkeypatch):
    p = write_wav(tmp_path / "aw.wav", 12)
    win.cfg["audio_autoplay"] = True
    monkeypatch.setattr(win.sync, "pull", lambda cfg, path: object())             # 模拟已启用同步：远端结果还没回来
    win.load_book(p)
    assert wait_for(lambda: win.audio.duration_ms() > 0)
    wait_for(lambda: False, 0.5)
    assert not win.audio.is_playing()                                               # 还在等远端位置
    win._on_remote_progress(p, sync.make_media_record(sync.book_id(p), "audio", 50.0, "phone", pos_ms=4000, updated=time.time()))
    assert wait_for(lambda: win.audio.is_playing() and win.audio.position_ms() >= 3500)
    win.show_shelf()


@needs_audio
def test_audio_user_seek_counts_as_reading(win, tmp_path):
    p = write_wav(tmp_path / "k.wav", 12)
    win.load_book(p)
    assert wait_for(lambda: win.audio.duration_ms() > 0)
    win.audio.seek(5000)
    assert wait_for(lambda: abs(win.audio.position_ms() - 5000) < 500)
    assert win._media["touched"] and win.bookmarks[p]["_progress_ms"] >= 4500
    win.show_shelf()


def test_close_without_changes_does_not_restamp_or_push(win, tmp_path):
    p = write_cbz(tmp_path / "u.cbz", pages=10)
    win.bookmarks[p] = {"_progress_page": 7, "_progress_ts_": 1000.0}
    win.load_book(p)
    assert win.comic.current_page() == 7
    win.show_shelf()
    assert win.bookmarks[p]["_progress_ts_"] == 1000.0 and win.pushed == []
    win.load_book(p)
    win.comic.go_to(8)
    win._media["saved_at"] -= 6
    win.comic.go_to(9)                                                              # 改过：关闭时照常存并推
    win.show_shelf()
    assert win.bookmarks[p]["_progress_page"] == 9 and win.pushed[-1]["page"] == 9


def test_comic_close_pushes_pending_unpushed_record(win, tmp_path):
    p = write_cbz(tmp_path / "pp.cbz", pages=10)
    win.load_book(p)
    win.comic.go_to(2)                                                              # 立刻存 + 推（节流起点）
    win.comic.go_to(3)
    win._media["saved_at"] -= 6
    win._on_progress_tick()                                                         # 存下了 3，但云端还在 30 秒节流里
    assert [r["page"] for r in win.pushed] == [2]
    win.show_shelf()                                                                # 关闭：没有新变化，但要补推
    assert [r["page"] for r in win.pushed] == [2, 3]


def test_comic_spread_last_screen_reaches_full_percent(win, tmp_path):
    p = write_cbz(tmp_path / "sp.cbz", pages=21)
    win.load_book(p)
    win.comic.set_spread(True)
    while win.comic.next_screen():
        pass
    assert win.comic.current_page() == 19 and win.comic.screen_last(19) == 20
    assert win._media_snapshot()["frac"] == 1.0
    win.show_shelf()
    assert win.pushed[-1]["percent"] == 100.0
    assert win.shelf._progress[sync.book_id(p)]["percent"] >= sh.DONE_PERCENT


def test_remote_comic_page_is_clamped_to_local_archive(win, tmp_path):
    p = write_cbz(tmp_path / "cl.cbz", pages=5)
    win.load_book(p)
    win._on_remote_progress(p, sync.make_media_record(sync.book_id(p), "comic", 100.0, "phone", page=1000, updated=time.time()))
    assert win.comic.current_page() == 4 and win._media["page"] == 4 and win.bookmarks[p]["_progress_page"] == 4
    win.show_shelf()


def test_sync_push_skips_record_older_than_one_already_queued(monkeypatch):
    w = sync.SyncWorker()
    sent = []
    monkeypatch.setattr(sync, "client_from_cfg", lambda cfg: object())
    monkeypatch.setattr(sync, "push_progress", lambda c, user, rec: sent.append(rec["updated"]))
    with w._push_lock:                                                              # 两个推送都在排队时，先发的那个晚拿到锁
        t_new = w.push({}, sync.make_media_record("b1", "comic", 10.0, "d", page=2, updated=200.0))
        t_old = w.push({}, sync.make_media_record("b1", "comic", 5.0, "d", page=1, updated=100.0))
        t_other = w.push({}, sync.make_media_record("b2", "comic", 5.0, "d", page=1, updated=50.0))
    for t in (t_new, t_old, t_other):
        t.join(5)
    assert sorted(sent) == [50.0, 200.0]
