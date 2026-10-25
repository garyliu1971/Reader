#!/usr/bin/env python3
"""davgui - visual manager for the PyReader WebDAV file store: books, photos, anything (PySide6).

Run:  python davgui.py
Optional env vars (same as davlib.py): DAV_URL, DAV_USER, DAV_PASS.  When user and password are
both set the app connects immediately; otherwise the connect dialog opens.  The password is
never written to disk (only URL + user go to ~/.davgui.json; the password goes to the OS
keyring only if the optional 'keyring' module is installed and you tick the box).

All network IO runs in worker threads (QThreadPool).  Workers never touch widgets: they return
results / emit signals, and callbacks run on the GUI thread.
"""
import dataclasses
import ipaddress
import itertools
import json
import os
import posixpath
import sys
import threading
import time
import traceback
from collections import Counter, namedtuple
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from PySide6.QtCore import (QAbstractTableModel, QByteArray, QItemSelection, QItemSelectionModel, QModelIndex, QObject,
                            QRunnable, QSortFilterProxyModel, Qt, QThreadPool, QTimer, Signal, Slot)
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence, QPainter, QPalette, QShortcut, QStandardItem, \
    QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QDockWidget,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter,
                               QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionProgressBar, QTableView, QToolBar,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from webdav3.exceptions import (ConnectionException, MethodNotSupported, NoConnection, RemoteResourceNotFound,
                                ResponseErrorCode, WebDavException)

import davlib

try:  # optional: only used when it is already installed
    import keyring
except Exception:  # pragma: no cover
    keyring = None

CONFIG = Path.home() / ".davgui.json"
APP_NAME = "WebDAV File Manager"
ROLE_ENTRY, ROLE_SORT, ROLE_PATH, ROLE_LOADED = (Qt.UserRole, Qt.UserRole + 1, Qt.UserRole, Qt.UserRole + 1)
Entry = namedtuple("Entry", "name is_dir size mtime")


# ------------------------------------------------------------------ small helpers

@dataclasses.dataclass(frozen=True)
class Conn:
    url: str
    user: str
    password: str = dataclasses.field(repr=False, default="")  # never shown in logs / tracebacks
    insecure: bool = False  # user explicitly allowed plain http to a remote host

    @property
    def host(self):
        return urlparse(self.url).hostname or self.url


def load_cfg():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        CONFIG.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def strip_userinfo(url):
    """Drop 'user:pass@' from a URL so credentials are never persisted, shown or used as a keyring key."""
    u = urlparse(url)
    if "@" not in u.netloc:
        return url
    return u._replace(netloc=u.netloc.rsplit("@", 1)[1]).geturl()


def insecure_problem(url, allow):
    """Return an error text if url is plain http to a non-local host and not explicitly allowed."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "The URL must start with http:// or https:// and contain a host name."
    if "@" in u.netloc:
        return "Do not put a user name or password in the URL; enter them in the User / Password fields."
    if u.scheme == "http" and not allow:
        host = u.hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host.lower() == "localhost"
        if not local:
            return ("Plain http to a remote host would send your password unencrypted. "
                    "Use https:// or tick 'Allow insecure connection'.")
    return ""


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_mtime(s):
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0


def join(parent, name):
    return davlib.norm(posixpath.join(parent, name))


def safe_local_name(name):
    return "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).rstrip(" .") or "_"


def unique_local_name(used, name, is_dir=False):
    """Windows-safe local name that differs (case-insensitively) from every name already in `used`."""
    safe = safe_local_name(name)
    base, ext = (safe, "") if is_dir else os.path.splitext(safe)
    cand, i = safe, 1
    while cand.casefold() in used:
        i += 1
        cand = f"{base} ({i}){ext}"
    used.add(cand.casefold())
    return cand


def friendly(e):
    """Turn library / network exceptions into one readable line (never includes credentials)."""
    if isinstance(e, ConnectionException):  # webdavclient wraps e.g. read timeouts
        e = e.exception
    if isinstance(e, ResponseErrorCode):
        texts = {401: "Authentication failed - wrong user name or password (401)",
                 403: "Permission denied (403) - the location may be read-only or a mount point",
                 404: "Not found (404)", 405: "Operation not allowed here (405)",
                 409: "Parent folder does not exist (409)", 412: "Target already exists (412)",
                 413: "File too large for the server (413)", 423: "Resource is locked (423)",
                 507: "Server is out of space (507)"}
        return texts.get(e.code, f"Server answered HTTP {e.code}")
    if isinstance(e, requests.exceptions.SSLError):
        return "TLS/certificate problem: the server certificate could not be verified"
    if isinstance(e, (NoConnection, requests.exceptions.ConnectionError)):
        return "Cannot reach the server - check the URL, your network or VPN/proxy"
    if isinstance(e, requests.exceptions.Timeout):
        return "The server did not answer in time"
    if isinstance(e, RemoteResourceNotFound):
        return "Not found on the server"
    if isinstance(e, davlib.Cancelled):
        return "Cancelled"
    if isinstance(e, OSError):
        return e.strerror or str(e)
    return (str(e) or e.__class__.__name__)[:300]


_tls = threading.local()


def client_for(conn, long=False):
    """One WebDAV client (HTTP session) per worker thread, connection and kind.  The default client has short
    timeouts (listings, probes, mkdir must not hang for minutes); long=True is for transfers / MOVE / DELETE."""
    cache = _tls.__dict__.setdefault("clients", {})
    key = (conn, long)
    if key not in cache:
        cache[key] = davlib.make_client_from(conn.url, conn.user, conn.password,
                                             600 if long else davlib.META_TIMEOUT)
    return cache[key]


def retry(fn, tries=3):
    """Retry transient failures (dropped connection, garbled PROPFIND reply seen with rclone under load)."""
    for i in range(tries):
        try:
            return fn()
        except (MethodNotSupported, NoConnection, requests.exceptions.RequestException):
            if i == tries - 1:
                raise
            time.sleep(0.4 * (i + 1))


def ensure_dir(c, path):
    """mkdir -p. Two uploads may race to create the same folder: the loser's MKCOL fails (405) and is retried."""
    try:
        retry(lambda: davlib.ensure_dir(c, path))
    except WebDavException:
        if not retry(lambda: davlib.exists(c, path)):
            raise


def list_remote(conn, path):
    c = client_for(conn)
    return [Entry(n, d, to_int(s), parse_mtime(m)) for n, d, s, m in retry(lambda: davlib.list_entries(c, path))]


# ------------------------------------------------------------------ async plumbing

class _Run(QRunnable):
    def __init__(self, fn, tid, sig):
        super().__init__()
        self.fn, self.tid, self.sig = fn, tid, sig

    def run(self):
        try:
            res, exc = self.fn(), None
        except BaseException as e:  # noqa: BLE001 - reported to the GUI thread
            res, exc = None, e
        self.sig.emit(self.tid, res, exc)


