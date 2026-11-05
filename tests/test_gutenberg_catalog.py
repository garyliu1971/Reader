"""Gutenberg 离线目录书源：下载一次 pg_catalog.csv -> 缓存 -> 本地搜索 -> epub 直链下载。全部走本机桩，不碰真实网络。"""
import os
import time

import pytest

import fakeopds
from pyreader import sync
from pyreader.booksource import (BookSourceError, GutenbergCatalogSource, enabled_sources, source_from_cfg)
from pyreader.booksource import gutenberg as gb

CSV = ("Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
       '1342,Text,1998-06-01,Pride and Prejudice,en,"Austen, Jane, 1775-1817",Love stories,PR,Classics\n'
       '20686,Sound,2007-02-03,Pride and Prejudice,en,"Austen, Jane, 1775-1817",Love stories,PR,\n'
       '26301,Text,2008-01-01,"Pride and Prejudice, Illustrated",en,"Austen, Jane, 1775-1817",x,PR,\n'
       '158,Text,1994-01-01,Emma,en,"Austen, Jane, 1775-1817",x,PR,\n'
       '777,Text,2000-01-01,Orgueil et Préjugés,fr; en,"Austen, Jane, 1775-1817",x,PR,\n'
       '5,Text,2000-01-01,"Multi\nLine Title",de,Someone,x,PR,\n'
       "bad,Text,2000-01-01,No Id,en,Nobody,x,PR,\n").encode("utf-8")


@pytest.fixture
def srv():
    s, h, base = fakeopds.start()
    h.routes["/feeds/pg_catalog.csv"] = (200, "text/csv", CSV, {})
    for pid in ("1342", "158"):
        h.routes[f"/ebooks/{pid}.epub.noimages"] = (200, "application/epub+zip", b"PK-" + pid.encode() * 50, {})
    yield h, base
    s.shutdown()
    s.server_close()


def make(base, tmp_path, **kw):
    return GutenbergCatalogSource("Project Gutenberg", base + "/feeds/pg_catalog.csv", allow_insecure=True,
                                  cache_dir=str(tmp_path / "cache"), epub_urls=(base + "/ebooks/{id}.epub.noimages",), **kw)


def test_search_local_ranking_and_text_only(srv, tmp_path):
    h, base = srv
    page = make(base, tmp_path).search("pride and prejudice")
    assert [e.title for e in page.entries] == ["Pride and Prejudice", "Pride and Prejudice, Illustrated"]   # 完全匹配排前；Sound 条目被跳过
    e = page.entries[0]
    assert e.kind == "book" and e.author.startswith("Austen") and e.formats() == [".epub"]
    assert e.acquisitions[0].href == base + "/ebooks/1342.epub.noimages"


def test_search_by_author_and_language(srv, tmp_path):
    h, base = srv
    s = make(base, tmp_path)
    assert {e.title for e in s.search("austen").entries} == {
        "Pride and Prejudice", "Pride and Prejudice, Illustrated", "Emma", "Orgueil et Préjugés"}
    assert [e.title for e in s.search("austen lang:fr").entries] == ["Orgueil et Préjugés"]    # 'fr; en' 多语言
    assert [e.title for e in s.search("lang:de").entries] == ["Multi Line Title"]              # 只给 lang 也行，换行被压成空格
    assert s.search("zzzz").entries == []
    with pytest.raises(BookSourceError):
        s.search("  ")


def test_catalog_downloaded_once_then_cached(srv, tmp_path):
    h, base = srv
    s = make(base, tmp_path)
    s.search("emma")
    s.search("austen")
    make(base, tmp_path).search("emma")                 # 新实例也直接用磁盘缓存
    assert [p for p, _ in h.log].count("/feeds/pg_catalog.csv") == 1
    assert os.path.isfile(s.cache_path) and not os.path.exists(s.cache_path + ".part")


def test_stale_cache_refreshed_and_failure_falls_back_to_stale(srv, tmp_path):
    h, base = srv
    s = make(base, tmp_path, max_age=3600)
    s.search("emma")
    old = time.time() - 8 * 3600
    os.utime(s.cache_path, (old, old))
    s.search("emma")
    assert [p for p, _ in h.log].count("/feeds/pg_catalog.csv") == 2          # 过期 -> 重新下载
    os.utime(s.cache_path, (old, old))
    h.routes["/feeds/pg_catalog.csv"] = (500, "text/plain", b"", {})
    assert s.search("emma").entries                                             # 刷新失败：继续用旧缓存


def test_download_failure_without_cache_is_booksource_error(srv, tmp_path):
    h, base = srv
    h.routes["/feeds/pg_catalog.csv"] = (404, "text/plain", b"", {})
    with pytest.raises(BookSourceError, match="下载 Gutenberg 目录失败"):
        make(base, tmp_path).search("emma")


def test_truncated_download_retried_once(srv, tmp_path):
    h, base = srv
    h.routes["/feeds/pg_catalog.csv"] = [(200, "text/csv", CSV, {"Content-Length": str(len(CSV)), "X-Truncate": "50"}),
                                         (200, "text/csv", CSV, {})]
    assert make(base, tmp_path).search("emma").entries
    assert [p for p, _ in h.log].count("/feeds/pg_catalog.csv") == 2


def test_corrupt_or_empty_catalog_rejected(srv, tmp_path):
    h, base = srv
    h.routes["/feeds/pg_catalog.csv"] = (200, "text/csv", b"nothing,useful\n1,2\n", {})
    with pytest.raises(BookSourceError, match="里没有书"):
        make(base, tmp_path).search("emma")


def test_catalog_requires_https_unless_insecure(srv, tmp_path):
    h, base = srv
    s = GutenbergCatalogSource("G", base + "/feeds/pg_catalog.csv", cache_dir=str(tmp_path / "c"))
    with pytest.raises(BookSourceError):
        s.search("emma")
    assert h.log == []


def test_browse_makes_no_request_and_acquire_downloads_epub(srv, tmp_path):
    h, base = srv
    s = make(base, tmp_path)
    assert s.browse().entries == [] and h.log == []        # 打开对话框不应触发 20MB 下载
    ent = s.search("emma").entries[0]
    acq = s.acquire(ent)
    dest = tmp_path / "dl" / "Emma.epub"
    sync.download_url(acq.href, str(dest), size=acq.size, headers=s.fetch_headers(acq.href), allow_insecure=True)
    assert dest.read_bytes() == b"PK-" + b"158" * 50


def test_user_agent_sent(srv, tmp_path):
    h, base = srv
    orig = gb.GutenbergCatalogSource.fetch_headers
    assert "PyReader" in orig(make(base, tmp_path), "x")["User-Agent"]


def test_source_from_cfg_dispatch_and_type_default():
    g = source_from_cfg({"name": "G", "type": "gutenberg_catalog", "url": "https://x/pg_catalog.csv"})
    o = source_from_cfg({"name": "Home", "url": "https://nas/opds"})            # 没有 type = opds（旧配置兼容）
    assert isinstance(g, GutenbergCatalogSource) and type(o).__name__ == "OpdsSource"
    assert source_from_cfg({"name": "x", "url": " "}) is None
    assert enabled_sources({"book_sources": [{"name": "G", "type": "gutenberg_catalog", "url": "https://x/"}]})[0]["type"] == "gutenberg_catalog"
