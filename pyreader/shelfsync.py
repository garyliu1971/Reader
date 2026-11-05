"""阶段 3：云端书库并入书架。设计见 c:\\tmp\\pyreader-bookshelf-design.md §3、§4.3 步骤 1、§4.8、§4.9、§6.3。

分三层，前两层是纯逻辑（不碰 Qt，用进程内 fake WebDAV 即可测试）：
  - fetch_library(client, prev_sizes, now)   列 <base>/library，名字校验 + 稳定性过滤（上传中的半截文件）
  - CloudCatalog                             云端已移除的判断：只信「完整」列表，且连续两轮完整列表都缺失
  - ShelfSyncWorker                          后台线程 + Signal：单飞的列表刷新、带 token 的下载

线程规则：所有网络都在后台线程；结果只经 Signal 回主线程；任何网络失败都不会抛进 UI（只发 Signal / 记日志）。
阶段 4 起新增三块（同样前两层是纯逻辑）：
  - pull_peers / load_cached_peers / digest_progress   其它设备的设备文件（进度摘要）：游标 + racy 规则 + peers/ 缓存
  - pull_progress_index                                 摘要里没有的书：小线程池逐书补缺，按文件体 book_id 校验
  - upload_book_safe                                    手动上传（.part + 完成校验 + MOVE / 退化 PUT，绝不静默覆盖）
阶段 5 起：本设备自己的设备文件也在每轮里核对并自愈（check_own_file：服务器回滚 / 被误删 / device_id 被克隆），
推送带读回校验（push_device_verified）；进度同步与推送两类任务共用一把 IO 锁，同一时间只有一个书架同步任务在碰设备文件。
上传永远是用户手动触发，没有任何自动上传。
"""
import json
import os
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from PySide6.QtCore import QObject, Signal

from . import shelf as sh
from . import sync as syncmod
from .config import _log_net
from .store import is_hidden_name, load_json, safe_component, safe_segment, save_json
from .sync import MAX_BOOK_BYTES, download_book, max_book_bytes
from .webdav import JSON_MAX_BYTES, MAX_FILES, WebDavClient, WebDavError, client_from_cfg, client_from_source, library_sources

LIBRARY = "library"
STABLE_SECONDS = 60          # mtime 距今不足这个秒数的文件视为「还在上传」
CLOCK_SLACK = 300            # mtime 比本机时钟超前超过这个秒数：当作服务器时钟偏差，忽略 mtime 只看大小变化
REMOVED_ROUNDS = 2           # 连续这么多轮「完整」列表都缺失才算云端已移除
MB = 1024 * 1024


# ================================================================== 列表
@dataclass
class Listing:
    files: List[sh.RemoteFile] = field(default_factory=list)    # 通过校验的全部云端书（含上传中的）
    unstable: Set[str] = field(default_factory=set)             # 上传中的 rel（不显示、不算指纹）
    complete: bool = False       # 无子树错误、未触到上限、library 目录存在：只有完整列表才能用来判断「消失」
    truncated: bool = False      # 触到 文件数 / 目录数 / 深度 上限
    errors: List[str] = field(default_factory=list)             # 读取失败的子目录
    skipped: int = 0             # 被名字过滤丢掉的条目数
    root_missing: bool = False   # 服务器上没有 library/ 目录
    fetched_at: float = 0.0

    @property
    def stable_files(self) -> List[sh.RemoteFile]:
        return [f for f in self.files if f.rel not in self.unstable]

    def problem(self) -> str:
        """列表不完整时给用户看的一句话（完整则为空）。多源时能区分「没 library 目录」「触上限」「某些源/子目录读失败」。"""
        if self.complete:
            return ""
        parts = []
        if self.root_missing:
            parts.append("云端没有 library 文件夹")
        if self.truncated:
            parts.append(f"书库超过读取上限（{MAX_FILES} 本），只显示了一部分")
        if self.errors:
            detail = "、".join(str(e)[:48] for e in self.errors[:2])
            more = f" 等 {len(self.errors)} 个" if len(self.errors) > 2 else ""
            parts.append(f"{len(self.errors)} 个来源/文件夹读取失败（{detail}{more}）")
        return "；".join(parts) or "云端列表不完整"


def _is_unstable(rf: sh.RemoteFile, prev_sizes: Optional[Dict[str, int]], now: float,
                 trusted: Optional[Dict[str, int]] = None) -> bool:
    """半截上传判断（design §4.3 步骤 1）：mtime 距今 < 60s，或大小与上一轮列表不同。
    trusted：本设备刚上传并核对过大小的 {rel: 大小}；大小仍相符就一定是完整文件（MOVE 会把 mtime 设成完成时刻，
    不加这一条，刚传完的那本会被当成「上传中」藏 60 秒）。"""
    if trusted and trusted.get(rf.rel) == rf.size:
        return False
    if rf.mtime and rf.mtime > 0:
        age = now - rf.mtime
        if -CLOCK_SLACK < age < STABLE_SECONDS:
            return True
    prev = (prev_sizes or {}).get(rf.rel)
    return prev is not None and prev != rf.size


def fetch_library(client: WebDavClient, prev_sizes: Optional[Dict[str, int]] = None,
                  now: Optional[float] = None, max_files: int = MAX_FILES,
                  trusted: Optional[Dict[str, int]] = None, prefix: str = "") -> Listing:
    """列 <base>/library。阻塞调用，只能在后台线程用。网络 / 协议错误抛 WebDavError（调用方负责接住）；
    library 目录不存在（404）不抛，返回 root_missing 的不完整列表。

    prefix：给每个 rel 加的前缀（多数据源时 = 源名，形如 源名/rel；主源为空字符串）。
    名字过滤：BOOK_EXTS、点 / ~$ 开头、rel 校验（valid_remote_rel）。稳定性：见 _is_unstable。"""
    now = time.time() if now is None else now
    try:
        tree = client.list_tree_ex(LIBRARY, max_files=max_files, accept=sh.valid_remote_rel)    # 非书文件不占 max_files 名额
    except WebDavError as e:
        if e.status == 404:
            return Listing(complete=False, root_missing=True, fetched_at=now)
        raise
    out: List[sh.RemoteFile] = []
    seen = set()
    skipped = tree.skipped
    for rel, ent in tree.files:
        rel = (prefix + "/" + rel) if prefix else rel
        if not sh.valid_remote_rel(rel) or rel in seen:
            skipped += 1
            continue
        seen.add(rel)
        out.append(sh.RemoteFile(rel, int(ent.size or 0), float(ent.mtime or 0.0)))
    out.sort(key=lambda f: f.rel)
    unstable = {f.rel for f in out if _is_unstable(f, prev_sizes, now, trusted)}
    return Listing(out, unstable, bool(tree.complete), bool(tree.truncated), list(tree.errors), skipped, False, now)


# ================================================================== 云端目录：已移除的判断
def _bid(rel: str) -> str:
    return "remote:" + rel


