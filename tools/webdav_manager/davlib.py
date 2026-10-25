#!/usr/bin/env python3
"""Small CLI to manage files on the PyReader WebDAV store: books, photos, anything (SFTPGo behind Caddy).

Config via env vars (keep credentials out of the command line):
    DAV_URL   default https://gary-pyreader.eastasia.cloudapp.azure.com/
    DAV_USER  e.g. gary
    DAV_PASS

Remote layout: library/ (shared books, subfolders = shelves), photo/ (private photos), u-<name>/ (per-user data).

Examples:
    python davlib.py ls library        # any path works, e.g. photo/2026
    python davlib.py tree library
    python davlib.py up D:\\books\\foo.epub library/Novels
    python davlib.py up D:\\books library --skip-existing
    python davlib.py down library/Novels/foo.epub D:\\dl
    python davlib.py mkdir library/Comics
    python davlib.py mv library/a.epub library/Novels/a.epub
    python davlib.py rm library/old.epub
"""
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from webdav3.client import Client
from webdav3.exceptions import RemoteResourceNotFound, WebDavException
from webdav3.urn import Urn

DEFAULT_URL = "https://gary-pyreader.eastasia.cloudapp.azure.com/"
# (connect, read) seconds for quick metadata calls (PROPFIND, MKCOL, connect probe); transfers use the long default
META_TIMEOUT = (10, 30)


class FolderExists(WebDavException):
    """A folder already has the name a file is about to be written to; never replace a folder with a file."""


def make_client_from(url: str, user: str, password: str, timeout=600) -> Client:
    """Build a client from explicit settings (used by the GUI; the CLI reads env vars)."""
    return Client({
        "webdav_hostname": url.rstrip("/"),
        "webdav_login": user,
        "webdav_password": password,
        "webdav_timeout": timeout,
        "disable_check": True,  # library check() uses HEAD; we use PROPFIND via exists()
    })


def make_client() -> Client:
    user = os.environ.get("DAV_USER", "")
    pwd = os.environ.get("DAV_PASS", "")
    if not user or not pwd:
        sys.exit("Set DAV_USER and DAV_PASS (and optionally DAV_URL).")
    return make_client_from(os.environ.get("DAV_URL", DEFAULT_URL), user, pwd)


def norm(p: str) -> str:
    return "/" + p.strip("/") if p.strip("/") else "/"


def exists(c: Client, path: str) -> bool:
    """PROPFIND-based existence check (HEAD is 405 on directories for some servers)."""
    try:
        c.info(norm(path))
        return True
    except RemoteResourceNotFound:
        return False


def remote_kind(c: Client, path: str):
    """None if path does not exist, else 'dir' or 'file' (one PROPFIND)."""
    try:
        return "dir" if c.is_dir(norm(path)) else "file"
    except RemoteResourceNotFound:
        return None


def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def list_entries(c: Client, path: str):
    """Return [(name, is_dir, size, modified)] for direct children of path."""
    path = norm(path)
    out = []
    base = unquote(urlparse(c.webdav.hostname).path)
    self_path = (base + path).strip("/")
    for info in c.list(path, get_info=True):
        raw = info["path"].strip("/")  # webdavclient3 has already URL-decoded the href; decoding again corrupts '%XX' names
        if raw == self_path:
            continue  # the directory itself
        name = raw.rsplit("/", 1)[-1]
        is_dir = info.get("isdir") in (True, "True", "true")
        out.append((name, is_dir, info.get("size") or 0, info.get("modified") or ""))
    return sorted(out, key=lambda e: (not e[1], e[0].lower()))


# ---- streaming transfers with byte-level progress and cancel (used by the GUI) -------------
CHUNK = 256 * 1024


class Cancelled(Exception):
    """Raised inside a transfer when the cancel callback returns True."""


class _UploadBody:
    """File-like request body: requests sends a Content-Length (via __len__) and streams
    it chunk by chunk, so multi-GB files never sit in memory and progress/cancel are exact."""

    def __init__(self, f, total, progress, cancel):
        self.f, self.total, self.progress, self.cancel, self.sent = f, total, progress, cancel, 0

    def __len__(self):
        return self.total

    def read(self, n=-1):
        if self.cancel and self.cancel():
            raise Cancelled()
        data = self.f.read(CHUNK if n is None or n < 0 else min(n, CHUNK))
        self.sent += len(data)
        if self.progress:
            self.progress(self.sent, self.total)
        return data

    def __iter__(self):
        while True:
            chunk = self.read(CHUNK)
            if not chunk:
                return
            yield chunk


def put_file(c: Client, remote: str, local, progress=None, cancel=None):
    """PUT local file to remote path (no .part handling here). progress(done, total)."""
    size = os.path.getsize(local)
    with open(local, "rb") as f:
        body = _UploadBody(f, size, progress, cancel) if size else b""
        c.execute_request("upload", Urn(norm(remote)).quote(), data=body)


