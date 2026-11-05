"""媒体格式（有声书 / 漫画压缩包）：书架可以存、列、传、下。
应用内媒体（音频、.zip / .cbz）有进度、打开发 openRequested（MainWindow 再分发到播放器 / 漫画页）；
外部格式（.cbr / .rar / .7z，没有内置解码器）阅读器读不了——打开交给系统默认应用，永远「未读」且没有进度。
覆盖：扩展名常量与 helper、扫描 / 校验 / 批量计划 / 云端列表接受它们（.part / 隐藏名仍被忽略）、
状态、打开路径、下载后交给系统应用、MainWindow 入口守卫、loader 兜底。
不会真的启动任何外部程序（external_opener / QDesktopServices.openUrl 都被替换）。"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import shelfview as sv  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader.webdav import WebDavClient  # noqa: E402

OLD = 1000.0
MB = 1024 * 1024


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


def put_book(h, rel, data=b"x", mtime=OLD):
    segs = rel.split("/")
    h.dirs.update(["/dav", "/dav/library"] + ["/dav/library/" + "/".join(segs[:i]) for i in range(1, len(segs))])
    p = "/dav/library/" + rel
    h.files[p] = data
    h.mtimes[p] = mtime
    h.etags[p] = '"e%d"' % (len(h.files) + len(data))
    return p


def lf(name, sub="", size=100, root="/lib"):
    return sh.LocalFile(f"{root}/{sub}/{name}" if sub else f"{root}/{name}", root, sub, name, size, 1.0)


@pytest.fixture
def rb(tmp_path, monkeypatch):
    d = tmp_path / "remote_books"
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(d))
    return d


def make_view(tmp_path, url, dirs=(), root="home"):
    cfg = {"webdav_enabled": True, "webdav_url": url} if url else {}
    ctx = {"library_dirs": list(dirs), "recent": [], "bookmarks": {}, "cfg": cfg}
    v = sv.ShelfView(lambda: ctx, root=str(tmp_path / root))
    v.ctx = ctx
    v.synced = []
    v.cloudSynced.connect(v.synced.append)
    v.opened = []
    v.openRequested.connect(v.opened.append)
    v.launched = []
    v.launch_ok = True
    v.external_opener = lambda p: (v.launched.append(p), v.launch_ok)[1]
    return v


def settle(v):
    assert wait_for(lambda: not v.scanning and not v._cloud_busy and not v._cloud._running
                    and not v._cloud._prog_running and not v._progress_busy), "云端同步 / 扫描没有在时限内结束"


def sync_wait(v):
    n = len(v.synced)
    assert v.sync_cloud(force=True) is True
    assert wait_for(lambda: len(v.synced) > n), "云端列表没有并入"
    settle(v)


# ================================================================== 扩展名常量 / helper
def test_ext_constants_invariants():
    assert set(sh.READABLE_EXTS).isdisjoint(sh.MEDIA_EXTS)
    assert set(sh.INAPP_MEDIA_EXTS).isdisjoint(sh.EXTERNAL_EXTS)
    assert sh.MEDIA_EXTS == sh.INAPP_MEDIA_EXTS + sh.EXTERNAL_EXTS
    assert sh.BOOK_EXTS == sh.READABLE_EXTS + sh.AUDIO_EXTS + sh.ARCHIVE_EXTS                # 书架可列的全部：与分流前一致
    assert set(sh.BOOK_EXTS) == set(sh.READABLE_EXTS) | set(sh.MEDIA_EXTS)
    for e in sh.BOOK_EXTS:
        assert e.startswith(".") and e == e.lower() and "." not in e[1:]
    for e in (".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav", ".zip", ".cbz"):
        assert e in sh.INAPP_MEDIA_EXTS
    assert sh.EXTERNAL_EXTS == (".cbr", ".7z", ".rar")                                         # 只有没有解码器的才交给系统应用
    for e in (".exe", ".bat", ".lnk", ".cmd", ".js", ".msi", ".part"):                 # 可执行类型永远不进白名单
        assert e not in sh.BOOK_EXTS
    assert sh.READABLE_EXTS == (".txt", ".md", ".markdown", ".epub", ".html", ".htm", ".mobi",
                                ".azw", ".azw3", ".prc", ".pdf", ".docx", ".pptx")


def test_is_readable_is_external_case_insensitive():
    assert sh.is_external("a/b/三体.CBR") and sh.is_external("X.Rar") and sh.is_external(r"C:\x\y.7Z")
    assert not sh.is_external("a.epub") and not sh.is_external("a.exe") and not sh.is_external("noext")
    assert not sh.is_external("a/b/三体.MP3") and not sh.is_external("X.Cbz") and not sh.is_external("y.zip")
    assert sh.is_readable("a.EPUB") and sh.is_readable("/x/y.pdf") and not sh.is_readable("a.mp3")
    assert not sh.is_external("a.cbr.part") and not sh.is_external(None) and not sh.is_readable(None)
    assert not sh.is_readable("a.zip") and not sh.is_external("a.rar.txt")
    assert sh.is_inapp_media("a/b/三体.MP3") and sh.is_inapp_media("X.Cbz") and sh.is_inapp_media("y.ZIP")
    assert not sh.is_inapp_media("a.cbr") and not sh.is_inapp_media("a.epub") and not sh.is_inapp_media(None)
    assert not sh.is_inapp_media("a.zip.txt") and not sh.is_inapp_media("a.mp3.part")


def test_inapp_kind_dispatch_table():
    assert [sh.inapp_kind(n) for n in ("a.MP3", "b.m4b", "c.wav", "d.zip", "e.CBZ")] == ["audio", "audio", "audio", "comic", "comic"]
    assert [sh.inapp_kind(n) for n in ("a.cbr", "b.rar", "c.7z", "d.epub", "e.pdf", "f.exe", "g", None)] == [""] * 8


# ================================================================== 本地扫描
def test_scan_local_includes_external_and_still_skips_hidden(tmp_path):
    lib = tmp_path / "lib"
    (lib / "有声书").mkdir(parents=True)
    (lib / ".cache").mkdir()
    for rel in ("a.epub", "有声书/ch1.mp3", "有声书/B.M4B", "漫画.ZIP", "x.cbz", "x.exe", "x.mp3.part", ".h.mp3",
                "~$t.zip", ".cache/z.zip", "readme.json"):
        (lib / rel).write_bytes(b"1")
    got = sorted(f.name for f in sh.scan_local([str(lib)]))
    assert got == ["B.M4B", "a.epub", "ch1.mp3", "x.cbz", "漫画.ZIP"]


# ================================================================== 校验 / 上传
def test_valid_remote_rel_and_upload_rel():
    for rel in ("a/b.MP3", "三体.cbz", "有声书/科幻/c.m4b", "x.7z", "y.RAR"):
        assert sh.valid_remote_rel(rel), rel
    for rel in ("a.mp3.part", ".a.mp3", "a/.b.zip", "x.exe", "noext", "~$a.zip", "a.mp3.exe"):
        assert not sh.valid_remote_rel(rel), rel
    assert ss.upload_rel("", "a.mp3") == "a.mp3"
    assert ss.upload_rel("有声书", "b.M4B") == "有声书/b.M4B"
    assert ss.upload_rel("漫画", "c.cbz") == "漫画/c.cbz" and ss.upload_rel("", "d.7z") == "d.7z"
    assert ss.upload_rel("", "x.mp3.part") is None and ss.upload_rel("", ".x.mp3") is None
    assert ss.upload_rel("", "a.exe") is None and ss.upload_rel("", "noext") is None


def test_upload_book_safe_mp3_end_to_end_no_overwrite(server, tmp_path):
    h, url = server
    f = tmp_path / "ch1.mp3"
    f.write_bytes(b"ID3" + b"\x00" * 5000)
    c = WebDavClient(url)
    r = ss.upload_book_safe(c, str(f), "有声书", "ch1.mp3")
    assert r.status == "ok", r
    assert h.files["/dav/library/有声书/ch1.mp3"].startswith(b"ID3")
    assert not [p for p in h.files if p.endswith(".part")]
    r2 = ss.upload_book_safe(c, str(f), "有声书", "ch1.mp3")
    assert r2.status == "exists"                                       # 不会静默覆盖
    assert ss.upload_book_safe(c, str(f), "", "ch1.exe").status == "error"


def test_upload_dialog_accepts_zip_rejects_exe():
    d = sv.UploadDialog([("/x/漫画.zip", "漫画.zip"), ("/x/a.exe", "a.exe")], [""])
    assert "不是支持的电子书 / 有声书 / 漫画压缩包格式" in d.problem()
    d2 = sv.UploadDialog([("/x/漫画.zip", "漫画.zip"), ("/x/b.m4b", "b.m4b")], [""])
    assert d2.problem() == ""


# ================================================================== 批量计划
def test_plan_batch_includes_external_and_applies_limits():
    files = [lf("ch1.mp3", "有声书/三体"), lf("a.cbz", "漫画"), lf("x.exe"), lf("big.m4b", "有声书", 5 * MB),
             lf("A.MP3", "有声书/三体"), lf("old.ZIP", "漫画")]
    plan = ss.plan_batch_upload(files, ["漫画/Old.zip"], 2 * MB)
    assert [(n, f) for _p, f, n in plan.items] == [("ch1.mp3", "有声书/三体"), ("a.cbz", "漫画"), ("A.MP3", "有声书/三体")]
    assert plan.skipped["名字或文件夹不合法"] == ["x.exe"]
    assert plan.skipped["云端已有同名文件"] == ["old.ZIP"]
    assert any("超过单个文件上限" in k and v == ["big.m4b"] for k, v in plan.skipped.items())


# ================================================================== 云端列表
def test_fetch_library_lists_external_but_not_part_or_hidden(server):
    h, url = server
    for rel in ("有声书/三体.mp3", "漫画/a.CBZ", "漫画/.b.cbz.part", "x.mp3.part", ".x.mp3", "小说/a.epub", "tool.exe"):
        put_book(h, rel)
    lst = ss.fetch_library(WebDavClient(url), now=time.time())
    assert lst.complete and not lst.problem()
    assert sorted(f.rel for f in lst.files) == sorted(["小说/a.epub", "有声书/三体.mp3", "漫画/a.CBZ"])


# ================================================================== Book 状态：没有进度 / 不在读
def test_build_books_external_never_has_progress_or_status():
    local = [lf("a.cbr", "漫画"), lf("b.epub", "小说")]
    bz, be = sync.book_id(local[0].path), sync.book_id(local[1].path)
    rid = "remote:有声书/c.7z"
    overlay = {"entries": {}, "progress": {bz: {"percent": 60.0, "updated": 50.0, "device": "d"},
                                           be: {"percent": 60.0, "updated": 50.0, "device": "d"},
                                           rid: {"percent": 99.0, "updated": 60.0, "device": "d"}}}
    bms = {local[0].path: {"_progress_": 500, "_progress_ts_": 70.0}, local[1].path: {"_progress_": 500, "_progress_ts_": 70.0}}
    books = sh.build_books(local, [("有声书/c.7z", 10, 1.0)], overlay, bookmarks=bms)
    z, e, m = books[bz], books[be], books[rid]
    assert (z.fmt, z.title, z.external, z.readable, z.inapp_media) == ("cbr", "a", True, False, False)
    assert (m.fmt, m.title, m.where) == ("7z", "c", "cloud")
    for b in (z, m):
        assert b.percent is None and not b.has_progress and b.status == "unread" and b.last_read == 0
        assert sv.progress_text(b) == ""
    assert e.percent == 60.0 and e.status == "reading" and e.readable          # 对照：电子书照旧
    shown = {b.id for b in sh.filter_books(books, "reading")}
    assert shown == {be}
    assert sh.filter_books(books, "done") == [] and {b.id for b in sh.filter_books(books, "all")} == {bz, be, rid}
    assert sh.continue_candidate(books).id == be
    del books[be]
    assert sh.continue_candidate(books) is None
    assert sh.note_progress(z, 50.0, 100.0) is False and z.percent is None
    sh.note_opened(z, 100.0)
    assert z.last_opened == 0 and z.last_read == 0


def test_external_placeholder_and_index_fmt_not_trusted():
    b = sh.Book("remote:x/a.cbr", "a", "", 0, "cloud:x", "", "missing", "x/a.cbr")      # 其它设备的占位：fmt 为空
    assert b.external and b.status == "unread"
    c = sh.Book("remote:x/a.epub", "a", "rar", 1, "cloud:x", "", "cloud", "x/a.epub")    # 索引缓存里 fmt 可信度低
    assert c.external is True                                                             # fmt 说是外部格式：按外部处理（更保守）
    d = sh.Book("remote:x/a.7z", "a", "epub", 1, "cloud:x", "", "cloud", "x/a.7z")
    assert d.external is True                                                             # 真实文件名为准
    e = sh.Book("remote:x/a.mp3", "a", "", 0, "cloud:x", "", "missing", "x/a.mp3")        # 占位的音频：应用内媒体，不是外部
    assert not e.external and e.inapp_media and e.readable
    f = sh.Book("remote:x/a.cbz", "a", "epub", 1, "cloud:x", "", "cloud", "x/a.cbz")
    assert f.inapp_media and not f.external                                               # 真实文件名为准


def test_index_roundtrip_keeps_external_cloud_book():
    books = sh.build_books([], [("有声书/c.rar", 10, 1.0)])
    wire = sh.index_to_wire(books)
    back = sh.index_from_wire(wire)[0]
    assert "remote:有声书/c.rar" in back and back["remote:有声书/c.rar"].external


# ================================================================== ShelfView：打开
def test_open_local_external_uses_os_app_not_reader(tmp_path, rb):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "三体.cbr"
    f.write_bytes(b"Rar!" * 100)
    v = make_view(tmp_path, None, [str(lib)])
    v.rescan(); settle(v)
    b = next(x for x in v.books.values() if x.fmt == "cbr")
    assert b.title == "三体" and b.status == "unread"
    assert v.open_book(b) is True
    assert v.launched == [str(f)] and v.opened == []                    # 交给系统应用，没有 openRequested
    assert v._opened == {} and v.books[b.id].last_read == 0             # 不进 recent / 索引
    assert v.progress_wanted() == []                                    # 也不会被拿去探测进度


def test_open_readable_still_emits_open_requested(tmp_path, rb):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.txt").write_text("hello")
    v = make_view(tmp_path, None, [str(lib)])
    v.rescan(); settle(v)
    assert v.open_book(next(iter(v.books.values()))) is True
    assert v.opened == [str(lib / "a.txt")] and v.launched == []


def test_os_app_missing_shows_notice(tmp_path, rb):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "c.cbr").write_bytes(b"Rar!")
    v = make_view(tmp_path, None, [str(lib)])
    v.launch_ok = False
    v.rescan(); settle(v)
    assert v.open_book(next(iter(v.books.values()))) is True
    assert v.launched and "没有能打开 .cbr" in v._notice and str(lib / "c.cbr") in v._notice


def test_launch_file_never_starts_non_whitelisted_extension(tmp_path, rb):
    v = make_view(tmp_path, None)
    v.launch_file(str(tmp_path / "evil.exe"))          # 非外部格式只会走 openRequested（阅读器入口），绝不启动系统程序
    assert v.launched == [] and v.opened == [str(tmp_path / "evil.exe")]


def test_cloud_only_external_downloads_then_opens_with_os_app(server, tmp_path, rb):
    h, url = server
    put_book(h, "有声书/三体.7z", b"7z\xbc\xaf" + b"x" * 20)
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    b = v.books["remote:有声书/三体.7z"]
    assert b.where == "cloud" and b.fmt == "7z" and sv.badge_text(b) == "仅云端"
    assert v.open_book(b) is True and v.model.busy == {b.id}
    assert wait_for(lambda: v.launched)
    dest = str(rb / "有声书" / "三体.7z")
    assert v.launched == [dest] and open(dest, "rb").read().startswith(b"7z")
    assert v.opened == [] and v._opened == {}
    settle(v)
    assert v.books[b.id].where == "synced" and v.books[b.id].status == "unread"
    v.launched.clear()
    assert v.open_book(v.books[b.id]) is True and v.launched == [dest]      # 已有副本：直接打开，不再下载
    assert len([r for r in h.log if r[0] == "GET" and r[1].endswith("三体.7z")]) == 1


def test_stale_external_download_does_not_launch(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.mp3", b"1")
    put_book(h, "b.txt", b"2")
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    assert v.request_download(v.books["remote:a.mp3"], open_when_done=False)
    assert wait_for(lambda: (rb / "a.mp3").exists() and not v._dl)
    settle(v)
    assert v.launched == [] and v.opened == []                           # 只下载，不打开
    v.note_opened(str(rb / "b.txt"))                                     # 用户后来打开了别的书：取消待打开意图
    assert v._open_token is None


def test_external_not_in_progress_wanted_but_book_is(server, tmp_path, rb):
    h, url = server
    put_book(h, "a.rar", b"1")
    put_book(h, "b.epub", b"2")
    v = make_view(tmp_path, url)
    sync_wait(v)
    assert v.progress_wanted() == ["remote:b.epub"]


def test_note_opened_and_progress_ignore_external(tmp_path, rb):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "a.7z"
    f.write_bytes(b"7z")
    v = make_view(tmp_path, None, [str(lib)])
    v.rescan(); settle(v)
    v.note_opened(str(f))
    v.note_progress(str(f), 50.0, 123.0)
    assert v._opened == {} and v._progress == {}


def test_card_and_list_show_format_without_progress(tmp_path, rb):
    b = sh.Book("remote:a.rar", "a", "rar", 1, "cloud:", "", "cloud", "a.rar", percent=50.0)
    assert sv.progress_text(b) == ""
    m = sv.BookModel()
    m.set_books([b])
    from PySide6.QtCore import Qt
    assert m.data(m.index(0, 1), Qt.ItemDataRole.DisplayRole) == "RAR"
    assert m.data(m.index(0, 2), Qt.ItemDataRole.DisplayRole) == ""


# ================================================================== MainWindow 入口守卫
@pytest.fixture
def win(monkeypatch):
    from pyreader.mainwin import MainWindow
    w = MainWindow()
    w.resize(1000, 700)
    w.show()
    QApplication.processEvents()
    yield w
    w._progress_timer.stop()
    w._resize_timer.stop()
    w.close()
    w.deleteLater()


def test_main_window_never_loads_external(win, monkeypatch, tmp_path):
    import pyreader.mainwin as mw
    launched = []
    win.shelf.external_opener = lambda p: (launched.append(p), True)[1]
    built = []
    monkeypatch.setattr(mw, "BookLoader", lambda p: built.append(p))
    noted = []
    monkeypatch.setattr(win.shelf, "note_opened", lambda *a, **k: noted.append(a))
    pulled = []
    monkeypatch.setattr(win.sync, "pull", lambda *a, **k: pulled.append(a))
    assert win._shelf_visible()
    for name in ("a.cbr", "b.RAR", "c.7z"):
        p = str(tmp_path / name)
        win.load_book(p)
        win._open_from_shelf(p)
    assert built == [] and noted == [] and pulled == []
    assert launched == [str(tmp_path / n) for n in ("a.cbr", "a.cbr", "b.RAR", "b.RAR", "c.7z", "c.7z")]
    assert win._shelf_visible() and not win._shelf_open_pending and win.book_path in (None, "")
    assert not win.cfg.get("recent")


def test_main_window_menus_and_filter_are_readable_only(win):
    assert win.BOOK_EXTS == sh.READABLE_EXTS
    assert not any(e in win.BOOK_EXTS for e in sh.EXTERNAL_EXTS)


def test_recent_menu_skips_external(win, tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.txt"
    a.write_bytes(b"1")
    b.write_text("1")
    win.cfg["recent"] = [str(a), str(b)]
    win._rebuild_recent_menu()
    assert [x.text() for x in win.recent_menu.actions()] == ["b.txt"]


def test_loader_refuses_external_instead_of_reading_it_as_text(tmp_path):
    from pyreader.loader import BookLoader
    f = tmp_path / "a.mp3"
    f.write_bytes(b"ID3" * 10)
    ld = BookLoader(str(f))
    errs = []
    ld.failed.connect(errs.append)
    ld.run()
    assert errs and "不能打开" in errs[0]
    assert ld._read_text_only(".mp3") is None


# ================================================================== 审查发现的回归（外部格式的杂散记录 / 列表上限 / 批量对话框 / 下载体验）
def test_stale_external_in_recent_and_opened_not_seeded(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    (d / "a.cbr").write_bytes(b"1")
    (d / "b.RAR").write_bytes(b"1")
    (d / "c.txt").write_bytes(b"1")
    fs = sh.scan_local([str(d)])
    paths = {f.name: f.path for f in fs}
    bid = {f.name: sync.book_id(f.path) for f in fs}
    bs = sh.build_books(fs, None, recent=[paths["a.cbr"], paths["b.RAR"], paths["c.txt"]],
                        opened={bid["a.cbr"]: 99.0, bid["b.RAR"]: 98.0})        # 旧版本留下的杂散记录
    assert bs[bid["a.cbr"]].last_read == 0 and bs[bid["b.RAR"]].last_read == 0
    assert [b.title for b in sh.filter_books(bs, "recent")] == ["c"]            # 外部格式不上「最近阅读」
    assert sh.continue_candidate(bs) is None


def test_index_restore_drops_external_opened_and_progress():
    books = sh.build_books([], [("有声书/c.7z", 10, 1.0), ("b.epub", 10, 1.0)])
    wire = sh.index_to_wire(books, {"remote:有声书/c.7z": 50.0, "remote:b.epub": 60.0},
                            {"remote:有声书/c.7z": {"percent": 40.0, "updated": 70.0, "device": "d"}})
    back, opened, _prog = sh.index_from_wire(wire)
    assert back["remote:有声书/c.7z"].last_read == 0 and back["remote:有声书/c.7z"].percent is None
    assert back["remote:有声书/c.7z"].last_opened == 0
    assert back["remote:b.epub"].last_opened == 60.0


def test_fetch_library_cap_counts_only_books(server):
    h, url = server
    for i in range(3):
        put_book(h, f"有声书/ch{i}.mp3")
    for i in range(30):                                   # 封面 / .cue / .nfo 之类的伴随文件：不能挤掉书的名额
        put_book(h, f"有声书/cover{i}.jpg")
        put_book(h, f"有声书/a{i}.cue")
    lst = ss.fetch_library(WebDavClient(url), now=time.time(), max_files=5)
    assert lst.complete and not lst.truncated
    assert sorted(f.rel for f in lst.files) == ["有声书/ch0.mp3", "有声书/ch1.mp3", "有声书/ch2.mp3"]
    assert lst.skipped >= 60
    for i in range(5):                                    # 书本身超过上限：仍然截断并报告
        put_book(h, f"小说/b{i}.epub")
    lst = ss.fetch_library(WebDavClient(url), now=time.time(), max_files=5)
    assert lst.truncated and not lst.complete and len(lst.files) == 5


def test_batch_dialog_breakdown_and_kind_selection(tmp_path):
    items = []
    for name, folder, n in (("a.epub", "小说", 100), ("b.pdf", "", 200), ("c.mp3", "有声书", 5000), ("d.zip", "备份", 7000)):
        p = tmp_path / name
        p.write_bytes(b"x" * n)
        items.append((str(p), folder, name))
    plan = ss.BatchPlan(items=items, total_bytes=12300)
    dlg = sv.BatchUploadDialog(plan)
    assert set(dlg.kind_boxes) == {"book", "audio", "archive"}
    assert dlg.selected_kinds() == {"book"}                               # 默认只选电子书
    assert "将上传 2 个文件" in dlg.head.text()
    ok = dlg.buttons.button(sv.QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()
    dlg.kind_boxes["audio"].setChecked(True)
    assert "将上传 3 个文件" in dlg.head.text() and dlg.selected_kinds() == {"book", "audio"}
    dlg.kind_boxes["book"].setChecked(False)
    dlg.kind_boxes["audio"].setChecked(False)
    assert not ok.isEnabled() and "至少勾选" in dlg.error.text()          # 什么都没选：不能开始
    only_audio = sv.BatchUploadDialog(ss.BatchPlan(items=[items[2]], total_bytes=5000))
    assert only_audio.selected_kinds() == set() and not only_audio.buttons.button(sv.QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_batch_start_respects_chosen_kinds(server, tmp_path, rb):
    h, url = server
    lib = tmp_path / "mine"
    lib.mkdir()
    (lib / "a.epub").write_bytes(b"e" * 20)
    (lib / "backup.zip").write_bytes(b"z" * 20)
    (lib / "ch1.mp3").write_bytes(b"m" * 20)
    v = make_view(tmp_path, url, [str(lib)])
    v.reports = []
    v.batch_reporter = lambda s, f: v.reports.append(s)
    v.confirm_push = lambda: False
    v.batch_chooser = lambda plan: ("", {"book", "audio"})                # 用户没勾压缩包
    v.rescan(force=True)
    assert wait_for(lambda: not v.scanning)
    assert v.start_batch_upload() is True
    assert wait_for(lambda: v._batch is None and v.reports)
    assert sorted(k for k in h.files if k.startswith("/dav/library/")) == ["/dav/library/a.epub", "/dav/library/ch1.mp3"]


def test_download_book_disk_space_check_and_progress(server, tmp_path, monkeypatch):
    h, url = server
    put_book(h, "a.mp3", b"x" * 5000)
    seen = []
    p = sync.download_book(WebDavClient(url), "a.mp3", 5000, dest_root=str(tmp_path / "ok"),
                           progress=lambda n, t: seen.append((n, t)))
    assert os.path.getsize(p) == 5000 and seen and seen[-1] == (5000, 5000)
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(sync.shutil, "disk_usage", lambda _p: Usage(10 ** 9, 10 ** 9 - 100, 100))
    gets = len([r for r in h.log if r[0] == "GET" and r[1].endswith("a.mp3")])
    with pytest.raises(Exception) as ei:
        sync.download_book(WebDavClient(url), "a.mp3", 5000, dest_root=str(tmp_path / "full"))
    assert "磁盘空间不足" in str(ei.value)
    assert not (tmp_path / "full" / "a.mp3").exists() and not (tmp_path / "full" / "a.mp3.part").exists()
    assert len([r for r in h.log if r[0] == "GET" and r[1].endswith("a.mp3")]) == gets      # 没发出新的 GET


def test_sweep_stale_parts(tmp_path):
    root = tmp_path / "rb"
    (root / "有声书").mkdir(parents=True)
    (root / "有声书" / "x.m4b.part").write_bytes(b"1" * 10)
    (root / "有声书" / "y.m4b").write_bytes(b"1" * 10)
    (root / "z.zip.part").write_bytes(b"1")
    assert sync.sweep_stale_parts(str(root)) == 2
    assert sorted(os.listdir(root / "有声书")) == ["y.m4b"] and not (root / "z.zip.part").exists()
    (root / "w.mp3.part").write_bytes(b"1")
    assert sync.sweep_stale_parts(str(root)) == 0 and (root / "w.mp3.part").exists()     # 每个目录每进程只扫一次


def test_oversize_download_blocked_locally_with_setting_hint(server, tmp_path, rb):
    h, url = server
    put_book(h, "有声书/big.m4b", b"x" * (3 * MB))
    v = make_view(tmp_path, url)
    v.ctx["cfg"]["webdav_max_book_mb"] = 1
    v.show()
    sync_wait(v)
    b = v.books["remote:有声书/big.m4b"]
    gets = len([r for r in h.log if r[0] == "GET" and r[1].endswith("big.m4b")])
    assert v.request_download(b, open_when_done=True) is False
    assert "超过单个文件上限（1 MB）" in v._notice and "设置" in v._notice
    assert len([r for r in h.log if r[0] == "GET" and r[1].endswith("big.m4b")]) == gets and not v._dl


def test_oversize_single_upload_blocked_with_hint(server, tmp_path, rb):
    h, url = server
    f = tmp_path / "big.mp3"
    f.write_bytes(b"x" * (2 * MB))
    v = make_view(tmp_path, url)
    v.ctx["cfg"]["webdav_max_book_mb"] = 1
    v.upload_chooser = lambda items, folders: pytest.fail("超限的不应进确认框")
    assert v.start_upload([str(f)]) is False
    assert "超过单个文件上限（1 MB）" in v._notice and "设置" in v._notice and not h.files


def test_external_download_launches_even_when_hidden_or_superseded(server, tmp_path, rb):
    h, url = server
    put_book(h, "有声书/a.rar", b"Rar!" + b"x" * 20)
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    b = v.books["remote:有声书/a.rar"]
    assert v.request_download(b, open_when_done=True)
    v.hide()                                              # 下载期间用户切走了书架
    v._open_token = None                                  # 并且打开了别的书（意图被取代）
    assert wait_for(lambda: v.launched)
    assert v.launched == [str(rb / "有声书" / "a.rar")] and v.opened == []       # 系统应用不会抢阅读窗口，所以照样启动


def test_readable_stale_download_tells_user_instead_of_silence(server, tmp_path, rb):
    h, url = server
    put_book(h, "b.txt", b"hello")
    v = make_view(tmp_path, url)
    v.show()
    sync_wait(v)
    msgs = []
    v.uploadStatus.connect(lambda t, ms: msgs.append(t))
    assert v.request_download(v.books["remote:b.txt"], open_when_done=True)
    v._open_token = None
    assert wait_for(lambda: not v._dl and msgs)
    assert v.opened == [] and "已下载：b.txt" in msgs[-1]


def test_download_progress_shown_in_notice(tmp_path, rb):
    v = make_view(tmp_path, None)
    b = sh.Book("remote:a.m4b", "有声书A", "m4b", 4 * MB, "cloud:", "", "cloud", "a.m4b")
    v._books = {b.id: b}
    v._dl[7] = (b.id, "a.m4b", True)
    v._on_download_progress(7, MB, 4 * MB)
    assert "正在下载：有声书A 25%" in v._notice and "1.0/4.0 MB" in v._notice
    v._on_download_progress(99, MB, 4 * MB)              # 未知 / 已结束的 token：忽略
    assert "有声书A 25%" in v._notice


def test_just_uploaded_file_not_hidden_as_uploading(server, tmp_path):
    h, url = server
    f = tmp_path / "big.m4b"
    f.write_bytes(b"A" * 3000)
    c = WebDavClient(url)
    r = ss.upload_book_safe(c, str(f), "有声书", "big.m4b")
    assert r.status == "ok"
    now = time.time()
    h.mtimes["/dav/library/有声书/big.m4b"] = now          # MOVE 把服务器 mtime 设成完成时刻
    assert ss.fetch_library(c, now=now).unstable == {"有声书/big.m4b"}                       # 别的设备：仍按「上传中」处理
    lst = ss.fetch_library(c, now=now, trusted={"有声书/big.m4b": 3000})                      # 本设备刚传完并核对过大小
    assert lst.unstable == set() and [x.rel for x in lst.stable_files] == ["有声书/big.m4b"]
    assert ss.fetch_library(c, now=now, trusted={"有声书/big.m4b": 1}).unstable == {"有声书/big.m4b"}   # 大小对不上：不信任


def test_worker_upload_records_trusted_rel(server, tmp_path):
    h, url = server
    f = tmp_path / "x.mp3"
    f.write_bytes(b"A" * 100)
    w = ss.ShelfSyncWorker()
    got = []
    w.uploadDone.connect(lambda tok, res: got.append(res))
    t = w.upload({"webdav_enabled": True, "webdav_url": url}, str(f), "有声书", "x.mp3", 1)
    t.join(10)
    assert wait_for(lambda: got) and got[0].status == "ok"
    assert w._trusted == {"有声书/x.mp3": 100}


def test_fmt_label_falls_back_to_filename_for_placeholders():
    b = sh.build_books([], [], {"entries": {"remote:有声书/a.rar": {"meta": {"title": "a"}}}, "progress": {}})["remote:有声书/a.rar"]
    assert b.fmt == "" and b.external and b.fmt_label == "RAR"
    assert sh.Book("local:" + "0" * 40, "t", "", 0, "", "", "missing").fmt_label == ""
    assert sh.media_kind("a.MP3") == "audio" and sh.media_kind("b.cbz") == "archive" and sh.media_kind("c.epub") == "book"
    assert sh.media_kind("d.exe") == "" and sh.media_kind(None) == ""


# ================================================================== 工具栏「上传到远程书库」（原「远程书库」下拉已移除，浏览走书架）
def test_toolbar_remote_button_is_upload_only(win, tmp_path, monkeypatch):
    act = win.remote_act
    assert act.menu() is None                                 # 不再有下拉（列表 / 浏览入口已去掉）
    assert "上传" in act.text() and "远程书库" in act.text()
    for gone in ("remote_menu", "_rebuild_remote_menu", "_fill_remote_menu", "_open_remote_book"):
        assert not hasattr(win, gone)
    calls = []
    monkeypatch.setattr(win.shelf, "start_upload", lambda paths: calls.append(list(paths)) or True)
    # 未启用 WebDAV：不上传
    monkeypatch.setattr(win, "_sync_on", lambda: False)
    act.trigger()
    assert calls == []
    # 启用但没打开书：不上传
    monkeypatch.setattr(win, "_sync_on", lambda: True)
    win.book_path = None
    act.trigger()
    assert calls == []
    # 启用且已打开书：走书架的上传流程（确认对话框 / skip-existing 等行为都在 start_upload 内）
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    win.book_path = str(f)
    act.trigger()
    assert calls == [[str(f)]]