class CloudCatalog:
    """记住「云端有哪些书」，并按设计判断「云端已移除」。纯逻辑。

    known    最近一次列表里（含上传中的）见过的书 {book_id: RemoteFile}；冷启动时由 index.json 缓存灌入
    shown    当前应当作为「云端书」显示的（稳定的、或还不能下结论的）{book_id: RemoteFile}
    missing  连续缺失的完整列表轮数 {book_id: {"n", "size", "mtime"}}；n >= REMOVED_ROUNDS 即「云端已移除」

    规则：
      - 不完整的列表（子树失败 / 截断 / library 不存在）不产生任何「消失」结论：计数不动，书继续显示。
      - 只在完整列表里缺失才计数；再次出现（含 rclone 式「先复制、再删旧」的旧路径之外的新路径）不影响别的书。
      - 计数在书重新出现时清零；n >= 2 才标记，单轮缺失（包括单轮子树失败）永远不会标记。
    """

    def __init__(self, known: Optional[Dict[str, sh.RemoteFile]] = None, missing: Optional[dict] = None):
        self.known: Dict[str, sh.RemoteFile] = dict(known or {})
        self.missing: Dict[str, dict] = {k: dict(v) for k, v in (missing or {}).items()
                                         if isinstance(v, dict) and isinstance(v.get("n"), int)}
        for bid in self.missing:                     # 已在 missing 里的不再算「已知存在」
            self.known.pop(bid, None)
        self.shown: Dict[str, sh.RemoteFile] = dict(self.known)

    @staticmethod
    def _ghost(bid: str, meta: dict) -> sh.RemoteFile:
        return sh.RemoteFile(bid[7:], int(meta.get("size", 0) or 0), float(meta.get("mtime", 0.0) or 0.0))

    def apply(self, listing: Listing, extra: Optional[Dict[str, sh.RemoteFile]] = None) -> None:
        """吸收一轮列表。extra：调用方额外知道的、本机有缓存副本的 remote: 书（索引丢失时也能判断它们的移除）。"""
        prev_shown = set(self.shown)
        listed = {_bid(f.rel): f for f in listing.files}
        cands: Dict[str, sh.RemoteFile] = dict(self.known)
        for bid, meta in self.missing.items():
            cands.setdefault(bid, self._ghost(bid, meta))
        for bid, rf in (extra or {}).items():
            cands.setdefault(bid, rf)
        new_missing: Dict[str, dict] = {}
        carried: Dict[str, sh.RemoteFile] = {}
        for bid, rf in cands.items():
            if bid in listed:
                continue                             # 出现了（含上传中）：计数清零
            n = self.missing.get(bid, {}).get("n", 0)
            if listing.complete:
                n += 1
            elif n == 0:
                carried[bid] = rf                    # 不完整列表里没看到：不下结论，仍当作存在
            if n > 0:
                new_missing[bid] = {"n": min(n, 99), "size": rf.size, "mtime": rf.mtime}
        self.missing = new_missing
        self.known = dict(listed)
        self.known.update(carried)
        self.shown = {bid: f for bid, f in listed.items() if f.rel not in listing.unstable}
        for bid, rf in carried.items():
            if bid in prev_shown:
                self.shown[bid] = rf

    def display_files(self) -> List[sh.RemoteFile]:
        """build_books 的 remote_files：稳定的云端书 + 还不能下结论的「缺失过一轮」的书。"""
        out = dict(self.shown)
        for bid, meta in self.missing.items():
            if meta["n"] < REMOVED_ROUNDS:
                out.setdefault(bid, self._ghost(bid, meta))
        return sorted(out.values(), key=lambda f: f.rel)

    def removed_ids(self) -> Set[str]:
        return {bid for bid, meta in self.missing.items() if meta["n"] >= REMOVED_ROUNDS}

    def removed_files(self) -> List[sh.RemoteFile]:
        return [self._ghost(bid, meta) for bid, meta in sorted(self.missing.items()) if meta["n"] >= REMOVED_ROUNDS]

    def to_state(self) -> dict:
        """写进 state.json 的 cloud 段。"""
        return {"missing": {bid: dict(m) for bid, m in self.missing.items()}}


def known_from_books(books: Iterable[sh.Book]) -> Dict[str, sh.RemoteFile]:
    """冷启动：从 index.json 还原出的书里取云端书目（where 为 cloud / synced）作为「已知存在」。"""
    out = {}
    for b in books:
        if b.remote_rel and b.id == _bid(b.remote_rel) and b.where in ("cloud", "synced"):
            out[b.id] = sh.RemoteFile(b.remote_rel, b.size, b.mtime)
    return out


def extra_candidates(books: Iterable[sh.Book]) -> Dict[str, sh.RemoteFile]:
    """本机有缓存副本的 remote: 书（不管索引里有没有）：它们的移除也要能判断。"""
    out = {}
    for b in books:
        if b.remote_rel and b.id == _bid(b.remote_rel) and b.where in ("synced", "removed"):
            out[b.id] = sh.RemoteFile(b.remote_rel, b.size, b.mtime)
    return out


# ================================================================== 阶段 4：设备文件（进度摘要）
RACY_SECONDS = 2.0           # 文件 mtime 距「抓取时刻」不足这么久：同一秒内的第二次写入可能不改 mtime / size / etag，游标不可信
FULL_EVERY = 10              # 每这么多轮强制全量 GET 一次（兜底：服务器戳不可靠时，错过的更新最迟这时也会补上）
FILL_PER_ROUND = 50          # 摘要里没有的书，每轮最多补这么多本的逐书进度
FILL_THREADS = 5             # 补缺的并发数（4–6）
NEG_RETRY_SECONDS = 1800.0   # 逐书进度文件不存在（404 / 内容不符）的书，这么久之内不再探测
USER_ERR = "同步用户名不合法（只允许字母、数字、点、下划线、连字符）"


def device_dir(user: str) -> str:
    return f"users/{user}/shelf"


def device_file(user: str, device_id: str) -> str:
    return f"{device_dir(user)}/{device_id}.json"


def check_user(user) -> Optional[str]:
    """sync_user_name 拼进 users/<name>/ 之前必须过 safe_component（空 = default）。不合法返回 None。"""
    return safe_component(user or "default")


def server_now(headers: Optional[dict], fallback: Optional[float] = None) -> float:
    """服务器时间：取响应 Date 头（和 mtime 同一个时钟域，本机时钟偏差不会让 racy 判断失效）；没有则用本机时间。"""
    try:
        import email.utils
        d = (headers or {}).get("date")
        if d:
            return float(email.utils.parsedate_to_datetime(d).timestamp())
    except Exception:
        pass
    return time.time() if fallback is None else fallback


def stamp_of(ent) -> dict:
    return {"mtime": float(getattr(ent, "mtime", 0) or 0.0), "size": int(getattr(ent, "size", 0) or 0),
            "etag": str(getattr(ent, "etag", "") or "")}


