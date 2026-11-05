"""阶段 0：webdav.py 加固（etag、list_tree 安全与完整性、put headers、响应头、get 上限、
目录缓存、重定向不泄露凭据、http 公网警告）。全部用进程内 fake WebDAV（tests/fakedav.py）。"""
import threading
import time

import pytest

import fakedav
from pyreader import config, webdav
from pyreader.webdav import (RISK_ANONYMOUS_PUBLIC, RISK_PLAIN_HTTP_PUBLIC, WebDavClient,
                             WebDavError, host_is_private, url_risks)


def _tree(h, c, files):
    """在 fake 服务器上建 library 树：files 是 'a/b.epub' 形式的相对路径列表。"""
    c.mkdirs("library")
    for f in files:
        d = f.rsplit("/", 1)[0] if "/" in f else ""
        if d:
            c.mkdirs("library/" + d)
        c.put("library/" + f, b"x")


def _names(res):
    return sorted(r for r, _ in res.files)


# ---------------- etag / mtime / 响应头 ----------------
def test_etag_and_mtime_change_on_put(server):
    h, url = server
    c = WebDavClient(url)
    h.now = lambda: 1700000000
    c.put("a.json", b"one")
    e1 = c.propfind("", 1)[0]
    h.now = lambda: 1700000005
    c.put("a.json", b"two")
    e2 = c.propfind("", 1)[0]
    assert e1.etag and e2.etag and e1.etag != e2.etag
    assert (e1.mtime, e2.mtime) == (1700000000, 1700000005)
    # 同一秒内的两次 PUT：mtime 相同、etag 不同（phase 4 的 racy 规则依赖 etag）
    c.put("a.json", b"three!")
    e3 = c.propfind("", 1)[0]
    c.put("a.json", b"four!!")
    e4 = c.propfind("", 1)[0]
    assert e3.mtime == e4.mtime and e3.etag != e4.etag


def test_etag_is_empty_when_server_gives_none(server):
    h, url = server
    c = WebDavClient(url)
    h.raw_propfind["/dav"] = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                              b'<d:response><d:href>/dav/f.txt</d:href><d:propstat><d:prop>'
                              b'<d:resourcetype/><d:getcontentlength>3</d:getcontentlength></d:prop>'
                              b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                              b'</d:multistatus>')
    (e,) = c.propfind("", 1)
    assert (e.name, e.size, e.etag) == ("f.txt", 3, "")


def test_response_headers_exposed_lowercase(server):
    h, url = server
    c = WebDavClient(url)
    r = c.put_ex("f.txt", b"hello")
    assert r.status == 201 and r.headers["etag"] == h.etags["/dav/f.txt"] and "date" in r.headers
    g = c.get_ex("f.txt")
    assert g.body == b"hello" and g.headers["etag"] == r.headers["etag"] and "date" in g.headers


def test_old_return_types_unchanged(server):
    h, url = server
    c = WebDavClient(url)
    assert c.put("f.txt", b"1") is None
    assert c.get("f.txt") == b"1"
    st, body = c._request("GET", "f.txt")
    assert (st, body) == (200, b"1")
    c.mkdirs("library")
    c.put("library/a.txt", b"1")
    out = c.list_tree("library")
    assert isinstance(out, list) and out[0][0] == "a.txt" and out[0][1].size == 1


# ---------------- put(headers=) / If-None-Match ----------------
def test_put_if_none_match_star(server):
    h, url = server
    c = WebDavClient(url)
    c.put("f.txt", b"first", headers={"If-None-Match": "*"})
    with pytest.raises(WebDavError) as ei:
        c.put("f.txt", b"second", headers={"If-None-Match": "*"})
    assert ei.value.status == 412
    assert h.files["/dav/f.txt"] == b"first"                    # 没有静默覆盖
    r = c.put_ex("f.txt", b"third", headers={"If-None-Match": "*"}, ok_codes=(412,))
    assert r.status == 412 and h.files["/dav/f.txt"] == b"first"
    c.put("f.txt", b"fourth")                                   # 不带头照常覆盖
    assert h.files["/dav/f.txt"] == b"fourth"


def test_conditional_get_etag(server):
    h, url = server
    c = WebDavClient(url)
    c.put("f.txt", b"1")
    et = h.etags["/dav/f.txt"]
    assert c.get_ex("f.txt", headers={"If-None-Match": et}, ok_codes=(304,)).status == 304


