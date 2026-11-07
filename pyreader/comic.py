"""漫画 / 扫描书压缩包（.zip / .cbz）的纯逻辑：列页、读页、分章，以及翻页 / 跨页 / 缩放的纯函数。不依赖 Qt（见 comicview.py）。
逻辑参照 ImageViewer（viewer_core.py / media_source.py），改进点：自然排序按「完整相对路径」逐级比较（ImageViewer 只比文件名），
所以 ch2/ 排在 ch10/ 之前，且同一目录的页挨在一起。

epub 漫画（只有图片、没有文字的 .epub）：按 OPF spine 取页序（epub_page_order，参照 ImageViewer 的 epubParseOpf / epubPageOrder），
is_image_epub 判定一本 epub 是不是这种书（只读 container.xml、OPF 和至多 EPUB_SAMPLE_PAGES 页小 xhtml，不解压图片）。

规则：只认 IMAGE_EXTS；跳过目录条目、__MACOSX、任何以 . 开头的路径段（.DS_Store、._xxx）；
条目只读字节，不落盘，路径穿越（../）无影响。防御：坏 zip / 加密 / 超大条目（zip 炸弹）都转成 ComicError。
"""
import html
import os
import re
import threading
import zipfile
from urllib.parse import unquote
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
COMIC_EXTS = (".zip", ".cbz")
MAX_ENTRY_SIZE = 64 * 1024 * 1024     # 单页解压后上限；声明的大小和实际读到的字节数都要过这一关
MAX_ENTRIES = 20000                   # 压缩包条目数上限
MIN_ZOOM, MAX_ZOOM = 0.02, 40.0
# ---- 图片型 epub 的判定（门槛实测：两本真实漫画 0.993 / 1.000，所有文字书最高 0.059，0.1–0.9 之间结论相同）----
EPUB_SAMPLE_PAGES = 12                # 至多抽查这么多页正文（均匀取）
EPUB_PAGE_TEXT_MAX = 50               # 一页可见文字不超过这个数（且有图）才算「图片页」
EPUB_COMIC_FRAC = 0.80                # 图片页占比达到这个数判为漫画
EPUB_COMIC_FRAC_HINTED = 0.50         # OPF 标了 book-type=comic 等时放宽到这个数（提示只放宽，从不单独触发）
EPUB_COMIC_BOOK_TYPES = frozenset(("comic", "comics", "manga"))
EPUB_EDGE_SKIP_MIN_SPINE = 5          # spine 超过 4 页才跳过首尾（封面包装页 / 版权页）
EPUB_WEBTOON_SPINE_MAX = 2            # spine 少于 3 页：整本在一两个 xhtml 里（长条漫画），改按总量判
EPUB_WEBTOON_MIN_IMGS = 10
EPUB_PAGE_MAX_BYTES = 4 * 1024 * 1024  # 读一页 xhtml / OPF 的字节上限
EPUB_PAGE_ORDER_MIN_COVERAGE = 0.5    # spine 引用到的图少于压缩包里图片的一半：引用关系读不出来，退回自然排序
EPUB_MEMO_MAX = 256
FIT_MODES = ("width", "height", "window")


class ComicError(Exception):
    """压缩包无法作为漫画读取：损坏 / 加密 / 条目过多 / 单页过大 / 已关闭。"""


class Chapter(NamedTuple):
    title: str      # 目录路径（页都在根目录时为 ""）
    start: int      # 起始页序号（0 起）
    count: int


# ================================================================== 排序 / 过滤
def natural_key(s: str) -> list:
    """自然排序：page2 排在 page10 之前。split 的结果恒为 str, int, str, ... 交替，所以两个 key 可直接比较。"""
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"(\d+)", s))]     # 按奇偶位判断：isdigit() 对 ①/² 也为真，int() 会抛 ValueError


def path_key(name: str) -> list:
    """完整相对路径的排序键：按路径段逐级做自然排序。"""
    return [natural_key(p) for p in name.replace("\\", "/").split("/")]


def is_page(name: str) -> bool:
    """压缩包里的条目是否算一页：图片扩展名，且不是目录 / 苹果垃圾 / 点文件。"""
    n = name.replace("\\", "/")
    if not n or n.endswith("/"):
        return False
    parts = n.split("/")
    for p in parts:
        if not p or p == "__MACOSX" or p.startswith("."):
            return False
    return os.path.splitext(parts[-1])[1].lower() in IMAGE_EXTS


