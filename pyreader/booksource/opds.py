"""OPDS 1.x（Atom）书源：导航条目、acquisition 链接、rel=next 分页、OpenSearch 搜索模板。

安全：只接受 https（allow_insecure 打开才放行 http）；请求走 config.net_urlopen（Reader 自己的代理策略、
跨主机重定向剥凭据）；凭据只发给与书源地址同源的 URL；响应体有大小上限；XML 用 lxml 解析且
禁用实体 / DTD / 网络访问，带 DOCTYPE / ENTITY 的文档直接拒绝。"""
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from lxml import etree

from ..config import net_urlopen, _origin, _log_net
from ..store import safe_segment, is_hidden_name
from ..webdav import WebDavClient
from .base import (Acquisition, BookEntry, BookPage, BookSource, BookSourceError,
                   acquisition_ext, pick_acquisition)

ATOM = "{http://www.w3.org/2005/Atom}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"
USER_AGENT = "PyReader-OPDS/0.1 (+https://github.com/garyliu1971/Reader)"
ACCEPT = "application/atom+xml;profile=opds-catalog, application/atom+xml, application/xml;q=0.8, */*;q=0.1"
FEED_MAX_BYTES = 8 * 1024 * 1024         # 单个 feed / OpenSearch 描述的响应体上限
MAX_ENTRIES = 1000                       # 单页条目数上限（防恶意超大 feed 撑爆列表）
SUMMARY_MAX = 300
_NAV_REL_PREFIXES = ("http://opds-spec.org/sort/", "http://opds-spec.org/facet")


def _parser():
    # 不解析实体、不读 DTD、不联网、不开 huge_tree
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                           huge_tree=False, remove_comments=True, remove_pis=True)


def parse_xml(body: bytes):
    low = body[:65536].lower()               # DOCTYPE 必须出现在根元素之前，开头一段就够判断
    if b"<!doctype" in low or b"<!entity" in low:
        raise BookSourceError("拒绝不安全的 XML（含 DOCTYPE / ENTITY）")
    try:
        return etree.fromstring(body, _parser())
    except etree.XMLSyntaxError as e:
        raise BookSourceError(f"目录格式错误：{e}") from e


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _is_atom(mime: str) -> bool:
    return "atom+xml" in (mime or "").lower()


def fill_template(tpl: str, query: str) -> str:
    """OpenSearch URL 模板：{searchTerms} -> 查询词（URL 编码）；startIndex / startPage 取 1，其它参数留空。"""
    def sub(m):
        name = m.group(1).strip().rstrip("?").split(":")[-1]
        if name == "searchTerms":
            return urllib.parse.quote_plus(query)
        return "1" if name in ("startIndex", "startPage") else ""
    return re.sub(r"\{([^{}]*)\}", sub, tpl)