# ---------------- get(max_bytes) ----------------
def test_get_max_bytes(server):
    h, url = server
    c = WebDavClient(url)
    c.put("big.bin", b"x" * 5000)
    assert c.get("big.bin", max_bytes=5000) == b"x" * 5000
    assert len(c.get("big.bin")) == 5000                         # 默认不限制
    with pytest.raises(WebDavError, match="too large"):
        c.get("big.bin", max_bytes=4999)


def test_get_max_bytes_without_content_length(server):
    """没有 Content-Length（如分块传输）时也要靠读取上限兜住。"""
    h, url = server

    class _R:
        headers = {}

        def __init__(self):
            self.n = 0

        def read(self, n=-1):
            return b"y" * (n if n >= 0 else 10 ** 6)
    with pytest.raises(WebDavError, match="too large"):
        WebDavClient._read(_R(), 100, "GET x")


def test_propfind_response_size_capped(server):
    h, url = server
    c = WebDavClient(url)
    h.raw_propfind["/dav"] = b"<d:multistatus xmlns:d='DAV:'>" + b" " * 5000 + b"</d:multistatus>"
    with pytest.raises(WebDavError, match="too large"):
        c.propfind("", 1, max_bytes=1000)


# ---------------- 恶意 multistatus ----------------
@pytest.mark.parametrize("body", [
    b"not xml at all",
    b"<d:multistatus xmlns:d='DAV:'><d:response>",              # 截断
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/&b;</d:href></d:response></d:multistatus>',
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><a>&e;</a>',
    b'<?xml version="1.0"?><!doctype x><d:multistatus xmlns:d="DAV:"/>',
])
def test_malicious_multistatus_rejected(server, body):
    h, url = server
    h.raw_propfind["/dav"] = body
    with pytest.raises(WebDavError):
        WebDavClient(url).propfind("", 1)


def test_multistatus_with_odd_hrefs_is_filtered(server):
    h, url = server
    c = WebDavClient(url)
    c.put("ok.txt", b"1")
    h.extra_entries["/dav"] = [("/dav/..", True), ("/dav/../etc/passwd", False), ("/etc/shadow", False),
                               ("/dav/sub/deeper.txt", False), ("/dav/.", True), ("", False)]
    assert [e.name for e in c.propfind("", 1)] == ["ok.txt"]


# ---------------- list_tree 安全 ----------------
def test_list_tree_skips_unsafe_and_hidden_names(server):
    h, url = server
    c = WebDavClient(url)
    _tree(h, c, ["good.epub", "小说/三体.epub", ".hidden.txt", "~$lock.docx", ".git/config",
                 ".part.epub"])
    h.dirs.add("/dav/library/~$tmpdir")
    h.files["/dav/library/~$tmpdir/inside.txt"] = b"1"
    h.extra_entries["/dav/library"] = [
        ("/dav/library/..", True), ("/dav/library/.", True),
        ("/dav/library/a%5Cb.epub", False),                      # 反斜杠
        ("/dav/library/..%5C..%5Cevil.epub", False),
        ("/dav/library/CON.txt", False),                         # Windows 保留名
        ("/dav/library/x%3Astream.epub", False),                 # 冒号
        ("/dav/library/trailing.", False),
        ("/dav/library/ctl%01.txt", False),
    ]
    res = c.list_tree_ex("library")
    assert _names(res) == ["good.epub", "小说/三体.epub"]
    assert res.complete and not res.truncated and not res.errors
    assert res.skipped >= 8
    # 被过滤的目录不应被访问
    paths = [p for m, p in h.log if m == "PROPFIND"]
    assert "/dav/library/.git" not in paths and "/dav/library/~$tmpdir" not in paths
    # 旧接口同样过滤
    assert sorted(r for r, _ in c.list_tree("library")) == ["good.epub", "小说/三体.epub"]


def test_list_tree_dedups_duplicate_entries(server):
    h, url = server
    c = WebDavClient(url)
    _tree(h, c, ["sub/a.epub"])
    h.extra_entries["/dav/library"] = [("/dav/library/sub/", True), ("/dav/library/sub/", True),
                                       ("/dav/library/sub/", True)]
    res = c.list_tree_ex("library")
    assert _names(res) == ["sub/a.epub"]
    assert [p for m, p in h.log if m == "PROPFIND"].count("/dav/library/sub") == 1   # 已访问集合