def display_name(name: str) -> str:
    """没标 UTF-8 的 zip 条目名会被 zipfile 按 cp437 解码；中文 Windows 打的包实际是 GBK，能还原就还原（只用于显示，排序不用）。"""
    try:
        name.encode("cp437")
    except UnicodeEncodeError:
        return name                   # 已经含非 cp437 字符，说明 zipfile 按 UTF-8 解的
    try:
        return name.encode("cp437").decode("gbk")
    except UnicodeDecodeError:
        return name


def group_chapters(pages: Sequence[str], title: Callable[[str], str] = str) -> List[Chapter]:
    """按所在目录把（已排序的）页切成章；只是数据，是否显示由界面决定。目录不同的相邻页开新章。title 把目录路径转成显示名。"""
    chapters: List[Chapter] = []
    cur, start = None, 0
    for i, name in enumerate(pages):
        d = name.replace("\\", "/").rpartition("/")[0]
        if i == 0 or d != cur:
            if i:
                chapters.append(Chapter(title(cur), start, i - start))
            cur, start = d, i
    if pages:
        chapters.append(Chapter(title(cur), start, len(pages) - start))
    return chapters


# ================================================================== epub 漫画：spine 页序 / 图片型判定
IMAGE_EPUB_MESSAGE = "这本 epub 只有图片（漫画），请用漫画阅读器打开"


class ImageEpubError(Exception):
    """这本 epub 只有图片（漫画），不能当文字书读；调用方应改用漫画阅读器（textio.load_epub 的保险丝）。"""


class _SpineItem(NamedTuple):
    path: str       # zip 内规范路径
    type: str       # manifest 的 media-type（小写）
    linear: bool


class _EpubPackage(NamedTuple):
    names: Dict[str, str]          # 小写条目名 -> 真实条目名
    spine: List[_SpineItem]        # 全部 itemref（含 linear="no"），按 spine 顺序
    rtl: Optional[bool]            # spine 的 page-progression-direction：rtl True / ltr False / 没写 None
    book_type: str                 # <meta name="book-type">，小写；没有为 ""


_ZIP_FAIL = (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError, RuntimeError)
_ITEM_RE = re.compile(r"<item\b[^>]*>", re.I)
_ITEMREF_RE = re.compile(r"<itemref\b[^>]*>", re.I)
_SPINE_RE = re.compile(r"<spine\b[^>]*>", re.I)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ROOTFILE_RE = re.compile(r"<rootfile\b[^>]*>", re.I)
_IMG_RE = re.compile(r"<(img|image)\b[^>]*>", re.I)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_NOT_VISIBLE_RES = (re.compile(r"<!--.*?-->", re.S), re.compile(r"<head\b.*?</head\s*>", re.I | re.S),
                    re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S))
_TAG_RE = re.compile(r"<[^>]*>", re.S)
_WS_RE = re.compile(r"\s+")
_IMAGE_EPUB_MEMO: Dict[tuple, bool] = {}
_MEMO_LOCK = threading.Lock()


