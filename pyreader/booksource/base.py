"""书源接口与数据结构（与具体协议无关，OPDS 1 / OPDS 2 / 其它目录都实现 BookSource）。

服务器返回的一切都视为不可信：协议实现负责把链接解析成绝对 URL 并过滤掉非 http(s)；
扩展名只从 mime 推（URL 常常没有扩展名，如 Gutenberg 的 /ebooks/1342.epub.noimages），不信任服务器给的文件名。"""
from dataclasses import dataclass, field
from typing import List, Optional

from ..shelf import READABLE_EXTS

# 下载格式偏好：epub 优先，其次 mobi / pdf；mime -> 扩展名。扩展名必须在 READABLE_EXTS 内（阅读器能打开）。
MIME_EXT = {
    "application/epub+zip": ".epub",
    "application/x-mobipocket-ebook": ".mobi",
    "application/vnd.amazon.ebook": ".azw",
    "application/pdf": ".pdf",
}
FORMAT_PREFERENCE = (".epub", ".mobi", ".pdf")


class BookSourceError(Exception):
    """书源层错误（网络 / 解析 / 拒绝不安全的地址）。消息可直接展示给用户。"""


@dataclass
class Acquisition:
    href: str                        # 绝对 URL
    mime: str
    ext: str                         # 小写带点，保证在 READABLE_EXTS 内
    size: int = 0                    # OPDS length 属性；0 = 未知
    title: str = ""


@dataclass
class BookEntry:
    title: str
    kind: str = "nav"                # "book"：自带可下载链接 | "nav"：导航条目（也可能是 Gutenberg 搜索结果这类「详情页」）
    href: str = ""                   # nav 条目指向的 feed（绝对 URL）
    author: str = ""
    summary: str = ""
    acquisitions: List[Acquisition] = field(default_factory=list)

    def formats(self) -> List[str]:
        """可下载的扩展名（去重，按偏好排序），如 ['.epub', '.mobi']。"""
        got = {a.ext for a in self.acquisitions}
        return [e for e in FORMAT_PREFERENCE if e in got]


@dataclass
class BookPage:
    title: str = ""
    entries: List[BookEntry] = field(default_factory=list)
    next_href: str = ""              # 分页：下一页 feed 的绝对 URL，没有则为空
    url: str = ""                    # 本页的最终 URL


def acquisition_ext(mime: str) -> str:
    """mime（忽略参数）-> 阅读器可读的扩展名；不认识 / 不可读返回 ''。"""
    ext = MIME_EXT.get((mime or "").split(";")[0].strip().lower(), "")
    return ext if ext in READABLE_EXTS else ""


def pick_acquisition(acqs: List[Acquisition]) -> Optional[Acquisition]:
    """epub > mobi > pdf；同一格式里挑已知大小最小的（Gutenberg 的 noimages 版只有几百 KB，对学英语的人最合适），
    大小未知的排在已知之后。没有可读格式返回 None。"""
    best = None
    for pref, ext in enumerate(FORMAT_PREFERENCE):
        cands = [a for a in acqs if a.ext == ext]
        if cands:
            cands.sort(key=lambda a: (a.size <= 0, a.size))
            best = cands[0]
            break
    return best


class BookSource:
    """书源接口。方法都是阻塞调用，只能在后台线程用。"""
    name = ""

    def browse(self, href: str = "") -> BookPage:
        """href 为空 = 根目录；否则是某个条目 / 分页给出的绝对 URL。"""
        raise NotImplementedError

    def search(self, query: str) -> BookPage:
        raise NotImplementedError

    def acquire(self, entry: BookEntry) -> Acquisition:
        """为条目挑一个可下载格式（必要时先取详情页）；没有可读格式抛 BookSourceError。"""
        raise NotImplementedError

    def fetch_headers(self, url: str) -> dict:
        """下载 url 时该带的请求头（凭据只给与书源同源的 URL）。"""
        return {}


_BAD_NAME_CHARS = '<>:"/\\|?*'


def book_filename(title: str, ext: str) -> str:
    """书名 -> 本地文件名（保留中文 / 空格，替换 Windows 非法字符，去掉首尾点和空格，限长）。ext 带点。"""
    t = "".join("_" if (c in _BAD_NAME_CHARS or ord(c) < 32) else c for c in (title or ""))
    t = " ".join(t.split()).strip(" .")[:80].strip(" .") or "book"
    if t.startswith("~$"):
        t = t[2:] or "book"
    return t + ext