def test_list_tree_terminates_on_symlink_loop(server):
    h, url = server
    c = WebDavClient(url)
    _tree(h, c, ["a.epub"])
    h.links["/dav/library/loop"] = "/dav/library"                # library/loop/loop/loop/...
    t0 = time.time()
    res = c.list_tree_ex("library")
    assert time.time() - t0 < 20
    assert not res.complete and res.truncated                    # 深度上限处停下并标记不完整
    assert "a.epub" in _names(res)
    depth = max(p.count("/loop") for m, p in h.log if m == "PROPFIND")
    assert depth <= webdav.MAX_DEPTH


def test_list_tree_depth_limit(server):
    h, url = server
    c = WebDavClient(url)
    path = "library"
    c.mkdirs(path)
    for i in range(6):
        path += f"/d{i}"
        c.mkdirs(path)
        c.put(path + "/f.txt", b"1")
    full = c.list_tree_ex("library", max_depth=10)
    assert full.complete and len(full.files) == 6
    cut = c.list_tree_ex("library", max_depth=3)
    assert not cut.complete and cut.truncated
    assert _names(cut) == ["d0/d1/d2/f.txt", "d0/d1/f.txt", "d0/f.txt"]


def test_list_tree_dir_limit(server):
    h, url = server
    c = WebDavClient(url)
    c.mkdirs("library")
    for i in range(10):
        c.mkdirs(f"library/s{i}")
        c.put(f"library/s{i}/f.txt", b"1")
    res = c.list_tree_ex("library", max_dirs=4)
    assert not res.complete and res.truncated
    assert len([p for m, p in h.log if m == "PROPFIND" and p.startswith("/dav/library")]) <= 4 + 1  # +exists 无
    assert c.list_tree_ex("library", max_dirs=50).complete


def test_list_tree_file_limit(server):
    h, url = server
    c = WebDavClient(url)
    c.mkdirs("library")
    for i in range(12):
        c.put(f"library/f{i:02d}.txt", b"1")
    res = c.list_tree_ex("library", max_files=5)
    assert len(res.files) == 5 and res.truncated and not res.complete
    exact = c.list_tree_ex("library", max_files=12)              # 恰好等于上限：没有被截断
    assert len(exact.files) == 12 and exact.complete


def test_list_tree_subtree_failure_marks_incomplete_but_root_failure_raises(server):
    h, url = server
    c = WebDavClient(url)
    _tree(h, c, ["a.epub", "bad/x.epub", "ok/y.epub"])
    h.fail_propfind.add("/dav/library/bad")
    res = c.list_tree_ex("library")
    assert not res.complete and res.errors == ["bad"] and not res.truncated
    assert _names(res) == ["a.epub", "ok/y.epub"]
    assert [r for r, _ in c.list_tree("library")] == [r for r, _ in res.files]   # 旧接口行为不变：跳过失败子树
    with pytest.raises(WebDavError):
        c.list_tree_ex("nonexistent")
    h.fail_propfind.add("/dav/library")
    with pytest.raises(WebDavError):
        c.list_tree_ex("library")


def test_client_url_rejects_dot_segments(server):
    h, url = server
    c = WebDavClient(url)
    for bad in ("../x", "a/../b", "./a"):
        with pytest.raises(WebDavError):
            c.get(bad)
    assert not [1 for m, p in h.log if m == "GET"]


# ---------------- mkdirs 缓存 ----------------
def test_mkdirs_cache_avoids_repeat_requests(server):
    h, url = server
    c = WebDavClient(url)
    c.mkdirs("users/alice/progress")
    n = len(h.log)
    assert n == 3
    c.mkdirs("users/alice/progress")
    c.mkdirs("users/alice")
    assert len(h.log) == n                                       # 全部命中缓存，零请求
    c.mkdirs("users/alice/shelf")
    assert len(h.log) == n + 1                                   # 只补缺的那一层
    # 其它客户端实例各有各的缓存
    WebDavClient(url).mkdirs("users/alice/progress")
    assert len(h.log) == n + 1 + 3