def _xml_attr(tag: str, name: str) -> Optional[str]:
    m = re.search(r"(?:^|\s)" + re.escape(name) + r"\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
    return html.unescape(m.group(2)) if m else None


def epub_resolve(base_dir: str, href: str) -> Optional[str]:
    """href（相对 base_dir，可含 ../ 、#片段、?查询、%编码）-> zip 内规范路径；外链（http: / data:）返回 None。"""
    h = unquote((href or "").split("#")[0].split("?")[0])
    if _SCHEME_RE.match(h):
        return None
    parts = [] if h.startswith("/") else [p for p in (base_dir or "").split("/") if p]
    for seg in h.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _read_member(zf: zipfile.ZipFile, names: Dict[str, str], name: str, limit: int = EPUB_PAGE_MAX_BYTES) -> Optional[str]:
    """按名字（大小写不敏感）读一个文本条目，最多 limit 字节；读不到 / 坏 / 加密返回 None。"""
    real = names.get((name or "").lower())
    if real is None:
        return None
    try:
        with zf.open(real) as f:
            data = f.read(limit)
    except Exception:                      # 坏条目 / 加密 / 不支持的压缩：当作没有，由调用方决定怎么办
        return None
    return data.decode("utf-8", "replace").lstrip("﻿")


def _read_package(zf: zipfile.ZipFile) -> Optional[_EpubPackage]:
    """container.xml -> OPF -> spine。不是 epub / 没有 OPF 返回 None。"""
    names = {n.replace("\\", "/").lower(): n for n in zf.namelist()}
    cx = _read_member(zf, names, "META-INF/container.xml")
    m = _ROOTFILE_RE.search(cx or "")
    opf_path = _xml_attr(m.group(0), "full-path") if m else None
    opf = _read_member(zf, names, opf_path) if opf_path else None
    if not opf:
        return None
    base = opf_path.rpartition("/")[0]
    manifest: Dict[str, Tuple[str, str]] = {}
    for it in _ITEM_RE.findall(opf):
        i, href = _xml_attr(it, "id"), _xml_attr(it, "href")
        if i and href:
            manifest[i] = (href, (_xml_attr(it, "media-type") or "").lower())
    spine: List[_SpineItem] = []
    for ref in _ITEMREF_RE.findall(opf):
        it = manifest.get(_xml_attr(ref, "idref") or "")
        p = epub_resolve(base, it[0]) if it else None
        if p:
            spine.append(_SpineItem(p, it[1], (_xml_attr(ref, "linear") or "yes").lower() != "no"))
    sp = _SPINE_RE.search(opf)
    ppd = (_xml_attr(sp.group(0), "page-progression-direction") or "").lower() if sp else ""
    book_type = ""
    for meta in _META_RE.findall(opf):
        if (_xml_attr(meta, "name") or "").lower() == "book-type":
            book_type = (_xml_attr(meta, "content") or "").strip().lower()
            break
    return _EpubPackage(names, spine, True if ppd == "rtl" else False if ppd == "ltr" else None, book_type)


def _image_refs(xhtml: str) -> List[str]:
    """一页 xhtml 里的图片引用：<img src>、svg 的 <image xlink:href / href>。"""
    out = []
    for m in _IMG_RE.finditer(xhtml):
        tag = m.group(0)
        v = _xml_attr(tag, "src") if m.group(1).lower() == "img" else (_xml_attr(tag, "xlink:href") or _xml_attr(tag, "href"))
        if v:
            out.append(v)
    return out


def _visible_chars(xhtml: str) -> int:
    """一页可见文字的字数：去掉 <head> / <script> / <style> / 注释 / 标签，再去掉全部空白。"""
    for rx in _NOT_VISIBLE_RES:
        xhtml = rx.sub("", xhtml)
    return len(_WS_RE.sub("", html.unescape(_TAG_RE.sub("", xhtml))))


def _image_entry(pkg: _EpubPackage, path: Optional[str]) -> Optional[str]:
    """spine 里引用到的图片路径 -> 压缩包里真实存在、且算一页的条目名；否则 None。"""
    real = pkg.names.get(path.lower()) if path else None
    return real if real is not None and is_page(real) else None


def _epub_pages(zf: zipfile.ZipFile, pkg: _EpubPackage) -> List[str]:
    """页序 = spine 顺序里每页 xhtml 引用的图片（去重）；spine 之外的图片不算。引用关系读不出来时返回 []。"""
    seen, out = set(), []

    def add(path):
        e = _image_entry(pkg, path)
        if e is not None and e not in seen:
            seen.add(e)
            out.append(e)

    for it in pkg.spine:
        if it.type.startswith("image/"):
            add(it.path)
            continue
        text = _read_member(zf, pkg.names, it.path)
        if text is None:
            continue
        base = it.path.rpartition("/")[0]
        for ref in _image_refs(text):
            add(epub_resolve(base, ref))
    n_images = sum(1 for n in zf.namelist() if is_page(n))
    return out if out and len(out) >= n_images * EPUB_PAGE_ORDER_MIN_COVERAGE else []


def _epub_page_order_zf(zf: zipfile.ZipFile) -> Tuple[List[str], Optional[bool]]:
    pkg = _read_package(zf)
    if pkg is None:
        return [], None
    return _epub_pages(zf, pkg), pkg.rtl


def epub_page_order(path: str) -> Tuple[List[str], Optional[bool]]:
    """epub 漫画的 (页序, 从右往左?)。页 = 压缩包里的条目名，封面（spine 第一项）在前；rtl 取 OPF 的 page-progression-direction
    （rtl True / ltr False / 没写 None）。读不出页序（坏 zip、没有 OPF、spine 引用不到图）时页序为 []，调用方退回自然排序。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return _epub_page_order_zf(zf)
    except _ZIP_FAIL:
        return [], None


def _sample(items: list, k: int) -> list:
    """从 items 里均匀取至多 k 个（含首尾）。"""
    n = len(items)
    if n <= k:
        return list(items)
    return [items[round(i * (n - 1) / (k - 1))] for i in range(k)]


def _detect_image_epub(zf: zipfile.ZipFile) -> bool:
    pkg = _read_package(zf)
    if pkg is None:
        return False
    spine = [it for it in pkg.spine if it.linear] or pkg.spine      # 全是 linear="no" 时退回全部 itemref
    if not spine:
        return False

    def stats(it: _SpineItem) -> Optional[Tuple[int, int]]:
        """(能解析到图片的引用数, 可见字数)；读不到这页返回 None。"""
        if it.type.startswith("image/"):
            return (1, 0) if _image_entry(pkg, it.path) else None
        text = _read_member(zf, pkg.names, it.path)
        if text is None:
            return None
        base = it.path.rpartition("/")[0]
        imgs = sum(1 for r in _image_refs(text) if _image_entry(pkg, epub_resolve(base, r)))
        return imgs, _visible_chars(text)

    if len(spine) <= EPUB_WEBTOON_SPINE_MAX:
        got = [s for s in map(stats, spine) if s is not None]
        imgs, text = sum(s[0] for s in got), sum(s[1] for s in got)
        return imgs >= EPUB_WEBTOON_MIN_IMGS and text <= EPUB_PAGE_TEXT_MAX * imgs
    inner = spine[1:-1] if len(spine) >= EPUB_EDGE_SKIP_MIN_SPINE else spine
    got = [s for s in map(stats, _sample(inner, EPUB_SAMPLE_PAGES)) if s is not None]
    if not got:
        return False
    picture = sum(1 for imgs, text in got if imgs >= 1 and text <= EPUB_PAGE_TEXT_MAX)
    thr = EPUB_COMIC_FRAC_HINTED if pkg.book_type in EPUB_COMIC_BOOK_TYPES else EPUB_COMIC_FRAC
    return picture / len(got) >= thr


def is_image_epub(path: str) -> bool:
    """这本 .epub 是不是只有图片的漫画。便宜（container.xml + OPF + 至多 EPUB_SAMPLE_PAGES 页小 xhtml，不解压图片），
    结果按 (路径, 大小, 修改时间) 记忆。任何读取失败都返回 False：判不出来就按文字书走，不挡住打开。"""
    if not isinstance(path, str) or not path.lower().endswith(".epub"):
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    with _MEMO_LOCK:
        hit = _IMAGE_EPUB_MEMO.get(key)
    if hit is not None:
        return hit
    try:
        with zipfile.ZipFile(path) as zf:
            res = _detect_image_epub(zf)
    except Exception:
        res = False
    with _MEMO_LOCK:
        if len(_IMAGE_EPUB_MEMO) >= EPUB_MEMO_MAX:
            _IMAGE_EPUB_MEMO.clear()
        _IMAGE_EPUB_MEMO[key] = res
    return res


# ================================================================== 压缩包
class ComicArchive:
    """打开的漫画压缩包。多线程可共享：所有读取都在锁里，close() 之后再读抛 ComicError。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._zf: Optional[zipfile.ZipFile] = None
        self.rtl: Optional[bool] = None      # OPF 声明的阅读方向（只有 epub 可能有）：True 从右往左 / False 从左往右 / None 没写
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as e:
            raise ComicError(f"无法打开压缩包：{e}") from e
        try:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ComicError(f"压缩包条目过多（{len(infos)}）")
            self._sizes: Dict[str, int] = {i.filename: i.file_size for i in infos if is_page(i.filename)}
            self._repair_names = not any(i.flag_bits & 0x800 for i in infos)   # 没有任何条目标了 UTF-8 才尝试按 GBK 还原
            epub_pages, self.rtl = _epub_page_order_zf(zf) if path.lower().endswith(".epub") else ([], None)
        except BaseException:
            zf.close()
            raise
        self._zf = zf
        self._flat = bool(epub_pages)        # epub 漫画按 spine 排页，一本一章（图片散在 OEBPS/ 与 OEBPS/images/ 下，目录不是章）
        self.pages: List[str] = epub_pages or sorted(self._sizes, key=path_key)

    def read(self, entry: str) -> bytes:
        size = self._sizes.get(entry)
        if size is None:
            raise ComicError(f"压缩包里没有这一页：{entry}")
        if size > MAX_ENTRY_SIZE:
            raise ComicError(f"页面过大（{size} 字节）：{entry}")
        with self._lock:
            if self._zf is None:
                raise ComicError("压缩包已关闭")
            try:
                with self._zf.open(entry) as f:
                    data = f.read(MAX_ENTRY_SIZE + 1)      # 头里声明的大小可以造假，按实际读到的再卡一次
            except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError, ValueError) as e:
                raise ComicError(f"读取失败（损坏或加密）：{entry}：{e}") from e
        if len(data) > MAX_ENTRY_SIZE:
            raise ComicError(f"页面过大：{entry}")
        return data

    def chapters(self) -> List[Chapter]:
        if self._flat:
            return [Chapter("", 0, len(self.pages))]
        return group_chapters(self.pages, display_name if self._repair_names else str)

    def close(self):
        with self._lock:
            zf, self._zf = self._zf, None
        if zf is not None:
            zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def list_pages(path: str) -> List[str]:
    """按自然顺序列出 .zip / .cbz 里的页（条目名）。坏 zip 抛 ComicError；没有图片返回 []。"""
    with ComicArchive(path) as a:
        return list(a.pages)