class Runner(QObject):
    """submit(fn, ok, err): fn runs in a worker thread; ok(result)/err(exc) run on the GUI thread."""
    _done = Signal(int, object, object)
    busy_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool, self.cbs, self.ids = QThreadPool(self), {}, itertools.count(1)
        self.pool.setMaxThreadCount(4)
        self._done.connect(self._on_done)

    @property
    def pending(self):
        return len(self.cbs)

    def submit(self, fn, ok=None, err=None):
        tid = next(self.ids)
        self.cbs[tid] = (ok, err)
        self.busy_changed.emit(self.pending)
        self.pool.start(_Run(fn, tid, self._done))
        return tid

    @Slot(int, object, object)
    def _on_done(self, tid, res, exc):
        ok, err = self.cbs.pop(tid, (None, None))
        try:
            if exc is None:
                ok and ok(res)
            elif err:
                err(exc)
            else:
                traceback.print_exception(type(exc), exc, exc.__traceback__)
        except Exception:
            traceback.print_exc()
        self.busy_changed.emit(self.pending)


# ------------------------------------------------------------------ transfers

class Job:
    _ids = itertools.count(1)

    def __init__(self, direction, name, src, dst, total, conn, mode):
        self.id, self.direction, self.name, self.src, self.dst = next(Job._ids), direction, name, src, dst
        self.total, self.conn, self.mode = total, conn, mode  # mode: skip | overwrite | keep
        self.done, self.state, self.error, self.speed = 0, "queued", "", 0.0
        self.cancel = threading.Event()
        self._last = (time.time(), 0)

    @property
    def final(self):
        return self.state in FINAL_STATES

    @property
    def key(self):
        """Identity of the destination: two unfinished jobs must never write the same file (shared .part)."""
        if self.direction == "up":
            return ("up", self.conn, self.dst)
        return ("down", None, os.path.normcase(os.path.abspath(self.dst)))


FINAL_STATES = ("done", "failed", "cancelled", "skipped")
ACTIVE_STATES = ("queued", "running", "cancelling")


def _rm_remote_part(c, part):
    for _ in range(6):  # the server may still be finishing the aborted PUT; retry briefly
        try:
            c.clean(part)
            return
        except RemoteResourceNotFound:
            return
        except Exception:
            time.sleep(0.3)


def do_upload(mgr, job):
    """Upload to <name>.part then MOVE, so a broken transfer never looks like a finished book.
    A folder that already has the target name is never replaced (MOVE never overwrites)."""
    c, cl, remote = client_for(job.conn), client_for(job.conn, True), job.dst
    parent = posixpath.dirname(remote) or "/"
    if (job.conn, parent) not in mgr.ensured:
        ensure_dir(c, parent)
        mgr.ensured.add((job.conn, parent))
    kind = retry(lambda: davlib.remote_kind(c, remote))
    if kind == "dir":
        raise davlib.FolderExists("A folder with this name already exists on the server")
    if kind:
        if job.mode == "keep":
            return "exists - kept"
        if job.mode == "skip":
            try:
                if to_int(retry(lambda: c.info(remote)).get("size")) == job.total:
                    return "same size - skipped"
            except WebDavException:
                pass
    part = remote + ".part"
    for attempt in (1, 2):
        try:
            davlib.put_file(cl, part, job.src, lambda d, t: setattr(job, "done", d), job.cancel.is_set)
            if job.cancel.is_set():
                raise davlib.Cancelled()
            if kind:  # replace the old *file* explicitly, then MOVE without Overwrite so a folder can't be wiped
                now = retry(lambda: davlib.remote_kind(c, remote))
                if now == "dir":
                    raise davlib.FolderExists("A folder with this name already exists on the server")
                if now:
                    cl.clean(remote)
            cl.move(remote_path_from=part, remote_path_to=remote, overwrite=False)
            job.done = job.total
            return ""
        except BaseException as e:
            _rm_remote_part(cl, part)
            # the folder may have vanished since we cached it (409, or the server drops the connection)
            stale = (isinstance(e, ResponseErrorCode) and e.code == 409) or isinstance(e, requests.exceptions.ConnectionError)
            if attempt == 2 or not stale or job.cancel.is_set():
                raise
            mgr.ensured.discard((job.conn, parent))
            ensure_dir(c, parent)
            mgr.ensured.add((job.conn, parent))


def do_download(mgr, job):
    """Download to <file>.part then os.replace, so partial files never look complete."""
    c, dest = client_for(job.conn, True), job.dst
    if os.path.exists(dest):
        if job.mode == "keep":
            return "exists - kept"
        if job.mode == "skip" and os.path.getsize(dest) == job.total:
            return "same size - skipped"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    try:
        davlib.get_file(c, job.src, part, lambda d, t: setattr(job, "done", d), job.cancel.is_set, job.total)
        if job.cancel.is_set():
            raise davlib.Cancelled()
        os.replace(part, dest)
        job.done = job.total
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return ""


class _JobRun(QRunnable):
    def __init__(self, mgr, job):
        super().__init__()
        self.mgr, self.job = mgr, job

    def run(self):
        j, m = self.job, self.mgr
        if j.final:  # cancelled while queued: already reported, must not report (or re-add a row) again
            return
        if j.cancel.is_set():
            return m.finish(j, "cancelled")
        if not m.set_state(j, "running", only_if="queued"):
            return
        try:
            note = (do_upload if j.direction == "up" else do_download)(m, j)
            m.finish(j, "skipped" if note else "done", note)
        except BaseException as e:  # noqa: BLE001 - one failing file must not stop the batch
            m.finish(j, "cancelled" if j.cancel.is_set() or isinstance(e, davlib.Cancelled) else "failed",
                     "" if j.cancel.is_set() else friendly(e))


class TransferManager(QObject):
    changed = Signal(object)  # a job changed state (queued/running/final)
    idle = Signal()           # nothing queued or running any more

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool, self.jobs, self.ensured = QThreadPool(self), [], set()
        self.lock = threading.Lock()
        self.counts = Counter()  # jobs per state, kept incrementally (a rescan per change is quadratic)
        self._keys = set()       # destinations of unfinished jobs
        self.pool.setMaxThreadCount(3)

    def add(self, job):
        """Queue a job.  Returns False (and does nothing) if an unfinished job already writes the same destination."""
        with self.lock:
            if job.key in self._keys:
                return False
            self._keys.add(job.key)
            self.counts["queued"] += 1
        self.jobs.append(job)
        self.changed.emit(job)
        self.pool.start(_JobRun(self, job))
        return True

    def active(self):
        return sum(self.counts[s] for s in ACTIVE_STATES)

    def set_state(self, job, state, error="", only_if=None):
        """Change a job's state.  Returns False if nothing changed (a final state is never left again)."""
        with self.lock:
            if job.final or (only_if and job.state != only_if):  # e.g. cancel arrived just after the job finished
                return False
            self.counts[job.state] -= 1
            self.counts[state] += 1
            job.state, job.error = state, error
            if job.final:
                self._keys.discard(job.key)
        if state == "running":
            job._last = (time.time(), job.done)
        self.changed.emit(job)
        return True

    def finish(self, job, state, note=""):
        if self.set_state(job, state, note) and self.active() == 0:
            self.idle.emit()

    def cancel(self, job):
        job.cancel.set()
        if self.set_state(job, "cancelled", only_if="queued"):
            if self.active() == 0:
                self.idle.emit()
        else:  # running: the worker stops at its next chunk (or when the server answers)
            self.set_state(job, "cancelling", only_if="running")

    def cancel_all(self):
        for j in list(self.jobs):
            if not j.final:
                self.cancel(j)

    def clear_finished(self):
        keep, gone = [], []
        for j in self.jobs:
            (gone if j.final else keep).append(j)
        with self.lock:
            for j in gone:
                self.counts[j.state] -= 1
        self.jobs[:] = keep
        return len(gone)


