"""极简 WebDAV 客户端：只用标准库 + config.net_urlopen（沿用 Reader 的代理策略）。

用户名/密码都为空时不发送 Authorization 头（匿名 WebDAV，如 rclone serve webdav 默认）。

服务器返回的一切都视为不可信：PROPFIND 列表里的名字要过滤（'.'、'..'、反斜杠、点开头、~$ 开头、
Windows 非法名），响应体有大小上限，XML 不接受 DTD/实体，递归列目录有目录数/深度/文件数上限并带
「是否完整」标志。现有函数的返回类型不变；新能力走 *_ex 新函数或带默认值的新参数。"""
import base64
import email.utils
import ipaddress
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import net_urlopen
from .store import is_hidden_name, safe_segment

_DAV = "{DAV:}"
_PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                  b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
                  b'<d:getcontentlength/><d:getlastmodified/><d:getetag/></d:prop></d:propfind>')

PROPFIND_MAX_BYTES = 8 * 1024 * 1024     # 单次 PROPFIND 响应体上限
JSON_MAX_BYTES = 1024 * 1024             # 设备文件 / 进度文件这类小 JSON 的建议上限（调用方传给 get）

# list_tree 资源上限
MAX_DIRS = 500
MAX_DEPTH = 8
MAX_FILES = 10000


class WebDavError(Exception):
    def __init__(self, msg="", status: Optional[int] = None):
        super().__init__(msg)
        self.status = status          # HTTP 状态码（非 HTTP 层错误为 None）


@dataclass
class DavEntry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    etag: str = ""                    # getetag 原文（去首尾空白；服务器不给则为空）


@dataclass
class DavResponse:
    status: int
    body: bytes = b""
    headers: dict = field(default_factory=dict)    # 键一律小写


@dataclass
class TreeResult:
    files: List[tuple]                # [(相对路径, DavEntry)]
    complete: bool = True             # 无子树错误且未被上限截断；只有 complete 的列表才能用来判断「书消失了」
    truncated: bool = False           # 触到 文件数/目录数/深度 上限
    errors: List[str] = field(default_factory=list)   # 列失败的子目录
    skipped: int = 0                  # 被名字过滤丢掉的条目数


# ---------------------------------------------------------------- 传输安全判断
def host_is_private(host: str) -> bool:
    """localhost / 回环 / 私有 / 链路本地 / CGNAT(100.64/10，Tailscale、WG) / 无点的内网主机名 / .local .lan 等。"""
    h = (host or "").strip().strip("[]").lower().rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return "." not in h or h.endswith((".local", ".lan", ".home", ".internal", ".home.arpa", ".intranet"))
    return ip.is_loopback or ip.is_link_local or not ip.is_global


RISK_PLAIN_HTTP_PUBLIC = "plain_http_public"      # http:// 且主机是公网：凭据与内容明文传输
RISK_ANONYMOUS_PUBLIC = "anonymous_public"        # 无凭据且主机是公网：任何人可读写
RISK_TEXT = {
    RISK_PLAIN_HTTP_PUBLIC: "当前使用 http:// 连接公网主机，账号密码和内容会以明文传输；请改用 https:// 或 VPN。",
    RISK_ANONYMOUS_PUBLIC: "未设置用户名/密码且服务器在公网：该书库可能被任何人读写。",
}


def url_risks(url: str, user: str = "", password: str = "") -> List[str]:
    """返回适用的风险码列表（空 = 无风险 / URL 无法解析时不报）。纯函数，不联网。"""
    try:
        u = urllib.parse.urlsplit((url or "").strip())
        scheme, host = (u.scheme or "").lower(), u.hostname or ""
    except ValueError:
        return []
    if scheme not in ("http", "https") or not host or host_is_private(host):
        return []
    risks = []
    if scheme == "http":
        risks.append(RISK_PLAIN_HTTP_PUBLIC)
    if not (user or password):
        risks.append(RISK_ANONYMOUS_PUBLIC)
    return risks


def _cmp_key(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _normalize_url(url: str) -> str:
    """把 URL 里 path 的非 ASCII 字符按 UTF-8 百分比编码（urllib 不会自动编码，直接传中文会抛
    UnicodeEncodeError，如 alist 挂载路径 /dav/夸克）。保留已编码的 %XX 与保留字符，不双编码。"""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    path = urllib.parse.quote(u.path, safe="/%:@!$&'()*+,;=-._~[]")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path, u.query, u.fragment))


