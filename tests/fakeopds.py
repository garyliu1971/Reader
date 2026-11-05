"""进程内最小 OPDS 1.x 桩（测试专用，永远不连真实服务器，只监听 127.0.0.1）。

start() -> (server, handler_class, base_url)；base_url 形如 http://127.0.0.1:PORT（无尾斜杠）。
handler_class 上的属性：
  routes  {path_with_query: (status, content_type, body_bytes, extra_headers)}  找不到回 404；
          路径含查询串时先按「带查询串」匹配，再按纯路径匹配
  log     [(path_with_query, Authorization 头或 None)]
默认路由模拟 Project Gutenberg 的形态：根目录（导航 + rel=search 指向 OpenSearch 描述）、分页的分类、
搜索结果只有详情页链接、详情 feed 里才有 acquisition；全部用相对链接。
"""
import http.server
import threading
ATOM_CT = "application/atom+xml;profile=opds-catalog;charset=utf-8"
NS = ('xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog" '
      'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"')
EPUB_BIG = b"PK-big-" + b"x" * 2000
EPUB_SMALL = b"PK-small-" + b"y" * 300
MOBI = b"MOBI-" + b"z" * 100
PDF = b"%PDF-1.4 " + b"p" * 50


def feed(title, entries="", links=""):
    return (f'<?xml version="1.0" encoding="utf-8"?><feed {NS}><title>{title}</title>{links}{entries}</feed>').encode()


def nav_entry(title, href, content=""):
    c = f'<content type="text">{content}</content>' if content else ""
    return (f'<entry><title>{title}</title>{c}'
            f'<link type="application/atom+xml;profile=opds-catalog" rel="subsection" href="{href}"/></entry>')


def acq_link(href, mime, length=None, title="", rel="http://opds-spec.org/acquisition"):
    ln = f' length="{length}"' if length is not None else ""
    return f'<link type="{mime}" rel="{rel}"{ln} title="{title}" href="{href}"/>'


def book_entry(title, author, links):
    return f'<entry><title>{title}</title><author><name>{author}</name></author>{links}</entry>'


def default_routes(base):
    r = {}
    r["/opds/"] = (200, ATOM_CT, feed(
        "Root",
        nav_entry("Fiction", "fiction/") + nav_entry("Absolute", f"{base}/opds/fiction/") +
        '<entry><title>Cover only</title><link rel="http://opds-spec.org/image" type="image/png" href="data:image/png;base64,AAAA"/></entry>',
        '<link rel="search" type="application/opensearchdescription+xml" href="osd.xml"/>'
        '<link rel="start" type="application/atom+xml;profile=opds-catalog" href="/opds/"/>'), {})
    p1 = (book_entry("Emma", "Austen, Jane", acq_link("../../books/emma.epub", "application/epub+zip", len(EPUB_SMALL))) +
          book_entry("Mixed", "Someone",
                     acq_link("../../books/mixed.pdf", "application/pdf", len(PDF)) +
                     acq_link("../../books/mixed.mobi", "application/x-mobipocket-ebook", len(MOBI)) +
                     acq_link("../../books/mixed-big.epub", "application/epub+zip", len(EPUB_BIG), "with images") +
                     acq_link("../../books/mixed.epub", "application/epub+zip", len(EPUB_SMALL), "no images")) +
          book_entry("Audio only", "Narrator", acq_link("../../books/a.mp3", "audio/mpeg", 10)))
    r["/opds/fiction/"] = (200, ATOM_CT, feed(
        "Fiction", p1, '<link rel="next" type="application/atom+xml;profile=opds-catalog" href="?page=2"/>'), {})
    r["/opds/fiction/?page=2"] = (200, ATOM_CT, feed(
        "Fiction 2", book_entry("Persuasion", "Austen, Jane", acq_link("../../books/persuasion.epub", "application/epub+zip", len(EPUB_SMALL)))), {})
    r["/opds/osd.xml"] = (200, "application/xml", (
        '<?xml version="1.0"?><OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
        '<Url type="text/html" template="%s/html?q={searchTerms}"/>'
        '<Url type="application/atom+xml" template="/opds/search?q={searchTerms}&amp;start={startIndex?}"/>'
        '</OpenSearchDescription>' % base).encode(), {})
    r["/opds/search?q=pride+and+prejudice&start=1"] = (200, ATOM_CT, feed(
        "Search: pride and prejudice",
        nav_entry("Pride and Prejudice", "/opds/detail/1342.opds", "Jane Austen")), {})
    r["/opds/search?q=nothing+here&start=1"] = (200, ATOM_CT, feed("Search: none"), {})
    r["/opds/detail/1342.opds"] = (200, ATOM_CT, feed(
        "Pride and Prejudice by Jane Austen",
        book_entry("Pride and Prejudice", "Austen, Jane",
                   acq_link("/books/pp-images.epub", "application/epub+zip", len(EPUB_BIG), "EPUB3 images") +
                   acq_link("/books/pp-noimages.epub", "application/epub+zip", len(EPUB_SMALL), "EPUB no images") +
                   acq_link("/books/pp.kindle", "application/x-mobipocket-ebook", len(MOBI)))), {})
    r["/opds/xxe/"] = (200, ATOM_CT, (
        b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&xxe;</title></feed>'), {})
    r["/opds/notfeed/"] = (200, "application/xml", b"<html><body>hi</body></html>", {})
    r["/opds/huge/"] = (200, ATOM_CT, b"<feed xmlns='http://www.w3.org/2005/Atom'>" + b"x" * (9 * 1024 * 1024), {})
    for name, data in (("emma.epub", EPUB_SMALL), ("mixed.epub", EPUB_SMALL), ("mixed-big.epub", EPUB_BIG),
                       ("mixed.mobi", MOBI), ("mixed.pdf", PDF), ("persuasion.epub", EPUB_SMALL),
                       ("pp-images.epub", EPUB_BIG), ("pp-noimages.epub", EPUB_SMALL), ("pp.kindle", MOBI)):
        r["/books/" + name] = (200, "application/octet-stream", data, {})
    return r


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    routes = None
    log = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        h = type(self)
        h.log.append((self.path, self.headers.get("Authorization")))
        key = self.path
        hit = h.routes.get(key) or h.routes.get(key.split("?")[0])
        if isinstance(hit, list):                # 依次返回各个应答，最后一个重复使用（造「第一次失败、重试成功」）
            hit = hit.pop(0) if len(hit) > 1 else hit[0]
        if hit is None:
            body, code, ct, extra = b"", 404, "text/plain", {}
        else:
            code, ct, body, extra = hit
        self.send_response(code)
        self.send_header("Content-Type", ct)
        for k, v in extra.items():
            self.send_header(k, v)
        if "Content-Length" not in extra:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if "X-Truncate" in extra:            # 声明的 Content-Length 比实际发的长：模拟连接中途被切断
                self.wfile.write(body[:int(extra["X-Truncate"])])
                self.close_connection = True
            else:
                self.wfile.write(body)
        except OSError:
            pass


def start():
    h = type("H", (Handler,), {"routes": {}, "log": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    h.routes.update(default_routes(base))
    return srv, h, base
