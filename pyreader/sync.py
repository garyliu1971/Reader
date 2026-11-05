"""阅读进度 WebDAV 同步（纯逻辑 + 后台 worker）。

远端布局： <base>/users/<sync_user_name>/progress/<book_id>.json
          <base>/library/<相对路径>
只同步进度；config.json（含密钥）与 bookmarks.json 永不上传。"""
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .config import APP_DIR, _log_net
from .store import is_hidden_name, normalize_rel
from .webdav import WebDavClient, WebDavError, client_from_cfg

REMOTE_BOOKS_DIR = os.path.join(APP_DIR, "remote_books")
MAX_BOOK_BYTES = 1024 * 1024 * 1024      # 单本书下载上限（流式写盘，不进内存）；cfg["webdav_max_book_mb"] 可覆盖
UNLIMITED_BOOK_MB = -1                   # cfg["webdav_max_book_mb"] 取该值 = 不限制


def max_book_bytes(cfg) -> int:
    """cfg["webdav_max_book_mb"] -> 字节上限：缺省/0/非法 = 1GB，<0 = 不限制。"""
    try:
        mb = int(cfg.get("webdav_max_book_mb") or 0)
    except (TypeError, ValueError):
        mb = 0
    if mb < 0:
        return 1 << 62
    return mb * 1024 * 1024 if mb > 0 else MAX_BOOK_BYTES


DISK_MARGIN = 16 * 1024 * 1024           # 下载前要求的额外剩余空间（给 .part 之外的写入留余地）
_SWEPT: set = set()


def sweep_stale_parts(root: Optional[str] = None) -> int:
    """清掉 remote_books 里上次异常退出（崩溃 / 下载到一半关程序）留下的 *.part，返回清了几个。
    只在启动时、且本进程拿到了书架单实例锁时调用（否则会删掉另一个进程正在下载的 .part）；每个目录每进程只扫一次。"""
    root = os.path.abspath(root or REMOTE_BOOKS_DIR)
    if root in _SWEPT:
        return 0
    _SWEPT.add(root)
    n = 0
    for d, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".part"):
                try:
                    os.remove(os.path.join(d, f))
                    n += 1
                except OSError:
                    pass
    return n


def default_device_name() -> str:
    try:
        return socket.gethostname() or "device"
    except Exception:
        return "device"