class OpdsSource(BookSource):
    def __init__(self, name: str, url: str, user: str = "", password: str = "",
                 allow_insecure: bool = False, timeout: float = 20):
        self.name = name
        self.url = (url or "").strip()
        self.user = user or ""
        self.password = password or ""
        self.allow_insecure = allow_insecure
        self.timeout = timeout
        self._search = None                  # (href, mime)：第一次见到的 rel=search 链接

    # ---- 网络 ----
    def _scheme_ok(self, url: str) -> bool:
        sch = urllib.parse.urlsplit(url).scheme.lower()
        return sch == "https" or (sch == "http" and self.allow_insecure)

    def fetch_headers(self, url: str) -> dict:
        h = {"User-Agent": USER_AGENT}
        if (self.user or self.password) and _origin(url) == _origin(self.url):
            h.update(WebDavClient("", self.user, self.password, allow_insecure_auth=True)._headers())
        return h

    def _get(self, url: str):
        """GET 一个文档，返回 (body, 最终 URL)。拒绝非 https（含重定向落点）、超大响应。"""
        if not self._scheme_ok(url):
            raise BookSourceError(f"拒绝非 https 地址：{url}（书源设置里可勾选允许 http）")
        headers = self.fetch_headers(url)
        headers["Accept"] = ACCEPT
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with net_urlopen(req, timeout=self.timeout) as r:
                final = r.geturl() or url
                if not self._scheme_ok(final):
                    raise BookSourceError(f"拒绝重定向到非 https 地址：{final}")
                cl = (r.headers.get("Content-Length") or "").strip() if r.headers else ""
                if cl.isdigit() and int(cl) > FEED_MAX_BYTES:
                    raise BookSourceError("目录响应过大")
                body = r.read(FEED_MAX_BYTES + 1)
                if len(body) > FEED_MAX_BYTES:
                    raise BookSourceError("目录响应过大")
                return body, final
        except urllib.error.HTTPError as e:
            raise BookSourceError(f"书源返回 HTTP {e.code}：{url}") from e
        except BookSourceError:
            raise
        except Exception as e:
            _log_net(f"[书源] 请求失败 {url}: {e}")
            raise BookSourceError(f"连接书源失败：{e}") from e

    # ---- 解析 ----
    @staticmethod
    def _abs(base: str, href: Optional[str]) -> str:
        """相对链接按所在文档的最终 URL 解析；非 http(s)（如 data: 封面）返回 ''。"""
        if not href or not href.strip():
            return ""
        u = urllib.parse.urljoin(base, href.strip())
        return u if urllib.parse.urlsplit(u).scheme.lower() in ("http", "https") else ""

    def _parse_entry(self, e, base: str) -> Optional[BookEntry]:
        title = _text(e.find(ATOM + "title")) or "（无标题）"
        author = ", ".join(n for n in (_text(a.find(ATOM + "name")) for a in e.findall(ATOM + "author")) if n)
        summ = _text(e.find(ATOM + "summary")) or _text(e.find(ATOM + "content"))
        ent = BookEntry(title=title, author=author, summary=summ[:SUMMARY_MAX])
        has_acq = False
        for ln in e.findall(ATOM + "link"):
            rel, mime = (ln.get("rel") or "").strip(), (ln.get("type") or "").strip()
            href = self._abs(base, ln.get("href"))
            if not href:
                continue
            if rel.startswith("http://opds-spec.org/acquisition"):
                has_acq = True
                ext = acquisition_ext(mime)
                if ext:
                    try:
                        size = max(0, int(ln.get("length") or 0))
                    except ValueError:
                        size = 0
                    ent.acquisitions.append(Acquisition(href, mime.split(";")[0].strip().lower(), ext, size,
                                                        (ln.get("title") or "").strip()))
            elif _is_atom(mime) and not ent.href and (rel in ("", "subsection") or rel.startswith(_NAV_REL_PREFIXES)):
                ent.href = href
        if has_acq:
            ent.kind = "book"
        elif not ent.href:
            return None                      # 既不能下载也不能进入：没用的条目
        return ent

    def _parse_feed(self, body: bytes, final_url: str) -> BookPage:
        root = parse_xml(body)
        if root.tag != ATOM + "feed":
            raise BookSourceError("不是 OPDS / Atom 目录（根元素不是 feed）")
        page = BookPage(title=_text(root.find(ATOM + "title")), url=final_url)
        for ln in root.findall(ATOM + "link"):
            rel, mime = (ln.get("rel") or "").strip(), (ln.get("type") or "").strip()
            href = self._abs(final_url, ln.get("href"))
            if not href:
                continue
            if rel == "next" and not page.next_href:
                page.next_href = href
            elif rel == "search" and self._search is None:
                self._search = (href, mime)
        for e in root.findall(ATOM + "entry"):
            ent = self._parse_entry(e, final_url)
            if ent is not None:
                page.entries.append(ent)
            if len(page.entries) >= MAX_ENTRIES:
                break
        return page

    # ---- BookSource ----
    def browse(self, href: str = "") -> BookPage:
        url = urllib.parse.urljoin(self.url, href) if href else self.url
        body, final = self._get(url)
        return self._parse_feed(body, final)

    def _search_template(self) -> str:
        if self._search is None:
            self.browse()                    # 根目录里找 rel=search
        if self._search is None:
            raise BookSourceError("该书源不支持搜索")
        href, mime = self._search
        if "opensearchdescription" not in mime.lower():
            return href                      # OPDS 1.0 老写法：链接本身就是模板
        body, final = self._get(href)
        root = parse_xml(body)
        urls = [u for u in root.iter(OS + "Url") if _is_atom(u.get("type") or "") and u.get("template")]
        if not urls:
            raise BookSourceError("OpenSearch 描述里没有 Atom 搜索模板")
        urls.sort(key=lambda u: "opds" not in u.get("type", "").lower())
        tpl = urllib.parse.urljoin(final, urls[0].get("template").strip())
        if urllib.parse.urlsplit(tpl).scheme.lower() == "http" and not self.allow_insecure:
            # Gutenberg 的描述文件里模板写的是 http://（服务器会 301 回 https）：先升级成 https 再请求，
            # 不让明文请求出门；升级后仍不通就如实报错。
            tpl = "https" + tpl[4:]
        return tpl

    def search(self, query: str) -> BookPage:
        q = (query or "").strip()
        if not q:
            raise BookSourceError("请输入搜索词")
        body, final = self._get(fill_template(self._search_template(), q))
        return self._parse_feed(body, final)

    def acquire(self, entry: BookEntry) -> Acquisition:
        best = pick_acquisition(entry.acquisitions)
        if best is None and entry.href:      # Gutenberg 搜索结果只有详情页链接，下载链接在详情 feed 里
            page = self.browse(entry.href)
            best = pick_acquisition([a for e in page.entries for a in e.acquisitions])
        if best is None:
            raise BookSourceError(f"《{entry.title}》没有可读格式（支持 epub / mobi / pdf）")
        return best


def opds_source_from_cfg(src: dict) -> Optional[OpdsSource]:
    if not isinstance(src, dict) or not (src.get("url") or "").strip():
        return None
    return OpdsSource(src.get("name", ""), src["url"], src.get("user", ""), src.get("password", ""),
                      allow_insecure=bool(src.get("allow_insecure", False)))


def enabled_sources(cfg: dict) -> list:
    """cfg["book_sources"] 里启用且合法的条目（名字要能当目录名，坏条目一律跳过）。"""
    out = []
    for s in cfg.get("book_sources") or []:
        if not isinstance(s, dict) or not s.get("enabled", True):
            continue
        name = safe_segment((s.get("name") or "").strip())
        if not name or is_hidden_name(name) or not (s.get("url") or "").strip():
            continue
        out.append({**s, "name": name})
    return out
