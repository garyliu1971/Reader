"""OPDS 书源：解析 / 搜索模板 / 分页 / 相对链接 / 格式选择 / 下载 / 安全。全部走 tests/fakeopds.py（127.0.0.1），不碰真实网络。"""
import os
import time

import pytest

import fakeopds
from pyreader import sync
from pyreader.booksource import (BookSourceError, OpdsSource, book_filename, enabled_sources, pick_acquisition)
from pyreader.booksource import opds as opdsmod
from pyreader.booksource.base import Acquisition
from pyreader.config import DEFAULT_CONFIG
from pyreader.webdav import WebDavError


@pytest.fixture
def srv():
    s, h, base = fakeopds.start()
    yield h, base
    s.shutdown()
    s.server_close()


def src(base, **kw):
    kw.setdefault("allow_insecure", True)             # 桩只有 http；真实书源只接受 https
    return OpdsSource("Fake", base + "/opds/", **kw)


# ---------------------------------------------------------------- 解析
def test_root_navigation_entries_and_relative_links(srv):
    h, base = srv
    page = src(base).browse()
    titles = [e.title for e in page.entries]
    assert titles == ["Fiction", "Absolute"]          # data: 封面那个既不能下载也不能进入的条目被丢弃
    assert page.entries[0].kind == "nav"
    assert page.entries[0].href == base + "/opds/fiction/"          # 相对链接按 feed URL 解析
    assert page.entries[1].href == base + "/opds/fiction/"          # 绝对链接原样保留


def test_acquisition_entries_formats_and_sizes(srv):
    h, base = srv
    page = src(base).browse(base + "/opds/fiction/")
    emma, mixed, audio = page.entries
    assert emma.kind == "book" and emma.author == "Austen, Jane"
    assert emma.acquisitions[0].href == base + "/books/emma.epub"   # ../books/ 相对链接
    assert emma.acquisitions[0].size == len(fakeopds.EPUB_SMALL)
    assert mixed.formats() == [".epub", ".mobi", ".pdf"]            # 按 epub > mobi > pdf 排序
    assert audio.kind == "book" and audio.formats() == []           # mp3 不在 READABLE_EXTS：不当成可下载格式


def test_pagination_rel_next(srv):
    h, base = srv
    s = src(base)
    p1 = s.browse(base + "/opds/fiction/")
    assert p1.next_href == base + "/opds/fiction/?page=2"
    p2 = s.browse(p1.next_href)
    assert [e.title for e in p2.entries] == ["Persuasion"] and p2.next_href == ""


def test_browse_accepts_relative_href_against_source_url(srv):
    h, base = srv
    assert [e.title for e in src(base).browse("fiction/").entries][0] == "Emma"


# ---------------------------------------------------------------- 搜索
def test_search_via_opensearch_description_template(srv):
    h, base = srv
    s = src(base)
    page = s.search("pride and prejudice")
    assert [e.title for e in page.entries] == ["Pride and Prejudice"]
    assert page.entries[0].summary == "Jane Austen"
    assert [p for p, _ in h.log] == ["/opds/", "/opds/osd.xml", "/opds/search?q=pride+and+prejudice&start=1"]


def test_search_no_results_and_empty_query(srv):
    h, base = srv
    s = src(base)
    assert s.search("nothing here").entries == []
    with pytest.raises(BookSourceError):
        s.search("   ")


def test_search_unsupported_source(srv):
    h, base = srv
    h.routes["/opds/"] = (200, fakeopds.ATOM_CT, fakeopds.feed("Root"), {})
    with pytest.raises(BookSourceError, match="不支持搜索"):
        src(base).search("x")


def test_fill_template():
    f = opdsmod.fill_template
    assert f("http://h/s?q={searchTerms}&p={startPage?}&c={count?}", "a b&c") == "http://h/s?q=a+b%26c&p=1&c="
    assert f("http://h/s?q={searchTerms}&l={language?}", "x") == "http://h/s?q=x&l="


def test_inline_atom_search_link_is_the_template(srv):
    h, base = srv
    h.routes["/opds/"] = (200, fakeopds.ATOM_CT, fakeopds.feed(
        "Root", "", '<link rel="search" type="application/atom+xml" href="/opds/search?q={searchTerms}&amp;start=1"/>'), {})
    assert [e.title for e in src(base).search("nothing here").entries] == []


def test_opensearch_http_template_upgraded_to_https_when_not_insecure(monkeypatch):
    """Gutenberg 的描述文件里模板是 http://（服务器 301 回 https）：严格模式下先升级成 https，不让明文请求出门。"""
    osd = (b'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
           b'<Url type="application/atom+xml" template="http://m.example.org/s?query={searchTerms}"/></OpenSearchDescription>')
    s = OpdsSource("G", "https://example.org/opds/")
    s._search = ("https://example.org/osd.xml", "application/opensearchdescription+xml")
    monkeypatch.setattr(s, "_get", lambda url: (osd, url))
    assert s._search_template() == "https://m.example.org/s?query={searchTerms}"