def read_page_bytes(path: str, entry: str) -> bytes:
    """读一页的原始字节（每次打开 / 关闭压缩包，线程安全；连续读很多页请用 ComicArchive）。"""
    with ComicArchive(path) as a:
        return a.read(entry)


# ================================================================== 跨页 / 方向 / 缩放（纯函数）
def is_portrait(w: float, h: float) -> bool:
    return h > w > 0


def spread_pages(index: int, count: int, portrait: Callable[[int], bool],
                 spread: bool = True, cover_alone: bool = True) -> Tuple[int, ...]:
    """index 起始的这一屏要显示哪几页（1 或 2 页）。只有两页都是竖页才合成跨页；cover_alone 时第 0 页（封面）单独显示。"""
    if not spread or index >= count - 1 or (cover_alone and index == 0):
        return (index,)
    if portrait(index) and portrait(index + 1):
        return (index, index + 1)
    return (index,)


def next_start(index: int, count: int, portrait: Callable[[int], bool],
               spread: bool = True, cover_alone: bool = True) -> Optional[int]:
    """下一屏的起始页；已经是最后一屏返回 None。"""
    nxt = spread_pages(index, count, portrait, spread, cover_alone)[-1] + 1
    return nxt if nxt < count else None


def prev_start(index: int, count: int, portrait: Callable[[int], bool],
               spread: bool = True, cover_alone: bool = True) -> Optional[int]:
    """上一屏的起始页；已经是第一屏返回 None。往回退时，前两页都是竖页（且不碰封面）就退成一个跨页。"""
    if index <= 0:
        return None
    s = index - 2
    if spread and s >= (1 if cover_alone else 0) and portrait(s) and portrait(index - 1):
        return s
    return index - 1