STATE_TEXT = {"queued": "Queued", "running": "Running", "done": "Done", "failed": "Failed",
              "cancelled": "Cancelled", "skipped": "Skipped", "cancelling": "Cancelling..."}


def job_progress(job):
    """(value 0..1000, text) for the progress cell."""
    if job.state == "queued":
        return 0, "waiting..."
    if job.state in ("done", "skipped"):
        return 1000, human(job.total) if job.state == "done" else "-"
    if job.total:
        return (min(1000, int(job.done * 1000 / job.total)),
                f"{min(100, job.done * 100 // job.total)}%  -  {human(job.done)} / {human(job.total)}")
    return 0, human(job.done)


class TransferModel(QAbstractTableModel):
    """Rows mirror mgr.jobs.  One model + one delegate instead of widgets per row keeps thousands of jobs cheap."""
    HEAD = ["Name", "Direction", "Folder", "Progress", "Speed", "State", ""]

    def __init__(self, mgr, style, parent=None):
        super().__init__(parent)
        self.mgr, self.n, self.rows = mgr, 0, {}
        self.icons = {"up": style.standardIcon(QStyle.SP_ArrowUp), "down": style.standardIcon(QStyle.SP_ArrowDown),
                      "cancel": style.standardIcon(QStyle.SP_DialogCancelButton)}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.n

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEAD)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEAD[section]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid() or idx.row() >= min(self.n, len(self.mgr.jobs)):
            return None
        j, col = self.mgr.jobs[idx.row()], idx.column()
        if col == 3 and role == Qt.UserRole:
            return job_progress(j)
        if role == Qt.DisplayRole:
            if col == 0:
                return j.name
            if col == 1:
                return "Upload" if j.direction == "up" else "Download"
            if col == 2:
                return (j.dst.rsplit("/", 1)[0] or "/") if j.direction == "up" else os.path.dirname(j.dst)
            if col == 4:
                return human(j.speed) + "/s" if j.state == "running" and j.speed else ""
            if col == 5:
                return STATE_TEXT[j.state] + (f" - {j.error}" if j.error else "")
        elif role == Qt.DecorationRole:
            if col == 1:
                return self.icons[j.direction]
            if col == 6 and not j.final:
                return self.icons["cancel"]
        elif role == Qt.ToolTipRole:
            if col == 0:
                return f"{j.src}\n->  {j.dst}"
            if col in (2, 5):
                return self.data(idx, Qt.DisplayRole)
            if col == 6 and not j.final:
                return "Cancel this transfer"
        return None

    def reset(self):
        self.beginResetModel()
        self.n = len(self.mgr.jobs)
        self.rows = {j.id: i for i, j in enumerate(self.mgr.jobs)}
        self.endResetModel()

    def sync(self, dirty):
        """Insert rows for new jobs (one signal for the whole batch) and refresh the changed ones.
        Returns True if rows were added."""
        jobs, old_n = self.mgr.jobs, self.n
        rows = sorted(r for r in (self.rows.get(j.id) for j in dirty) if r is not None and r < old_n)
        if len(jobs) > old_n:
            self.beginInsertRows(QModelIndex(), old_n, len(jobs) - 1)
            for i in range(old_n, len(jobs)):
                self.rows[jobs[i].id] = i
            self.n = len(jobs)
            self.endInsertRows()
        last = len(self.HEAD) - 1
        if len(rows) > 64:  # e.g. "cancel all": one span instead of thousands of signals
            self.dataChanged.emit(self.index(rows[0], 0), self.index(rows[-1], last))
        else:
            for r in rows:
                self.dataChanged.emit(self.index(r, 0), self.index(r, last))
        return len(jobs) > old_n


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)  # row background / alternate colours
        value, text = index.data(Qt.UserRole)
        o = QStyleOptionProgressBar()
        o.rect = option.rect.adjusted(2, 3, -2, -3)
        o.palette, o.state = option.palette, QStyle.State_Enabled
        o.minimum, o.maximum, o.progress = 0, 1000, value
        o.text, o.textVisible, o.textAlignment = text, True, Qt.AlignCenter
        (option.widget.style() if option.widget else QApplication.style()).drawControl(QStyle.CE_ProgressBar, o, painter)


class TransferPanel(QWidget):
    def __init__(self, mgr, parent=None):
        super().__init__(parent)
        self.mgr, self.dirty = mgr, {}
        st = self.style()
        self.model = TransferModel(mgr, st, self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(3, ProgressDelegate(self.table))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Interactive)
        h.setSectionResizeMode(5, QHeaderView.Stretch)
        for col, w in ((0, 240), (1, 90), (2, 200), (3, 230), (4, 80), (6, 36)):
            self.table.setColumnWidth(col, w)
        self.table.clicked.connect(self._clicked)
        bar = QHBoxLayout()
        self.summary = QLabel("No transfers")
        b1 = QPushButton(st.standardIcon(QStyle.SP_DialogCancelButton), "Cancel all")
        b1.setToolTip("Cancel every queued and running transfer")
        b2 = QPushButton(st.standardIcon(QStyle.SP_DialogResetButton), "Clear finished")
        b2.setToolTip("Remove finished, failed and cancelled rows from this list")
        b1.clicked.connect(mgr.cancel_all)
        b2.clicked.connect(self.clear_finished)
        bar.addWidget(self.summary, 1)
        bar.addWidget(b1)
        bar.addWidget(b2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(bar)
        lay.addWidget(self.table)
        mgr.changed.connect(self.on_changed)
        self.flush_timer = QTimer(self, singleShot=True, interval=100)  # at most ~10 refreshes/s, however many jobs
        self.flush_timer.timeout.connect(self.flush)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(400)

    def on_changed(self, job):
        self.dirty[job.id] = job
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def flush(self):
        dirty, self.dirty = self.dirty, {}
        if self.model.sync(dirty.values()):
            self.table.scrollToBottom()
        self._summary()

    def _clicked(self, idx):
        if idx.column() == 6 and idx.row() < len(self.mgr.jobs):
            j = self.mgr.jobs[idx.row()]
            if not j.final:
                self.mgr.cancel(j)

    def tick(self):
        now = time.time()
        for j in self.mgr.jobs:
            if j.state == "running":
                t0, d0 = j._last
                if now - t0 >= 0.4:
                    inst = (j.done - d0) / (now - t0)
                    j.speed = inst if j.speed == 0 else 0.6 * j.speed + 0.4 * inst
                    j._last = (now, j.done)
                self.on_changed(j)

    def _summary(self):
        c = self.mgr.counts
        self.summary.setText(f"{c['running']} running, {c['queued']} queued, {c['done'] + c['skipped']} done, "
                             f"{c['failed']} failed, {c['cancelled']} cancelled" if self.mgr.jobs else "No transfers")

    def clear_finished(self):
        self.mgr.clear_finished()
        self.dirty.clear()
        self.model.reset()
        self._summary()


# ------------------------------------------------------------------ widgets

class FolderTree(QTreeWidget):
    """Folder-only tree whose children are loaded lazily when a node is expanded."""
    need_children = Signal(str)
    path_selected = Signal(str)

    def __init__(self, root_label, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self._silent = False
        self.set_root(root_label)
        self.itemExpanded.connect(self._expanded)
        self.currentItemChanged.connect(self._current)

    def set_root(self, root_label):
        self._silent = True
        self.clear()
        self._silent = False
        self.root_item = self._make(None, root_label, "/")
        self.root_item.setIcon(0, self.style().standardIcon(QStyle.SP_DriveNetIcon))

    def _make(self, parent, name, path):
        item = QTreeWidgetItem(parent or self, [name])
        item.setData(0, ROLE_PATH, path)
        item.setData(0, ROLE_LOADED, False)
        item.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))
        QTreeWidgetItem(item, ["loading..."]).setData(0, ROLE_PATH, None)  # placeholder shows the arrow
        return item

    def find(self, path):
        item = self.root_item
        for part in [p for p in path.split("/") if p]:
            item = next((item.child(i) for i in range(item.childCount())
                         if item.child(i).data(0, ROLE_PATH) and item.child(i).text(0) == part), None)
            if item is None:
                return None
        return item

    def set_children(self, path, names):
        item = self.find(path)
        if item is None:
            return
        have = {item.child(i).text(0): item.child(i) for i in range(item.childCount())
                if item.child(i).data(0, ROLE_PATH)}
        for i in reversed(range(item.childCount())):  # drop placeholder + vanished folders
            c = item.child(i)
            if not c.data(0, ROLE_PATH) or c.text(0) not in names:
                item.takeChild(i)
        for n in sorted(set(names) - set(have), key=str.casefold):
            self._make(item, n, join(path, n))
        item.sortChildren(0, Qt.AscendingOrder)
        item.setData(0, ROLE_LOADED, True)

    def _expanded(self, item):
        if not item.data(0, ROLE_LOADED):
            self.need_children.emit(item.data(0, ROLE_PATH))

    def _current(self, item, _prev):
        if item is not None and not self._silent and item.data(0, ROLE_PATH):
            self.path_selected.emit(item.data(0, ROLE_PATH))

    def select_path(self, path):
        """Select (and expand towards) path as far as the tree is loaded; emits nothing."""
        self._silent = True
        try:
            item, node = self.root_item, self.root_item
            for part in [p for p in path.split("/") if p]:
                nxt = next((node.child(i) for i in range(node.childCount())
                            if node.child(i).data(0, ROLE_PATH) and node.child(i).text(0) == part), None)
                if nxt is None:
                    break
                node.setExpanded(True)
                item = node = nxt
            self.setCurrentItem(item)
            self.scrollToItem(item)
        finally:
            self._silent = False


