"""书架（Shelf）纯逻辑模型：无 Qt、无网络。设计见 c:\\tmp\\pyreader-bookshelf-design.md。

内容：
  - fp / fp_weak：内容指纹提示（NFC 规范化文件名；不是身份）
  - scan_local / build_books：本地与云端书目归并成 Book（归并只看来源，不单靠 fp）
  - derive_status / filter_books / sort_books：派生状态、智能书架、搜索、排序
  - 覆盖层（overlay）：validate_overlay → merge → set_field（混合逻辑时钟）→ to_wire（上传白名单）
  - load_state / save_state / load_overlay / save_overlay：~/.pyreader/shelf/ 下的原子读写

覆盖层的内存形式（「视图」，由 validate_overlay / merge 产生）：
    {"version": 1, "_view": True, "device_id": <写者，仅本设备覆盖层有>, "_clock": [时钟异常设备],
     "entries":  {book_id: {"fav": [bool, ts_ms, dev], "hidden": [...], "meta": [{"fp","title"}, ts_ms, dev],
                            "rekeyed_from": [book_id...], <未知字段原样保留>}},
     "progress": {book_id: {"percent": float, "updated": 秒, "device": dev}}}
每个寄存器带写者 dev，是为了让「ts 相同取 device_id 大者」在多次合并后仍满足交换律 / 结合律 / 幂等。
线上格式（to_wire）里寄存器只有 [值, ts] 两项：写者就是文件名里的 device_id。
远端内容一律先 validate_overlay(raw, device_id=<取自通过校验的文件名>)，再 merge。
"""
import hashlib
import json
import math
import os
import re
import secrets
import socket
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .store import is_hidden_name, load_json, normalize_rel, safe_component, save_json

WIRE_VERSION = 1
STATE_VERSION = 1
MEDIA_PROGRESS_KEYS = ("_progress_ms", "_progress_page", "_progress_pct_")     # bookmarks.json 里应用内媒体的进度字段

# 单一来源：READABLE_EXTS = 阅读器（文字版式）能打开的，只有它们进 load_book / BookLoader；
# INAPP_MEDIA_EXTS = 应用内播放 / 浏览的媒体（音频 → AudioPlayerView，.zip / .cbz 漫画 → ComicView），有进度但不进 load_book；
# EXTERNAL_EXTS = 只存不读（没有内置解码器的 .cbr / .rar / .7z），打开时交给系统默认应用，不记进度；
# MEDIA_EXTS = 以上两类合起来（凡不是文字书的都在里面）；BOOK_EXTS = 书架可列的全部。
# 必须全小写且带点（endswith 吃元组）；不要放 .exe / .bat / .lnk 等可执行类型。
READABLE_EXTS = (".txt", ".md", ".markdown", ".epub", ".html", ".htm", ".mobi",
                 ".azw", ".azw3", ".prc", ".pdf", ".docx", ".pptx")
AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav")
ARCHIVE_EXTS = (".zip", ".cbz", ".uvz", ".cbr", ".7z", ".rar")
COMIC_EXTS = (".zip", ".cbz", ".uvz")
INAPP_MEDIA_EXTS = AUDIO_EXTS + COMIC_EXTS
EXTERNAL_EXTS = (".cbr", ".7z", ".rar")
MEDIA_EXTS = INAPP_MEDIA_EXTS + EXTERNAL_EXTS
BOOK_EXTS = READABLE_EXTS + AUDIO_EXTS + ARCHIVE_EXTS


def media_kind(name) -> str:
    """文件名的类别：book 电子书 | audio 有声书 / 音频 | archive 压缩包 / 漫画 | ''（不支持）。批量上传按它分类汇总、可单独勾选。"""
    if not isinstance(name, str):
        return ""
    n = name.lower()
    return "audio" if n.endswith(AUDIO_EXTS) else "archive" if n.endswith(ARCHIVE_EXTS) else "book" if n.endswith(READABLE_EXTS) else ""


def inapp_kind(name) -> str:
    """应用内媒体的类别：audio（AudioPlayerView）| comic（.zip / .cbz，ComicView）| ''（不是应用内媒体）。"""
    if not isinstance(name, str):
        return ""
    n = name.lower()
    return "audio" if n.endswith(AUDIO_EXTS) else "comic" if n.endswith(COMIC_EXTS) else ""


def is_readable(name) -> bool:
    """文件名 / 路径的扩展名是否属于阅读器能打开的格式（不区分大小写）。"""
    return isinstance(name, str) and name.lower().endswith(READABLE_EXTS)


def is_external(name) -> bool:
    """文件名 / 路径的扩展名是否属于「只存不读」格式（.cbr / .rar / .7z，没有内置解码器），不区分大小写。"""
    return isinstance(name, str) and name.lower().endswith(EXTERNAL_EXTS)


def is_inapp_media(name) -> bool:
    """文件名 / 路径的扩展名是否属于应用内播放 / 浏览的媒体（音频、.zip / .cbz 漫画），不区分大小写。"""
    return isinstance(name, str) and name.lower().endswith(INAPP_MEDIA_EXTS)

FUTURE_LIMIT_MS = 24 * 3600 * 1000       # 超过「现在 + 24 小时」的时间戳属于时钟异常
CLAMPED_TS = 1                           # 时钟异常的 ts 截断成最旧的 1ms（见 _parse_ts 的说明）
MAX_ENTRIES = 20000                      # 覆盖层条目数 / 进度条目数上限
MAX_STR = 256                            # 字符串长度上限
MAX_REKEYED = 100
MAX_EXTRA_BYTES = 1024                   # 未知字段值序列化后的上限
SCAN_LIMIT = 5000                        # 本地扫描总量上限（Library 菜单的 300 上限不沿用）
DONE_PERCENT = 97.0                      # >= 97 视为读完

CLOUD = "cloud:"
LOCAL = "local:"

REG_FIELDS = ("fav", "hidden")           # 布尔 LWW 寄存器
KNOWN_ENTRY_KEYS = frozenset(("fav", "hidden", "meta", "rekeyed_from"))