def needs_fetch(cur: Optional[dict], ent) -> bool:
    """游标 (mtime, size, etag, fetched_at) 与服务器现在给的戳比：要不要重新 GET。

    必须重新 GET：没有游标；服务器什么戳都不给（没有 mtime 也没有 etag）；etag / mtime / size 任一不同；
    游标是 racy 的（上次抓取时刻 - 文件 mtime < RACY_SECONDS：当时同一秒内可能还有后续写入，戳不会变）。"""
    if not isinstance(cur, dict):
        return True
    st = stamp_of(ent)
    if not st["mtime"] and not st["etag"]:
        return True
    if (st["etag"] or cur.get("etag")) and (cur.get("etag") or "") != st["etag"]:
        return True
    if float(cur.get("mtime", 0) or 0) != st["mtime"] or int(cur.get("size", -1)) != st["size"]:
        return True
    fetched = cur.get("fetched_at")
    if not isinstance(fetched, (int, float)) or fetched - float(cur.get("mtime", 0) or 0) < RACY_SECONDS:
        return True
    return False


def peer_cache_path(root: Optional[str], dev: str) -> str:
    return os.path.join(sh.peers_dir(root), dev + ".json")


def _parse_json(body: bytes):
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def load_peer_view(root: Optional[str], dev: str) -> Optional[dict]:
    """读 peers/<dev>.json 并校验成视图；缺失 / 损坏（被隔离为 .bad-<ts>）/ 校验不过都返回 None。"""
    if safe_component(dev) is None:
        return None
    raw = load_json(peer_cache_path(root, dev), None, dict)
    if raw is None:
        return None
    clean, _ = sh.validate_overlay(raw, device_id=dev)
    return clean


def load_cached_peers(root: Optional[str] = None) -> Dict[str, dict]:
    """冷启动 / 离线：把 peers/ 里缓存的其它设备文件读成 {device_id: 视图}。设备 id 只取自通过校验的文件名。"""
    out: Dict[str, dict] = {}
    try:
        names = sorted(os.listdir(sh.peers_dir(root)))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and safe_component(n[:-5]):
            v = load_peer_view(root, n[:-5])
            if v is not None:
                out[n[:-5]] = v
    return out


def digest_progress(views: Dict[str, dict]) -> Dict[str, dict]:
    """各设备摘要合并成 {book_id: {percent, updated, device}}：updated 大者胜，平局设备 id 大者胜。
    device 显示用该设备文件里的 hostname（没有则用 device_id）。"""
    best: Dict[str, tuple] = {}
    for dev in sorted(views):
        v = views[dev] or {}
        label = sh._clean_str(v.get("hostname"), 64) or dev
        for bid, rec in (v.get("progress") or {}).items():
            key = (rec["updated"], dev)
            if bid not in best or key > best[bid][0]:
                best[bid] = (key, {"percent": rec["percent"], "updated": rec["updated"], "device": label})
    return {bid: v[1] for bid, v in best.items()}


@dataclass
class PeerResult:
    views: Dict[str, dict] = field(default_factory=dict)     # {device_id: 校验后的视图}（含本轮没重新 GET、取自缓存的）
    cursors: dict = field(default_factory=lambda: {"peers": {}, "rounds_since_full": 0})
    fetched: List[str] = field(default_factory=list)         # 本轮重新 GET 的设备
    unchanged: List[str] = field(default_factory=list)       # 游标可信、直接用缓存的设备
    failed: List[str] = field(default_factory=list)          # GET / 写缓存失败：保留上次好副本，游标不动
    corrupt: List[str] = field(default_factory=list)         # 内容坏了（不是 JSON / 校验不过）：跳过，游标前进（不反复 GET）
    dropped: List[str] = field(default_factory=list)         # 列表里不再出现：权威的「已删除」，缓存已丢弃
    skipped: int = 0                                         # 名字不合法的条目数
    requests: int = 0                                        # 本轮发出的请求数（1 次 PROPFIND + 每个重新抓取的设备 1 次 GET）
    own: Optional["OwnCheck"] = None                         # 本设备自己的设备文件的核对结果（check_own=True 才有）
    skew_ms: Optional[int] = None                            # 服务器 Date 头相对本机时钟的偏差（毫秒，含正负；没有 Date 头为 None）


@dataclass
class OwnCheck:
    """本设备自己的设备文件（users/<me>/shelf/<device_id>.json）这一轮的核对结果（design §4.3 步骤 2、3：自愈）。
    本函数只做读取与校验，不改任何东西；是否采纳 / 重推由主线程（覆盖层的所有者）用 shelf.reconcile_own 决定。"""
    listed: bool = False                 # PROPFIND 里列出了自己的文件
    missing: bool = False                # 没列出（404 / 被误删），而本地有内容：需要重推
    fetched: bool = False                # 本轮重新 GET 并校验了
    remote: Optional[dict] = None        # GET 到的内容（validate_overlay(device_id=本设备) 的视图）
    corrupt: bool = False                # 文件在，但内容不是合法的覆盖层：需要重推覆盖
    failed: bool = False                 # GET 失败：什么都不下结论，下一轮再看
    stamp: Optional[dict] = None         # 自己文件现在的戳（含 fetched_at）；没重新抓取时是传入的游标原样


def check_own_file(client: WebDavClient, user: str, own_device: str, own_ent, own_stamp: Optional[dict],
                   has_content: bool, fetch_time: float, force: bool = False) -> tuple:
    """-> (OwnCheck, 请求数)。own_ent：PROPFIND 里列出的自己的文件（None = 没列出）；own_stamp：上次确认过的戳
    （state.json 的 remote_stamp，含 fetched_at）。戳没变且游标可信（见 needs_fetch）就不 GET。"""
    if own_ent is None:
        return OwnCheck(listed=False, missing=bool(has_content)), 0
    if not force and not needs_fetch(own_stamp, own_ent):
        return OwnCheck(listed=True, stamp=own_stamp), 0
    st = dict(stamp_of(own_ent), fetched_at=float(fetch_time))
    try:
        r = client.get_ex(device_file(user, own_device), JSON_MAX_BYTES)
    except WebDavError as e:
        _log_net(f"[书架] 读取本设备文件失败: {e}")
        return OwnCheck(listed=True, failed=True), 1
    raw = _parse_json(r.body)
    clean, _issues = sh.validate_overlay(raw, device_id=own_device) if isinstance(raw, dict) else (None, ["not an object"])
    if clean is None:
        return OwnCheck(listed=True, fetched=True, corrupt=True, stamp=st), 1
    return OwnCheck(listed=True, fetched=True, remote=clean, stamp=st), 1