class _PathEdit(QLineEdit):
    cancelled = Signal()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(ev)


class PathBar(QStackedWidget):
    """Clickable breadcrumb; click on empty space (or Ctrl+L) to type a path."""
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.crumbs = QWidget()
        self.lay = QHBoxLayout(self.crumbs)
        self.lay.setContentsMargins(4, 0, 4, 0)
        self.lay.setSpacing(0)
        self.cwd = "/"
        self.edit = _PathEdit()
        self.edit.setFrame(False)
        self.addWidget(self.crumbs)
        self.addWidget(self.edit)
        self.setFixedHeight(30)
        self.setToolTip("Click a folder to jump there; click empty space to type a path")
        self.edit.returnPressed.connect(lambda: self.navigate.emit(self.edit.text()))
        self.edit.editingFinished.connect(lambda: self.setCurrentIndex(0))
        self.edit.cancelled.connect(lambda: self.setCurrentIndex(0))

    def set_path(self, path):
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        cur = ""
        for label, target in [("/", "/")] + [(p, None) for p in path.split("/") if p]:
            if target is None:
                cur += "/" + label
                target = cur
                sep = QLabel("›")
                self.lay.addWidget(sep)
            b = QToolButton()
            b.setText(label)
            b.setAutoRaise(True)
            if label == "/":
                b.setIcon(self.style().standardIcon(QStyle.SP_DriveNetIcon))
            b.clicked.connect(lambda _=False, t=target: self.navigate.emit(t))
            self.lay.addWidget(b)
        self.lay.addStretch(1)
        self.cwd = path
        self.edit.setText(path)

    def start_edit(self):
        self.edit.setText(self.cwd)  # discard whatever was typed and abandoned last time
        self.setCurrentIndex(1)
        self.edit.setFocus()
        self.edit.selectAll()

    def mousePressEvent(self, ev):
        if self.currentIndex() == 0:
            self.start_edit()