def test_mkdirs_cache_also_remembers_existing_dirs_and_heals(server):
    h, url = server
    h.dirs.add("/dav/pre")
    c = WebDavClient(url)
    c.mkdirs("pre")                                              # 405 也记入缓存
    n = len(h.log)
    c.mkdirs("pre")
    assert len(h.log) == n
    # 目录被别人删掉后，PUT 失败会清缓存，下次 mkdirs 重新创建
    h.dirs.discard("/dav/pre")
    with pytest.raises(WebDavError) as ei:
        c.put("pre/f.txt", b"1")
    assert ei.value.status == 409
    c.mkdirs("pre")
    c.put("pre/f.txt", b"1")
    assert h.files["/dav/pre/f.txt"] == b"1"


def test_failed_mkcol_is_not_cached(server):
    h, url = server
    c = WebDavClient(url)
    with pytest.raises(WebDavError):
        c.mkcol("a/b")                                           # 父目录不存在 -> 409
    c.mkdirs("a/b")
    assert "/dav/a/b" in h.dirs


def test_mkcol_403_on_existing_dir_is_treated_as_exists(server):
    """SFTPGo 对虚拟目录挂载点（如 /library）的 MKCOL 回 403：目录确实在 -> 视为已存在，上传才能继续。"""
    h, url = server
    h.dirs.add("/dav/library")
    h.mkcol_existing_status = 403
    c = WebDavClient(url)
    assert c.mkcol("library") is False
    c.mkdirs("library")                                          # 已记入缓存，不再发请求
    n = len(h.log)
    c.mkdirs("library")
    assert len(h.log) == n


def test_mkcol_403_on_missing_dir_still_raises(server):
    """403 且目录并不存在：是真的权限问题，必须报错，不能当成已存在。"""
    h, url = server
    h.mkcol_forbidden.add("/dav/nope")
    c = WebDavClient(url)
    with pytest.raises(WebDavError) as ei:
        c.mkcol("nope")
    assert ei.value.status == 403
    with pytest.raises(WebDavError):
        c.mkdirs("nope")                                         # 失败不进缓存，下次仍会再试


# ---------------- 重定向不泄露凭据 ----------------
def _two_servers(auth="bob:secret"):
    s1, h1, u1 = fakedav.start(auth)
    s2, h2, u2 = fakedav.start()                                 # 另一个端口 = 另一个源
    h1.dirs.add("/dav")
    h2.dirs.add("/dav")
    return (s1, s2), h1, u1, h2, u2


def test_cross_host_redirect_strips_credentials():
    (s1, s2), h1, u1, h2, u2 = _two_servers()
    try:
        h2.files["/dav/stolen.txt"] = b"from-other-host"
        h2.etags["/dav/stolen.txt"] = '"e"'
        h1.redirects["/dav/f.txt"] = u2 + "/stolen.txt"
        c = WebDavClient(u1, "bob", "secret")
        assert c.get("f.txt") == b"from-other-host"              # 重定向本身照常跟随
        assert h1.seen_auth == ["Basic Ym9iOnNlY3JldA=="]         # 第一跳带了凭据
        assert h2.seen_auth == [None]                            # 第二跳（另一个主机/端口）没带
    finally:
        s1.shutdown(); s2.shutdown()


def test_same_origin_redirect_keeps_credentials():
    (s1, s2), h1, u1, h2, u2 = _two_servers()
    try:
        h1.files["/dav/real.txt"] = b"same-host"
        h1.etags["/dav/real.txt"] = '"e"'
        h1.redirects["/dav/f.txt"] = u1 + "/real.txt"
        assert WebDavClient(u1, "bob", "secret").get("f.txt") == b"same-host"
        assert h1.seen_auth == ["Basic Ym9iOnNlY3JldA=="] * 2
    finally:
        s1.shutdown(); s2.shutdown()


def test_redirect_handler_strips_other_credential_headers():
    import urllib.request
    h = config._SafeRedirectHandler()
    req = urllib.request.Request("http://a.example/x", headers={"Authorization": "Bearer t", "api-key": "k",
                                                                "X-Other": "keep"})
    new = h.redirect_request(req, None, 302, "Found", {}, "http://b.example/y")
    assert {k.lower() for k in new.headers} == {"x-other"}
    same = h.redirect_request(req, None, 302, "Found", {}, "http://a.example/y")
    assert {k.lower() for k in same.headers} == {"authorization", "api-key", "x-other"}
    other_port = h.redirect_request(req, None, 302, "Found", {}, "http://a.example:8080/y")
    assert "authorization" not in {k.lower() for k in other_port.headers}
    downgrade = h.redirect_request(urllib.request.Request("https://a.example/x", headers={"Authorization": "x"}),
                                   None, 302, "Found", {}, "http://a.example/x")
    assert "authorization" not in {k.lower() for k in downgrade.headers}