# ---------------------------------------------------------------- 格式选择
def test_pick_acquisition_prefers_epub_then_smallest():
    a = lambda ext, size: Acquisition("u" + ext + str(size), "m", ext, size)
    assert pick_acquisition([a(".pdf", 5), a(".mobi", 5), a(".epub", 900), a(".epub", 100)]).size == 100
    assert pick_acquisition([a(".epub", 0), a(".epub", 900)]).size == 900        # 大小未知的排后面
    assert pick_acquisition([a(".pdf", 5), a(".mobi", 9)]).ext == ".mobi"
    assert pick_acquisition([a(".pdf", 5)]).ext == ".pdf"
    assert pick_acquisition([]) is None


def test_acquire_picks_smallest_epub_in_inline_entry(srv):
    h, base = srv
    s = src(base)
    mixed = s.browse(base + "/opds/fiction/").entries[1]
    acq = s.acquire(mixed)
    assert acq.ext == ".epub" and acq.href.endswith("/mixed.epub")


def test_acquire_follows_detail_page_for_search_results(srv):
    h, base = srv
    s = src(base)
    ent = s.search("pride and prejudice").entries[0]
    assert ent.acquisitions == []
    acq = s.acquire(ent)
    assert acq.href == base + "/books/pp-noimages.epub"
    assert "/opds/detail/1342.opds" in [p for p, _ in h.log]


def test_acquire_no_readable_format_raises(srv):
    h, base = srv
    s = src(base)
    audio = s.browse(base + "/opds/fiction/").entries[2]
    with pytest.raises(BookSourceError, match="没有可读格式"):
        s.acquire(audio)


# ---------------------------------------------------------------- 下载
def test_download_url_writes_file_without_part(srv, tmp_path):
    h, base = srv
    dest = tmp_path / "Fake" / "emma.epub"
    got = sync.download_url(base + "/books/emma.epub", str(dest), size=len(fakeopds.EPUB_SMALL), allow_insecure=True)
    assert got == str(dest) and dest.read_bytes() == fakeopds.EPUB_SMALL
    assert not os.path.exists(str(dest) + ".part")


def test_download_url_size_mismatch_discards_part_and_keeps_old_file(srv, tmp_path):
    h, base = srv
    dest = tmp_path / "emma.epub"
    dest.write_bytes(b"old")
    with pytest.raises(WebDavError, match="size mismatch"):
        sync.download_url(base + "/books/emma.epub", str(dest), size=len(fakeopds.EPUB_SMALL) + 1, allow_insecure=True)
    assert dest.read_bytes() == b"old" and not os.path.exists(str(dest) + ".part")


def test_download_url_truncated_response_fails_cleanly(srv, tmp_path):
    h, base = srv
    h.routes["/books/cut.epub"] = (200, "application/epub+zip", b"x" * 1000, {"Content-Length": "1000", "X-Truncate": "100"})
    dest = tmp_path / "cut.epub"
    with pytest.raises(WebDavError):
        sync.download_url(base + "/books/cut.epub", str(dest), allow_insecure=True)
    assert not dest.exists() and not os.path.exists(str(dest) + ".part")


def test_download_url_respects_max_bytes_and_cancel(srv, tmp_path):
    h, base = srv
    with pytest.raises(WebDavError, match="too large"):
        sync.download_url(base + "/books/mixed-big.epub", str(tmp_path / "a.epub"), max_bytes=100, allow_insecure=True)
    with pytest.raises(WebDavError, match="cancelled"):
        sync.download_url(base + "/books/mixed-big.epub", str(tmp_path / "b.epub"), should_cancel=lambda: True, allow_insecure=True)
    assert os.listdir(tmp_path) == []


def test_download_url_reuses_same_size_file(srv, tmp_path):
    h, base = srv
    dest = tmp_path / "emma.epub"
    sync.download_url(base + "/books/emma.epub", str(dest), size=len(fakeopds.EPUB_SMALL), allow_insecure=True)
    n = len(h.log)
    sync.download_url(base + "/books/emma.epub", str(dest), size=len(fakeopds.EPUB_SMALL), allow_insecure=True)
    assert len(h.log) == n


def test_download_url_progress_callback(srv, tmp_path):
    h, base = srv
    seen = []
    sync.download_url(base + "/books/mixed-big.epub", str(tmp_path / "p.epub"), size=len(fakeopds.EPUB_BIG),
                      allow_insecure=True, progress=lambda n, t: seen.append((n, t)))
    assert seen and seen[-1] == (len(fakeopds.EPUB_BIG), len(fakeopds.EPUB_BIG))


def test_download_url_rejects_http_unless_insecure(srv, tmp_path):
    h, base = srv
    with pytest.raises(WebDavError, match="non-https"):
        sync.download_url(base + "/books/emma.epub", str(tmp_path / "x.epub"))
    assert h.log == []                                  # 连请求都没发
    with pytest.raises(WebDavError, match="non-https"):
        sync.download_url("file:///etc/passwd", str(tmp_path / "y.epub"), allow_insecure=True)