class DropTable(QTableView):
    """Table that accepts files/folders dragged from Windows Explorer."""
    dropped = Signal(list, str)  # local paths, target folder name ("" = current folder)
    hint = ""

    def _ok(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            ev.ignore()

    dragEnterEvent = dragMoveEvent = _ok

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        idx = self.indexAt(ev.position().toPoint())
        target = ""
        if idx.isValid():
            entry = self.model().index(idx.row(), 0).data(ROLE_ENTRY)
            target = entry.name if entry.is_dir else ""
        if paths:
            ev.acceptProposedAction()
            self.dropped.emit(paths, target)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self.hint and self.model() is not None and self.model().rowCount() == 0:
            p = QPainter(self.viewport())
            p.setPen(self.palette().color(QPalette.Disabled, QPalette.Text))
            p.drawText(self.viewport().rect().adjusted(20, 20, -20, -20), Qt.AlignCenter | Qt.TextWordWrap, self.hint)
            p.end()


class SortProxy(QSortFilterProxyModel):
    """Folders always first (also when descending); sort by typed key, not by display text."""

    def lessThan(self, left, right):
        a, b = left.siblingAtColumn(0).data(ROLE_ENTRY), right.siblingAtColumn(0).data(ROLE_ENTRY)
        if a.is_dir != b.is_dir:
            return a.is_dir == (self.sortOrder() == Qt.AscendingOrder)
        ka, kb = left.data(ROLE_SORT), right.data(ROLE_SORT)
        return a.name.casefold() < b.name.casefold() if ka == kb else ka < kb


class ConnectDialog(QDialog):
    def __init__(self, url, user, password, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to WebDAV server")
        self.setMinimumWidth(460)
        self.url, self.user, self.pw = QLineEdit(url), QLineEdit(user), QLineEdit(password)
        self.pw.setEchoMode(QLineEdit.Password)
        self.insecure = QCheckBox("Allow insecure connection (plain http to a remote host)")
        self.remember = QCheckBox("Remember password in the OS keyring")
        self.remember.setVisible(keyring is not None)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c0392b;")
        self.error.hide()
        form = QFormLayout()
        form.addRow("Server URL", self.url)
        form.addRow("User", self.user)
        form.addRow("Password", self.pw)
        form.addRow("", self.insecure)
        form.addRow("", self.remember)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Connect")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(bb)
        (self.pw if user else self.user).setFocus()

    def show_error(self, text):
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def _accept(self):
        url = self.url.text().strip()
        if url and "://" not in url:
            url = "https://" + url
            self.url.setText(url)
        problem = insecure_problem(url, self.insecure.isChecked())
        if not problem and not self.user.text().strip():
            problem = "Enter a user name."
        self.show_error(problem)
        if not problem:
            self.accept()

    def conn(self):
        return Conn(self.url.text().strip().rstrip("/"), self.user.text().strip(), self.pw.text(),
                    self.insecure.isChecked())


class FolderPicker(QDialog):
    """Pick a remote folder (used by Move). Children load lazily through the main window."""

    def __init__(self, win, title, start):
        super().__init__(win)
        self.setWindowTitle(title)
        self.resize(420, 480)
        self.tree = FolderTree(win.conn.host)
        self.label = QLabel("/")
        for path in sorted(win.dirs, key=lambda p: p.count("/")):
            self.tree.set_children(path, win.dirs[path])
        self.tree.need_children.connect(win.load)
        win.listed.connect(self._listed)
        self.tree.path_selected.connect(self.label.setText)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Choose the destination folder:"))
        lay.addWidget(self.tree)
        lay.addWidget(self.label)
        lay.addWidget(bb)
        self.tree.select_path(start)
        self.label.setText(start)
        self.finished.connect(lambda _: win.listed.disconnect(self._listed))

    @Slot(str, list)
    def _listed(self, path, entries):
        self.tree.set_children(path, [e.name for e in entries if e.is_dir])

    def path(self):
        return self.label.text() or "/"


# ------------------------------------------------------------------ main window

class MainWindow(QMainWindow):
    listed = Signal(str, list)   # any directory listing finished (path, entries)
    connected = Signal()
    COLS = ["Name", "Size", "Modified", "Type"]

    def __init__(self):
        super().__init__()
        self.cfg, self.conn, self.cwd = load_cfg(), None, "/"
        self.hist, self.hidx, self.dirs, self.entries = [], -1, {}, []
        self._seq, self._keep_sel, self.quiet, self.last_error = {}, set(), False, ""
        self._connect_seq, self._tried_insecure = 0, False
        self.policy = self.cfg.get("policy", "skip")
        self.runner, self.mgr = Runner(self), TransferManager(self)
        self.setWindowTitle(APP_NAME + " (not connected)")
        self.setAcceptDrops(False)
        self.resize(1180, 720)
        self._build_actions()
        self._build_ui()
        self.refresh_timer = QTimer(self, singleShot=True, interval=600)
        self.refresh_timer.timeout.connect(self.refresh)
        self.runner.busy_changed.connect(lambda n: self.busy.setVisible(n > 0))
        self.mgr.changed.connect(self._job_changed)
        self.mgr.idle.connect(self._transfers_idle)
        try:
            self.restoreGeometry(QByteArray.fromBase64(self.cfg.get("geometry", "").encode()))
            self.splitter.restoreState(QByteArray.fromBase64(self.cfg.get("splitter", "").encode()))
        except Exception:
            pass
        self._update_actions()

    # ---- construction
    def _act(self, text, icon, slot, shortcut=None, tip=""):
        a = QAction(self.style().standardIcon(icon) if isinstance(icon, QStyle.StandardPixmap) else QIcon(), text, self)
        a.setToolTip(tip or text.replace("&", ""))
        a.triggered.connect(lambda _=False: slot())
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        return a

    def _build_actions(self):
        S = QStyle
        self.a_back = self._act("Back", S.SP_ArrowBack, self.back, "Alt+Left", "Previous folder (Alt+Left)")
        self.a_fwd = self._act("Forward", S.SP_ArrowForward, self.forward, "Alt+Right", "Next folder (Alt+Right)")
        self.a_up = self._act("Up", S.SP_FileDialogToParent, self.up, "Alt+Up", "Parent folder (Alt+Up)")
        self.a_refresh = self._act("Refresh", S.SP_BrowserReload, self.refresh, "F5", "Reload this folder (F5)")
        self.a_upf = self._act("Upload files", S.SP_DialogOpenButton, self.upload_files_dialog, "Ctrl+U",
                               "Upload files to the current folder (Ctrl+U)")
        self.a_upd = self._act("Upload folder", S.SP_DirOpenIcon, self.upload_folder_dialog, "Ctrl+Shift+U",
                               "Upload a whole folder recursively (Ctrl+Shift+U)")
        self.a_down = self._act("Download", S.SP_DialogSaveButton, self.download_dialog, "Ctrl+D",
                                "Download the selected files/folders (Ctrl+D)")
        self.a_mkdir = self._act("New folder", S.SP_FileDialogNewFolder, self.new_folder_dialog, "Ctrl+Shift+N",
                                 "Create a folder (Ctrl+Shift+N)")
        self.a_rename = self._act("Rename", S.SP_FileDialogDetailedView, self.rename_dialog, None, "Rename (F2)")
        self.a_move = self._act("Move...", S.SP_FileDialogContentsView, self.move_dialog, "Ctrl+M",
                                "Move the selection to another folder (Ctrl+M)")
        self.a_delete = self._act("Delete", S.SP_TrashIcon, self.delete_dialog, None, "Delete from server (Del)")
        self.a_connect = self._act("Connect...", S.SP_DriveNetIcon, self.connect_dialog, "Ctrl+K",
                                   "Connect to a server (Ctrl+K)")
        self.a_open = self._act("Open", S.SP_DirOpenIcon, self.open_selected)
        self.a_find = self._act("Filter", S.SP_FileDialogContentsView, lambda: self.search.setFocus(), "Ctrl+F")
        self.a_path = self._act("Edit path", S.SP_FileDialogContentsView, lambda: self.pathbar.start_edit(), "Ctrl+L")
        self.policy_group = QActionGroup(self)
        self.policy_acts = {}
        for key, text in (("skip", "Skip if same size (replace if different)"), ("overwrite", "Always overwrite"),
                          ("keep", "Never overwrite (keep existing)")):
            a = QAction(text, self, checkable=True, checked=(key == self.policy))
            a.triggered.connect(lambda _=False, k=key: setattr(self, "policy", k))
            self.policy_group.addAction(a)
            self.policy_acts[key] = a

    def _build_ui(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction(self.a_connect)
        m.addSeparator()
        m.addAction(self.a_upf)
        m.addAction(self.a_upd)
        m.addAction(self.a_down)
        m.addSeparator()
        m.addAction(self._act("Quit", QStyle.SP_DialogCloseButton, self.close, "Ctrl+Q"))
        m = mb.addMenu("&Edit")
        for a in (self.a_mkdir, self.a_rename, self.a_move, self.a_delete, self.a_find, self.a_path):
            m.addAction(a)
        m = mb.addMenu("&Options")
        m.addSection("If a file already exists (up/download)")
        for a in self.policy_acts.values():
            m.addAction(a)

        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_refresh, None, self.a_upf, self.a_upd, self.a_down, None,
                  self.a_mkdir, self.a_rename, self.a_move, self.a_delete, None, self.a_connect):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.addToolBar(tb)

        self.tree = FolderTree("(not connected)")
        self.tree.root_item.takeChildren()  # nothing to expand until connected
        self.tree.need_children.connect(self.load)
        self.tree.path_selected.connect(self.go)
        self.model = QStandardItemModel(0, 4, self)
        self.model.setHorizontalHeaderLabels(self.COLS)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)
        self.table = DropTable()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setAcceptDrops(True)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        h = self.table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in ((1, 90), (2, 150), (3, 120)):
            h.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, w)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        self.table.dropped.connect(self._dropped)
        self.table.selectionModel().selectionChanged.connect(lambda *_: (self._update_actions(), self._update_counts()))
        for key, slot in ((Qt.Key_Delete, self.delete_dialog), (Qt.Key_F2, self.rename_dialog),
                          (Qt.Key_Return, self.open_selected), (Qt.Key_Enter, self.open_selected)):
            QShortcut(QKeySequence(key), self.table, activated=slot, context=Qt.WidgetShortcut)

        self.pathbar = PathBar()
        self.pathbar.navigate.connect(self.go)
        self.pathbar.set_path("/")
        self.search = QLineEdit(placeholderText="Filter this folder by name...", clearButtonEnabled=True)
        self.search.setFixedWidth(260)
        self.search.setToolTip("Type to filter the current folder (Ctrl+F)")
        self.search.textChanged.connect(self._filter_changed)
        top = QHBoxLayout()
        top.addWidget(self.pathbar, 1)
        top.addWidget(self.search)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 0)
        rl.addLayout(top)
        rl.addWidget(self.table)
        self.splitter = QSplitter()
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([260, 900])
        self.setCentralWidget(self.splitter)

        self.panel = TransferPanel(self.mgr)
        self.dock = QDockWidget("Transfers", self)
        self.dock.setObjectName("transfers")
        self.dock.setWidget(self.panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
        self.resizeDocks([self.dock], [190], Qt.Vertical)
        mb.addMenu("&View").addAction(self.dock.toggleViewAction())

        sb = self.statusBar()
        self.lbl_count = QLabel("")
        self.lbl_count.setMinimumWidth(420)
        self.lbl_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(90)
        self.busy.setMaximumHeight(12)
        self.busy.setTextVisible(False)
        self.busy.hide()
        sb.addPermanentWidget(self.busy)
        sb.addPermanentWidget(self.lbl_count)
        self.table.hint = self._empty_hint()

    def _filter_changed(self, text):
        self.proxy.setFilterFixedString(text)
        self._update_hint()
        self._update_counts()

    def _update_hint(self):
        self.table.hint = ("No file matches the filter."
                           if self.search.text() and not self.proxy.rowCount() else self._empty_hint())

    def _empty_hint(self):
        if self.conn is None:
            return "Not connected.\nUse File > Connect... (Ctrl+K)."
        return "This folder is empty.\nDrag files or folders here from Explorer to upload them."

    # ---- connection
    def startup(self):
        user, pw = os.environ.get("DAV_USER", ""), os.environ.get("DAV_PASS", "")
        if user and pw:
            # credentials from the environment go only to DAV_URL (or the default server, like the CLI), never to
            # whatever server was used last time
            self.connect_to(Conn(os.environ.get("DAV_URL", davlib.DEFAULT_URL).rstrip("/"), user, pw))
        else:
            self.connect_dialog()

    def connect_dialog(self, error=""):
        url = strip_userinfo(os.environ.get("DAV_URL") or self.cfg.get("url") or davlib.DEFAULT_URL)
        user = os.environ.get("DAV_USER") or self.cfg.get("user", "")
        pw = os.environ.get("DAV_PASS", "")
        if not pw and keyring is not None and user:
            try:
                pw = keyring.get_password("davgui:" + url, user) or ""
            except Exception:
                pw = ""
        dlg = ConnectDialog(url, user, pw, self)
        dlg.insecure.setChecked(self._tried_insecure)
        dlg.show_error(error)
        if dlg.exec() == QDialog.Accepted:
            if dlg.remember.isChecked() and keyring is not None:
                try:
                    keyring.set_password("davgui:" + dlg.conn().url, dlg.conn().user, dlg.conn().password)
                except Exception:
                    pass
            self.connect_to(dlg.conn())

    def connect_to(self, conn):
        self._tried_insecure = conn.insecure
        problem = insecure_problem(conn.url, conn.insecure or os.environ.get("DAV_ALLOW_INSECURE") == "1")
        if problem:
            return self._connect_failed(problem)
        self._connect_seq = seq = self._connect_seq + 1  # a newer Connect supersedes this attempt
        self.statusBar().showMessage(f"Connecting to {conn.host} ...")
        self.runner.submit(lambda: client_for(conn).list("/"),
                           lambda _: seq == self._connect_seq and self._connected(conn),
                           lambda e: seq == self._connect_seq and self._connect_failed(friendly(e)))

    def _connect_failed(self, text):
        self.statusBar().showMessage("Connection failed: " + text)
        self.last_error = text
        if not self.quiet:
            self.connect_dialog(error="Connection failed: " + text)

    def _connected(self, conn):
        self.conn = conn
        self.cfg.update(url=conn.url, user=conn.user)
        save_cfg({k: v for k, v in self.cfg.items() if k != "password"})
        self.setWindowTitle(f"{APP_NAME} - {conn.user}@{conn.host}")
        self.dirs, self.hist, self.hidx = {}, [], -1
        self.tree.set_root(conn.host)
        self.statusBar().showMessage(f"Connected to {conn.url}", 5000)
        self.go("/")
        self.connected.emit()

    # ---- navigation / listing
    def go(self, path, push=True):
        if self.conn is None:
            return
        path = davlib.norm(path)
        if push and (self.hidx < 0 or self.hist[self.hidx] != path):
            self.hist = self.hist[:self.hidx + 1] + [path]
            self.hidx = len(self.hist) - 1
        self.cwd = path
        self.search.clear()
        self.model.setRowCount(0)
        self.table.hint = "Loading..."
        self.lbl_count.setText("Loading...")
        self.pathbar.set_path(path)
        parents = ["/"] + ["/" + "/".join(path.strip("/").split("/")[:i]) for i in range(1, path.strip("/").count("/") + 1)]
        for a in parents:
            if a != path and a not in self.dirs:
                self.load(a)  # so the tree can expand down to the folder we jumped to
        self.refresh()

    def back(self):
        if self.hidx > 0:
            self.hidx -= 1
            self.go(self.hist[self.hidx], push=False)

    def forward(self):
        if self.hidx < len(self.hist) - 1:
            self.hidx += 1
            self.go(self.hist[self.hidx], push=False)

    def up(self):
        if self.cwd != "/":
            self.go(posixpath.dirname(self.cwd) or "/")

    def refresh(self, keep=None):
        """Reload the current folder; keep = names to select afterwards (default: the current selection)."""
        if self.conn:
            self._keep_sel = set(keep) if keep else {e.name for e in self.selected()}
            self.load(self.cwd)

    @Slot(str)
    def load(self, path):
        conn = self.conn
        if conn is None:
            return
        self._seq[path] = seq = self._seq.get(path, 0) + 1
        self.runner.submit(lambda: list_remote(conn, path), lambda es: self._listed(path, es, seq),
                           lambda e: self._list_failed(path, e, seq))

    def _listed(self, path, entries, seq):
        if seq != self._seq.get(path):  # a newer listing of this folder was requested; this one may be stale
            return
        self.dirs[path] = [e.name for e in entries if e.is_dir]
        self.tree.set_children(path, self.dirs[path])
        self.listed.emit(path, entries)
        if path == self.cwd:
            self._populate(entries)
            self.tree.select_path(self.cwd)
        elif self.cwd.startswith(path.rstrip("/") + "/"):
            self.tree.select_path(self.cwd)

    def _list_failed(self, path, exc, seq):
        text = friendly(exc)
        self.statusBar().showMessage(f"Cannot list {path}: {text}")
        if path == self.cwd and seq == self._seq[path]:
            self.last_error = text
            self.entries = []
            self.model.setRowCount(0)
            self.table.hint = f"Cannot list this folder:\n{text}"
            self.lbl_count.setText("")
            self.table.viewport().update()

    def _populate(self, entries):
        self.entries = entries
        keep, self._keep_sel = self._keep_sel, set()
        self.proxy.setDynamicSortFilter(False)
        self.model.setRowCount(0)
        icon_d, icon_f = (self.style().standardIcon(s) for s in (QStyle.SP_DirIcon, QStyle.SP_FileIcon))
        for e in entries:
            ext = posixpath.splitext(e.name)[1][1:].upper()
            kind = "Folder" if e.is_dir else "Partial upload" if e.name.endswith(".part") else f"{ext} file" if ext else "File"
            items = [QStandardItem(icon_d if e.is_dir else icon_f, e.name),
                     QStandardItem("" if e.is_dir else human(e.size)),
                     QStandardItem(datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d %H:%M") if e.mtime else ""),
                     QStandardItem(kind)]
            for it, key in zip(items, (e.name.casefold(), e.size, e.mtime, kind)):
                it.setEditable(False)
                it.setData(key, ROLE_SORT)
            items[0].setData(e, ROLE_ENTRY)
            items[0].setToolTip(join(self.cwd, e.name))
            items[1].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.model.appendRow(items)
        self.proxy.setDynamicSortFilter(True)
        try:
            if keep:  # restore the selection with ONE select() call (one selectionChanged, not one per row)
                picked = QItemSelection()
                for r in range(self.proxy.rowCount()):
                    if self.proxy.index(r, 0).data(ROLE_ENTRY).name in keep:
                        picked.select(self.proxy.index(r, 0), self.proxy.index(r, self.proxy.columnCount() - 1))
                if not picked.isEmpty():
                    flags = QItemSelectionModel.SelectionFlag
                    self.table.selectionModel().select(picked, flags.Select | flags.Rows)
        finally:
            self._update_hint()
            self._update_actions()
            self._update_counts()
            self.table.viewport().update()

    def selected(self):
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        return [self.proxy.index(r, 0).data(ROLE_ENTRY) for r in sorted(rows)]

    def _update_counts(self):
        shown = [self.proxy.index(r, 0).data(ROLE_ENTRY) for r in range(self.proxy.rowCount())]
        files = [e for e in shown if not e.is_dir]
        text = f"{len(shown)} items ({len(shown) - len(files)} folders, {len(files)} file{"" if len(files) == 1 else "s"}) - {human(sum(e.size for e in files))}"
        sel = self.selected()
        if sel:
            text = f"{len(sel)} selected ({human(sum(e.size for e in sel if not e.is_dir))}) | " + text
        self.lbl_count.setText(text if self.conn else "")

    def _update_actions(self):
        on, sel = self.conn is not None, self.selected() if hasattr(self, "table") else []
        if hasattr(self, "table"):
            self.table.setAcceptDrops(on)
        for a in (self.a_refresh, self.a_upf, self.a_upd, self.a_mkdir):
            a.setEnabled(on)
        self.a_back.setEnabled(on and self.hidx > 0)
        self.a_fwd.setEnabled(on and self.hidx < len(self.hist) - 1)
        self.a_up.setEnabled(on and self.cwd != "/")
        self.a_down.setEnabled(on and bool(sel))
        self.a_move.setEnabled(on and bool(sel))
        self.a_delete.setEnabled(on and bool(sel))
        self.a_rename.setEnabled(on and len(sel) == 1)

    def open_selected(self):
        sel = self.selected()
        if len(sel) == 1 and sel[0].is_dir:
            self.go(join(self.cwd, sel[0].name))

    def _context_menu(self, pos):
        m, sel = QMenu(self), self.selected()
        if len(sel) == 1 and sel[0].is_dir:
            m.addAction(self.a_open)
        for a in (self.a_down, self.a_rename, self.a_move, self.a_delete):
            m.addAction(a)
        m.addSeparator()
        for a in (self.a_upf, self.a_upd, self.a_mkdir, self.a_refresh):
            m.addAction(a)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---- error reporting
    def error(self, title, exc_or_text):
        text = exc_or_text if isinstance(exc_or_text, str) else friendly(exc_or_text)
        self.last_error = text
        self.statusBar().showMessage(f"{title}: {text}", 10000)
        if not self.quiet:
            QMessageBox.warning(self, title, text)

    # ---- transfers
    def upload_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files to " + self.cwd, self.cfg.get("up_dir", ""))
        if files:
            self.cfg["up_dir"] = os.path.dirname(files[0])
            self.upload_paths(files)

    def upload_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Upload folder to " + self.cwd, self.cfg.get("up_dir", ""))
        if d:
            self.cfg["up_dir"] = os.path.dirname(d)
            self.upload_paths([d])

    def _dropped(self, paths, target):
        self.upload_paths(paths, join(self.cwd, target) if target else None)

    def upload_paths(self, paths, dest=None):
        if self.conn is None:
            self.statusBar().showMessage("Not connected - connect to a server first (Ctrl+K).", 5000)
            return
        dest, conn, mode = dest or self.cwd, self.conn, self.policy

        def enumerate_local():
            out, empty = [], []
            for p in map(Path, paths):
                if p.is_file():
                    out.append((str(p), join(dest, p.name), p.stat().st_size))
                elif p.is_dir():
                    for dp, dns, fns in os.walk(p):
                        dns.sort()
                        rel = Path(dp).relative_to(p).as_posix()
                        if not dns and not fns:  # empty folder: nothing would ever create it on the server
                            empty.append(join(dest, p.name if rel == "." else f"{p.name}/{rel}"))
                        for fn in sorted(fns):
                            fp = Path(dp, fn)
                            out.append((str(fp), join(dest, f"{p.name}/{fp.relative_to(p).as_posix()}"), fp.stat().st_size))
            made, errors = 0, []
            c = client_for(conn)
            for d in empty:
                try:
                    ensure_dir(c, d)
                    made += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{d}: {friendly(e)}")
            return out, made, errors

        def queue(res):
            items, made, errors = res
            added = sum(self.mgr.add(Job("up", dst[len(dest):].lstrip("/"), src, dst, size, conn, mode))
                        for src, dst, size in items)
            msg = f"Queued {added} upload(s) to {dest}" if added else "Nothing to upload"
            if len(items) - added:
                msg += f" ({len(items) - added} already queued)"
            if made:
                msg += f"; created {made} empty folder(s)"
                self.refresh_timer.start()
            if errors:
                msg += f"; could not create: {errors[0]}"
            self.statusBar().showMessage(msg, 8000 if errors else 5000)

        self.statusBar().showMessage("Scanning local files ...")
        self.runner.submit(enumerate_local, queue, lambda e: self.error("Upload", e))
        self.dock.show()

    def download_dialog(self):
        sel = self.selected()
        if not sel:
            return
        d = QFileDialog.getExistingDirectory(self, "Download to ...", self.cfg.get("dl_dir", ""))
        if d:
            self.cfg["dl_dir"] = d
            self.download_entries(sel, d)

    def download_entries(self, entries, local_dir, remote_dir=None):
        remote_dir, conn, mode = remote_dir or self.cwd, self.conn, self.policy

        def enumerate_remote():
            c, out = client_for(conn), []

            def walk(rpath, lpath):
                os.makedirs(lpath, exist_ok=True)
                used = set()  # distinct remote names must stay distinct locally (a:b vs a_b, A vs a)
                for n, is_dir, size, _ in retry(lambda: davlib.list_entries(c, rpath)):
                    ln = unique_local_name(used, n, is_dir)
                    if is_dir:
                        walk(join(rpath, n), os.path.join(lpath, ln))
                    else:
                        out.append((join(rpath, n), os.path.join(lpath, ln), to_int(size)))

            used = set()
            for e in entries:
                ln = unique_local_name(used, e.name, e.is_dir)
                if e.is_dir:
                    walk(join(remote_dir, e.name), os.path.join(local_dir, ln))
                else:
                    out.append((join(remote_dir, e.name), os.path.join(local_dir, ln), e.size))
            return out

        def queue(items):
            added = sum(self.mgr.add(Job("down", src[len(remote_dir):].lstrip("/"), src, dst, size, conn, mode))
                        for src, dst, size in items)
            msg = f"Queued {added} download(s) to {local_dir}"
            if len(items) - added:
                msg += f" ({len(items) - added} already queued)"
            self.statusBar().showMessage(msg, 5000)

        self.statusBar().showMessage("Scanning remote folders ...")
        self.runner.submit(enumerate_remote, queue, lambda e: self.error("Download", e))
        self.dock.show()

    def _job_changed(self, job):
        if job.final and job.direction == "up" and job.state == "done" and job.dst.startswith(self.cwd.rstrip("/") + "/"):
            self.refresh_timer.start()  # debounce: one refresh per burst of finished files

    def _transfers_idle(self):
        bad = self.mgr.counts["failed"]
        self.statusBar().showMessage("All transfers finished" + (f" - {bad} failed (see Transfers panel)" if bad else ""), 8000)
        self.refresh_timer.start()

    # ---- remote operations
    def _forget(self, paths):
        """Drop cached listings of these folders and everything below them (they moved, were renamed or deleted)."""
        for p in paths:
            for k in [k for k in self.dirs if k == p or k.startswith(p.rstrip("/") + "/")]:
                del self.dirs[k]

    def _op(self, title, fn, done_msg="", keep=None, forget=(), reload=()):
        """Run fn() in a worker; fn returns a list of error strings.  When it has finished (not before!), drop cached
        listings under `forget`, then reload the current folder (selecting `keep`) and every folder in `reload`."""
        self.mgr.ensured.clear()  # remote folders may be gone after delete/move/rename

        def finished():
            self._forget(forget)
            self.refresh(keep)
            for d in reload:
                if d != self.cwd:
                    self.load(d)

        def ok(errors):
            if errors:
                self.error(title, "\n".join(errors[:8]) + (f"\n... and {len(errors) - 8} more" if len(errors) > 8 else ""))
            elif done_msg:
                self.statusBar().showMessage(done_msg, 5000)
            finished()
        self.runner.submit(fn, ok, lambda e: (self.error(title, e), finished()))

    def new_folder_dialog(self):
        name, ok = QInputDialog.getText(self, "New folder", f"Name of the new folder in {self.cwd}:")
        if ok and name.strip():
            self.do_mkdir(name.strip())

    def do_mkdir(self, name):
        if "/" in name or "\\" in name:
            return self.error("New folder", "A folder name cannot contain / or \\.")
        conn, path = self.conn, join(self.cwd, name)
        self._op("New folder", lambda: (ensure_dir(client_for(conn), path), [])[1], f"Created {path}")

    def rename_dialog(self):
        sel = self.selected()
        if len(sel) == 1:
            name, ok = QInputDialog.getText(self, "Rename", f"New name for '{sel[0].name}':", text=sel[0].name)
            if ok and name.strip() and name.strip() != sel[0].name:
                self.do_rename(sel[0], name.strip())

    def do_rename(self, entry, new_name):
        if "/" in new_name or "\\" in new_name:
            return self.error("Rename", "A name cannot contain / or \\.")
        conn, src, dst = self.conn, join(self.cwd, entry.name), join(self.cwd, new_name)

        def fn():
            c = client_for(conn)
            if davlib.exists(c, dst):
                return [f"'{new_name}' already exists in this folder."]
            client_for(conn, True).move(remote_path_from=src, remote_path_to=dst, overwrite=False)  # may be slow
            return []
        self._op("Rename", fn, f"Renamed to {new_name}", keep={new_name}, forget=[src])

    def move_dialog(self):
        sel = self.selected()
        if not sel:
            return
        dlg = FolderPicker(self, f"Move {len(sel)} item(s) to ...", self.cwd)
        if dlg.exec() == QDialog.Accepted:
            self.do_move(sel, dlg.path())

    def do_move(self, entries, dest):
        conn, cwd = self.conn, self.cwd
        bad = [e.name for e in entries if e.is_dir and (dest == join(cwd, e.name) or dest.startswith(join(cwd, e.name) + "/"))]
        if bad:
            return self.error("Move", f"Cannot move folder '{bad[0]}' into itself.")
        if dest == cwd:
            return self.error("Move", "The items are already in that folder.")

        def fn():
            c, cl, errors = client_for(conn), client_for(conn, True), []
            for e in entries:
                try:
                    if davlib.exists(c, join(dest, e.name)):
                        errors.append(f"{e.name}: already exists in {dest}")
                        continue
                    cl.move(remote_path_from=join(cwd, e.name), remote_path_to=join(dest, e.name), overwrite=False)
                except Exception as ex:  # noqa: BLE001 - keep going with the other items
                    errors.append(f"{e.name}: {friendly(ex)}")
            return errors
        self._op("Move", fn, f"Moved {len(entries)} item(s) to {dest}",
                 forget=[join(cwd, e.name) for e in entries] + [join(dest, e.name) for e in entries], reload=[dest])

    def confirm_delete(self, entries):
        folders = sum(e.is_dir for e in entries)
        lines = [("[folder]  " if e.is_dir else "") + e.name for e in entries[:12]]
        if len(entries) > 12:
            lines.append(f"... and {len(entries) - 12} more")
        box = QMessageBox(QMessageBox.Warning, "Delete from server", f"Permanently delete {len(entries)} item(s) from the server?", parent=self)
        text = "\n".join(lines)
        if folders:
            text += f"\n\nWARNING: {folders} folder(s) will be deleted RECURSIVELY, including everything inside them."
        box.setInformativeText(text + "\n\nThis cannot be undone.")
        yes = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        return box.clickedButton() is yes

    def delete_dialog(self):
        sel = self.selected()
        if sel and self.confirm_delete(sel):
            self.do_delete(sel)

    def do_delete(self, entries):
        conn, cwd = self.conn, self.cwd

        def fn():
            c, errors = client_for(conn, True), []  # deleting a big folder can take a while
            for e in entries:
                try:
                    c.clean(join(cwd, e.name))
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{e.name}: {friendly(ex)}")
            return errors
        self._op("Delete", fn, f"Deleted {len(entries)} item(s)", forget=[join(cwd, e.name) for e in entries])

    # ---- shutdown
    def closeEvent(self, ev):
        if self.mgr.active():
            r = QMessageBox.question(self, "Transfers running", f"{self.mgr.active()} transfer(s) are still active. Cancel them and quit?")
            if r != QMessageBox.Yes:
                return ev.ignore()
            self.mgr.cancel_all()
        self.cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.cfg["splitter"] = bytes(self.splitter.saveState().toBase64()).decode()
        self.cfg["policy"] = self.policy
        save_cfg(self.cfg)
        self.mgr.pool.waitForDone(15000)  # let cancelled jobs remove their .part files
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    QTimer.singleShot(0, win.startup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
