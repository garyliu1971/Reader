"""Regenerate web/test/fixtures/golden.json from the authoritative Python implementation (pyreader/*).

Run from the repo root:  python web/tools/gen_golden.py
The JS tests compare web/js/core/* against these outputs, so a drift from the Qt app's behaviour fails `node --test`.
Read-only use of pyreader; only writes the fixture file.
"""
import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.dont_write_bytecode = True
sys.path.insert(0, ROOT)
from pyreader import shelf as sh  # noqa: E402
from pyreader import sync  # noqa: E402
from pyreader import textio  # noqa: E402
from pyreader.store import is_hidden_name, normalize_rel, safe_component, safe_segment  # noqa: E402

NOW = 1_800_000_000_000  # fixed "now" (ms)

out = {}

# ---- ids
names = [("示例小说.txt", 4716), ("a.epub", 0), ("Book (1).epub", 123456), ("é.txt", 7), ("é.txt", 7)]
out["local_ids"] = [[n, s, "local:" + hashlib.sha1(f"{n}{s}".encode()).hexdigest()] for n, s in names]
rels = ["a.epub", "科幻/三体.epub", "x/y/z.txt", "Ünï.md"]
out["progress_files"] = [[r, sync._safe_name("remote:" + r) + ".json"] for r in rels]

# ---- name validation
comp = ["web-a1b2c3", "ok.name_1-2", ".hidden", "trail.", "a b", "", "CON", "con.txt", "COM1", "x" * 64, "x" * 65, "..", "a/b", "中文", "NUL.tar.gz", "lpt9"]
out["safe_component"] = [[c, safe_component(c)] for c in comp]
seg = ["a.txt", "中文.epub", "..", ".", "", "a/b", "a\\b", "a:b", "x.", "x ", "aux.txt", "‮txt.exe", "ctl\x01", "ok name.txt", "y" * 255, "y" * 256]
out["safe_segment"] = [[c, safe_segment(c)] for c in seg]
rel = ["a.txt", "a/b/c.epub", "/a", "a//b", "a/../b", "科幻/三体.epub", "a/.hidden", "", "x/CON/y.txt", "a/b "]
out["normalize_rel"] = [[c, normalize_rel(c)] for c in rel]
bids = ["remote:a.epub", "remote:科幻/三体.epub", "remote:.x/a.txt", "remote:~$a.docx", "remote:", "local:" + "a" * 40, "local:" + "A" * 40, "local:abc", "x:y", "remote:a/../b"]
out["valid_book_id"] = [[b, sh.valid_book_id(b)] for b in bids]
rr = ["a.epub", "dir/b.txt", ".x.epub.part", "a.epub.part", "~$a.docx", "a.exe", "a.PDF", "a.mp3"]
out["valid_remote_rel_py"] = [[r, sh.valid_remote_rel(r)] for r in rr]

# ---- overlays
dev_a = {
    "version": 1, "device_id": "dev-a", "hostname": "pc-a", "user": "gary",
    "entries": {
        "remote:a.epub": {"fav": [True, NOW - 5000], "meta": [{"fp": "abc123", "title": "A 书"}, NOW - 5000]},
        "local:" + "1" * 40: {"hidden": [True, NOW - 3000]},
        "bad id": {"fav": [True, NOW]},
        "remote:b.txt": {"fav": [True, NOW + 10 * 86400 * 1000]},     # clock anomaly -> clamped
        "remote:c.txt": {"fav": ["yes", NOW]},                         # bad type
    },
    "progress": {
        "remote:a.epub": {"percent": 40.5, "updated": NOW / 1000 - 100},
        "remote:b.txt": {"percent": 150, "updated": NOW / 1000 - 50},
        "remote:c.txt": {"percent": 1, "updated": -1},
    },
}
dev_b = {
    "version": 1, "device_id": "dev-b", "entries": {
        "remote:a.epub": {"fav": [False, NOW - 4000]},
        "local:" + "1" * 40: {"hidden": [False, NOW - 3000]},   # tie on ts -> larger device id wins
    },
    "progress": {"remote:a.epub": {"percent": 55.0, "updated": NOW / 1000 - 10}},
}
out["overlay_inputs"] = {"a": dev_a, "b": dev_b}
va, ia = sh.validate_overlay(dev_a, NOW, device_id="dev-a")
vb, ib = sh.validate_overlay(dev_b, NOW, device_id="dev-b")
out["validate_a"] = {"view": va, "n_issues": len(ia)}
m = sh.merge(va, vb, now_ms=NOW)
out["merge_ab"] = m
out["merge_ba"] = sh.merge(vb, va, now_ms=NOW)

ov = sh.new_overlay("web-123abc", "My Phone", "gary")
t1 = sh.set_field(ov, "remote:a.epub", "fav", True, merged=m, now_ms=NOW)
t2 = sh.set_field(ov, "remote:a.epub", "hidden", True, now_ms=NOW, meta={"fp": "ff00", "title": "T"})
sh.set_progress(ov, "remote:a.epub", 61.25, NOW / 1000)
sh.set_progress(ov, "local:" + "2" * 40, 3.0, NOW / 1000 - 5)
out["set_field_ts"] = [t1, t2]
out["own_overlay"] = {k: v for k, v in ov.items()}
out["wire_text"] = sh.wire_bytes(ov).decode("utf-8")
out["wire"] = sh.to_wire(ov)

# prune
many = sh.new_overlay("web-x")
for i in range(12):
    sh.set_progress(many, "local:" + ("%040x" % i), float(i), 1000.0 + (i % 5))
sh.prune_progress(many, 5)
out["prune_kept"] = sorted(many["progress"])

# ---- text: chapters / decoding
sample = open(os.path.join(ROOT, "示例小说.txt"), "rb").read()
text = textio.decode_text(sample)
out["sample_chapters"] = [[p, t] for p, t in textio.scan_chapters(text)]
out["sample_len"] = len(text)

synthetic = (
    "书名\n\n序章 起点\n\n" + "这是一段很长的正文。" * 40 + "\n\n"
    + "".join(f"第{n}章 标题{n}\n\n" + ("正文内容，继续往下写。" * 30) + "\n\n" for n in ["一", "二", "三", "四", "十一", "二十"])
)
out["synthetic_text"] = synthetic
out["synthetic_chapters"] = [[p, t] for p, t in textio.scan_chapters(synthetic)]
en = "".join(f"Chapter {n}\n\n" + ("Some words here and there. " * 40) + "\n\n" for n in range(1, 6))
out["en_text"] = en
out["en_chapters"] = [[p, t] for p, t in textio.scan_chapters(en)]
out["cn2int"] = [[s, textio.cn2int(s)] for s in ["十一", "二十三", "一百零五", "一千二百", "一万二千", "廿一", "０１２", "壹贰叁", "二〇二", "abc"]]

p = os.path.join(ROOT, "web", "test", "fixtures")
os.makedirs(p, exist_ok=True)
with open(os.path.join(p, "golden.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
print("wrote", os.path.join(p, "golden.json"))