def display_order(pages: Sequence[int], rtl: bool) -> Tuple[int, ...]:
    """跨页从左到右的绘制顺序：从右往左阅读时，先读的页在右边。"""
    return tuple(reversed(pages)) if rtl else tuple(pages)


def click_side(x: float, width: float) -> str:
    """点击位置落在左 / 中 / 右三分之一。"""
    if width <= 0:
        return "middle"
    if x < width / 3:
        return "left"
    if x > width * 2 / 3:
        return "right"
    return "middle"


def nav_delta(side: str, rtl: bool) -> int:
    """点击 / 方向键的左右侧对应的翻页方向：+1 下一页，-1 上一页，0 无动作。从右往左阅读时左侧是下一页。"""
    if side == "left":
        return 1 if rtl else -1
    if side == "right":
        return -1 if rtl else 1
    return 0


def spread_size(sizes: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """一屏的合成尺寸：各页先缩放到同一高度（最高那页的高度）再并排。单页就是原尺寸。"""
    h = max(s[1] for s in sizes)
    return sum(w * h / ph for w, ph in sizes), h


def compute_zoom(fit: str, cw: float, ch: float, rw: float, rh: float) -> float:
    """按适配模式算缩放倍率（参照 ImageViewer compute_zoom）。cw/ch 视口，rw/rh 图片（或跨页）尺寸。"""
    cw, ch = max(cw, 1), max(ch, 1)
    rw, rh = max(rw, 1), max(rh, 1)
    if fit == "width":
        z = cw / rw
    elif fit == "height":
        z = ch / rh
    else:
        z = min(cw / rw, ch / rh)
    return min(max(z, MIN_ZOOM), MAX_ZOOM)