def _remote_rel(path: str) -> Optional[str]:
    """若 path 位于 remote_books 下，返回相对路径（/ 分隔），否则 None。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(REMOTE_BOOKS_DIR))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def book_id(path: str) -> str:
    """远程书库的书 → 远端相对路径；本地书 → sha1(文件名 + 大小)（与本机路径无关）。"""
    rel = _remote_rel(path)
    if rel:
        return "remote:" + rel
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    h = hashlib.sha1(f"{os.path.basename(path)}{size}".encode("utf-8")).hexdigest()
    return "local:" + h


def _safe_name(bid: str) -> str:
    # 不能把非 ASCII 字符替换成 "_"：不同的中文书名会得到同一个文件名而互相覆盖进度
    return hashlib.sha1(bid.encode("utf-8")).hexdigest()[:20]


def progress_path(user: str, bid: str) -> str:
    return f"users/{user or 'default'}/progress/{_safe_name(bid)}.json"


def make_record(bid, offset, percent, chapter, device, updated=None) -> dict:
    return {"book_id": bid, "offset": int(offset), "percent": float(percent),
            "chapter": chapter, "updated": float(updated if updated is not None else time.time()),
            "device": device}


MEDIA_KINDS = ("audio", "comic")
MAX_POS_MS = 1000 * 3600 * 1000          # 远端值的合理上限：1000 小时 / 20 万页，超出的当作坏数据
MAX_PAGE = 200000


def make_media_record(bid, kind, percent, device, pos_ms=None, page=None, updated=None) -> dict:
    """应用内媒体（音频 / 漫画）的进度记录。在文字书记录的基础上加可选字段 kind / pos_ms / page，
    offset 恒为 0、chapter 为空：旧版只认 book_id / offset / percent / updated / device，会无视多出来的字段，
    而旧版本来就不会为 .mp3 / .zip 拉取进度（它们对旧版是「外部格式」），所以不会被这条记录误导。"""
    rec = make_record(bid, 0, percent, "", device, updated)
    rec["kind"] = kind if kind in MEDIA_KINDS else ""
    if pos_ms is not None:
        rec["pos_ms"] = int(pos_ms)
    if page is not None:
        rec["page"] = int(page)
    return rec


def media_position(rec, kind: str) -> Optional[int]:
    """远端记录里的媒体位置：audio → pos_ms（毫秒），comic → page（页序号，0 起）。字段缺失 / 类型错 / 越界返回 None
    （来自服务器的数据不可信：布尔、浮点、负数、超上限都不采信）。"""
    if not isinstance(rec, dict):
        return None
    key, cap = ("pos_ms", MAX_POS_MS) if kind == "audio" else ("page", MAX_PAGE)
    v = rec.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= cap:
        return None
    return v


def merge_progress(local: Optional[dict], remote: Optional[dict]) -> Optional[dict]:
    """较新的 updated 获胜；相等时偏向本地。"""
    if not remote:
        return local
    if not local:
        return remote
    return remote if float(remote.get("updated", 0)) > float(local.get("updated", 0)) else local


def push_progress(client: WebDavClient, user: str, rec: dict):
    client.mkdirs(f"users/{user or 'default'}/progress")
    client.put(progress_path(user, rec["book_id"]), json.dumps(rec, ensure_ascii=False).encode("utf-8"))


def pull_progress(client: WebDavClient, user: str, bid: str) -> Optional[dict]:
    p = progress_path(user, bid)
    if not client.exists(p):
        return None
    try:
        d = json.loads(client.get(p).decode("utf-8"))
        if isinstance(d, dict) and "offset" in d and d.get("book_id") == bid:
            return d
        return None
    except (ValueError, UnicodeDecodeError):
        return None


def resolve_offset(rec: dict, text_len: int) -> int:
    """优先用 offset；文件/解析器不同导致越界时退回按 percent 换算。"""
    off = int(rec.get("offset", 0))
    if 0 <= off < max(1, text_len):
        return off
    return int(max(0.0, min(1.0, float(rec.get("percent", 0)) / 100.0)) * max(0, text_len - 1))


def download_book(client: WebDavClient, rel: str, size: int, dest_root: Optional[str] = None,
                  max_bytes: int = MAX_BOOK_BYTES, should_cancel=None, progress=None,
                  dest_rel: Optional[str] = None) -> str:
    """把 library/<rel> 下载到 <dest_root>/<dest_rel>（默认 dest_rel=rel，即 remote_books/<rel>），返回本地路径。
    阻塞调用，只能在后台线程用。

    - rel：WebDAV 上的相对路径（去掉源前缀后的源内路径），必须是合法相对路径且不含点 / ~$ 开头的段。
    - dest_rel：本机缓存目录下的相对路径（多源时带源前缀），默认等于 rel；同样要合法且落在 dest_root 之内。
    - 本地已有同大小的文件则直接复用（size > 0 时）。
    - 先流式写入 <dest>.part，字节数必须等于列表给出的 size（size <= 0 表示列表没给大小，只校验 Content-Length），
      不符则丢弃 .part、不动旧文件并抛 WebDavError；成功后才 os.replace 成最终路径。
    - 列表给了 size 时，下载前先查磁盘剩余空间（size + 16MB），不够直接报错，不去写满磁盘。
    - progress(已收字节, 总字节)：下载进度回调（调用方节流）。"""
    if normalize_rel(rel) is None or any(is_hidden_name(s) for s in rel.split("/")):
        raise WebDavError("illegal path")
    dest_rel = dest_rel if dest_rel is not None else rel
    if normalize_rel(dest_rel) is None or any(is_hidden_name(s) for s in dest_rel.split("/")):
        raise WebDavError("illegal path")
    root = os.path.abspath(dest_root or REMOTE_BOOKS_DIR)
    dest = os.path.abspath(os.path.join(root, *dest_rel.split("/")))
    if not dest.startswith(root + os.sep):
        raise WebDavError("illegal path")
    size = int(size or 0)
    if size > 0 and os.path.isfile(dest) and os.path.getsize(dest) == size:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if size > 0:
        try:
            free = shutil.disk_usage(os.path.dirname(dest)).free
        except OSError:
            free = None                      # 查不到就不拦：交给写盘时的 OSError
        if free is not None and free < size + DISK_MARGIN:
            raise WebDavError(f"磁盘空间不足：需要 {size // (1024 * 1024)} MB，剩余 {free // (1024 * 1024)} MB")
    tmp = dest + ".part"
    kw = {"progress": (lambda n, e: progress(n, size or e))} if progress is not None else {}
    try:
        with open(tmp, "wb") as f:
            n = client.get_to_file("library/" + rel, f, max_bytes=max_bytes, should_cancel=should_cancel, **kw)
        if size > 0 and n != size:
            raise WebDavError(f"size mismatch for {rel}: got {n} bytes, expected {size}")
        os.replace(tmp, dest)
    finally:
        try:
            os.remove(tmp)                   # 成功时 tmp 已被 replace 走；失败 / 取消时清掉残留
        except OSError:
            pass
    return dest


class SyncWorker(QObject):
    """后台线程执行同步任务；结果经 Signal 回主线程。所有异常只记日志，不外抛。"""
    pulled = Signal(str, object)    # (book_path, remote_record_or_None)
    status = Signal(str)
    downloaded = Signal(str)        # 本地路径
    remoteList = Signal(object)     # [(rel, size)]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._push_lock = threading.Lock()      # 进度推送串行；慢链路上先发的 PUT 不会晚于后发的而盖掉新进度
        self._push_newest = {}                  # book_id → 已排队的最新 updated

    @staticmethod
    def _spawn(fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        return t

    def pull(self, cfg: dict, book_path: str):
        c = client_from_cfg(cfg)
        if not c:
            return None
        user, bid = cfg.get("sync_user_name") or "default", book_id(book_path)

        def run():
            try:
                self.pulled.emit(book_path, pull_progress(c, user, bid))
            except Exception as e:
                _log_net(f"[同步] 拉取进度失败: {e}")
                self.status.emit("同步进度失败（已忽略）")
        return self._spawn(run)

    def push(self, cfg: dict, rec: dict):
        c = client_from_cfg(cfg)
        if not c:
            return None
        user = cfg.get("sync_user_name") or "default"
        bid, upd = rec.get("book_id"), rec.get("updated", 0)
        self._push_newest[bid] = max(self._push_newest.get(bid, 0), upd)

        def run():
            try:
                with self._push_lock:
                    if upd < self._push_newest.get(bid, 0):
                        return                  # 同一本书已有更新的记录排在后面：这条过时了，不推
                    push_progress(c, user, rec)
            except Exception as e:
                _log_net(f"[同步] 上传进度失败: {e}")
                self.status.emit("同步进度失败（已忽略）")
        return self._spawn(run)

    def list_library(self, cfg: dict, book_exts):
        c = client_from_cfg(cfg)
        if not c:
            return None

        def run():
            try:
                files = [(r, e.size) for r, e in c.list_tree("library")
                         if r.lower().endswith(tuple(book_exts))
                         and not os.path.basename(r).startswith((".", "~$"))]
                files.sort()
                self.remoteList.emit(files)
            except Exception as e:
                _log_net(f"[同步] 列远程书库失败: {e}")
                self.status.emit("读取远程书库失败")
        return self._spawn(run)

    def download(self, cfg: dict, rel: str, size: int):
        c = client_from_cfg(cfg)
        if not c:
            return None

        def run():
            try:
                self.downloaded.emit(download_book(c, rel, size))
            except Exception as e:
                _log_net(f"[同步] 下载失败 {rel}: {e}")
                self.status.emit("下载远程书失败")
        return self._spawn(run)

    def upload_book(self, cfg: dict, path: str):
        """上传到 library/ 根目录（旧入口，保留给兼容）。不再覆盖云端已有的同名文件：先查是否已存在，
        走 .part + 完成校验（见 shelfsync.upload_book_safe）；书架里的「上传到云端…」对话框可选文件夹 / 改名。"""
        c = client_from_cfg(cfg)
        if not c:
            return None

        def run():
            try:
                from .shelfsync import upload_book_safe     # 延迟导入：shelfsync 反过来依赖本模块
                res = upload_book_safe(c, path, "", os.path.basename(path))
                if res.status == "error":
                    self.status.emit(res.message if res.message.startswith("上传失败") else "上传失败：" + res.message)
                elif res.status == "ok":
                    self.status.emit(f"已上传到远程书库：{os.path.basename(path)}")
                else:
                    self.status.emit(res.message + "（未覆盖）")
            except Exception as e:
                _log_net(f"[同步] 上传书失败: {e}")
                self.status.emit("上传失败")
        return self._spawn(run)