def pull_peers(client: WebDavClient, root: Optional[str], user: str, own_device: str, cursors: Optional[dict] = None,
               *, now: Optional[float] = None, force_full: bool = False, check_own: bool = False,
               own_stamp: Optional[dict] = None, own_has_content: bool = False) -> PeerResult:
    """design §4.3 步骤 2：PROPFIND users/<me>/shelf/，对每个其它设备的文件按游标决定是否 GET。阻塞，只能在后台线程用。

    - 设备 id 只取自通过 safe_component 的文件名（<id>.json），不取自 JSON 内字段；本设备自己的文件不读。
    - 先原子写 peers/<id>.json，再更新游标（顺序不可反）；GET 失败保留上次好副本、游标不动。
    - PROPFIND 成功且不再列出某设备 = 权威的「已删除」：丢弃它的 peers/ 缓存与游标；目录 404 = 一个设备文件都没有。
    - PROPFIND 本身失败（离线 / 5xx）抛 WebDavError，缓存与游标原样不动（调用方负责接住）。
    - 请求数上界：1 + 设备数（check_own 时再加自己的文件最多 1 次 GET）。
    - check_own：同一次 PROPFIND 里也核对本设备自己的文件（见 res.own）；自己的文件永远不进 views / peers/ 缓存。"""
    u = check_user(user)
    if u is None:
        raise WebDavError(USER_ERR)
    cursors = cursors if isinstance(cursors, dict) else {}
    peers_cur = {k: dict(v) for k, v in (cursors.get("peers") or {}).items() if safe_component(k) and isinstance(v, dict)}
    rounds = int(cursors.get("rounds_since_full", 0) or 0) + 1
    full = bool(force_full) or rounds >= FULL_EVERY
    res = PeerResult()
    hdr: dict = {}
    t_list = time.time() if now is None else now
    try:
        entries = client.propfind(device_dir(u), 1, headers_out=hdr)
    except WebDavError as e:
        if e.status != 404:
            raise
        entries = []                                      # 还没有任何设备文件
    res.requests += 1
    t_back = time.time()
    fetch_time = server_now(hdr, t_list) if "date" in hdr and now is None else t_list     # 与 mtime 同一时钟域
    if now is None and "date" in hdr:         # 可选增强（design §4.6）：服务器 Date 头 - 本机时钟；Date 只有秒级精度，调用方只在偏差够大时才采信
        res.skew_ms = int((server_now(hdr, t_back) - (t_list + t_back) / 2.0) * 1000)
    listed = {}
    own_ent = None
    for e in entries:
        if e.is_dir or not e.name.endswith(".json"):
            continue
        dev = safe_component(e.name[:-5])
        if dev is None:
            res.skipped += 1
            continue
        if dev != own_device:
            listed[dev] = e
        else:
            own_ent = e

    for dev in sorted(listed):
        ent = listed[dev]
        if not full and not needs_fetch(peers_cur.get(dev), ent):
            view = load_peer_view(root, dev)             # 游标可信：用缓存（缓存丢了就当没有游标）
            if view is not None:
                res.unchanged.append(dev)
                res.views[dev] = view
                continue
        try:
            r = client.get_ex(device_file(u, dev), JSON_MAX_BYTES)
        except WebDavError as e:
            res.requests += 1
            _log_net(f"[书架] 读取设备文件失败 {dev}: {e}")
            res.failed.append(dev)
            old = load_peer_view(root, dev)
            if old is not None:
                res.views[dev] = old                      # 保留上次好副本
            continue
        res.requests += 1
        raw = _parse_json(r.body)
        clean, _issues = sh.validate_overlay(raw, device_id=dev) if isinstance(raw, dict) else (None, ["not an object"])
        new_cur = dict(stamp_of(ent), fetched_at=float(fetch_time))
        if clean is None:
            _log_net(f"[书架] 设备文件 {dev}.json 内容无效，已跳过")
            res.corrupt.append(dev)
            peers_cur[dev] = new_cur                      # 同一份坏内容不重复抓取；文件一变戳就变，会再试
            old = load_peer_view(root, dev)
            if old is not None:
                res.views[dev] = old
            continue
        try:
            save_json(peer_cache_path(root, dev), raw)   # 先写缓存……
        except OSError as e:
            _log_net(f"[书架] 写入 peers 缓存失败 {dev}: {e}")
            res.failed.append(dev)
            continue
        peers_cur[dev] = new_cur                          # ……再更新游标
        res.fetched.append(dev)
        res.views[dev] = clean

    # 不再列出 = 权威的已删除
    try:
        cached = {n[:-5] for n in os.listdir(sh.peers_dir(root)) if n.endswith(".json") and safe_component(n[:-5])}
    except OSError:
        cached = set()
    for dev in sorted((cached | set(peers_cur)) - set(listed)):
        peers_cur.pop(dev, None)
        try:
            os.remove(peer_cache_path(root, dev))
        except OSError:
            pass
        res.dropped.append(dev)
    res.cursors = {"peers": peers_cur, "rounds_since_full": 0 if full else rounds}
    if check_own:
        res.own, n = check_own_file(client, u, own_device, own_ent, own_stamp, own_has_content, fetch_time, full)
        res.requests += n
    return res


# ================================================================== 阶段 4：摘要里没有的书，逐书补缺
def parse_progress_body(body: bytes, bid: str, now: Optional[float] = None) -> Optional[dict]:
    """逐书进度文件体 → {percent, updated, device}。文件体里的 book_id 必须等于期望的 id（永远不按文件名认书）；
    percent / updated 必须是有限数；updated 超过「现在 + 24 小时」视为坏记录。不合格返回 None。"""
    d = _parse_json(body)
    if not isinstance(d, dict) or d.get("book_id") != bid:
        return None
    pct, upd = sh._num(d.get("percent")), sh._num(d.get("updated"))
    now = time.time() if now is None else now
    if pct is None or upd is None or pct < 0 or upd <= 0 or upd > now + sh.FUTURE_LIMIT_MS / 1000.0:
        return None
    return {"percent": min(100.0, pct), "updated": upd, "device": sh._clean_str(d.get("device"), 64) or ""}


@dataclass
class FillResult:
    found: Dict[str, dict] = field(default_factory=dict)     # {book_id: {percent, updated, device}}，键是期望的 id（已核对文件体）
    absent: List[str] = field(default_factory=list)          # 服务器上没有这本书的进度文件（404）
    bad: List[str] = field(default_factory=list)             # 有文件但内容不合格（book_id 不符 / 不是 JSON / 数值非法）：忽略
    failed: List[str] = field(default_factory=list)          # 网络 / 服务器错误：下次再试
    requests: int = 0