# ---------------- http 公网警告 ----------------
@pytest.mark.parametrize("host,private", [
    ("localhost", True), ("127.0.0.1", True), ("[::1]", True), ("::1", True), ("10.1.2.3", True),
    ("192.168.1.5", True), ("172.16.0.9", True), ("169.254.1.1", True), ("100.64.0.1", True),
    ("fd00::1", True), ("nas", True), ("nas.local", True), ("printer.lan", True),
    ("8.8.8.8", False), ("172.32.0.1", False), ("example.com", False), ("dav.example.org", False),
    ("2606:4700::1111", False), ("", False),
])
def test_host_is_private(host, private):
    assert host_is_private(host) is private


def test_url_risks():
    assert url_risks("http://example.com/dav", "u", "p") == [RISK_PLAIN_HTTP_PUBLIC]
    assert url_risks("http://example.com/dav") == [RISK_PLAIN_HTTP_PUBLIC, RISK_ANONYMOUS_PUBLIC]
    assert url_risks("https://example.com/dav") == [RISK_ANONYMOUS_PUBLIC]
    assert url_risks("https://example.com/dav", "u", "") == []
    assert url_risks("http://192.168.1.2:8080/dav") == []        # 私有地址：不警告
    assert url_risks("http://localhost:8080") == []
    assert url_risks("http://8.8.8.8/x", "", "pw") == [RISK_PLAIN_HTTP_PUBLIC]
    assert url_risks("ftp://example.com") == [] and url_risks("") == [] and url_risks("not a url") == []
    assert set(webdav.RISK_TEXT) == {RISK_PLAIN_HTTP_PUBLIC, RISK_ANONYMOUS_PUBLIC}


def test_client_refuses_credentials_over_plain_http_public(monkeypatch):
    calls = []

    def fake_open(req, timeout=0):
        calls.append(req.full_url)
        raise OSError("no network in this test")
    monkeypatch.setattr(webdav, "net_urlopen", fake_open)
    c = WebDavClient("http://example.com/dav", "bob", "pw", allow_insecure_auth=False)
    with pytest.raises(WebDavError, match="refusing"):
        c.get("x")
    with pytest.raises(WebDavError, match="refusing"):
        c.put("x", b"1")
    assert calls == []                                           # 一个请求都没发出去
    # 没有凭据时没有东西可泄露，不拦；https / 私有地址也不拦
    for u, user, pw in (("http://example.com/dav", "", ""), ("https://example.com/dav", "bob", "pw"),
                        ("http://192.168.1.2/dav", "bob", "pw")):
        with pytest.raises(WebDavError, match="no network"):
            WebDavClient(u, user, pw, allow_insecure_auth=False).get("x")
    assert len(calls) == 3


def test_default_client_refuses_plain_http_public_but_allows_private(auth_server, monkeypatch):
    """默认 allow_insecure_auth=False：公网 http + 凭据被拒；本机/内网 http 不受影响。"""
    h, url = auth_server
    assert WebDavClient(url, "bob", "secret").exists("")
    with pytest.raises(WebDavError, match="refusing"):
        WebDavClient("http://example.com/dav", "bob", "pw").get("x")


def test_client_from_cfg_insecure_opt_in():
    cfg = {"webdav_enabled": True, "webdav_url": "http://example.com/dav", "webdav_user": "b", "webdav_password": "p"}
    assert webdav.client_from_cfg(cfg).allow_insecure_auth is False
    assert webdav.client_from_cfg({**cfg, "webdav_allow_insecure": True}).allow_insecure_auth is True


def test_client_normalizes_non_ascii_base_url(server):
    """base_url 里的中文（如 alist 挂载路径 /dav/夸克）要能被百分比编码：urllib 不自动编码，否则抛 UnicodeEncodeError。"""
    h, url = server
    h.dirs.update({"/dav", "/dav/夸克", "/dav/夸克/library"})
    h.files["/dav/夸克/library/三体.epub"] = b"x"
    c = WebDavClient(url + "/夸克")
    assert c.base == (url + "/%E5%A4%B8%E5%85%8B").rstrip("/")
    assert [e.name for e in c.propfind("library", 1)] == ["三体.epub"]
