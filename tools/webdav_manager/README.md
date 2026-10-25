# WebDAV file manager

Standalone tools (not part of the PyReader app) for managing files on the WebDAV store
(SFTPGo behind Caddy): books, photos, anything. Any remote path works; typical layout:
`library/` (books, subfolders = shelves, the only folder PyReader reads), `photo/` (private
photos, per user, invisible to PyReader), `users/<name>/` (PyReader progress and shelf data).

    pip install webdavclient3 PySide6      # PySide6 only for the GUI

Credentials come from env vars (never stored): `DAV_USER`, `DAV_PASS`, optional `DAV_URL`.

    python davgui.py                       # visual manager (browse, upload/download, drag & drop, transfers)
    python davlib.py tree library          # CLI
    python davlib.py up D:\books library --skip-existing

Uploads go to `name.part` then MOVE; downloads to a local `.part` then rename, so partial
files never look complete. Tested only against a local rclone WebDAV server, not yet against
SFTPGo / Azure Blob; no resume of interrupted transfers.
