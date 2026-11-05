"""阶段 3：云端书库并入书架的逻辑层（进程内 fake WebDAV，永远不连真实服务器）。
覆盖：列表名字过滤 / 稳定性过滤（半截上传）、「云端已移除」的两轮完整列表规则（含子树失败、截断、rclone 式移动）、
下载（.part、大小校验、上限、路径穿越）、后台 worker 的单飞 / 离线 / 下载并入。Qt 层见 test_shelfcloud.py。"""
import io
import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pyreader import shelf as sh  # noqa: E402
from pyreader import shelfsync as ss  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader import webdav  # noqa: E402
from pyreader.webdav import WebDavClient, WebDavError  # noqa: E402

OLD = 1000.0                      # 「很久以前」的 mtime：稳定


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def pump(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


def put_book(h, rel, data=b"x", mtime=OLD):
    """直接往 fake 服务器的 library/ 放书（并建好父目录），可指定 mtime。"""
    parts = ["/dav", "/dav/library"]
    segs = rel.split("/")
    for i in range(1, len(segs)):
        parts.append("/dav/library/" + "/".join(segs[:i]))
    h.dirs.update(parts)
    p = "/dav/library/" + rel
    h.files[p] = data
    h.mtimes[p] = mtime
    h.etags[p] = '"e%d"' % len(h.files)
    return p


def drop_book(h, rel):
    p = "/dav/library/" + rel
    h.files.pop(p, None)


def rels(files):
    return sorted(f.rel for f in files)


# ================================================================== 列表：名字过滤
def test_fetch_filters_names_and_extensions(server):
    h, url = server
    for rel in ("小说/三体.epub", "小说/科幻/沙丘.TXT", "杂项.txt", "笔记.log", ".hidden.epub", "~$lock.docx",
                "小说/.git.txt", "小说/.三体.epub.part", "说明.exe"):
        put_book(h, rel)
    lst = ss.fetch_library(WebDavClient(url), now=time.time())
    assert lst.complete and not lst.root_missing and not lst.problem()
    assert rels(lst.files) == ["小说/三体.epub", "小说/科幻/沙丘.TXT", "杂项.txt"]       # 非书扩展名 / 点 / ~$ / .part 全忽略
    assert lst.unstable == set()
    assert [f.size for f in lst.files] == [1, 1, 1]


def test_fetch_rejects_hostile_names(server):
    h, url = server
    put_book(h, "ok.txt")
    h.extra_entries["/dav/library"] = [("/dav/library/..%5Cevil.txt", False), ("/dav/library/a%5Cb.txt", False),
                                       ("/dav/library/CON.txt", False), ("/dav/evil.txt", False)]
    lst = ss.fetch_library(WebDavClient(url), now=time.time())
    assert rels(lst.files) == ["ok.txt"]


def test_fetch_library_root_missing_is_incomplete_not_an_error(server):
    h, url = server                                  # 服务器上没有 library/
    lst = ss.fetch_library(WebDavClient(url), now=time.time())
    assert lst.files == [] and not lst.complete and lst.root_missing and "library" in lst.problem()


def test_fetch_propagates_network_errors(server):
    h, url = server
    h.fail_propfind.add("/dav/library")              # 根目录自己读失败（500）
    with pytest.raises(WebDavError):
        ss.fetch_library(WebDavClient(url), now=time.time())


def test_fetch_subtree_failure_and_truncation_are_incomplete(server):
    h, url = server
    put_book(h, "a/1.txt")
    put_book(h, "b/2.txt")
    h.fail_propfind.add("/dav/library/b")
    lst = ss.fetch_library(WebDavClient(url), now=time.time())
    assert rels(lst.files) == ["a/1.txt"] and not lst.complete and lst.errors == ["b"]
    assert "读取失败" in lst.problem()
    h.fail_propfind.clear()
    lst = ss.fetch_library(WebDavClient(url), now=time.time(), max_files=1)
    assert len(lst.files) == 1 and not lst.complete and lst.truncated and "上限" in lst.problem()


# ================================================================== 稳定性过滤（别人上传到一半的文件）
def test_recent_mtime_is_unstable_then_stable_later(server):
    h, url = server
    now = 1_700_000_000.0
    put_book(h, "新.epub", mtime=now - 10)           # 10 秒前才写入：可能还在上传
    put_book(h, "旧.epub", mtime=now - 3600)
    c = WebDavClient(url)
    lst = ss.fetch_library(c, now=now)
    assert lst.unstable == {"新.epub"} and [f.rel for f in lst.stable_files] == ["旧.epub"]
    assert rels(lst.files) == ["新.epub", "旧.epub"]                 # 仍在 files 里：不能当作「消失」
    lst2 = ss.fetch_library(c, {f.rel: f.size for f in lst.files}, now=now + 120)
    assert lst2.unstable == set()                                    # 两分钟后（大小没变）：稳定了


def test_size_change_between_rounds_is_unstable(server):
    h, url = server
    put_book(h, "大.epub", b"a" * 10)
    c = WebDavClient(url)
    prev = {f.rel: f.size for f in ss.fetch_library(c, now=time.time()).files}
    put_book(h, "大.epub", b"a" * 25)                # mtime 仍然很旧，但大小变了：还在写
    lst = ss.fetch_library(c, prev, now=time.time())
    assert lst.unstable == {"大.epub"}
    prev = {f.rel: f.size for f in lst.files}
    assert ss.fetch_library(c, prev, now=time.time()).unstable == set()


def test_mtime_far_in_future_is_server_clock_skew_not_forever_unstable(server):
    h, url = server
    now = 1_700_000_000.0
    put_book(h, "x.epub", mtime=now + 86400)         # 服务器时钟快一天
    assert ss.fetch_library(WebDavClient(url), now=now).unstable == set()


def test_half_uploaded_file_is_not_shown_nor_counted_removed():
    """半截上传：既不显示，也不会因为它「不在稳定列表里」被算成消失。"""
    cat = ss.CloudCatalog()
    rf = sh.RemoteFile("a.epub", 10, 0.0)
    lst = ss.Listing([rf], {"a.epub"}, complete=True)
    cat.apply(lst)
    assert cat.display_files() == [] and cat.removed_ids() == set() and "remote:a.epub" in cat.known
    cat.apply(ss.Listing([rf], set(), complete=True))
    assert rels(cat.display_files()) == ["a.epub"]


# ================================================================== 「云端已移除」：两轮完整列表
def L(*rels_, complete=True, unstable=()):
    return ss.Listing([sh.RemoteFile(r, 10, OLD) for r in rels_], set(unstable), complete=complete)


def test_removed_requires_two_complete_listings():
    cat = ss.CloudCatalog()
    cat.apply(L("a.epub", "b.epub"))
    cat.apply(L("b.epub"))                           # 第一轮缺失 a
    assert cat.removed_ids() == set()
    assert rels(cat.display_files()) == ["a.epub", "b.epub"]         # 还不能下结论：a 仍然显示
    cat.apply(L("b.epub"))                           # 第二轮缺失
    assert cat.removed_ids() == {"remote:a.epub"} and rels(cat.display_files()) == ["b.epub"]
    assert rels(cat.removed_files()) == ["a.epub"]
    cat.apply(L("a.epub", "b.epub"))                 # 重新出现：不再是已移除
    assert cat.removed_ids() == set() and rels(cat.display_files()) == ["a.epub", "b.epub"]


def test_single_subtree_failure_never_marks_removed():
    cat = ss.CloudCatalog()
    cat.apply(L("科幻/沙丘.epub", "杂项.txt"))
    for _ in range(5):                               # 子树一直读失败（不完整列表）：无论多少轮都不下结论
        cat.apply(L("杂项.txt", complete=False))
        assert cat.removed_ids() == set()
        assert rels(cat.display_files()) == ["杂项.txt", "科幻/沙丘.epub"]
    cat.apply(L("科幻/沙丘.epub", "杂项.txt"))
    assert cat.missing == {}


def test_incomplete_round_between_complete_ones_does_not_reset_or_advance():
    cat = ss.CloudCatalog()
    cat.apply(L("a.epub", "b.epub"))
    cat.apply(L("b.epub"))                           # 完整 1
    cat.apply(L("b.epub", complete=False))           # 不完整：不算
    assert cat.removed_ids() == set() and cat.missing["remote:a.epub"]["n"] == 1
    cat.apply(L("b.epub"))                           # 完整 2
    assert cat.removed_ids() == {"remote:a.epub"}


def test_rclone_style_move_copy_then_delete():
    """先复制到新路径、再删旧路径：旧路径两轮完整列表后才成为已移除；新路径是新书，互不影响。"""
    cat = ss.CloudCatalog()
    cat.apply(L("旧/书.epub"))
    cat.apply(L("旧/书.epub", "新/书.epub"))         # 复制阶段：两处都在
    assert cat.removed_ids() == set() and rels(cat.display_files()) == sorted(["旧/书.epub", "新/书.epub"])
    cat.apply(L("新/书.epub"))                       # 旧路径被删
    assert cat.removed_ids() == set() and "remote:旧/书.epub" in cat.missing
    cat.apply(L("新/书.epub"))
    assert cat.removed_ids() == {"remote:旧/书.epub"} and rels(cat.display_files()) == ["新/书.epub"]


def test_missing_counts_survive_restart():
    cat = ss.CloudCatalog()
    cat.apply(L("a.epub"))
    cat.apply(L())                                   # 缺失 1 次
    st = json.loads(json.dumps(cat.to_state()))      # 经 state.json 往返
    cat2 = ss.CloudCatalog({}, sh._clean_cloud(st)["missing"])
    assert rels(cat2.display_files()) == ["a.epub"] and cat2.removed_ids() == set()
    cat2.apply(L())
    assert cat2.removed_ids() == {"remote:a.epub"}


def test_cold_start_candidates_from_index_and_local_copies():
    """索引里的云端书（known_from_books）和本机缓存副本（extra_candidates）都能被判断移除。"""
    b1 = sh.Book("remote:x.epub", "x", "epub", 5, "cloud:", "", "cloud", "x.epub", 1.0)
    b2 = sh.Book("remote:y.epub", "y", "epub", 7, "cloud:", "", "synced", "y.epub", 1.0, ["/tmp/y.epub"])
    cat = ss.CloudCatalog(ss.known_from_books([b1]))
    for _ in range(2):
        cat.apply(L(), extra=ss.extra_candidates([b2]))
    assert cat.removed_ids() == {"remote:x.epub", "remote:y.epub"}


def test_clean_cloud_state_drops_bad_entries():
    st = sh._clean_cloud({"missing": {"remote:ok.epub": {"n": 2, "size": 5, "mtime": 1.5},
                                      "remote:../x": {"n": 2}, "local:" + "a" * 40: {"n": 1},
                                      "remote:bad.epub": {"n": "2"}, "remote:neg.epub": {"n": -1}, "remote:z.epub": 5}})
    assert list(st["missing"]) == ["remote:ok.epub"] and st["missing"]["remote:ok.epub"]["n"] == 2
    assert sh._clean_cloud(None) == {"missing": {}} and sh._clean_cloud({"missing": []}) == {"missing": {}}


def test_removed_book_shows_in_build_books_even_without_local_copy():
    gone = sh.RemoteFile("小说/旧.epub", 9, 5.0)
    books = sh.build_books([], [], None, removed={"remote:小说/旧.epub"}, removed_files=[gone])
    b = books["remote:小说/旧.epub"]
    assert b.where == "removed" and b.removed and b.auto_hidden and not b.local_paths
    assert sh.filter_books(books, "removed") == [b] and sh.filter_books(books, "all") == []
    # 云端列表未知（None）时不据此推断任何「消失」
    assert sh.build_books([], None, None, removed={"remote:小说/旧.epub"}, removed_files=[gone]) == {}


# ================================================================== 下载
def test_download_ok_writes_final_file_and_no_part(server, tmp_path):
    h, url = server
    put_book(h, "小说/三体.txt", b"hello world")
    dest = sync.download_book(WebDavClient(url), "小说/三体.txt", 11, dest_root=str(tmp_path))
    assert dest == str(tmp_path / "小说" / "三体.txt") and open(dest, "rb").read() == b"hello world"
    assert not [f for f in os.listdir(tmp_path / "小说") if f.endswith(".part")]


def test_download_size_mismatch_is_discarded_and_old_file_kept(server, tmp_path):
    h, url = server
    put_book(h, "a.txt", b"12345")
    old = tmp_path / "a.txt"
    old.write_bytes(b"OLD-CONTENT")
    with pytest.raises(WebDavError) as ei:
        sync.download_book(WebDavClient(url), "a.txt", 9, dest_root=str(tmp_path))     # 列表说 9 字节，实际 5
    assert "size mismatch" in str(ei.value)
    assert old.read_bytes() == b"OLD-CONTENT"                        # 旧文件原样
    assert os.listdir(tmp_path) == ["a.txt"]                         # .part 已清掉
    assert not (tmp_path / "b.txt").exists()
    put_book(h, "b.txt", b"123")
    with pytest.raises(WebDavError):
        sync.download_book(WebDavClient(url), "b.txt", 99, dest_root=str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["a.txt"]                 # 没有 b.txt，也没有 b.txt.part


def test_download_reuses_same_size_file_and_unknown_size_is_allowed(server, tmp_path):
    h, url = server
    put_book(h, "a.txt", b"12345")
    (tmp_path / "a.txt").write_bytes(b"XXXXX")
    sync.download_book(WebDavClient(url), "a.txt", 5, dest_root=str(tmp_path))
    assert (tmp_path / "a.txt").read_bytes() == b"XXXXX"             # 大小相同：不重下
    sync.download_book(WebDavClient(url), "a.txt", 0, dest_root=str(tmp_path))     # 列表没给大小：只校验传输完整性
    assert (tmp_path / "a.txt").read_bytes() == b"12345"


@pytest.mark.parametrize("rel", ["../evil.txt", "a/../../evil.txt", "a\\b.txt", "/abs.txt", ".hid.txt", "d/.x.epub",
                                 "~$t.docx", "CON.txt", "", "a//b.txt", "a:b.txt"])
def test_download_rejects_illegal_rel(server, tmp_path, rel):
    h, url = server
    with pytest.raises(WebDavError):
        sync.download_book(WebDavClient(url), rel, 1, dest_root=str(tmp_path / "root"))
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "root").exists()


def test_download_max_bytes_cap_and_cancel(server, tmp_path):
    h, url = server
    put_book(h, "big.txt", b"x" * 5000)
    with pytest.raises(WebDavError) as ei:
        sync.download_book(WebDavClient(url), "big.txt", 5000, dest_root=str(tmp_path), max_bytes=1000)
    assert "too large" in str(ei.value) and os.listdir(tmp_path) == []
    with pytest.raises(WebDavError):
        sync.download_book(WebDavClient(url), "big.txt", 5000, dest_root=str(tmp_path), should_cancel=lambda: True)
    assert os.listdir(tmp_path) == []


def test_get_to_file_detects_truncated_body(monkeypatch):
    class R:
        headers = {"Content-Length": "100"}
        def __init__(self): self.b = io.BytesIO(b"x" * 40)
        def read(self, n=-1): return self.b.read(n)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(webdav, "net_urlopen", lambda req, timeout=0: R())
    with pytest.raises(WebDavError) as ei:
        WebDavClient("http://127.0.0.1:1/dav").get_to_file("library/a.txt", io.BytesIO())
    assert "truncated" in str(ei.value)


def test_get_to_file_http_error_maps_to_status(server):
    h, url = server
    with pytest.raises(WebDavError) as ei:
        WebDavClient(url).get_to_file("library/none.txt", io.BytesIO())
    assert ei.value.status == 404


# ================================================================== ShelfSyncWorker
def cfg_for(url):
    return {"webdav_enabled": True, "webdav_url": url}


def test_worker_noop_when_disabled():
    w = ss.ShelfSyncWorker()
    assert w.refresh({}) is None and w.refresh({"webdav_enabled": False, "webdav_url": "http://x"}) is None
    assert w.download({}, "a.txt", 1, True, 1) is None


def test_worker_refresh_emits_listing_and_tracks_sizes(server):
    h, url = server
    put_book(h, "a/1.txt", b"abc")
    put_book(h, "2.epub", b"abcd")
    w = ss.ShelfSyncWorker()
    got, failed = [], []
    w.listed.connect(got.append)
    w.listFailed.connect(failed.append)
    w.refresh(cfg_for(url)).join(10)
    assert pump(lambda: got) and not failed
    assert got[0].complete and rels(got[0].files) == ["2.epub", "a/1.txt"]
    put_book(h, "2.epub", b"abcdefgh")               # 第二轮：大小变了 → 上传中
    w.refresh(cfg_for(url)).join(10)
    assert pump(lambda: len(got) == 2) and got[1].unstable == {"2.epub"}


def test_worker_offline_reports_failure_without_raising():
    w = ss.ShelfSyncWorker()
    got, failed = [], []
    w.listed.connect(got.append)
    w.listFailed.connect(failed.append)
    t = w.refresh({"webdav_enabled": True, "webdav_url": "http://127.0.0.1:9/dav"})     # 端口 9：连不上
    t.join(30)
    assert not t.is_alive() and pump(lambda: failed) and not got
    assert not w._running                           # 失败后仍可再次刷新（单飞标志已复位）


def test_worker_refresh_single_flight_and_rerun(server):
    h, url = server
    put_book(h, "a.txt")
    gate = threading.Event()
    started = []

    class Slow(WebDavClient):
        def list_tree_ex(self, *a, **k):
            started.append(1)
            gate.wait(10)
            return super().list_tree_ex(*a, **k)
    w = ss.ShelfSyncWorker(client_factory=lambda cfg: Slow(url))
    got = []
    w.listed.connect(got.append)
    t = w.refresh({"x": 1})
    assert pump(lambda: started)
    assert w.refresh({"x": 1}) is None and w.refresh({"x": 1}) is None     # 在跑：合并为「再跑一次」，不开新线程
    gate.set()
    t.join(10)
    assert pump(lambda: len(got) == 2) and len(started) == 2               # 恰好多跑一轮，而不是三轮


def test_worker_download_signals_and_token(server, tmp_path, monkeypatch):
    h, url = server
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(tmp_path / "rb"))
    put_book(h, "小说/三体.txt", b"12345")
    w = ss.ShelfSyncWorker()
    ok, bad, msgs = [], [], []
    w.downloadedFor.connect(lambda tok, p: ok.append((tok, p)))
    w.downloadFailed.connect(lambda tok, m: bad.append((tok, m)))
    w.status.connect(msgs.append)
    w.download(cfg_for(url), "小说/三体.txt", 5, True, 7).join(10)
    assert pump(lambda: ok)
    assert ok == [(7, str(tmp_path / "rb" / "小说" / "三体.txt"))] and not bad and "正在打开" in msgs[-1]
    w.download(cfg_for(url), "小说/三体.txt", 6, False, 8).join(10)       # 大小不符
    assert pump(lambda: bad) and bad[0][0] == 8 and "下载失败" in bad[0][1]
    assert len(ok) == 1