def get_file(c: Client, remote: str, local, progress=None, cancel=None, total=0):
    """GET remote file into local path, streaming. Raises on a short read."""
    r = c.execute_request("download", Urn(norm(remote)).quote(), headers_ext=["Accept-Encoding: identity"])
    try:
        total = int(r.headers.get("content-length") or total or 0)
        done = 0
        with open(local, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if cancel and cancel():
                    raise Cancelled()
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if total and done != total:
            raise IOError(f"incomplete download ({done} of {total} bytes)")
    finally:
        r.close()


def cmd_ls(c, a):
    for name, is_dir, size, mod in list_entries(c, a.path):
        print(f"{'d' if is_dir else '-'} {'' if is_dir else fmt_size(size):>9}  {mod:<31} {name}{'/' if is_dir else ''}")


def cmd_tree(c, a, path=None, prefix=""):
    path = norm(path or a.path)
    if not prefix:
        print(path)
    entries = list_entries(c, path)
    for i, (name, is_dir, size, _) in enumerate(entries):
        last = i == len(entries) - 1
        print(f"{prefix}{'└── ' if last else '├── '}{name}{'/' if is_dir else '  (' + fmt_size(size) + ')'}")
        if is_dir:
            cmd_tree(c, a, f"{path}/{name}", prefix + ("    " if last else "│   "))


def ensure_dir(c: Client, path: str):
    """mkdir -p; SFTPGo mount points answer 403 on MKCOL, so only create what is missing."""
    cur = ""
    for part in norm(path).strip("/").split("/"):
        if not part:
            continue
        cur += "/" + part
        if not exists(c, cur):
            c.mkdir(cur)


def upload_one(c: Client, local: Path, remote_dir: str, skip_existing: bool):
    remote = f"{norm(remote_dir)}/{local.name}"
    size = local.stat().st_size
    kind = remote_kind(c, remote)
    if kind == "dir":
        raise FolderExists(f"{remote}: a folder with this name exists")
    if kind:
        if skip_existing:
            try:
                if int(c.info(remote).get("size") or -1) == size:
                    print(f"skip   {remote}")
                    return
            except WebDavException:
                pass
        else:
            print(f"exists {remote} (use --skip-existing or rm first)")
            return
    # upload to .part then MOVE so a broken transfer never looks like a finished book
    part = remote + ".part"
    print(f"up     {local}  ->  {remote}  ({fmt_size(size)})")
    c.upload_sync(remote_path=part, local_path=str(local))
    if kind:  # replace the old *file* explicitly; MOVE never overwrites, so it can never clobber a folder
        if remote_kind(c, remote) == "dir":
            raise FolderExists(f"{remote}: a folder with this name exists")
        c.clean(remote)
    c.move(remote_path_from=part, remote_path_to=remote, overwrite=False)


def cmd_up(c, a):
    src = Path(a.local)
    if not src.exists():
        sys.exit(f"not found: {src}")
    ensure_dir(c, a.remote_dir)
    if src.is_file():
        upload_one(c, src, a.remote_dir, a.skip_existing)
        return
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel_dir = f.parent.relative_to(src).as_posix()
        rdir = norm(a.remote_dir) + ("" if rel_dir == "." else "/" + rel_dir)
        ensure_dir(c, rdir)
        try:
            upload_one(c, f, rdir, a.skip_existing)
        except WebDavException as e:
            print(f"FAIL   {f}: {e}")


def cmd_down(c, a):
    dest = Path(a.local_dir)
    dest.mkdir(parents=True, exist_ok=True)
    remote = norm(a.remote)
    if c.is_dir(remote):
        for name, is_dir, _, _ in list_entries(c, remote):
            sub = argparse.Namespace(remote=f"{remote}/{name}", local_dir=str(dest / remote.rsplit('/', 1)[-1]))
            cmd_down(c, sub)
        return
    target = dest / remote.rsplit("/", 1)[-1]
    print(f"down   {remote}  ->  {target}")
    c.download_sync(remote_path=remote, local_path=str(target))


def cmd_mkdir(c, a):
    ensure_dir(c, a.path)


def cmd_rm(c, a):
    if not a.yes and input(f"delete {a.path} ? [y/N] ").lower() != "y":
        return
    c.clean(norm(a.path))


def cmd_mv(c, a):
    c.move(remote_path_from=norm(a.src), remote_path_to=norm(a.dst), overwrite=a.overwrite)


def cmd_info(c, a):
    for k, v in c.info(norm(a.path)).items():
        print(f"{k}: {v}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ls"); p.add_argument("path", nargs="?", default="/"); p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("tree"); p.add_argument("path", nargs="?", default="/"); p.set_defaults(fn=cmd_tree)
    p = sub.add_parser("up"); p.add_argument("local"); p.add_argument("remote_dir")
    p.add_argument("--skip-existing", action="store_true"); p.set_defaults(fn=cmd_up)
    p = sub.add_parser("down"); p.add_argument("remote"); p.add_argument("local_dir", nargs="?", default="."); p.set_defaults(fn=cmd_down)
    p = sub.add_parser("mkdir"); p.add_argument("path"); p.set_defaults(fn=cmd_mkdir)
    p = sub.add_parser("rm"); p.add_argument("path"); p.add_argument("-y", "--yes", action="store_true"); p.set_defaults(fn=cmd_rm)
    p = sub.add_parser("mv"); p.add_argument("src"); p.add_argument("dst"); p.add_argument("--overwrite", action="store_true"); p.set_defaults(fn=cmd_mv)
    p = sub.add_parser("info"); p.add_argument("path"); p.set_defaults(fn=cmd_info)
    a = ap.parse_args()
    try:
        a.fn(make_client(), a)
    except WebDavException as e:
        sys.exit(f"WebDAV error: {e}")


if __name__ == "__main__":
    main()