class WebDavClient:
    def __init__(self, base_url: str, user: str = "", password: str = "", timeout: float = 20,
                 allow_insecure_auth: bool = False):
        """allow_insecure_auth=False：目标是 http:// 公网主机时拒绝发送凭据（默认；
        设置页勾选「允许明文 http 发送密码」才会传 True）。"""
        self.base = _normalize_url((base_url or "").strip()).rstrip("/")
        self.user = user or ""
        self.password = password or ""
        self.timeout = timeout
        self.allow_insecure_auth = allow_insecure_auth
        self._known_dirs = set()          # 本会话内已确认存在的目录（mkdirs 缓存）
        self._dirs_lock = threading.Lock()

    # ---- 内部 ----
    def _url(self, path: str = "") -> str:
        parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise WebDavError(f"illegal path segment in {path!r}")
        quoted = "/".join(urllib.parse.quote(p, safe="") for p in parts)
        return self.base + ("/" + quoted if quoted else "")

    def _headers(self, extra=None) -> dict:
        h = dict(extra or {})
        if self.user or self.password:
            if not self.allow_insecure_auth and RISK_PLAIN_HTTP_PUBLIC in url_risks(self.base, self.user, self.password):
                raise WebDavError("refusing to send credentials over plain http to a public host")
            tok = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
            h["Authorization"] = "Basic " + tok
        return h

    @staticmethod
    def _read(r, max_bytes, what):
        """读响应体，超过 max_bytes 就报错（先看 Content-Length，再用 read(n+1) 兜底）。"""
        if max_bytes is None:
            return r.read()
        cl = (r.headers.get("Content-Length") or "").strip() if r.headers else ""
        if cl.isdigit() and int(cl) > max_bytes:
            raise WebDavError(f"{what}: response too large ({cl} > {max_bytes})")
        body = r.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise WebDavError(f"{what}: response too large (> {max_bytes})")
        return body

    @staticmethod
    def _hdrs(r) -> dict:
        try:
            return {k.lower(): v for k, v in r.headers.items()}
        except Exception:
            return {}

    def _request_ex(self, method, path, data=None, headers=None, ok_codes=None,
                    max_bytes=None) -> DavResponse:
        """完整形式：返回 DavResponse(status, body, headers)。HTTP 错误码在 ok_codes 内则不抛。"""
        what = f"{method} {path or '/'}"
        req = urllib.request.Request(self._url(path), data=data, method=method,
                                     headers=self._headers(headers))
        try:
            with net_urlopen(req, timeout=self.timeout) as r:
                return DavResponse(r.status, self._read(r, max_bytes, what), self._hdrs(r))
        except urllib.error.HTTPError as e:
            if ok_codes and e.code in ok_codes:
                try:
                    body = self._read(e, max_bytes, what)
                except WebDavError:
                    raise
                except Exception:
                    body = b""
                return DavResponse(e.code, body, self._hdrs(e))
            raise WebDavError(f"{what} -> HTTP {e.code}", status=e.code) from e
        except WebDavError:
            raise
        except Exception as e:
            raise WebDavError(f"{what} failed: {e}") from e

    def _request(self, method, path, data=None, headers=None, ok_codes=None):
        """返回 (status, body_bytes)。HTTP 错误码在 ok_codes 内则不抛。"""
        r = self._request_ex(method, path, data, headers, ok_codes)
        return r.status, r.body

    # ---- 操作 ----
    def propfind(self, path: str = "", depth: int = 1, max_bytes: int = PROPFIND_MAX_BYTES,
                 headers_out: Optional[dict] = None) -> List[DavEntry]:
        """headers_out 给定时，把响应头（键小写）写进去（取服务器 Date 用：见 shelfsync 的 racy 判断）。"""
        resp = self._request_ex("PROPFIND", path, data=_PROPFIND_BODY,
                                headers={"Depth": str(depth),
                                         "Content-Type": "application/xml; charset=utf-8"},
                                max_bytes=max_bytes)
        body = resp.body
        if headers_out is not None:
            headers_out.update(resp.headers)
        # WebDAV 响应不需要 DTD；拒绝 DOCTYPE/ENTITY 以杜绝实体膨胀（billion laughs）和外部实体
        low = body.lower()
        if b"<!doctype" in low or b"<!entity" in low:
            raise WebDavError("unsafe multistatus XML (DTD/entity not allowed)")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise WebDavError(f"bad multistatus XML: {e}") from e
        base_path = urllib.parse.unquote(urllib.parse.urlparse(self._url(path)).path).rstrip("/")
        base_key = _cmp_key(base_path)
        out = []
        for resp in root.iter(_DAV + "response"):
            href = resp.findtext(_DAV + "href") or ""
            hp = urllib.parse.unquote(urllib.parse.urlparse(href).path)
            is_dir = False
            size = 0
            mtime = 0.0
            etag = ""
            for ps in resp.findall(_DAV + "propstat"):
                st = ps.findtext(_DAV + "status") or "HTTP/1.1 200 OK"
                if " 200" not in st:
                    continue
                prop = ps.find(_DAV + "prop")
                if prop is None:
                    continue
                rt = prop.find(_DAV + "resourcetype")
                if rt is not None and rt.find(_DAV + "collection") is not None:
                    is_dir = True
                cl = (prop.findtext(_DAV + "getcontentlength") or "").strip()
                if cl.isdigit():
                    size = int(cl)
                lm = (prop.findtext(_DAV + "getlastmodified") or "").strip()
                if lm:
                    try:
                        mtime = email.utils.parsedate_to_datetime(lm).timestamp()
                    except Exception:
                        pass
                et = (prop.findtext(_DAV + "getetag") or "").strip()
                if et:
                    etag = et
            full = hp.rstrip("/")
            if _cmp_key(full) == base_key:
                continue  # 自身
            parent, _, name = full.rpartition("/")
            # 只接受请求目录的直接子项：丢弃 '..' 穿越、别处路径、'.'、空名
            if _cmp_key(parent) != base_key or not name or name in (".", ".."):
                continue
            out.append(DavEntry(name, is_dir, size, mtime, etag))
        return out

    def get_ex(self, path: str, max_bytes: Optional[int] = None, headers=None, ok_codes=None) -> DavResponse:
        return self._request_ex("GET", path, headers=headers, ok_codes=ok_codes, max_bytes=max_bytes)

    def get(self, path: str, max_bytes: Optional[int] = None) -> bytes:
        """max_bytes 给定时，响应体超限即抛 WebDavError（不会把超大响应读进内存）。"""
        return self.get_ex(path, max_bytes).body

    def get_to_file(self, path: str, fileobj, max_bytes: Optional[int] = None, chunk: int = 65536,
                    should_cancel=None, progress: Optional[Callable[[int, int], None]] = None) -> int:
        """流式 GET：按块写入已打开的二进制文件对象，返回写入字节数（大文件不进内存）。
        超过 max_bytes、响应体短于 Content-Length（连接被截断）或 should_cancel() 为真都抛 WebDavError。
        progress(已收字节, Content-Length 或 0)：每块回调一次（调用方自行节流）；回调抛错不影响下载。"""
        what = f"GET {path or '/'}"
        req = urllib.request.Request(self._url(path), method="GET", headers=self._headers())
        try:
            with net_urlopen(req, timeout=self.timeout) as r:
                cl = (r.headers.get("Content-Length") or "").strip() if r.headers else ""
                expect = int(cl) if cl.isdigit() else None
                if max_bytes is not None and expect is not None and expect > max_bytes:
                    raise WebDavError(f"{what}: response too large ({expect} > {max_bytes})")
                total = 0
                while True:
                    if should_cancel is not None and should_cancel():
                        raise WebDavError(f"{what}: cancelled")
                    buf = r.read(chunk)
                    if not buf:
                        break
                    total += len(buf)
                    if max_bytes is not None and total > max_bytes:
                        raise WebDavError(f"{what}: response too large (> {max_bytes})")
                    fileobj.write(buf)
                    if progress is not None:
                        try:
                            progress(total, expect or 0)
                        except Exception:
                            pass
                if expect is not None and total != expect:
                    raise WebDavError(f"{what}: truncated response ({total} of {expect} bytes)")
                return total
        except urllib.error.HTTPError as e:
            raise WebDavError(f"{what} -> HTTP {e.code}", status=e.code) from e
        except WebDavError:
            raise
        except Exception as e:
            raise WebDavError(f"{what} failed: {e}") from e

    def put_ex(self, path: str, data: bytes, headers=None, ok_codes=None) -> DavResponse:
        """headers 会合并进请求头（如 {"If-None-Match": "*"}）；ok_codes（如 (412,)）内的状态不抛。"""
        h = {"Content-Type": "application/octet-stream"}
        h.update(headers or {})
        try:
            return self._request_ex("PUT", path, data=data, headers=h, ok_codes=ok_codes)
        except WebDavError as e:
            if e.status in (404, 409):
                self.forget_dirs()        # 父目录可能已被别人删掉：下次 mkdirs 重新确认
            raise

    def put(self, path: str, data: bytes, headers=None):
        self.put_ex(path, data, headers)

    def put_file(self, path: str, fileobj, size: int, headers=None, ok_codes=None) -> DavResponse:
        """流式 PUT：fileobj 是已打开的二进制文件，size 必须是要发送的字节数（写进 Content-Length；
        大文件不进内存）。headers / ok_codes 同 put_ex。"""
        h = {"Content-Type": "application/octet-stream", "Content-Length": str(int(size))}
        h.update(headers or {})
        try:
            return self._request_ex("PUT", path, data=fileobj, headers=h, ok_codes=ok_codes)
        except WebDavError as e:
            if e.status in (404, 409):
                self.forget_dirs()
            raise

    def move(self, src: str, dst: str, overwrite: bool = False, ok_codes=None) -> DavResponse:
        """WebDAV MOVE（仅内部使用，如上传完成时 .part 改名；不向用户暴露）。overwrite=False 发 Overwrite: F，
        目标已存在时服务器应回 412，调用方把 412 放进 ok_codes 自行处理。"""
        return self._request_ex("MOVE", src, headers={"Destination": self._url(dst),
                                                       "Overwrite": "T" if overwrite else "F"},
                                ok_codes=ok_codes)

    def mkcol(self, path: str) -> bool:
        """创建单层目录；已存在（405）视为成功（返回 False 表示原本就在）。
        部分服务器（SFTPGo 的虚拟目录挂载点）对已存在的目录回 403 而不是 405：此时用 PROPFIND 确认，
        目录确实在就当「已存在」，不在才把 403 当真抛出（不掩盖真正的权限问题）。"""
        st, _ = self._request("MKCOL", path, ok_codes=(403, 405))
        if st == 403:
            if not self.exists(path):
                raise WebDavError(f"MKCOL {path!r}: 403 Forbidden", status=403)
            st = 405
        self._remember_dir(path)
        return st != 405

    def _dir_key(self, path: str) -> str:
        return "/".join(p for p in (path or "").replace("\\", "/").split("/") if p)

    def _remember_dir(self, path: str):
        with self._dirs_lock:
            self._known_dirs.add(self._dir_key(path))

    def forget_dirs(self):
        """清空 mkdirs 的已创建目录缓存。"""
        with self._dirs_lock:
            self._known_dirs.clear()

    def mkdirs(self, path: str):
        """mkdir -p。本会话内确认过的目录不再发请求。"""
        cur = ""
        for p in [x for x in path.replace("\\", "/").split("/") if x]:
            cur = f"{cur}/{p}" if cur else p
            with self._dirs_lock:
                known = cur in self._known_dirs
            if not known:
                self.mkcol(cur)

    def exists(self, path: str = "") -> bool:
        st, _ = self._request("PROPFIND", path, data=_PROPFIND_BODY,
                              headers={"Depth": "0", "Content-Type": "application/xml"},
                              ok_codes=(404,))
        return st != 404

    def list_tree_ex(self, path: str = "", max_files: int = MAX_FILES, max_dirs: int = MAX_DIRS,
                     max_depth: int = MAX_DEPTH, accept: Optional[Callable[[str], bool]] = None) -> TreeResult:
        """递归列出文件。根不存在/不可读则抛 WebDavError；子树失败或触到上限不抛，而是 complete=False。

        过滤：'.'、'..'、含反斜杠/冒号/控制字符/Windows 保留名的名字，以及 '.' 或 '~$' 开头的文件和目录。
        用已访问集合防止同一目录被重复列出；目录数/深度/文件数都有上限。
        accept(相对路径)：不接受的文件计入 skipped，且不占 max_files 名额（有声书文件夹里的封面 / .cue / .nfo 不该挤掉书）。"""
        res = TreeResult([])
        seen_files = set()
        visited = {""}
        stack = [("", 0)]
        listed = 0
        while stack:
            rel, depth = stack.pop()
            if listed >= max_dirs:
                res.truncated = True
                break
            listed += 1
            try:
                entries = self.propfind("/".join(x for x in (path, rel) if x), 1)
            except WebDavError:
                if rel == "":
                    raise
                res.errors.append(rel)
                continue
            for e in entries:
                if safe_segment(e.name) is None or is_hidden_name(e.name):
                    res.skipped += 1
                    continue
                r = f"{rel}/{e.name}" if rel else e.name
                if e.is_dir:
                    if depth + 1 > max_depth:
                        res.truncated = True
                        continue
                    key = unicodedata.normalize("NFC", r)
                    if key in visited:
                        continue
                    visited.add(key)
                    stack.append((r, depth + 1))
                else:
                    key = unicodedata.normalize("NFC", r)
                    if key in seen_files:
                        continue
                    if accept is not None and not accept(r):
                        res.skipped += 1
                        continue
                    if len(res.files) >= max_files:
                        res.truncated = True
                        stack.clear()
                        break
                    seen_files.add(key)
                    res.files.append((r, e))
        res.complete = not res.errors and not res.truncated
        return res

    def list_tree(self, path: str = "", max_files: int = MAX_FILES) -> List[tuple]:
        """递归列出文件，返回 [(相对路径, DavEntry)]；根不存在则抛 WebDavError。
        需要「列表是否完整」时用 list_tree_ex。"""
        return self.list_tree_ex(path, max_files=max_files).files