def pull_progress_index(client: WebDavClient, user: str, bids: Iterable[str], *, limit: int = FILL_PER_ROUND,
                        threads: int = FILL_THREADS, now: Optional[float] = None) -> FillResult:
    """对不在任何设备摘要里的书（例如书架功能上线前读过的），用 sync.progress_path(user, id) 算路径，
    小线程池并发 GET（每本 1 次请求，404 不再补一次 exists），最多 limit 本。结果按文件体 book_id 校验，
    键是期望的 id，永远不是文件名。阻塞，只能在后台线程用。连续两批全部失败（离线）就提前放弃。"""
    u = check_user(user)
    if u is None:
        raise WebDavError(USER_ERR)
    n = max(1, int(threads))
    ids = [b for b in dict.fromkeys(bids) if sh.valid_book_id(b)][:max(0, int(limit))]
    res = FillResult()

    def one(bid):
        try:
            r = client.get_ex(syncmod.progress_path(u, bid), JSON_MAX_BYTES, ok_codes=(404,))
        except Exception:
            return bid, "failed", None
        if r.status == 404:
            return bid, "absent", None
        rec = parse_progress_body(r.body, bid, now)
        return (bid, "found", rec) if rec else (bid, "bad", None)

    failed_batches = 0
    with ThreadPoolExecutor(max_workers=n) as ex:
        for i in range(0, len(ids), n):
            out = list(ex.map(one, ids[i:i + n]))
            res.requests += len(out)
            for bid, kind, rec in out:
                if kind == "found":
                    res.found[bid] = rec
                elif kind == "absent":
                    res.absent.append(bid)
                elif kind == "bad":
                    res.bad.append(bid)
                else:
                    res.failed.append(bid)
            failed_batches = failed_batches + 1 if all(k == "failed" for _b, k, _r in out) else 0
            if failed_batches >= 2:
                break
    return res


def push_device_verified(client: WebDavClient, user: str, device_id: str, payload: bytes) -> tuple:
    """PUT users/<user>/shelf/<device_id>.json（只有本设备写这个文件），再回读比对（design §4.3 推送与自愈 2）。
    返回 (etag, 戳)：etag 取 PUT 响应（没有则 ""）；戳 = 回读时 PROPFIND 给出的 (mtime, size, etag) + fetched_at，
    调用方记成 remote_stamp，下一轮就能判断「服务器上的文件是不是我上次写的那份」。
    回读内容与 payload 不一致（被截断 / 别的写者同时写）抛 WebDavError，调用方不得记录为已推送。
    阻塞，只能在后台线程用。payload 只能是 shelf.wire_bytes(overlay)（to_wire 白名单）。"""
    u = check_user(user)
    if u is None or safe_component(device_id) is None:
        raise WebDavError(USER_ERR)
    client.mkdirs(device_dir(u))
    path = device_file(u, device_id)
    etag = client.put_ex(path, payload).headers.get("etag", "")
    hdr: dict = {}
    t0 = time.time()
    ents = client.propfind(device_dir(u), 1, headers_out=hdr)
    mine = next((e for e in ents if e.name == f"{device_id}.json" and not e.is_dir), None)
    if mine is None or (mine.size and mine.size != len(payload)):
        raise WebDavError("推送后校验失败：服务器上的设备文件与刚写入的不符")
    back = client.get_ex(path, JSON_MAX_BYTES).body
    if back != payload:
        raise WebDavError("推送后回读校验失败：服务器上的内容与刚写入的不一致（可能有别的写者），稍后重试")
    stamp = dict(stamp_of(mine), fetched_at=float(server_now(hdr, t0)))
    return etag, stamp


def push_device_file(client: WebDavClient, user: str, device_id: str, payload: bytes) -> str:
    """push_device_verified 的兼容包装：只返回 ETag。"""
    return push_device_verified(client, user, device_id, payload)[0]


# ================================================================== 阶段 4：手动上传（design §4.10）
STALE_PART_SECONDS = 900.0   # 别人的 .part 超过这么久没动（或是空文件）视为残留，可以覆盖
MOVE_UNSUPPORTED = (400, 403, 405, 415, 501)


@dataclass
class UploadResult:
    status: str                  # ok 成功 | exists 云端已有同名文件（未上传、未覆盖，请改名） | busy 别人正在上传同名文件 | error 失败
    rel: str = ""                # 目标 library 相对路径
    message: str = ""
    method: str = ""             # ok 时：move（.part 改名）| put（服务器不支持 MOVE，退化为直接 PUT）


class _ProgressReader:
    """包一层只读文件对象：每读一块就累计字节数并回调 progress(sent, total)。
    回调节流：百分比变化或距上次回调超过 0.3 秒才触发，读完（sent == total）一定触发一次；回调抛错不影响上传。"""

    def __init__(self, fh, total: int, progress: Callable[[int, int], None], clock=time.monotonic):
        self._fh, self._total, self._cb, self._clock = fh, int(total), progress, clock
        self._sent = 0
        self._last_pct = -1
        self._last_t = 0.0

    def read(self, n: int = -1):
        data = self._fh.read(n)
        self._sent += len(data)
        pct = self._sent * 100 // self._total if self._total > 0 else 100
        now = self._clock()
        if pct != self._last_pct and (now - self._last_t >= 0.3 or self._sent >= self._total):
            self._last_pct, self._last_t = pct, now
            try:
                self._cb(self._sent, self._total)
            except Exception:
                pass
        return data


def normalize_folder(folder) -> Optional[str]:
    """上传目标文件夹：'' = library 根（未分类）；否则 / 分隔、每段过 safe_segment 且不以 . 或 ~$ 开头，最多 8 层。
    返回规范化后的字符串，不合法返回 None。"""
    if folder is None:
        return ""
    if not isinstance(folder, str):
        return None
    f = folder.strip().strip("/")
    if not f:
        return ""
    segs = f.split("/")
    if len(segs) > 8 or any(safe_segment(x) is None or is_hidden_name(x) or x != x.strip() for x in segs):
        return None
    return "/".join(segs)


def upload_rel(folder, name) -> Optional[str]:
    """合法的上传目标 library 相对路径；名字 / 文件夹不合法、扩展名不在 BOOK_EXTS（电子书 / 有声书 / 漫画压缩包）内、.part 名会超长都返回 None。"""
    f = normalize_folder(folder)
    if f is None or not isinstance(name, str) or len(name) > 240 or name != name.strip() or safe_segment(name) is None:
        return None                                       # name 必须是单段名字：不能带 / \ : 等
    rel = f + "/" + name if f else name
    return rel if sh.valid_remote_rel(rel) else None