def test_worker_download_same_rel_merges_tokens(server, tmp_path, monkeypatch):
    h, url = server
    monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(tmp_path / "rb"))
    put_book(h, "a.txt", b"12345")
    gate = threading.Event()

    class Slow(WebDavClient):
        def get_to_file(self, *a, **k):
            gate.wait(10)
            return super().get_to_file(*a, **k)
    w = ss.ShelfSyncWorker(client_factory=lambda cfg: Slow(url))
    ok = []
    w.downloadedFor.connect(lambda tok, p: ok.append(tok))
    t = w.download({"x": 1}, "a.txt", 5, True, 1)
    assert w.download({"x": 1}, "a.txt", 5, True, 2) is None           # 同一本书：并入同一次下载
    gate.set()
    t.join(10)
    assert pump(lambda: len(ok) == 2) and sorted(ok) == [1, 2]
    assert [r for r in h.log if r[0] == "GET" and r[1].endswith("a.txt")].__len__() == 1


def test_worker_multiple_sources_list_and_download(monkeypatch, tmp_path):
    """多数据源：主源 rel 不带前缀，额外源以源名作前缀；下载额外源的书打到对应服务器、落到带前缀的本地目录。"""
    import fakedav
    srv1, h1, url1 = fakedav.start()
    srv2, h2, url2 = fakedav.start()
    try:
        put_book(h1, "主库/一.txt", b"111")
        put_book(h2, "子目录/二.txt", b"2222")
        monkeypatch.setattr(sync, "REMOTE_BOOKS_DIR", str(tmp_path / "rb"))
        cfg = {"webdav_enabled": True, "webdav_url": url1,
               "webdav_libraries": [{"name": "夸克", "url": url2, "enabled": True}]}
        w = ss.ShelfSyncWorker()
        got, failed = [], []
        w.listed.connect(got.append)
        w.listFailed.connect(failed.append)
        w.refresh(cfg).join(10)
        assert pump(lambda: got) and not failed
        assert rels(got[0].files) == ["主库/一.txt", "夸克/子目录/二.txt"]
        assert got[0].complete
        # 下载额外源的书：请求打到 url2 的 library/子目录/二.txt，本地落到 remote_books/夸克/子目录/二.txt
        ok, bad = [], []
        w.downloadedFor.connect(lambda tok, p: ok.append((tok, p)))
        w.downloadFailed.connect(lambda tok, m: bad.append((tok, m)))
        w.download(cfg, "夸克/子目录/二.txt", 4, False, 9).join(10)
        assert pump(lambda: ok) and not bad
        assert ok == [(9, str(tmp_path / "rb" / "夸克" / "子目录" / "二.txt"))]
        gets2 = [r for r in h2.log if r[0] == "GET"]
        assert gets2 and gets2[-1][1].endswith("/library/子目录/二.txt")
        assert not [r for r in h1.log if r[0] == "GET" and "二.txt" in r[1]]   # 主源不应收到额外源的下载
    finally:
        srv1.shutdown(); srv1.server_close()
        srv2.shutdown(); srv2.server_close()