def client_from_cfg(cfg: dict) -> Optional[WebDavClient]:
    """主书库（进度同步 / 上传目标）。未启用或 url 为空 → None（调用方一律视为 no-op）。"""
    if not cfg.get("webdav_enabled") or not (cfg.get("webdav_url") or "").strip():
        return None
    return WebDavClient(cfg["webdav_url"], cfg.get("webdav_user", ""), cfg.get("webdav_password", ""),
                        allow_insecure_auth=bool(cfg.get("webdav_allow_insecure", False)))


def client_from_source(src: dict) -> Optional[WebDavClient]:
    """从单个书库源字典建客户端（url 为空返回 None）。"""
    if not isinstance(src, dict) or not (src.get("url") or "").strip():
        return None
    return WebDavClient(src["url"], src.get("user", ""), src.get("password", ""),
                        allow_insecure_auth=bool(src.get("allow_insecure", False)))


def library_sources(cfg: dict) -> List[dict]:
    """所有启用的书库源（主源在前，额外的只读源在后）。每项：
    {id, name, url, user, password, allow_insecure}。主源的 id 为空字符串（其 rel 不带前缀，兼容旧数据）；
    额外源以 name 作为命名空间前缀（safe_segment 允许中文，name 会直接成为书架 cloud:<name>/… 目录前缀）。
    坏条目（name 不合法 / url 为空 / 未启用）一律跳过。"""
    out: List[dict] = []
    if cfg.get("webdav_enabled") and (cfg.get("webdav_url") or "").strip():
        out.append({"id": "", "name": "主书库",
                    "url": cfg["webdav_url"], "user": cfg.get("webdav_user", ""),
                    "password": cfg.get("webdav_password", ""),
                    "allow_insecure": bool(cfg.get("webdav_allow_insecure", False))})
    for s in cfg.get("webdav_libraries", []) or []:
        if not isinstance(s, dict) or not s.get("enabled", True):
            continue
        name = safe_segment((s.get("name") or "").strip())
        if not name or is_hidden_name(name) or not (s.get("url") or "").strip():
            continue
        out.append({"id": name, "name": name,
                    "url": (s["url"] or "").strip(),
                    "user": s.get("user", ""), "password": s.get("password", ""),
                    "allow_insecure": bool(s.get("allow_insecure", False))})
    return out