def test_download_url_cross_host_redirect_drops_credentials(srv, tmp_path):
    h, base = srv
    s2, h2, base2 = fakeopds.start()
    try:
        h.routes["/books/redir.epub"] = (302, "text/plain", b"", {"Location": base2 + "/books/emma.epub"})
        o = src(base, user="bob", password="secret")
        hdr = o.fetch_headers(base + "/books/redir.epub")
        assert hdr["Authorization"].startswith("Basic ")
        sync.download_url(base + "/books/redir.epub", str(tmp_path / "r.epub"), headers=hdr, allow_insecure=True)
        assert h.log[-1][1] is not None                 # 原主机收到凭据
        assert h2.log[-1] == ("/books/emma.epub", None)  # 另一个主机（端口不同即不同源）没有
    finally:
        s2.shutdown()
        s2.server_close()


def test_credentials_only_sent_to_same_origin(srv):
    h, base = srv
    o = src(base, user="bob", password="secret")
    assert "Authorization" in o.fetch_headers(base + "/books/x.epub")
    assert "Authorization" not in o.fetch_headers("http://127.0.0.1:1/books/x.epub")
    assert "Authorization" not in o.fetch_headers("https://cdn.example.org/x.epub")
    o.browse()
    assert h.log[-1][1].startswith("Basic ")
    assert "Authorization" not in src(base).fetch_headers(base + "/x")


# ---------------------------------------------------------------- 安全
def test_xxe_doctype_rejected(srv):
    h, base = srv
    with pytest.raises(BookSourceError, match="DOCTYPE"):
        src(base).browse(base + "/opds/xxe/")


def test_lxml_parser_does_not_expand_entities_even_without_precheck():
    """第二道防线：即使 DOCTYPE 预检被绕过（如放在 64KB 之后），解析器本身也不展开实体、不读文件。"""
    evil = (b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&xxe;</title></feed>')
    doc = (b'<?xml version="1.0"?><!-- ' + b"x" * 70000 + b' --><!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>' + evil)
    try:
        root = opdsmod.etree.fromstring(doc, opdsmod._parser())
    except opdsmod.etree.XMLSyntaxError:
        return                                          # 直接拒绝也可以
    assert "hostname" not in "".join(root.itertext()) and (root.findtext("{http://www.w3.org/2005/Atom}title") or "") == ""


def test_not_a_feed_and_oversized_response_rejected(srv, monkeypatch):
    h, base = srv
    with pytest.raises(BookSourceError, match="不是 OPDS"):
        src(base).browse(base + "/opds/notfeed/")
    with pytest.raises(BookSourceError, match="过大"):
        src(base).browse(base + "/opds/huge/")


def test_http_source_rejected_without_allow_insecure(srv):
    h, base = srv
    with pytest.raises(BookSourceError, match="https"):
        src(base, allow_insecure=False).browse()
    assert h.log == []
    assert src(base, allow_insecure=True).browse().entries


def test_http_status_error_becomes_booksource_error(srv):
    h, base = srv
    with pytest.raises(BookSourceError, match="404"):
        src(base).browse(base + "/opds/missing/")


def test_unreachable_server_becomes_booksource_error():
    with pytest.raises(BookSourceError):
        OpdsSource("x", "http://127.0.0.1:1/opds/", allow_insecure=True, timeout=2).browse()


# ---------------------------------------------------------------- 配置 / 命名
def test_default_config_has_gutenberg_source():
    srcs = DEFAULT_CONFIG["book_sources"]
    assert len(srcs) == 1 and srcs[0]["url"] == "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    assert srcs[0]["type"] == "gutenberg_catalog"          # 不碰 /ebooks/search*（robots.txt 禁止）
    assert srcs[0]["user"] == "" and srcs[0]["password"] == "" and srcs[0]["enabled"] is True
    assert [s["name"] for s in enabled_sources(DEFAULT_CONFIG)] == ["Project Gutenberg"]


def test_enabled_sources_skips_bad_entries():
    cfg = {"book_sources": [{"name": "ok", "url": "https://a/"}, {"name": "off", "url": "https://a/", "enabled": False},
                            {"name": "a/b", "url": "https://a/"}, {"name": ".hid", "url": "https://a/"},
                            {"name": "nourl", "url": " "}, "junk", {"name": "CON", "url": "https://a/"}]}
    assert [s["name"] for s in enabled_sources(cfg)] == ["ok"]


def test_book_filename():
    assert book_filename("Pride and Prejudice", ".epub") == "Pride and Prejudice.epub"
    assert book_filename('A/B: "C"?', ".epub") == "A_B_ _C__.epub"
    assert book_filename("  ..  ", ".pdf") == "book.pdf"
    assert book_filename("红楼梦", ".epub") == "红楼梦.epub"
    assert len(book_filename("x" * 500, ".epub")) <= 85