def _nkey(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _clash(ents: dict, name: str) -> bool:
    return any(_nkey(n) == _nkey(name) for n, e in ents.items() if not e.is_dir)


def _clear_part(client: WebDavClient, part: str) -> None:
    """尽力把残留的 .part 清空（不向用户暴露 DELETE）：空的 .part 下次上传时视为残留，可以直接覆盖。"""
    try:
        client.put(part, b"")
    except Exception:
        pass


def upload_book_safe(client: WebDavClient, path: str, folder, name: str, *, max_bytes: int = MAX_BOOK_BYTES,
                     now: Optional[float] = None,
                     progress: Optional[Callable[[int, int], None]] = None) -> UploadResult:
    """把本机文件 path 手动上传为 library/<folder>/<name>。阻塞，只能在后台线程用；永远不会静默覆盖已有文件。

    1. 目标（NFC + 忽略大小写比较）已存在 -> exists（不上传，调用方提示用户改名）。
    2. 流式 PUT 到 library/<folder>/.<name>.part（点开头：其它设备的列表忽略它）。.part 已存在且不是残留 -> busy；
       不存在时带 If-None-Match: *（服务器支持则两个上传者不会互相覆盖）。
    3. 校验 .part 的大小等于本机文件大小；再查一次目标是否出现了。
    4. MOVE（Overwrite: F）改成最终名；目标已存在（412）-> exists。服务器不支持 MOVE 时退化为：直接 PUT 目标
       （带 If-None-Match: *，不支持则只能事后校验），再把 .part 清空（不暴露 DELETE）。
    5. 事后核对最终文件大小。
    progress(sent, total)：上传字节进度回调（在调用线程里触发；退化为直接 PUT 时从 0 重新计）。
    所有网络 / 服务器错误都以 UploadResult(error) 返回，不外抛。"""
    now = time.time() if now is None else now
    rel = upload_rel(folder, name)
    if rel is None:
        return UploadResult("error", "", "名字或文件夹不合法（只允许电子书 / 有声书 / 漫画压缩包扩展名，不能含 / \\ : 等字符，不能以 . 开头）")
    f = normalize_folder(folder)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return UploadResult("error", rel, f"读不了本机文件：{e}")
    if size <= 0:
        return UploadResult("error", rel, "文件是空的")
    if size > max_bytes:
        return UploadResult("error", rel, f"文件超过上传上限（{max_bytes // (1024 * 1024)} MB）")
    ldir = "library/" + f if f else "library"
    part_name = "." + name + ".part"
    exists = UploadResult("exists", rel, f"云端已有同名文件：{rel}")
    busy = UploadResult("busy", rel, f"另一台设备正在上传同名文件：{rel}（稍后再试）")
    try:
        client.mkdirs(ldir)
        ents = {e.name: e for e in client.propfind(ldir, 1)}
        if _clash(ents, name):
            return exists
        part = ldir + "/" + part_name
        hdrs = {}
        old = ents.get(part_name)
        if old is None:
            hdrs["If-None-Match"] = "*"
        elif not (old.size == 0 or (old.mtime and now - old.mtime > STALE_PART_SECONDS)):
            return busy
        with open(path, "rb") as fh:
            r = client.put_file(part, _ProgressReader(fh, size, progress) if progress else fh, size, hdrs, ok_codes=(412,))
        if r.status == 412:
            return busy
        ents = {e.name: e for e in client.propfind(ldir, 1)}
        pe = ents.get(part_name)
        if pe is None or pe.size != size:
            return UploadResult("error", rel, "上传不完整（服务器上的大小与本机文件不符），请重试")
        if _clash(ents, name):
            _clear_part(client, part)
            return exists
        target = ldir + "/" + name
        method = "move"
        try:
            mr = client.move(part, target, overwrite=False, ok_codes=(412,))
            if mr.status == 412:
                _clear_part(client, part)
                return exists
        except WebDavError as e:
            if e.status not in MOVE_UNSUPPORTED:
                raise
            method = "put"                                # 服务器不支持 MOVE：退化为直接 PUT 目标
            with open(path, "rb") as fh:
                r = client.put_file(target, _ProgressReader(fh, size, progress) if progress else fh, size,
                                    {"If-None-Match": "*"}, ok_codes=(412,))
            _clear_part(client, part)
            if r.status == 412:
                return exists
        got = {e.name: e for e in client.propfind(ldir, 1)}.get(name)
        if got is None or got.is_dir or got.size != size:
            return UploadResult("error", rel, "上传后校验失败（云端文件的大小与本机不符），请重试")
        return UploadResult("ok", rel, f"已上传：{rel}", method)
    except WebDavError as e:
        _log_net(f"[书架] 上传失败 {rel}: {e}")
        return UploadResult("error", rel, f"上传失败：{str(e)[:120]}")
    except OSError as e:
        return UploadResult("error", rel, f"上传失败：{e}")


# ================================================================== 批量上传计划（纯函数，不碰网络）
@dataclass
class BatchPlan:
    items: List[tuple] = field(default_factory=list)                 # [(本机路径, 云端文件夹, 云端文件名)]
    total_bytes: int = 0
    skipped: Dict[str, List[str]] = field(default_factory=dict)      # 跳过原因 -> 文件名（同一原因里按出现顺序）

    def skip(self, why: str, name: str) -> None:
        self.skipped.setdefault(why, []).append(name)


def limit_text(max_bytes: int) -> str:
    """单个文件超限的统一说法（批量跳过原因 / 单本上传 / 下载共用），告诉用户限制来自哪个设置。"""
    return f"超过单个文件上限（{max_bytes // (1024 * 1024)} MB），可在 设置 > 同步与网络 里调大"


def plan_batch_upload(files: Iterable, cloud_rels: Iterable[str], max_bytes: int, prefix: str = "",
                      is_cached: Optional[Callable[[str], bool]] = None) -> BatchPlan:
    """把本机文件（sh.LocalFile）整理成批量上传计划：云端文件夹 = prefix/<书库根下的子文件夹>，保持本地目录结构。
    跳过：云端已有同名（NFC + 忽略大小写）、云端缓存副本、空文件、超过 max_bytes、名字 / 文件夹不合法、
    本批里目标重名。绝不会产生覆盖已有文件的条目（upload_book_safe 还会再确认一次）。"""
    plan = BatchPlan()
    have = {_nkey(r) for r in cloud_rels}
    seen: Set[str] = set()
    for lf in files:
        if is_cached is not None and is_cached(lf.path):
            plan.skip("已经是云端缓存副本", lf.name)
            continue
        folder = normalize_folder("/".join(x for x in (prefix, lf.sub) if x))
        rel = upload_rel(folder, lf.name) if folder is not None else None
        if rel is None:
            plan.skip("名字或文件夹不合法", lf.name)
        elif _nkey(rel) in have:
            plan.skip("云端已有同名文件", lf.name)
        elif _nkey(rel) in seen:
            plan.skip("本批里重名", lf.name)
        elif lf.size <= 0:
            plan.skip("文件是空的", lf.name)
        elif lf.size > max_bytes:
            plan.skip(limit_text(max_bytes), lf.name)
        else:
            seen.add(_nkey(rel))
            plan.items.append((lf.path, folder, lf.name))
            plan.total_bytes += lf.size
    return plan


@dataclass
class ProgressResult:
    """一轮进度同步的结果（后台线程 -> 主线程）。"""
    peers: PeerResult = field(default_factory=PeerResult)
    digest: Dict[str, dict] = field(default_factory=dict)    # 各设备摘要合并后的进度 {book_id: {percent, updated, device}}
    found: Dict[str, dict] = field(default_factory=dict)     # 逐书补缺得到的进度
    probed: int = 0                                          # 本轮探测了多少本
    cursors: dict = field(default_factory=dict)              # 新游标（主线程写进 state.json）
    requests: int = 0


# ================================================================== 后台线程
class ShelfSyncWorker(QObject):
    """书架的云端后台任务。refresh / download 都立刻返回（线程句柄仅供测试 join），结果经 Signal 回主线程。
    所有异常只记日志并发失败信号，绝不外抛。"""
    listed = Signal(object)              # Listing
    listFailed = Signal(str)             # 列表读取失败（离线 / 认证 / 服务器错误）：调用方保留本地缓存
    downloadedFor = Signal(object, str)  # (token, 本地路径)：与永久接到 load_book 的 SyncWorker.downloaded 无关
    downloadFailed = Signal(object, str)  # (token, 提示)
    downloadProgress = Signal(object, int, int)   # (token, 已收字节, 总字节)：下载中，已节流
    status = Signal(str)
    progressPulled = Signal(object)      # ProgressResult：其它设备的摘要 + 逐书补缺的进度
    progressFailed = Signal(str)         # 进度同步失败（离线 / 认证 / 用户名不合法）：调用方保留本地缓存
    pushed = Signal(int, str, str, object)   # (rev, 内容哈希, etag, 读回校验后的戳 dict)：设备文件已推送（读回比对通过）
    pushFailed = Signal(str)
    uploadDone = Signal(object, object)  # (token, UploadResult)
    uploadProgress = Signal(object, int, int)   # (token, 已发送字节, 总字节)：上传中，已节流

    def __init__(self, parent=None, client_factory: Callable[[dict], Optional[WebDavClient]] = client_from_cfg):
        super().__init__(parent)
        self._factory = client_factory
        self._lock = threading.Lock()
        self._running = False
        self._rerun = False
        self._cfg: Optional[dict] = None
        self._prev_sizes: Dict[str, int] = {}
        self._trusted: Dict[str, int] = {}                 # 本设备上传成功（已核对大小）的 rel -> 大小：列表里不当作「上传中」
        self._downloads: Dict[str, List[object]] = {}      # rel -> 等这次下载的 token 们（同一文件只下载一次）
        self.generation = 0                                # 每完成一轮列表 +1（测试用）
        self.progress_generation = 0                       # 每完成一轮进度同步 +1（测试用）
        self._prog_job: Optional[tuple] = None
        self._prog_running = False
        self._prog_cursors: Optional[dict] = None          # 上一轮产出的游标：后到者合并成「再跑一次」时接着用，不回退
        self._neg: Dict[str, float] = {}                   # 逐书进度文件不存在的书 -> 上次探测时间（time.monotonic）
        self._push_job: Optional[tuple] = None
        self._push_running = False
        self._push_thread: Optional[threading.Thread] = None
        self._io = threading.Lock()                        # 进度同步 / 推送共用：同一时间只有一个书架同步任务在碰设备文件（单飞）

    # ------------------------------------------------------------ 列表（单飞：同一时间只有一个，后到者合并为「再跑一次」）
    def _library_clients(self, cfg: dict) -> List[tuple]:
        """(前缀, 显示名, client) 列表：主源在前（用注入/默认工厂，兼容测试），额外只读源在后（真实 client）。"""
        out: List[tuple] = []
        primary = self._factory(cfg)
        if primary is not None:
            out.append(("", "主书库", primary))
        for src in library_sources(cfg):
            if src["id"]:                      # 只取额外源（主源 id 为空，已在上一步处理）
                c = client_from_source(src)
                if c is not None:
                    out.append((src["id"], src["name"], c))
        return out

    def _client_for_rel(self, cfg: dict, rel: str) -> tuple:
        """把书架 rel 解析成 (client, 源内相对路径)。前缀命中额外源则用该源并去前缀，否则回落到主源。"""
        first = (rel or "").split("/", 1)[0]
        for src in library_sources(cfg):
            if src["id"] and src["id"] == first:
                c = client_from_source(src)
                return c, (rel[len(first) + 1:] if len(rel) > len(first) else "")
        return self._factory(cfg), rel

    def refresh(self, cfg: dict) -> Optional[threading.Thread]:
        """没有任何可用的书库源时是空操作，返回 None。已有一轮在跑则登记「再跑一次」，也返回 None。"""
        if not self._library_clients(cfg):
            return None
        with self._lock:
            self._cfg = cfg
            if self._running:
                self._rerun = True
                return None
            self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        return t

    def _loop(self):
        while True:
            with self._lock:
                cfg = self._cfg
            try:
                clients = self._library_clients(cfg)
                if not clients:
                    raise WebDavError("WebDAV 未启用")
                merged = Listing()
                complete = True
                any_ok = False
                for prefix, name, client in clients:
                    try:
                        lst = fetch_library(client, self._prev_sizes, trusted=dict(self._trusted), prefix=prefix)
                    except WebDavError as e:
                        _log_net(f"[书架] 读取书库 {name} 失败: {e}")
                        complete = False
                        merged.errors.append(f"{name}: {str(e)[:80]}")
                        continue
                    any_ok = True
                    merged.files.extend(lst.files)
                    merged.unstable |= lst.unstable
                    merged.skipped += lst.skipped
                    merged.errors.extend(lst.errors)
                    merged.truncated = merged.truncated or lst.truncated
                    if lst.root_missing:
                        merged.root_missing = True
                    if not lst.complete:
                        complete = False
                if not any_ok:                     # 所有源都读不了（离线 / 认证失败）：走 listFailed，调用方保留本地缓存
                    raise WebDavError("；".join(merged.errors) or "WebDAV 未启用")
                merged.fetched_at = time.time()
                merged.complete = complete
                self._prev_sizes = {f.rel: f.size for f in merged.files}
                self.generation += 1
                self.listed.emit(merged)
            except Exception as e:                   # 离线 / 401 / 5xx / 解析错误：只报告，绝不抛
                _log_net(f"[书架] 读取云端书库失败: {e}")
                try:
                    self.listFailed.emit(str(e)[:200])
                except Exception:
                    pass
            with self._lock:
                if self._rerun:
                    self._rerun = False
                    continue
                self._running = False
                return

    # ------------------------------------------------------------ 下载（与打开解耦，带 token）
    def download(self, cfg: dict, rel: str, size: int, open_when_done: bool, token) -> Optional[threading.Thread]:
        """下载 library/<rel> 到 remote_books/<rel>（多源时 rel 带源前缀，WebDAV 请求路径会去掉前缀）。
        完成经 downloadedFor(token, path)，失败经 downloadFailed(token, msg)。
        open_when_done 只影响状态提示文字：「要不要打开」由调用方按 token 是否仍然有效决定（迟到的完成不会劫持窗口）。
        同一个 rel 已在下载时，新的 token 并入同一次下载（返回 None）。没有可用源返回 None。"""
        client, raw_rel = self._client_for_rel(cfg, rel)
        if client is None:
            return None
        max_bytes = max_book_bytes(cfg)
        with self._lock:
            waiters = self._downloads.get(rel)
            if waiters is not None:
                waiters.append(token)
                return None
            self._downloads[rel] = [token]

        def run():
            path, err = "", ""
            last = [-1, 0.0]

            def report(done, total):         # 节流：百分比变化且距上次 > 0.3 秒；GB 级文件不能每块都发信号
                pct = done * 100 // total if total > 0 else -1
                now = time.monotonic()
                if pct == last[0] or now - last[1] < 0.3:
                    return
                last[0], last[1] = pct, now
                with self._lock:
                    toks = list(self._downloads.get(rel, ()))
                for tok in toks:
                    self.downloadProgress.emit(tok, done, total)
            try:
                path = download_book(client, raw_rel, size, max_bytes=max_bytes, progress=report, dest_rel=rel)
            except Exception as e:
                _log_net(f"[书架] 下载失败 {rel}: {e}")
                err = f"下载失败：{rel.rsplit('/', 1)[-1]}（{str(e)[:120]}）"
            with self._lock:
                tokens = self._downloads.pop(rel, [])
            name = rel.rsplit("/", 1)[-1]
            for tok in tokens:
                try:
                    if err:
                        self.downloadFailed.emit(tok, err)
                    else:
                        self.downloadedFor.emit(tok, path)
                except Exception:
                    pass
            try:
                self.status.emit(err or (f"已下载，正在打开：{name}" if open_when_done else f"已下载：{name}"))
            except Exception:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------ 进度同步（阶段 4）：其它设备的摘要 + 逐书补缺
    def forget_cursors(self) -> None:
        """重置书架状态后：丢掉工作线程里记着的游标（不然会接着用重置前的游标）。"""
        with self._lock:
            self._prog_cursors = None

    def sync_progress(self, cfg: dict, root: Optional[str], device_id: str, cursors: Optional[dict],
                      want_ids: Iterable[str], force_full: bool = False,
                      own: Optional[dict] = None) -> Optional[threading.Thread]:
        """后台拉取：users/<me>/shelf/ 里其它设备的摘要（游标 + racy 规则），再对 want_ids（摘要里仍没有进度的书）
        小线程池补缺。结果经 progressPulled；失败经 progressFailed（不抛）。单飞：已有一轮在跑则合并为「再跑一次」。
        own：{"stamp": 上次确认过的本设备文件的戳, "has_content": 本地覆盖层有没有内容} —— 给了就同时核对本设备自己的文件
        （自愈，见 OwnCheck）；不给（只读实例）则完全不碰自己的文件。未启用 WebDAV 返回 None。"""
        if self._factory(cfg) is None:
            return None
        with self._lock:
            self._prog_job = (cfg, root, device_id, cursors, list(want_ids), force_full, own)
            if self._prog_running:
                return None
            self._prog_running = True
        t = threading.Thread(target=self._progress_loop, daemon=True)
        t.start()
        return t

    def _progress_loop(self):
        while True:
            with self._lock:
                job, self._prog_job = self._prog_job, None
                if job is None:
                    self._prog_running = False
                    return
            cfg, root, device_id, cursors, want, force_full, own = job
            try:
                client = self._factory(cfg)
                if client is None:
                    raise WebDavError("WebDAV 未启用")
                user = cfg.get("sync_user_name") or "default"
                if self._prog_cursors is not None:
                    cursors = self._prog_cursors
                with self._io:
                    peers = pull_peers(client, root, user, device_id, cursors, force_full=force_full,
                                       check_own=own is not None, own_stamp=(own or {}).get("stamp"),
                                       own_has_content=bool((own or {}).get("has_content")))
                self._prog_cursors = peers.cursors
                digest = digest_progress(peers.views)
                now = time.monotonic()
                todo = [b for b in want if b not in digest and now - self._neg.get(b, -1e12) >= NEG_RETRY_SECONDS]
                with self._io:
                    fill = pull_progress_index(client, user, todo) if todo else FillResult()
                for b in fill.absent + fill.bad:
                    self._neg[b] = now
                for b in fill.found:
                    self._neg.pop(b, None)
                if len(self._neg) > 20000:
                    self._neg.clear()
                self.progress_generation += 1
                self.progressPulled.emit(ProgressResult(peers, digest, fill.found, min(len(todo), FILL_PER_ROUND),
                                                        peers.cursors, peers.requests + fill.requests))
            except Exception as e:                   # 离线 / 401 / 5xx / 解析错误：只报告，绝不抛
                _log_net(f"[书架] 进度同步失败: {e}")
                try:
                    self.progressFailed.emit(str(e)[:200])
                except Exception:
                    pass

    # ------------------------------------------------------------ 推送设备文件（阶段 4：只含进度摘要）
    def push_device(self, cfg: dict, device_id: str, payload: bytes, rev: int, digest: str) -> Optional[threading.Thread]:
        """后台 PUT users/<me>/shelf/<device_id>.json。单飞且「最新者胜」：推送期间又来的新内容只留最后一份，当前这份完成后再推。
        成功经 pushed(rev, digest, etag)；失败经 pushFailed。未启用 WebDAV 返回 None。"""
        if self._factory(cfg) is None:
            return None
        with self._lock:
            self._push_job = (cfg, device_id, payload, int(rev), digest)
            if self._push_running:
                return None
            self._push_running = True
        t = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread = t
        t.start()
        return t

    def join_push(self, timeout: float) -> bool:
        """等推送线程结束（关闭窗口时的有界 flush；timeout 秒封顶）。返回是否已空闲。"""
        t = self._push_thread
        if t is not None and t.is_alive():
            t.join(max(0.0, timeout))
        with self._lock:
            return not self._push_running

    def _push_loop(self):
        while True:
            with self._lock:
                job, self._push_job = self._push_job, None
                if job is None:
                    self._push_running = False
                    return
            cfg, device_id, payload, rev, digest = job
            try:
                client = self._factory(cfg)
                if client is None:
                    raise WebDavError("WebDAV 未启用")
                with self._io:
                    etag, stamp = push_device_verified(client, cfg.get("sync_user_name") or "default", device_id, payload)
                self.pushed.emit(rev, digest, etag, stamp)
            except Exception as e:
                _log_net(f"[书架] 推送设备文件失败: {e}")
                try:
                    self.pushFailed.emit(str(e)[:200])
                except Exception:
                    pass

    # ------------------------------------------------------------ 手动上传（阶段 4）
    def upload(self, cfg: dict, path: str, folder: str, name: str, token) -> Optional[threading.Thread]:
        """后台上传一本书（见 upload_book_safe）。结果经 uploadDone(token, UploadResult)；绝不覆盖、绝不抛。
        只由用户操作触发（没有任何自动上传）。未启用 WebDAV 返回 None。"""
        client = self._factory(cfg)
        if client is None:
            return None
        max_bytes = max_book_bytes(cfg)

        def report(sent, total):
            self.uploadProgress.emit(token, sent, total)

        def run():
            try:
                res = upload_book_safe(client, path, folder, name, max_bytes=max_bytes, progress=report)
            except Exception as e:
                _log_net(f"[书架] 上传异常 {name}: {e}")
                res = UploadResult("error", name, f"上传失败：{str(e)[:120]}")
            if res.status == "ok":
                try:
                    self._trusted[res.rel] = os.path.getsize(path)
                except OSError:
                    pass
            try:
                self.uploadDone.emit(token, res)
            except Exception:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t
