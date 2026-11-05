"""Project Gutenberg 离线目录书源：不碰 /ebooks/search*（robots.txt 禁止，且 XML OPDS 预计 2027 年退役），
改用官方每日更新的目录文件 pg_catalog.csv（见 https://www.gutenberg.org/ebooks/offline_catalogs.html，
官方建议用它做本地检索而不是爬网站）。首次搜索时下载一次（约 20 MB），缓存到 ~/.pyreader/booksource_cache/，
CATALOG_MAX_AGE 内不再下载；之后在本地按书名 / 作者 / 语言检索，选中后用 epub 直链下载。

搜索语法：空格分隔的词必须全部出现在「书名 + 作者」里（不分大小写）；可加 lang:en 之类的词限定语言（目录里的语言代码）。"""
import csv
import os
import time

from ..config import APP_DIR, _log_net
from ..webdav import WebDavError
from .base import Acquisition, BookEntry, BookPage, BookSource, BookSourceError

CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
# 直链（会 302 到 /cache/epub/<id>/...）：noimages 只有几百 KB（纯文字，学英语够用），images 版常有 20 MB+
EPUB_URLS = ("https://www.gutenberg.org/ebooks/{id}.epub.noimages",
             "https://www.gutenberg.org/ebooks/{id}.epub3.images")
EPUB_MIME = "application/epub+zip"
CATALOG_MAX_AGE = 7 * 24 * 3600
CATALOG_MAX_BYTES = 200 * 1024 * 1024
MAX_RESULTS = 200
CACHE_DIR = os.path.join(APP_DIR, "booksource_cache")
USER_AGENT = "PyReader-OPDS/0.1 (+https://github.com/garyliu1971/Reader)"


class GutenbergCatalogSource(BookSource):
    def __init__(self, name: str = "Project Gutenberg", url: str = CATALOG_URL, allow_insecure: bool = False,
                 cache_dir: str = CACHE_DIR, max_age: float = CATALOG_MAX_AGE, epub_urls=EPUB_URLS,
                 timeout: float = 60):
        self.name = name
        self.url = (url or CATALOG_URL).strip()
        self.allow_insecure = allow_insecure
        self.cache_path = os.path.join(cache_dir, "pg_catalog.csv")
        self.max_age = max_age
        self.epub_urls = tuple(epub_urls)
        self.timeout = timeout
        self._rows = None                    # [(id, title, authors, lang, haystack)]
        self._rows_mtime = None

    def fetch_headers(self, url: str) -> dict:
        return {"User-Agent": USER_AGENT}

    # ---- 目录文件 ----
    def _stale(self) -> bool:
        try:
            return time.time() - os.path.getmtime(self.cache_path) > self.max_age
        except OSError:
            return True

    def ensure_catalog(self) -> str:
        """缓存不存在或过期才下载（.part + os.replace，下载失败时有旧缓存就继续用旧的）。返回缓存路径。"""
        if self._stale():
            from .. import sync                 # 延迟导入：sync 依赖 Qt
            err = None
            for _attempt in range(2):           # 实测第一次偶发被服务器中途切断（truncated response），重试一次即可
                try:
                    sync.download_url(self.url, self.cache_path, max_bytes=CATALOG_MAX_BYTES, timeout=self.timeout,
                                      headers=self.fetch_headers(self.url), allow_insecure=self.allow_insecure)
                    err = None
                    break
                except WebDavError as e:
                    err = e
                    if e.status is not None and e.status < 500:
                        break                   # 4xx / 非瞬时错误不重试
            if err is not None:
                _log_net(f"[书源] 下载 Gutenberg 目录失败: {err}")
                if not os.path.isfile(self.cache_path):
                    raise BookSourceError(f"下载 Gutenberg 目录失败：{err}") from err
        return self.cache_path

    def _load(self):
        path = self.ensure_catalog()
        mt = os.path.getmtime(path)
        if self._rows is not None and self._rows_mtime == mt:
            return self._rows
        rows = []
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if (r.get("Type") or "") != "Text":          # 跳过有声书 / 图片 / 数据集
                        continue
                    pid = (r.get("Text#") or "").strip()
                    if not pid.isdigit():
                        continue
                    title = " ".join((r.get("Title") or "").split())
                    authors = (r.get("Authors") or "").strip()
                    rows.append((pid, title, authors, (r.get("Language") or "").strip().lower(),
                                 (title + " " + authors).lower()))
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            raise BookSourceError(f"Gutenberg 目录文件损坏：{e}（删除 {path} 后重试）") from e
        if not rows:
            raise BookSourceError(f"Gutenberg 目录文件里没有书（删除 {path} 后重试）")
        self._rows, self._rows_mtime = rows, mt
        return rows

    # ---- BookSource ----
    def browse(self, href: str = "") -> BookPage:
        return BookPage(title=f"{self.name}：输入书名或作者搜索（首次搜索会下载约 20 MB 的官方目录，之后 7 天内不再下载）；"
                              "可加 lang:en 限定语言", url=self.url)

    def search(self, query: str) -> BookPage:
        words, lang = [], ""
        for w in (query or "").lower().split():
            if w.startswith("lang:"):
                lang = w[5:]
            else:
                words.append(w)
        if not words and not lang:
            raise BookSourceError("请输入搜索词")
        hits = []
        phrase = " ".join(words)
        for pid, title, authors, lg, hay in self._load():
            if lang and lang not in [x.strip() for x in lg.split(";")]:
                continue
            if all(w in hay for w in words):
                tl = title.lower()
                hits.append((0 if tl == phrase else 1 if tl.startswith(phrase) else 2, int(pid), pid, title, authors))
        hits.sort()                          # 书名完全匹配 > 前缀匹配 > 其它；同级按编号（老书 / 名著编号小）
        page = BookPage(title=f"搜索：{query}（共 {len(hits)} 项" + (f"，显示前 {MAX_RESULTS}" if len(hits) > MAX_RESULTS else "") + "）",
                        url=self.url)
        for _r, _n, pid, title, authors in hits[:MAX_RESULTS]:
            page.entries.append(BookEntry(
                title=title or f"#{pid}", kind="book", author=authors,
                acquisitions=[Acquisition(u.format(id=pid), EPUB_MIME, ".epub", 0, "EPUB") for u in self.epub_urls]))
        return page

    def acquire(self, entry: BookEntry) -> Acquisition:
        if not entry.acquisitions:
            raise BookSourceError(f"《{entry.title}》没有可下载的 epub")
        return entry.acquisitions[0]