_HEX40 = re.compile(r"[0-9a-f]{40}", re.ASCII)
_BIDI = set("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


def _canon(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _encodable(s) -> bool:
    """字符串能否无损编码为 UTF-8（孤立代理项（如 U+D800）会让 json.loads 通过、写盘 / 上传时抛 UnicodeEncodeError）。"""
    try:
        s.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _clean_str(s, limit=MAX_STR) -> Optional[str]:
    """去掉控制字符、代理项（Cs）与 bidi 控制字符、截断到 limit。非字符串返回 None。"""
    if not isinstance(s, str):
        return None
    s = "".join(c for c in s if unicodedata.category(c) not in ("Cc", "Cs") and c not in _BIDI)
    return s[:limit]


def _default_root() -> str:
    """数据根（等价于 config.APP_DIR）。延迟求值：尊重 PYREADER_HOME，且 import 本模块不触碰磁盘。"""
    from .config import resolve_app_dir
    return resolve_app_dir()


def _sync():
    """延迟导入 sync：book_id 直接复用 sync.book_id（design：不另造 id 体系），
    同时让 shelf 模块本身在 import 时不依赖 Qt。"""
    from . import sync
    return sync


def _hostname() -> str:
    try:
        h = socket.gethostname() or ""
    except Exception:
        h = ""
    h = re.sub(r"[^a-z0-9-]", "", h.lower()).strip("-")[:40]
    return h or "device"


# ================================================================== 指纹
def fp_weak(name: str, size: int) -> str:
    """弱指纹：sha1(NFC(文件名) + 大小)。云端书下载前只能算这个；本地书也用它做跨来源提示比较。"""
    base = _nfc(os.path.basename(str(name).replace("\\", "/")))
    return hashlib.sha1(f"{base}|{int(size)}".encode("utf-8")).hexdigest()[:20]


def fp(path: str) -> Optional[str]:
    """强指纹：sha1(NFC(文件名) + 大小 + 首尾各 64KB 的 sha1)，取前 20 位十六进制。读不了返回 None。
    只是提示，不是身份：不能单独用作自动合并依据。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(65536)
            f.seek(max(0, size - 65536))
            tail = f.read(65536)
    except OSError:
        return None
    h = hashlib.sha1()
    h.update(f"{_nfc(os.path.basename(path))}|{size}|".encode("utf-8"))
    h.update(hashlib.sha1(head).digest())
    h.update(hashlib.sha1(tail).digest())
    return h.hexdigest()[:20]


def local_id_for(name: str, size: int) -> str:
    """与 sync.book_id 对非 remote_books 文件的算法一致：local:sha1(basename+size)。
    用来给「云端书的本地缓存副本」查它原来的 local: 进度别名（design §4.9 进度读取回退）。"""
    return LOCAL + hashlib.sha1(f"{os.path.basename(name)}{int(size)}".encode("utf-8")).hexdigest()


def valid_book_id(bid) -> bool:
    """book_id 只能是 remote:<合法 rel> 或 local:<40 位小写十六进制>。"""
    if not isinstance(bid, str) or len(bid) > 1100 or not _encodable(bid):
        return False
    if bid.startswith("remote:"):
        rel = bid[7:]
        return normalize_rel(rel) is not None and not any(is_hidden_name(s) for s in rel.split("/"))
    if bid.startswith(LOCAL):
        return _HEX40.fullmatch(bid[6:]) is not None
    return False


def valid_remote_rel(rel) -> bool:
    """云端 library/ 下可显示的文件：rel 合法、没有点 / ~$ 开头的段、扩展名在 BOOK_EXTS（可读 + 外部格式）内。"""
    if normalize_rel(rel) is None:
        return False
    segs = rel.split("/")
    if any(is_hidden_name(s) for s in segs):
        return False
    return segs[-1].lower().endswith(BOOK_EXTS)


# ================================================================== 状态派生
def derive_status(percent: Optional[float], has_progress: bool = False) -> str:
    """percent == 0 未读；0 < percent < 97 在读；>= 97 读完。
    percent 未知（None）时：已知有本地进度偏移（has_progress）则按「在读」，否则「未读」。"""
    if percent is None:
        return "reading" if has_progress else "unread"
    try:
        p = float(percent)
    except (TypeError, ValueError, OverflowError):
        return "unread"
    if not math.isfinite(p) or p <= 0:
        return "unread"
    return "done" if p >= DONE_PERCENT else "reading"


# ================================================================== 书签键归一
def norm_path(p: str) -> str:
    """书签键归一：打开对话框返回正斜杠、扫描得到反斜杠、Windows 盘符大小写不一 → 统一后再比。"""
    return os.path.normcase(os.path.normpath(p))


def norm_bookmarks(bookmarks) -> Dict[str, dict]:
    """{归一后的路径: 条目}。只读，不改写 bookmarks。多个键归一后相同则 _progress_ts_ 较新者优先。"""
    out: Dict[str, dict] = {}
    if not isinstance(bookmarks, dict):
        return out
    for k, v in bookmarks.items():
        if not isinstance(k, str) or not isinstance(v, dict) or not k:
            continue
        nk = norm_path(k)
        old = out.get(nk)
        if old is None or _num(v.get("_progress_ts_"), 0.0) > _num(old.get("_progress_ts_"), 0.0):
            out[nk] = v
    return out


def bookmark_for(bookmarks, path: str) -> Optional[dict]:
    """按归一后的路径查书签条目（_progress_ / _progress_ts_ 都在条目里）。"""
    return norm_bookmarks(bookmarks).get(norm_path(path))


def _num(x, default=None):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return default
    try:
        f = float(x)                      # 巨大整数（json.loads 接受）在这里抛 OverflowError；math.isfinite(int) 同样会抛
    except (OverflowError, ValueError):
        return default
    return f if math.isfinite(f) else default


# ================================================================== 本地扫描
@dataclass
class LocalFile:
    path: str
    root: str            # 书库根（library_dirs 中的一项），正斜杠
    sub: str             # 根下的相对文件夹（/ 分隔，根目录为 ""）
    name: str
    size: int
    mtime: float


class ScanList(list):
    truncated = False    # 达到 limit 被截断


def scan_local(roots: Iterable[str], limit: int = SCAN_LIMIT, exts=BOOK_EXTS, should_stop=None) -> ScanList:
    """遍历书库目录，返回 LocalFile 列表。忽略点 / ~$ 开头的文件与文件夹、不在 exts（默认 BOOK_EXTS）内的扩展名、符号链接目录。
    顺序确定（目录与文件名排序）。应在后台线程调用；should_stop() 为真时尽快返回。"""
    out = ScanList()
    for root in roots:
        if not isinstance(root, str) or not os.path.isdir(root):
            continue
        rroot = root.replace("\\", "/").rstrip("/") or root
        for dp, dns, fns in os.walk(root, followlinks=False):
            if should_stop is not None and should_stop():
                return out
            dns[:] = sorted(d for d in dns if not is_hidden_name(d))
            rel = os.path.relpath(dp, root)
            sub = "" if rel == "." else rel.replace(os.sep, "/")
            for fn in sorted(fns):
                if is_hidden_name(fn) or not fn.lower().endswith(exts):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if len(out) >= limit:
                    out.truncated = True
                    return out
                out.append(LocalFile(p, rroot, sub, fn, int(st.st_size), float(st.st_mtime)))
    return out


# ================================================================== Book / build_books
@dataclass
class RemoteFile:
    rel: str
    size: int = 0
    mtime: float = 0.0


@dataclass
class Book:
    id: str
    title: str
    fmt: str = ""
    size: int = 0
    shelf: str = ""                  # cloud:<文件夹>（根目录为 "cloud:"）或 local:<根>/<子文件夹>
    fp: str = ""                     # 弱指纹（提示用）
    where: str = "local"             # cloud 仅云端 | synced 已同步 | local 仅本地 | removed 云端已移除 | missing 占位
    remote_rel: str = ""
    mtime: float = 0.0               # 文件修改时间（云端取远端 mtime），用于「最近添加」
    local_paths: List[str] = field(default_factory=list)
    author: str = ""
    fav: bool = False
    hidden: bool = False
    percent: Optional[float] = None
    has_progress: bool = False       # 本机书签里有 _progress_ 偏移（percent 未知时用来判「在读」）
    progress_updated: float = 0.0
    progress_device: str = ""
    last_opened: float = 0.0
    last_read: float = 0.0           # max(last_opened, 进度更新时间, 书签时间)
    same_as: List[str] = field(default_factory=list)   # 来源不同但文件名+大小相同的书（「可能是同一本书」）
    unpushed: bool = False           # 本设备对这本书的 fav / hidden 修改还没推送到云端（橙点；只是显示用，不写索引）

    @property
    def external(self) -> bool:
        """只存不读的格式（.cbr / .rar / .7z）：按真实文件名判断，不单信 fmt（索引缓存里的 fmt 不可信，占位卡 fmt 为空）。"""
        if is_external(self.remote_rel) or any(is_external(p) for p in self.local_paths):
            return True
        return bool(self.fmt) and ("." + self.fmt) in EXTERNAL_EXTS

    @property
    def inapp_media(self) -> bool:
        """应用内播放 / 浏览的媒体（音频、.zip / .cbz 漫画）：有进度，但不是文字书。判断依据同 external。"""
        if is_inapp_media(self.remote_rel) or any(is_inapp_media(p) for p in self.local_paths):
            return True
        return bool(self.fmt) and ("." + self.fmt) in INAPP_MEDIA_EXTS

    @property
    def readable(self) -> bool:
        """会记进度 / 最近阅读的书（文字书与应用内媒体）；只有外部格式不记。"""
        return not self.external

    @property
    def fmt_label(self) -> str:
        """格式标签（大写）：fmt 为空的占位卡 / 索引缓存回退到真实文件名的扩展名。"""
        return (self.fmt or os.path.splitext(self.filename)[1].lstrip(".")).upper()

    @property
    def status(self) -> str:
        if self.external:        # 外部格式永远「未读」：不进在读 / 读完 / 继续阅读
            return "unread"
        return derive_status(self.percent, self.has_progress)

    @property
    def filename(self) -> str:
        if self.remote_rel:
            return self.remote_rel.rsplit("/", 1)[-1]
        return os.path.basename(self.local_paths[0]) if self.local_paths else self.title

    @property
    def removed(self) -> bool:
        return self.where == "removed"

    @property
    def placeholder(self) -> bool:
        return self.where == "missing"

    @property
    def auto_hidden(self) -> bool:
        """默认不显示：其它设备的占位；云端已移除且本机无副本、也不在读（design §4.8 / §4.9）。"""
        if self.where == "missing":
            return True
        return self.where == "removed" and not self.local_paths and self.status != "reading"


def shelf_of_remote(rel: str) -> str:
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return CLOUD + folder


def shelf_of_local(f: LocalFile) -> str:
    return LOCAL + (f.root + "/" + f.sub if f.sub else f.root)


def _stem(name: str) -> str:
    return os.path.splitext(name)[0] or name


def field_value(view: dict, bid: str, name: str, default=None):
    """从覆盖层视图取某条目的寄存器值。"""
    reg = ((view or {}).get("entries", {}).get(bid) or {}).get(name)
    if isinstance(reg, (list, tuple)) and len(reg) >= 2:
        return reg[0]
    return default


def _coerce_remote(x) -> Optional[RemoteFile]:
    if isinstance(x, RemoteFile):
        r = x
    elif isinstance(x, dict):
        r = RemoteFile(x.get("rel"), x.get("size", 0), x.get("mtime", 0.0))
    elif isinstance(x, (tuple, list)) and x:
        r = RemoteFile(*x[:3])
    else:
        return None
    if not valid_remote_rel(r.rel):
        return None
    try:
        return RemoteFile(r.rel, int(r.size or 0), float(r.mtime or 0.0))
    except (TypeError, ValueError):
        return None


def build_books(local_files: Iterable[LocalFile] = (), remote_files=None, overlay: Optional[dict] = None, *,
                bookmarks=None, recent: Iterable[str] = (), opened: Optional[dict] = None,
                removed: Iterable[str] = (), removed_files: Iterable = ()) -> Dict[str, Book]:
    """把本地扫描、云端列表、覆盖层视图与进度归并成 {book_id: Book}。纯函数，不做网络。

    - remote_files：None 表示云端列表未知（离线 / 列表不完整）；此时不会据列表推断任何「消失」。
    - 归并只靠来源：本地文件恰好位于 remote_books/<rel> 才是该云端书的缓存副本（sync.book_id 给出 remote:<rel>）。
      其它本地文件即使文件名+大小相同也是独立卡片，仅在 same_as 里互相提示。
    - 同一 book_id 的多个本地路径（同名同大小的拷贝）合成一张卡片（book_id 的既有语义）。
    - removed：调用方按「连续两轮完整列表都缺失」判定出的 id 集合；本函数不做二轮判断。
    - removed_files：被判定为已移除的云端书的最后已知条目（RemoteFile / (rel, size, mtime)）；
      本机没有副本、覆盖层里也没有它们时，靠这个才能在「云端已移除」书架里显示。
    - overlay：validate_overlay / merge 产生的视图（None = 无覆盖层）。
    """
    sync = _sync()
    overlay = overlay or {}
    entries = overlay.get("entries", {}) if isinstance(overlay, dict) else {}
    digest = overlay.get("progress", {}) if isinstance(overlay, dict) else {}
    removed = set(removed or ())
    opened = opened or {}
    nbm = norm_bookmarks(bookmarks)
    books: Dict[str, Book] = {}

    # 1) 云端列表
    remote_ids = set()
    for x in (remote_files or ()):
        r = _coerce_remote(x)
        if r is None:
            continue
        bid = "remote:" + r.rel
        remote_ids.add(bid)
        name = r.rel.rsplit("/", 1)[-1]
        b = Book(bid, _stem(name), os.path.splitext(name)[1].lstrip(".").lower(), r.size,
                 shelf_of_remote(r.rel), fp_weak(name, r.size), "cloud", r.rel, r.mtime)
        cp = os.path.join(sync.REMOTE_BOOKS_DIR, *r.rel.split("/"))
        if os.path.isfile(cp):
            b.local_paths.append(cp)
            b.where = "synced"
        books[bid] = b

    # 2) 本地文件
    for f in local_files:
        bid = sync.book_id(f.path)
        b = books.get(bid)
        if b is None:
            if bid.startswith("remote:"):
                rel = bid[7:]
                b = Book(bid, _stem(f.name), os.path.splitext(f.name)[1].lstrip(".").lower(), f.size,
                         shelf_of_remote(rel), fp_weak(f.name, f.size),
                         "removed" if (remote_files is not None and bid in removed) else "synced", rel, f.mtime)
            else:
                b = Book(bid, _stem(f.name), os.path.splitext(f.name)[1].lstrip(".").lower(), f.size,
                         shelf_of_local(f), fp_weak(f.name, f.size), "local", "", f.mtime)
            books[bid] = b
        if norm_path(f.path) not in {norm_path(x) for x in b.local_paths}:
            b.local_paths.append(f.path)
        if b.where == "cloud":
            b.where = "synced"

    # 2b) 云端已移除、本机没有副本的书：靠最后已知的列表条目显示（默认隐藏，仅「云端已移除」书架可见）
    if remote_files is not None:
        for x in (removed_files or ()):
            r = _coerce_remote(x)
            bid = "remote:" + r.rel if r is not None else ""
            if r is None or bid in books or bid not in removed:
                continue
            name = r.rel.rsplit("/", 1)[-1]
            books[bid] = Book(bid, _stem(name), os.path.splitext(name)[1].lstrip(".").lower(), r.size,
                              shelf_of_remote(r.rel), fp_weak(name, r.size), "removed", r.rel, r.mtime)

    # 3) 覆盖层里有、本机什么都没找到的条目 → 占位
    for bid in sorted(entries):
        if bid in books or not valid_book_id(bid):
            continue
        meta = field_value(overlay, bid, "meta") or {}
        title = meta.get("title") or (bid[7:].rsplit("/", 1)[-1] if bid.startswith("remote:") else bid[6:14])
        rel = bid[7:] if bid.startswith("remote:") else ""
        b = Book(bid, title, "", 0, shelf_of_remote(rel) if rel else "", meta.get("fp", ""), "missing", rel)
        if rel and remote_files is not None and bid in removed:
            b.where = "removed"
        books[bid] = b

    # 4) 覆盖层字段、进度、最近打开
    recent = [p for p in (recent or ()) if isinstance(p, str)]
    recent_rank = {}
    for i, p in enumerate(recent):
        recent_rank.setdefault(norm_path(p), float(len(recent) - i))     # 越靠前（越新）数值越大
    for bid, b in books.items():
        meta = field_value(overlay, bid, "meta")
        if isinstance(meta, dict) and meta.get("title"):
            b.title = meta["title"]
        b.fav = bool(field_value(overlay, bid, "fav", False))
        b.hidden = bool(field_value(overlay, bid, "hidden", False))

        cands = [bid]
        if bid.startswith("remote:") and b.size:     # 先用本地读、后来才进云端书库的书：进度在 local: 别名下
            cands.append(local_id_for(b.remote_rel, b.size))
        best = None
        for c in cands:
            rec = digest.get(c) if isinstance(digest, dict) else None
            if isinstance(rec, dict) and (best is None or rec.get("updated", 0) > best.get("updated", 0)):
                best = rec
        if best and b.readable:      # 外部格式不记进度（别的设备 / 旧别名里的杂散记录也不采信）
            b.percent, b.progress_updated, b.progress_device = best.get("percent"), best.get("updated", 0.0), best.get("device", "")
        bm_ts, bm_off, bm_frac = 0.0, 0.0, 0.0
        for p in b.local_paths:
            ent = nbm.get(norm_path(p)) if b.readable else None
            if ent:
                bm_ts = max(bm_ts, _num(ent.get("_progress_ts_"), 0.0))
                bm_off = max(bm_off, _num(ent.get("_progress_"), 0.0), _num(ent.get("_progress_ms"), 0.0),
                             _num(ent.get("_progress_page"), 0.0))
                bm_frac = max(bm_frac, _num(ent.get("_progress_pct_"), 0.0))
        b.has_progress = bm_off > 0
        if b.percent is None and bm_frac > 0 and b.inapp_media:      # 应用内媒体：书签里的 0..1 比例兜底（进度摘要 / 索引还没有时）
            b.percent, b.progress_updated = min(100.0, bm_frac * 100.0), bm_ts
        seed = max([recent_rank.get(norm_path(p), 0.0) for p in b.local_paths] or [0.0])
        b.last_opened = float(opened.get(bid, seed) or 0.0) if b.readable else 0.0     # 外部格式不进「最近阅读」（旧版 recent / 索引里的杂散记录不采信）
        b.last_read = max(b.last_opened, b.progress_updated, bm_ts)

    # 5) 「可能是同一本书」：仅限 local: 与 remote: 两种来源之间（同来源的同名书各自独立）
    by_fp: Dict[str, List[Book]] = {}
    for b in books.values():
        if b.fp and b.where != "missing":
            by_fp.setdefault(b.fp, []).append(b)
    for grp in by_fp.values():
        for b in grp:
            kind = b.id.startswith("remote:")
            b.same_as = sorted(o.id for o in grp if o.id.startswith("remote:") != kind)
    return books


# ================================================================== 过滤 / 排序
SMART_SHELVES = ("all", "recent", "reading", "done", "fav", "removed", "uncategorized", "missing", "hidden")
SORT_KEYS = ("last_read", "title", "added", "progress", "size")


def _visible(b: Book) -> bool:
    return not b.hidden and not b.auto_hidden


def filter_books(books, shelf: str = "all", query: str = "") -> List[Book]:
    """按智能书架 / 文件夹书架过滤，再按 query 做 casefold 子串匹配（书名、作者、文件名）。

    智能书架：all 全部可见；recent 读过 / 打开过的；reading 在读；done 读完；fav 收藏；
    removed 云端已移除（含默认隐藏的）；uncategorized 云端 library 根目录的书；
    missing 其它设备的占位；hidden 我隐藏的。其余形如 cloud:<文件夹> / local:<路径> 的字符串按书架名精确匹配。"""
    bs = list(books.values()) if isinstance(books, dict) else list(books)
    if shelf == "all":
        out = [b for b in bs if _visible(b)]
    elif shelf == "recent":
        out = [b for b in bs if _visible(b) and b.last_read > 0]
    elif shelf == "reading":
        out = [b for b in bs if _visible(b) and b.status == "reading"]
    elif shelf == "done":
        out = [b for b in bs if _visible(b) and b.status == "done"]
    elif shelf == "fav":
        out = [b for b in bs if b.fav and not b.hidden and not b.auto_hidden]
    elif shelf == "removed":
        out = [b for b in bs if b.removed and not b.hidden]
    elif shelf == "uncategorized":
        out = [b for b in bs if _visible(b) and b.shelf == CLOUD]
    elif shelf == "missing":
        out = [b for b in bs if b.placeholder and not b.hidden]
    elif shelf == "hidden":
        out = [b for b in bs if b.hidden]
    else:
        out = [b for b in bs if _visible(b) and b.shelf == shelf]
    q = _nfc(query or "").casefold().strip()
    if q:
        out = [b for b in out if q in _nfc(b.title).casefold() or q in _nfc(b.author).casefold()
               or q in _nfc(b.filename).casefold()]
    return out


def sort_books(books, key: str = "last_read") -> List[Book]:
    """确定性排序（最后以 id 兜底）。last_read / added / progress / size 为降序，title 为升序（casefold）。"""
    bs = list(books.values()) if isinstance(books, dict) else list(books)
    if key == "title":
        return sorted(bs, key=lambda b: (_nfc(b.title).casefold(), b.id))
    if key == "added":
        return sorted(bs, key=lambda b: (-b.mtime, _nfc(b.title).casefold(), b.id))
    if key == "progress":
        return sorted(bs, key=lambda b: (-(b.percent or 0.0), _nfc(b.title).casefold(), b.id))
    if key == "size":
        return sorted(bs, key=lambda b: (-b.size, _nfc(b.title).casefold(), b.id))
    return sorted(bs, key=lambda b: (-b.last_read, _nfc(b.title).casefold(), b.id))


# ================================================================== 覆盖层：校验
def _parse_ts(x, now_ms: int):
    """-> (ts, clamped)。ts 必须是正整数（或整数值的浮点）；其它返回 (None, False)。
    超过 now+24h 的 ts 属于时钟异常，截断为 CLAMPED_TS（最旧）并返回 clamped=True。

    与设计文档的偏差：§2.3 写的是「视为 now」。但「视为 now」是随时间前进的值：用户点击得到 ts=now+1，
    下一次同步时对端那个异常值又被视为更晚的 now，把用户的点击压回去——字段仍被永久冻结。
    截断成最旧值则与时间无关：异常值只在没有任何别的写入时才生效，用户随后的任何点击都能永久胜出，
    并且 merge 仍满足结合律 / 幂等（截断结果是不动点）。"""
    if isinstance(x, bool):
        return None, False
    if isinstance(x, float):
        if not math.isfinite(x) or x != math.floor(x):
            return None, False
        x = int(x)
    if not isinstance(x, int) or x <= 0:
        return None, False
    if x > now_ms + FUTURE_LIMIT_MS:
        return CLAMPED_TS, True
    return x, False


def _parse_reg(raw, now_ms, dev_default, view):
    """寄存器 [值, ts(, dev)] → (值, ts, dev, clamped) 或 None。"""
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    ts, clamped = _parse_ts(raw[1], now_ms)
    if ts is None:
        return None
    dev = dev_default
    if view and len(raw) >= 3:
        d = safe_component(raw[2])
        if d:
            dev = d
    if not dev:
        return None
    return raw[0], ts, dev, clamped


def _clean_meta(v) -> Optional[dict]:
    if not isinstance(v, dict):
        return None
    out = {}
    fpv = v.get("fp")
    if fpv is not None:
        fpv = safe_component(fpv)
        if fpv:
            out["fp"] = fpv
    t = _clean_str(v.get("title"))
    if t:
        out["title"] = t
    return out or None


def validate_overlay(raw, now_ms: Optional[int] = None, *, device_id: Optional[str] = None, view: bool = False):
    """校验一份覆盖层内容（远端内容在合并前必须先过这里）。返回 (clean | None, issues)。

    - device_id：写者，取自通过校验的文件名；给出时忽略 JSON 内的 device_id 与寄存器第三项。
    - view=True：输入已是本模块产生的视图（寄存器第三项 dev 可信）。
    - 不通过的条目 / 字段被跳过并写进 issues，不抛异常；整体类型不对或主版本过高返回 (None, issues)。
    - ts：非数字 / 非整数 / <=0 → 丢弃该寄存器；> now+24h → 截断为最旧值（CLAMPED_TS）并把该设备记入 _clock。
    - 条目数超过 MAX_ENTRIES 时按 id 排序取前 MAX_ENTRIES 个。"""
    issues: List[str] = []
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    now_ms = int(now_ms)
    if not isinstance(raw, dict):
        return None, ["overlay: not an object"]
    ver = raw.get("version", WIRE_VERSION)
    if isinstance(ver, bool) or not isinstance(ver, int):
        return None, ["overlay: bad version"]
    if ver > WIRE_VERSION:
        return None, [f"overlay: version {ver} is newer than supported"]
    if device_id is not None:
        dev = safe_component(device_id)
        if not dev:
            return None, ["overlay: bad device_id"]
        view = False
    else:
        dev = safe_component(raw.get("device_id")) if raw.get("device_id") is not None else None
        if raw.get("device_id") is not None and not dev:
            return None, ["overlay: bad device_id"]
        view = view or raw.get("_view") is True
        if not dev and not view:
            return None, ["overlay: missing device_id"]
    now_s = now_ms / 1000.0
    clock = set()
    if view and isinstance(raw.get("_clock"), (list, tuple)):
        clock.update(d for d in (safe_component(x) for x in raw["_clock"]) if d)

    out_entries: Dict[str, dict] = {}
    entries = raw.get("entries", {})
    if not isinstance(entries, dict):
        issues.append("entries: not an object")
        entries = {}
    bids = sorted(k for k in entries if isinstance(k, str))
    if len(bids) > MAX_ENTRIES:
        issues.append("entries: too many, truncated")
        bids = bids[:MAX_ENTRIES]
    for bid in bids:
        ent = entries[bid]
        if not valid_book_id(bid) or not isinstance(ent, dict):
            issues.append(f"entry {bid[:60]!r}: skipped")
            continue
        clean: dict = {}
        for fname in REG_FIELDS:
            if fname not in ent:
                continue
            r = _parse_reg(ent[fname], now_ms, dev, view)
            if r is None or not isinstance(r[0], bool):
                issues.append(f"entry {bid[:60]!r}.{fname}: skipped")
                continue
            clean[fname] = [r[0], r[1], r[2]]
            if r[3]:
                clock.add(r[2])
        if "meta" in ent:
            r = _parse_reg(ent["meta"], now_ms, dev, view)
            m = _clean_meta(r[0]) if r else None
            if r is None or m is None:
                issues.append(f"entry {bid[:60]!r}.meta: skipped")
            else:
                clean["meta"] = [m, r[1], r[2]]
                if r[3]:
                    clock.add(r[2])
        if "rekeyed_from" in ent:
            rk = ent["rekeyed_from"]
            if isinstance(rk, (list, tuple)):
                ids = sorted({x for x in rk if valid_book_id(x)})[:MAX_REKEYED]
                if ids:
                    clean["rekeyed_from"] = ids
            else:
                issues.append(f"entry {bid[:60]!r}.rekeyed_from: skipped")
        for k, v in ent.items():              # 未知字段：原样保留（有界、不含 _ 开头的本地簿记键）
            if k in KNOWN_ENTRY_KEYS or not isinstance(k, str) or not k or len(k) > 64 or k.startswith("_"):
                continue
            try:
                c = _canon(v)
                if _encodable(k) and _encodable(c) and len(c) <= MAX_EXTRA_BYTES:   # 含孤立代理项的键 / 值（含嵌套）丢弃
                    clean[k] = v
            except (TypeError, ValueError):
                pass
        if clean:
            out_entries[bid] = clean

    out_prog: Dict[str, dict] = {}
    prog = raw.get("progress", {})
    if not isinstance(prog, dict):
        issues.append("progress: not an object")
        prog = {}
    pids = sorted(k for k in prog if isinstance(k, str))
    if len(pids) > MAX_ENTRIES:
        issues.append("progress: too many, truncated")
        pids = pids[:MAX_ENTRIES]
    for bid in pids:
        rec = prog[bid]
        if not valid_book_id(bid) or not isinstance(rec, dict):
            issues.append(f"progress {bid[:60]!r}: skipped")
            continue
        pct, upd = _num(rec.get("percent")), _num(rec.get("updated"))
        if pct is None or upd is None or upd <= 0:
            issues.append(f"progress {bid[:60]!r}: skipped")
            continue
        d = dev
        if view:
            d = safe_component(rec.get("device")) or dev
        if not d:
            continue
        if upd > now_s + FUTURE_LIMIT_MS / 1000.0:
            upd = CLAMPED_TS / 1000.0
            clock.add(d)
        out_prog[bid] = {"percent": min(100.0, max(0.0, pct)), "updated": upd, "device": d}

    clean_ov = {"version": WIRE_VERSION, "_view": True, "entries": out_entries, "progress": out_prog,
                "_clock": sorted(clock)}
    if dev:
        clean_ov["device_id"] = dev
    if device_id is not None or not view:           # 本设备 / 远端设备文件才有这些元信息；合并视图没有
        h = _clean_str(raw.get("hostname"), 64)
        u = safe_component(raw.get("user")) if raw.get("user") is not None else None
        if h:
            clean_ov["hostname"] = h
        if u:
            clean_ov["user"] = u
    return clean_ov, issues


# ================================================================== 覆盖层：合并
def _reg_key(reg):
    return (reg[1], reg[2], _canon(reg[0]))


def merge(*overlays, now_ms: Optional[int] = None) -> dict:
    """合并任意多份覆盖层（原始内容或视图），返回视图。满足交换律、结合律、幂等（now_ms 相同时）。

    规则（design §2.3）：
      fav / hidden / meta：LWW 寄存器，ts 大者胜，ts 相同则 device_id 字典序大者胜
                           （仍相同则按值的规范化 JSON 比较，保证全序）；
      rekeyed_from：并集；
      progress：按 book_id，updated 大者胜，平局 device 大者胜；
      未知字段：不参与合并，取规范化 JSON 最大者（确定性，且满足上面三条性质）。
    每份输入先过 validate_overlay：非法条目被跳过，超前 24 小时的 ts 截断为最旧值并记入 _clock。
    注意：未经校验的远端原始 JSON 请先用 validate_overlay(raw, device_id=<文件名>) 处理。"""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    entries: Dict[str, dict] = {}
    progress: Dict[str, dict] = {}
    clock = set()
    for ov in overlays:
        if isinstance(ov, dict) and ov.get("_view") is True:
            clean, _ = validate_overlay(ov, now_ms, view=True)
        else:
            clean, _ = validate_overlay(ov, now_ms)
        if clean is None:
            continue
        clock.update(clean["_clock"])
        for bid, ent in clean["entries"].items():
            cur = entries.setdefault(bid, {})
            for k, v in ent.items():
                if k in REG_FIELDS or k == "meta":
                    if k not in cur or _reg_key(v) > _reg_key(cur[k]):
                        cur[k] = v
                elif k == "rekeyed_from":
                    cur[k] = sorted(set(cur.get(k, ())) | set(v))[:MAX_REKEYED]
                else:
                    if k not in cur or _canon(v) > _canon(cur[k]):
                        cur[k] = v
        for bid, rec in clean["progress"].items():
            old = progress.get(bid)
            key = (rec["updated"], rec["device"], rec["percent"])
            if old is None or key > (old["updated"], old["device"], old["percent"]):
                progress[bid] = rec
    return {"version": WIRE_VERSION, "_view": True, "entries": entries, "progress": progress,
            "_clock": sorted(clock)}


# ================================================================== 覆盖层：本地编辑
def new_overlay(device_id: str, hostname: str = "", user: str = "") -> dict:
    """本设备的空覆盖层（视图形式，写者 = device_id）。"""
    dev = safe_component(device_id)
    if not dev:
        raise ValueError("bad device_id")
    ov = {"version": WIRE_VERSION, "_view": True, "device_id": dev, "entries": {}, "progress": {},
          "_clock": [], "_rev": 0}
    if hostname and _clean_str(hostname, 64):
        ov["hostname"] = _clean_str(hostname, 64)
    if user and safe_component(user):
        ov["user"] = safe_component(user)
    return ov


def effective_ts(view: Optional[dict], bid: str, name: str, now_ms: int) -> int:
    """视图里某寄存器的有效 ts；超过 now+24h 的视为最旧值（CLAMPED_TS）；没有则 0。"""
    reg = ((view or {}).get("entries", {}).get(bid) or {}).get(name)
    if isinstance(reg, (list, tuple)) and len(reg) >= 2:
        ts, _ = _parse_ts(reg[1], now_ms)
        return ts or 0
    return 0


def set_field(ov: dict, bid: str, name: str, value, *, merged: Optional[dict] = None,
              now_ms: Optional[int] = None, skew_ms: int = 0, meta: Optional[dict] = None) -> int:
    """本设备写一个字段（fav / hidden），返回写入的 ts。混合逻辑时钟：
         ts = max(now_ms + skew_ms, effective_ts(合并后的值) + 1)
    其中 effective_ts 取自 merged（含所有设备的合并视图；缺省则只看 ov 自身），所以用户的点击总能压过
    他当时看到的值；对端 ts 超过 now+24h 视为最旧值（坏时钟不能永久冻结字段，见 _parse_ts）。
    meta={"fp":..., "title":...}：若合并视图里还没有等值的 meta，则同时写一个 meta 寄存器。
    会让 ov["_rev"] += 1。参数非法抛 ValueError（编程错误，不是网络错误）。"""
    dev = safe_component(ov.get("device_id")) if isinstance(ov, dict) else None
    if not dev:
        raise ValueError("overlay has no device_id")
    if name not in REG_FIELDS:
        raise ValueError(f"unknown field {name!r}")
    if not isinstance(value, bool):
        raise ValueError("value must be bool")
    if not valid_book_id(bid):
        raise ValueError("bad book id")
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    now_ms = int(now_ms)
    base = now_ms + int(skew_ms)
    ents = ov.setdefault("entries", {})
    if bid not in ents and len(ents) >= MAX_ENTRIES:
        raise ValueError("too many overlay entries")

    def _eff(field_name):
        return max(effective_ts(ov, bid, field_name, base), effective_ts(merged, bid, field_name, base))

    ts = max(base, _eff(name) + 1)
    ent = ents.setdefault(bid, {})
    ent[name] = [value, ts, dev]
    if meta is not None:
        m = _clean_meta(meta)
        if m:
            cur = field_value(merged, bid, "meta")
            if cur is None:
                cur = field_value(ov, bid, "meta")
            if cur != m:
                ent["meta"] = [m, max(base, _eff("meta") + 1), dev]
    ov["_rev"] = int(ov.get("_rev", 0)) + 1
    return ts


def set_progress(ov: dict, bid: str, percent: float, updated: Optional[float] = None) -> None:
    """记录本设备对某本书的进度摘要（design §4.3 progress 段；来自主线程的阅读事件）。"""
    dev = safe_component(ov.get("device_id")) if isinstance(ov, dict) else None
    if not dev or not valid_book_id(bid):
        raise ValueError("bad overlay or book id")
    p = _num(percent)
    if p is None:
        raise ValueError("bad percent")
    prog = ov.setdefault("progress", {})
    if bid not in prog and len(prog) >= MAX_ENTRIES:
        raise ValueError("too many progress entries")
    upd = _num(updated if updated is not None else time.time())
    if upd is None:
        raise ValueError("bad updated")
    prog[bid] = {"percent": min(100.0, max(0.0, p)), "updated": upd, "device": dev}
    ov["_rev"] = int(ov.get("_rev", 0)) + 1


DIGEST_KEEP = 500                        # 设备文件里的进度摘要只留最近读过的这么多本（更早的靠逐书进度文件补缺）


def prune_progress(ov: dict, keep: int = DIGEST_KEEP) -> int:
    """进度摘要只保留 updated 最新的 keep 条（并列时 book_id 大者留下，保证确定性）。返回丢弃的条数；有丢弃才 _rev += 1。"""
    prog = ov.get("progress") if isinstance(ov, dict) else None
    if not isinstance(prog, dict) or len(prog) <= keep:
        return 0
    ranked = sorted(prog, key=lambda b: (_num((prog[b] or {}).get("updated"), 0.0), b), reverse=True)
    drop = ranked[max(0, keep):]
    for b in drop:
        del prog[b]
    ov["_rev"] = int(ov.get("_rev", 0)) + 1
    return len(drop)


def continue_candidate(books) -> Optional[Book]:
    """「继续阅读」条显示哪一本：可见的、状态为「在读」的书里最近读过的一本（任一设备；last_read 已含
    各设备进度摘要的 updated）。没有则 None。并列时 book_id 大者胜，保证确定性。"""
    best, best_key = None, None
    for b in (books.values() if isinstance(books, dict) else books):
        if not _visible(b) or b.external or b.status != "reading" or b.last_read <= 0:
            continue
        key = (b.last_read, b.id)
        if best_key is None or key > best_key:
            best, best_key = b, key
    return best


# ================================================================== 上传白名单
def to_wire(ov: dict, device_id: Optional[str] = None) -> dict:
    """上传内容只能由本函数构造：白名单 version / device_id / hostname / user / entries{meta,fav,hidden} / progress。
    以 _ 开头的键、未知字段、rekeyed_from、本机路径、配置里的任何东西一律不出现。
    只输出写者为本设备的寄存器 / 进度（别的设备的值由它自己的文件负责）。"""
    dev = safe_component(device_id if device_id is not None else (ov or {}).get("device_id"))
    if not dev:
        raise ValueError("overlay has no device_id")
    wire = {"version": WIRE_VERSION, "device_id": dev}
    h = _clean_str(ov.get("hostname"), 64)
    if h:
        wire["hostname"] = h
    u = safe_component(ov.get("user")) if ov.get("user") is not None else None
    if u:
        wire["user"] = u
    ents: Dict[str, dict] = {}
    src = ov.get("entries") if isinstance(ov.get("entries"), dict) else {}
    for bid in sorted(k for k in src if isinstance(k, str)):
        ent = src[bid]
        if not valid_book_id(bid) or not isinstance(ent, dict):
            continue
        out = {}
        for fname in REG_FIELDS:
            reg = ent.get(fname)
            if (isinstance(reg, (list, tuple)) and len(reg) >= 2 and isinstance(reg[0], bool)
                    and isinstance(reg[1], int) and not isinstance(reg[1], bool) and reg[1] > 0
                    and (len(reg) < 3 or reg[2] == dev)):
                out[fname] = [reg[0], reg[1]]
        reg = ent.get("meta")
        if (isinstance(reg, (list, tuple)) and len(reg) >= 2 and isinstance(reg[1], int)
                and not isinstance(reg[1], bool) and reg[1] > 0 and (len(reg) < 3 or reg[2] == dev)):
            m = _clean_meta(reg[0])
            if m:
                out["meta"] = [m, reg[1]]
        if out:
            ents[bid] = out
    wire["entries"] = ents
    prog: Dict[str, dict] = {}
    psrc = ov.get("progress") if isinstance(ov.get("progress"), dict) else {}
    for bid in sorted(k for k in psrc if isinstance(k, str)):
        rec = psrc[bid]
        if not valid_book_id(bid) or not isinstance(rec, dict) or rec.get("device", dev) != dev:
            continue
        pct, upd = _num(rec.get("percent")), _num(rec.get("updated"))
        if pct is None or upd is None or upd <= 0:
            continue
        prog[bid] = {"percent": min(100.0, max(0.0, pct)), "updated": upd}
    wire["progress"] = prog
    return wire


def wire_bytes(ov: dict, device_id: Optional[str] = None) -> bytes:
    """to_wire 的规范化 UTF-8 JSON（键排序）。"""
    return json.dumps(to_wire(ov, device_id), sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8")


def wire_hash(ov: dict, device_id: Optional[str] = None) -> str:
    """上传内容的 sha1（state.json 的 pushed_hash）。"""
    return hashlib.sha1(_canon(to_wire(ov, device_id)).encode("utf-8")).hexdigest()


# ================================================================== 本机存储（~/.pyreader/shelf/）
def shelf_dir(root: Optional[str] = None) -> str:
    return os.path.join(root or _default_root(), "shelf")


def state_path(root=None) -> str:
    return os.path.join(shelf_dir(root), "state.json")


def overlay_path(root=None) -> str:
    return os.path.join(shelf_dir(root), "overlay.json")


def index_path(root=None) -> str:
    return os.path.join(shelf_dir(root), "index.json")


def peers_dir(root=None) -> str:
    return os.path.join(shelf_dir(root), "peers")


def new_device_id() -> str:
    """hostname + 6 位随机十六进制；首次生成后固定。"""
    return f"{_hostname()}-{secrets.token_hex(3)}"


DEFAULT_UI = {"view": "grid", "sort": "last_read", "collapsed": [], "last_shelf": "all", "start_in_shelf": False}


def _int(x, default, lo=None, hi=None):
    if isinstance(x, bool) or not isinstance(x, int):
        return default
    if lo is not None and x < lo:
        return default
    if hi is not None and x > hi:
        return default
    return x


def _clean_ui(ui) -> dict:
    ui = ui if isinstance(ui, dict) else {}
    out = dict(DEFAULT_UI)
    if ui.get("view") in ("grid", "list"):
        out["view"] = ui["view"]
    if ui.get("sort") in SORT_KEYS:
        out["sort"] = ui["sort"]
    if isinstance(ui.get("collapsed"), list):
        out["collapsed"] = [s[:MAX_STR] for s in ui["collapsed"] if isinstance(s, str)][:500]
    if isinstance(ui.get("last_shelf"), str) and ui["last_shelf"]:
        out["last_shelf"] = ui["last_shelf"][:MAX_STR]
    if isinstance(ui.get("start_in_shelf"), bool):
        out["start_in_shelf"] = ui["start_in_shelf"]
    return out


def _clean_stamp(st):
    if not isinstance(st, dict):
        return None
    mt, sz = _num(st.get("mtime")), _int(st.get("size"), None, 0)
    if mt is None or sz is None:
        return None
    out = {"mtime": mt, "size": sz}
    if isinstance(st.get("etag"), str):
        out["etag"] = st["etag"][:256]
    ft = _num(st.get("fetched_at"))
    if ft is not None:
        out["fetched_at"] = ft                   # racy 规则要用：抓取时刻（服务器时钟域）
    return out


def _clean_cursors(cur) -> dict:
    cur = cur if isinstance(cur, dict) else {}
    peers = {}
    for k, v in (cur.get("peers") if isinstance(cur.get("peers"), dict) else {}).items():
        if safe_component(k) and isinstance(v, dict):
            st = _clean_stamp(v)
            if st:
                ft = _num(v.get("fetched_at"))
                if ft is not None:
                    st["fetched_at"] = ft
                peers[k] = st
    return {"peers": peers, "rounds_since_full": _int(cur.get("rounds_since_full"), 0, 0)}


MAX_MISSING = 5000


def _clean_cloud(cl) -> dict:
    """state.json 的 cloud 段：{"missing": {book_id: {"n": 连续缺失的完整列表轮数, "size", "mtime"}}}。
    只收 remote: 的合法 id、n 为 1..99 的整数；坏条目逐条丢弃。"""
    cl = cl if isinstance(cl, dict) else {}
    out = {}
    raw = cl.get("missing") if isinstance(cl.get("missing"), dict) else {}
    for bid, v in list(raw.items())[:MAX_MISSING]:
        if not isinstance(bid, str) or not bid.startswith("remote:") or not valid_book_id(bid) or not isinstance(v, dict):
            continue
        n = _int(v.get("n"), 0, 1, 99)
        size = _int(v.get("size"), 0, 0)
        mt = _num(v.get("mtime"), 0.0)
        if n:
            out[bid] = {"n": n, "size": size, "mtime": mt}
    return {"missing": out}


MAX_UNPUSHED = 5000


def _clean_ids(x) -> List[str]:
    """state.json 的 unpushed 段：本设备改过、还没推送的 book_id（橙点用）。坏条目逐条丢弃。"""
    if not isinstance(x, list):
        return []
    out, seen = [], set()
    for b in x:
        if isinstance(b, str) and b not in seen and valid_book_id(b):
            seen.add(b)
            out.append(b)
        if len(out) >= MAX_UNPUSHED:
            break
    return out


def default_state(device_id: Optional[str] = None) -> dict:
    return {"version": STATE_VERSION, "device_id": device_id or new_device_id(), "clock_skew_ms": 0,
            "confirmed_upload": False, "pushed_rev": 0, "pushed_hash": "", "remote_stamp": None,
            "ui": dict(DEFAULT_UI, collapsed=[]), "cursors": {"peers": {}, "rounds_since_full": 0},
            "cloud": {"missing": {}}, "unpushed": [], "reset_pending": False}


def load_state(root: Optional[str] = None) -> dict:
    """读 state.json：缺失字段补默认值、类型不对的字段回退默认；损坏文件被 load_json 隔离为 .bad-<ts>。
    首次生成 device_id 后立即落盘（固定下来）。主版本高于本程序时返回 _readonly=True，save_state 将拒绝写。
    未知字段原样保留。"""
    path = state_path(root)
    raw = load_json(path, None, dict)
    if raw is None:
        st = default_state()
        try:
            save_json(path, _persist_form(st))
        except OSError:
            pass
        return st
    ver = raw.get("version", STATE_VERSION)
    st = {k: v for k, v in raw.items() if isinstance(k, str) and not k.startswith("_")}
    dev = safe_component(raw.get("device_id"))
    fresh = dev is None
    st["version"] = _int(ver, STATE_VERSION, 1)
    st["device_id"] = dev or new_device_id()
    st["clock_skew_ms"] = _int(raw.get("clock_skew_ms"), 0, -FUTURE_LIMIT_MS, FUTURE_LIMIT_MS)
    st["confirmed_upload"] = raw.get("confirmed_upload") is True
    st["pushed_rev"] = _int(raw.get("pushed_rev"), 0, 0)
    st["pushed_hash"] = raw["pushed_hash"][:64] if isinstance(raw.get("pushed_hash"), str) else ""
    st["remote_stamp"] = _clean_stamp(raw.get("remote_stamp"))
    st["ui"] = _clean_ui(raw.get("ui"))
    st["cursors"] = _clean_cursors(raw.get("cursors"))
    st["cloud"] = _clean_cloud(raw.get("cloud"))
    st["unpushed"] = _clean_ids(raw.get("unpushed"))
    st["reset_pending"] = raw.get("reset_pending") is True
    if st["version"] > STATE_VERSION:
        st["_readonly"] = True               # 更高主版本：只读不写
    elif fresh:
        try:
            save_json(path, _persist_form(st))
        except OSError:
            pass
    return st


def _persist_form(st: dict) -> dict:
    return {k: v for k, v in st.items() if not k.startswith("_")}


def save_state(st: dict, root: Optional[str] = None) -> bool:
    """原子写 state.json（不写 _ 开头的内存字段）。只读状态返回 False。"""
    if st.get("_readonly"):
        return False
    save_json(state_path(root), _persist_form(st))
    return True


def load_overlay(root: Optional[str] = None, device_id: Optional[str] = None, hostname: str = "", user: str = "") -> dict:
    """读本设备的 overlay.json；不存在视为空（version=1）。损坏文件被隔离后返回空覆盖层；
    主版本过高返回空覆盖层并带 _readonly=True（save_overlay 拒绝写）。device_id 缺省取 state.json 的。"""
    dev = device_id or load_state(root)["device_id"]
    raw = load_json(overlay_path(root), None, dict)
    if raw is None:
        return new_overlay(dev, hostname or _hostname(), user)
    clean, _issues = validate_overlay(raw, device_id=dev)
    if clean is None:
        ver = raw.get("version")
        ov = new_overlay(dev, hostname or _hostname(), user)
        if isinstance(ver, int) and not isinstance(ver, bool) and ver > WIRE_VERSION:
            ov["_readonly"] = True
        return ov
    clean.setdefault("hostname", hostname or _hostname())
    if user and "user" not in clean and safe_component(user):
        clean["user"] = safe_component(user)
    clean["_rev"] = 0
    return clean


def save_overlay(ov: dict, root: Optional[str] = None) -> bool:
    """原子写 overlay.json（内容 = to_wire，同时就是上传内容）。只读状态返回 False。"""
    if ov.get("_readonly"):
        return False
    save_json(overlay_path(root), to_wire(ov))
    return True


# ================================================================== 阶段 2：封面 / 进度快照 / index.json 缓存
INDEX_VERSION = 1
MAX_PATH_STR = 1024                       # index.json 里本地路径的长度上限
MAX_LOCAL_PATHS = 20                      # 一本书（同名同大小拷贝）最多缓存的路径数
MAX_NOTES = SCAN_LIMIT                    # 本机记录的「打开时间 / 进度百分比」条数上限（超出保留最新的）
_WHERE = ("local", "synced", "cloud", "removed", "missing")


def _is_cjk(c: str) -> bool:
    o = ord(c)
    return 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF


def cover_hsl(fp_hint: str, title: str = "") -> tuple:
    """色块封面的颜色：由 fp（缺省退回书名）的哈希确定性地取 (色相 0-359, 饱和度, 明度)。
    明度压在中低区间，保证白色书名在任何色相上都可读。"""
    seed = fp_hint if isinstance(fp_hint, str) and fp_hint else _nfc(title or "?")
    d = hashlib.sha1(seed.encode("utf-8", "replace")).digest()
    hue = int.from_bytes(d[:2], "big") % 360
    sat = 90 + d[2] % 60                  # 90..149（/255）
    light = 70 + d[3] % 40                # 70..109（/255）
    return hue, sat, light


def cover_glyph(title: str) -> str:
    """封面上的大字：CJK 书名取前两字；拉丁书名取前几个单词的首字母（最多 3 个，大写）。"""
    t = "".join(c for c in _nfc(title or "") if unicodedata.category(c) not in ("Cc", "Cs") and c not in _BIDI).strip()
    if not t:
        return "?"
    if _is_cjk(t[0]):
        return t[:2]
    words = re.findall(r"[^\W_]+", t)
    if not words:
        return t[:2]
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words[:3]).upper()


def bookmark_snapshot(bookmarks) -> Dict[str, dict]:
    """给后台线程用的书签快照：只取 _progress_ / _progress_ts_ 与媒体进度字段（主线程拷贝，之后不再共享可变的 bookmarks）。"""
    out: Dict[str, dict] = {}
    if not isinstance(bookmarks, dict):
        return out
    for k, v in list(bookmarks.items()):
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = {"_progress_": v.get("_progress_"), "_progress_ts_": v.get("_progress_ts_")}
            for f in MEDIA_PROGRESS_KEYS:            # 应用内媒体的进度字段（音频毫秒 / 漫画页 / 0..1 比例）
                if f in v:
                    out[k][f] = v[f]
    return out


def progress_overlay(progress: Optional[dict], entries: Optional[dict] = None) -> dict:
    """把本机记录的进度百分比 {book_id: {percent, updated, device}} 包成 build_books 认的「覆盖层视图」。
    不是覆盖层本身（不写 overlay.json、不上传）。阶段 5 起 entries 传入合并视图里的 fav / hidden / meta 条目。"""
    return {"version": WIRE_VERSION, "_view": True, "entries": dict(entries or {}), "progress": dict(progress or {})}


def note_opened(b: Book, ts: float) -> None:
    """记一次「打开」：last_opened 与 last_read 取较大者（就地更新，不重建索引）。外部格式不记。"""
    if b.external:
        return
    b.last_opened = max(b.last_opened, float(ts))
    b.last_read = max(b.last_read, b.last_opened)


def note_progress(b: Book, percent: float, updated: float, device: str = "") -> bool:
    """记一次阅读进度（就地更新）。比已有记录旧则忽略，返回是否生效。外部格式不记进度。"""
    if b.external:
        return False
    p, u = _num(percent), _num(updated)
    if p is None or u is None or p < 0:
        return False
    if u < b.progress_updated:
        return False
    b.percent = min(100.0, p)
    b.progress_updated = u
    b.progress_device = _clean_str(device, 64) or ""
    b.has_progress = b.has_progress or p > 0
    b.last_read = max(b.last_read, u)
    return True


def _trim_notes(d: dict, key) -> dict:
    """只保留最新的 MAX_NOTES 条（key(v) 取排序用的时间）。"""
    if len(d) <= MAX_NOTES:
        return dict(d)
    keep = sorted(d.items(), key=lambda kv: -key(kv[1]))[:MAX_NOTES]
    return dict(keep)


def index_to_wire(books: Dict[str, Book], opened: Optional[dict] = None, progress: Optional[dict] = None,
                  built: Optional[float] = None) -> dict:
    """派生索引（可随时删除、可重建）。写本机有文件的书，以及云端列表里的书（cloud / removed，没有本地路径）——
    后者让离线启动时仍能显示云端书库；opened / progress 是本机独有的记录
    （书籍扫描会整体替换 books，这两份不能从扫描里重建，所以随索引一起落盘）。"""
    entries = {}
    for bid, b in sorted(books.items()):
        if not valid_book_id(bid) or not (b.local_paths or (b.remote_rel and b.where in ("cloud", "removed"))):
            continue
        e = {"fp": b.fp, "title": b.title, "format": b.fmt, "size": int(b.size), "shelf": b.shelf,
             "where": b.where, "mtime": float(b.mtime), "local": list(b.local_paths[:MAX_LOCAL_PATHS]),
             "has_progress": bool(b.has_progress), "last_read": float(b.last_read)}
        if b.remote_rel:
            e["remote"] = {"rel": b.remote_rel}
        entries[bid] = e
    op = _trim_notes({k: float(v) for k, v in (opened or {}).items() if valid_book_id(k) and _num(v) is not None},
                     lambda v: v)
    pg = {}
    for k, v in (progress or {}).items():
        if not valid_book_id(k) or not isinstance(v, dict):
            continue
        p, u = _num(v.get("percent")), _num(v.get("updated"))
        if p is None or u is None:
            continue
        pg[k] = {"percent": min(100.0, max(0.0, p)), "updated": u, "device": _clean_str(v.get("device"), 64) or ""}
    pg = _trim_notes(pg, lambda v: v["updated"])
    return {"version": INDEX_VERSION, "built": float(built if built is not None else time.time()),
            "books": entries, "opened": op, "progress": pg}


def _clean_path(p) -> Optional[str]:
    if not isinstance(p, str) or not p or len(p) > MAX_PATH_STR or not _encodable(p) or "\x00" in p:
        return None
    return p


def index_from_wire(raw):
    """校验并还原 index.json：返回 (books, opened, progress)；无法使用（类型不对 / 主版本更高）返回 None。
    坏条目逐条跳过，不抛异常；还原出的书一律没有 same_as（那是扫描的产物）。"""
    if not isinstance(raw, dict):
        return None
    ver = raw.get("version")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1 or ver > INDEX_VERSION:
        return None
    books: Dict[str, Book] = {}
    ents = raw.get("books")
    if isinstance(ents, dict):
        for bid, e in list(ents.items())[:MAX_NOTES]:
            if not valid_book_id(bid) or not isinstance(e, dict):
                continue
            lp = e.get("local") if isinstance(e.get("local"), list) else []
            paths = [q for q in (_clean_path(p) for p in lp[:MAX_LOCAL_PATHS]) if q]
            title = _clean_str(e.get("title"))
            size = e.get("size")
            where = e.get("where") if e.get("where") in _WHERE else "local"
            rem = e.get("remote") if isinstance(e.get("remote"), dict) else {}
            rel = rem.get("rel") if valid_remote_rel(rem.get("rel")) else ""
            if not title or not (paths or (rel and bid == "remote:" + rel and where in ("cloud", "removed"))):
                continue                         # 没有本地路径的只能是云端列表里的书
            b = Book(bid, title, _clean_str(e.get("format"), 16) or "",
                     size if isinstance(size, int) and not isinstance(size, bool) and 0 <= size < 2 ** 53 else 0,
                     _clean_str(e.get("shelf"), MAX_PATH_STR) or "", _clean_str(e.get("fp"), 64) or "", where, rel,
                     _num(e.get("mtime"), 0.0), paths)
            b.has_progress = e.get("has_progress") is True
            b.last_read = max(0.0, _num(e.get("last_read"), 0.0))
            books[bid] = b
    opened: Dict[str, float] = {}
    if isinstance(raw.get("opened"), dict):
        for k, v in list(raw["opened"].items())[:MAX_NOTES]:
            f = _num(v)
            if valid_book_id(k) and f is not None and f > 0:
                opened[k] = f
    progress: Dict[str, dict] = {}
    if isinstance(raw.get("progress"), dict):
        for k, v in list(raw["progress"].items())[:MAX_NOTES]:
            if not valid_book_id(k) or not isinstance(v, dict):
                continue
            p, u = _num(v.get("percent")), _num(v.get("updated"))
            if p is None or u is None or p < 0:
                continue
            progress[k] = {"percent": min(100.0, p), "updated": u, "device": _clean_str(v.get("device"), 64) or ""}
    for bid, b in books.items():             # 进度百分比是独立缓存：还原到书上
        if b.external:                       # 外部格式不还原进度 / 打开时间（旧缓存里的 last_read 也清掉）
            b.last_read = 0.0
            continue
        rec = progress.get(bid)
        if rec:
            b.percent, b.progress_updated, b.progress_device = rec["percent"], rec["updated"], rec["device"]
            b.last_read = max(b.last_read, rec["updated"])
        if bid in opened:
            b.last_opened = opened[bid]
            b.last_read = max(b.last_read, b.last_opened)
    return books, opened, progress


def load_index(root: Optional[str] = None):
    """读 index.json。不存在 → None；损坏 → 被 load_json 隔离为 .bad-<ts> 后返回 None（索引可重建，不丢数据）；
    主版本更高 → 返回 None，且 index_readonly(root) 为真。"""
    raw = load_json(index_path(root), None, dict)
    return index_from_wire(raw) if raw is not None else None


def index_readonly(root: Optional[str] = None) -> bool:
    """磁盘上的 index.json 来自更高主版本：本程序只读不写（避免降级覆盖）。"""
    try:
        with open(index_path(root), "rb") as f:
            raw = json.loads(f.read().decode("utf-8-sig"))
    except (OSError, ValueError):
        return False
    ver = raw.get("version") if isinstance(raw, dict) else None
    return isinstance(ver, int) and not isinstance(ver, bool) and ver > INDEX_VERSION


def save_index(books, opened=None, progress=None, root: Optional[str] = None) -> bool:
    """原子写 index.json。更高主版本的文件不覆盖，返回 False。"""
    if index_readonly(root):
        return False
    save_json(index_path(root), index_to_wire(books, opened, progress))
    return True


def reset_index(root: Optional[str] = None) -> None:
    """删除 index.json（派生缓存）。其它书架文件（state.json / overlay.json / peers/）不动。"""
    try:
        os.remove(index_path(root))
    except OSError:
        pass


# ================================================================== 阶段 3：首次打开云端书缓存副本时种入本地进度
def progress_seed(bookmarks, copy_path: str) -> Optional[dict]:
    """用户在本机另一个路径上读过同一个文件、现在第一次打开它的云端缓存副本（remote_books/<rel>）时，
    把本地进度带过来（design §4.9）：返回 {"_progress_": 偏移, "_progress_ts_": 时间, "source": 来源路径}，否则 None。

    只在副本自己还没有进度（bookmarks 里没有或 _progress_ <= 0）时种入。来源必须是 bookmarks 里
    文件名（NFC）相同、不在 remote_books 内、文件仍存在、大小相同、强指纹 fp 相同且 _progress_ 为有限正数的路径；
    多个来源取 _progress_ts_ 最新者。只读，不改 bookmarks；写入由调用者做。"""
    if not isinstance(bookmarks, dict) or not isinstance(copy_path, str):
        return None
    sync = _sync()
    if sync._remote_rel(copy_path) is None or not os.path.isfile(copy_path):
        return None
    own = bookmark_for(bookmarks, copy_path)
    if own and (_num(own.get("_progress_"), 0.0) or 0.0) > 0:
        return None
    try:
        size = os.path.getsize(copy_path)
    except OSError:
        return None
    base, mine = _nfc(os.path.basename(copy_path)), norm_path(copy_path)
    copy_fp = None
    best = None
    for k, v in list(bookmarks.items()):
        if not isinstance(k, str) or not isinstance(v, dict) or not k:
            continue
        if _nfc(os.path.basename(k.replace("\\", "/"))) != base or norm_path(k) == mine:
            continue
        off = _num(v.get("_progress_"))
        if off is None or off <= 0 or off >= 2 ** 53 or sync._remote_rel(k) is not None:
            continue
        ts = _num(v.get("_progress_ts_"), 0.0)
        if best is not None and ts <= best["_progress_ts_"]:
            continue
        try:
            if not os.path.isfile(k) or os.path.getsize(k) != size:
                continue
        except OSError:
            continue
        if copy_fp is None:
            copy_fp = fp(copy_path)
            if copy_fp is None:
                return None
        if fp(k) != copy_fp:
            continue
        best = {"_progress_": int(off), "_progress_ts_": ts, "source": k}
    return best


# ================================================================== 阶段 5：覆盖层同步（自愈 / 单实例锁 / 重置）
def apply_flags(books, merged: Optional[dict]) -> int:
    """把合并视图里的 fav / hidden 就地写到 Book 上（冷启动用 index.json 缓存画界面时、编辑后立即生效）。返回被改动的书数。"""
    n = 0
    for b in (books.values() if isinstance(books, dict) else books):
        fav = bool(field_value(merged, b.id, "fav", False))
        hid = bool(field_value(merged, b.id, "hidden", False))
        if b.fav != fav or b.hidden != hid:
            b.fav, b.hidden = fav, hid
            n += 1
    return n


def content_key(ov: dict, device_id: Optional[str] = None) -> str:
    """覆盖层「会被上传的内容」（entries + progress，不含 hostname 等元信息）的哈希。
    用来比较「远端文件」与「本地」是否一致：只有 hostname 不同不算不一致（避免克隆设备互相覆盖元信息而死循环）。"""
    w = to_wire(ov, device_id if device_id is not None else ov.get("device_id"))
    return hashlib.sha1(_canon([w["entries"], w["progress"]]).encode("utf-8")).hexdigest()


def has_content(ov: Optional[dict]) -> bool:
    """本设备覆盖层里有没有任何会上传的东西（条目或进度摘要）。"""
    return bool(ov) and bool(ov.get("entries") or ov.get("progress"))


def reconcile_own(local: dict, remote: dict, now_ms: Optional[int] = None):
    """自愈（design §4.3 步骤 3）：本设备自己的远端文件 remote（validate_overlay(device_id=本设备) 的结果）与本地 local 比。
    返回 (adopted, behind)：
      adopted  远端有、本地没有（或更新）的值并进本地之后的新覆盖层（local 的拷贝）；本地不需要变则为 None。
               典型：device_id 被克隆，另一台机器写了同一个文件。进度摘要并入后仍按 DIGEST_KEEP 修剪（避免来回拉锯）。
      behind   远端缺少或落后于（并入后的）本地的任何值：需要重新 PUT（服务器回滚、被误删后又被别处重建的旧内容等）。
    不修改 local（主线程拿到结果后自己决定怎么替换）。"""
    m = merge(local, remote, now_ms=now_ms)
    cand = dict(local)
    cand["entries"], cand["progress"], cand["_clock"] = m["entries"], m["progress"], m["_clock"]
    prune_progress(cand)
    changed = content_key(cand) != content_key(local)
    behind = content_key(cand) != content_key(remote, local.get("device_id"))
    return (cand if changed else None), behind


def reset_overlay(ov: dict) -> None:
    """「重置书架状态」：截断本设备覆盖层（保留 device_id / hostname / user）。"""
    ov["entries"], ov["progress"], ov["_clock"] = {}, {}, []
    ov["_rev"] = int(ov.get("_rev", 0)) + 1


def clear_peers(root: Optional[str] = None) -> int:
    """清空 peers/ 缓存（其它设备文件的只读拷贝）。返回删除的文件数。"""
    n = 0
    d = peers_dir(root)
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        try:
            os.remove(os.path.join(d, name))
            n += 1
        except OSError:
            pass
    return n


class ShelfLock:
    """~/.pyreader/shelf/.lock 的单实例锁（design §4.5）：OS 级独占锁（Windows msvcrt、其它 fcntl），进程退出时系统自动释放。
    取不到的实例以只读模式打开书架。"""

    def __init__(self, fh, path: str, kind: str):
        self._fh, self.path, self.kind = fh, path, kind

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if self.kind == "msvcrt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif self.kind == "fcntl":
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        if self.kind == "pid":
            try:
                os.remove(self.path)
            except OSError:
                pass

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pid_lock(path: str) -> Optional[ShelfLock]:
    """回退：PID 锁文件（OS 锁不可用时，例如文件系统不支持）。文件里的进程已不存在则视为残留，接管。"""
    for _ in range(2):
        try:
            fd = os.open(path + ".pid", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                with open(path + ".pid", "r", encoding="ascii") as f:
                    pid = int((f.read() or "0").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid) and pid != os.getpid():
                return None
            try:
                os.remove(path + ".pid")
            except OSError:
                return None
            continue
        except OSError:
            return None
        fh = os.fdopen(fd, "w", encoding="ascii")
        fh.write(str(os.getpid()))
        fh.flush()
        return ShelfLock(fh, path + ".pid", "pid")
    return None


def acquire_lock(root: Optional[str] = None) -> Optional[ShelfLock]:
    """取单实例锁；取不到（另一个进程 / 同进程里另一个持有者在用）返回 None。拿到后一直持有到 release() 或进程退出。"""
    import errno
    path = os.path.join(shelf_dir(root), ".lock")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a+b")
    except OSError:
        return _pid_lock(path)
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return ShelfLock(fh, path, "msvcrt")
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return ShelfLock(fh, path, "fcntl")
    except (ImportError, AttributeError):
        fh.close()
        return _pid_lock(path)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EWOULDBLOCK", errno.EAGAIN), 36):
            return None                                  # 别人持有
        return _pid_lock(path)                           # 其它错误（文件系统不支持锁等）：退回 PID 锁文件
