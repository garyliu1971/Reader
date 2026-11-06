"""Local WebDAV dev/test server for PyReader Web: tests/fakedav.py (the Qt tests' in-process fake) plus a CORS shim.

Nothing here touches tests/ (imported read-only) and it never connects anywhere: it only listens on 127.0.0.1.

    python web/tools/devdav.py [--port 8099] [--auth user:pass] [--seed DIR]

--seed DIR copies the files in DIR into /dav/library (old mtime, so they are not treated as "still uploading").
Prints "READY <url>" once listening. CORS: echoes the request Origin (never '*'), answers preflight without auth,
allows OPTIONS/GET/PUT/PROPFIND/MKCOL/MOVE and the headers the web app sends -- the same contract web/README.md
asks of the real server.
"""
import argparse
import itertools
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tests"))
import fakedav  # noqa: E402  (read-only import of the Qt tests' fake server)

ALLOW_METHODS = "OPTIONS, GET, PUT, PROPFIND, MKCOL, MOVE"
ALLOW_HEADERS = "Authorization, Depth, Content-Type, Destination, Overwrite, If-None-Match"
EXPOSE = "Date, ETag, Content-Length, Last-Modified"


class CorsHandler(fakedav.Handler):
    def _cors(self):
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", EXPOSE)

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/__debug/log":   # test hook: request header log (methods + the headers the web app must send)
            import json
            return self._send(200, json.dumps({"headers": type(self).req_headers, "methods": type(self).log}).encode(),
                              {"Content-Type": "application/json"})
        return super().do_GET()

    def do_OPTIONS(self):  # preflight: must succeed without credentials
        type(self).log.append(("OPTIONS", self.path))
        type(self).req_headers.append(("OPTIONS", self.path, {k: self.headers.get(k) for k in ("Authorization", "Origin", "Access-Control-Request-Method", "Access-Control-Request-Headers") if self.headers.get(k)}))
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", ALLOW_METHODS)
        self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()


def start(port=0, auth=None, seed=None):
    h = type("H", (CorsHandler,), {
        "files": {}, "dirs": {"/dav", "/dav/library", "/dav/users"}, "auth": auth, "seen_auth": [], "mtimes": {},
        "etags": {}, "log": [], "fail_propfind": set(), "extra_entries": {}, "raw_propfind": {}, "links": {},
        "redirects": {}, "mkcol_forbidden": set(), "put_hooks": [], "move_hooks": [], "req_headers": [],
        "counter": itertools.count(1), "now": staticmethod(time.time)})
    if seed:
        for name in sorted(os.listdir(seed)):
            p = os.path.join(seed, name)
            if os.path.isfile(p):
                key = "/dav/library/" + name
                with open(p, "rb") as f:
                    h.files[key] = f.read()
                h.mtimes[key] = int(time.time()) - 3600
                h.etags[key] = '"seed-%d"' % next(h.counter)
    srv = ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, h, "http://127.0.0.1:%d/dav" % srv.server_address[1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--auth", default=None, help="user:pass (omit for anonymous)")
    ap.add_argument("--seed", default=None)
    a = ap.parse_args()
    srv, handler, url = start(a.port, a.auth, a.seed)
    print("READY", url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
