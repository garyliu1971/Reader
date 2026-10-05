"""章节解析结果本地缓存。

缓存策略：
- key = SHA-1(文件绝对路径 + 文件大小 + 文件最后修改时间)
  只要文件内容没变，key 不变，直接命中；文件更新后 key 变化，自动重解析。
- 每条缓存保存：chapters（章节列表）、title（书名）、extra_meta（版式元信息）。
  注意：全文 text 不缓存（可能几百 MB），章节仅存 (start, end, title)，
  重建 LazyPager 只需要这三列。
- 目录下缓存文件超过 MAX_CACHE_FILES 时，按最后访问时间删最旧的。
"""
import os
import json
import hashlib
from .config import CACHE_DIR

MAX_CACHE_FILES = 50   # 最多缓存 50 本书的章节信息


def _cache_key(path: str) -> str:
    """根据文件路径 + 大小 + 修改时间生成 SHA-1 指纹。"""
    try:
        st = os.stat(path)
        raw = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}"
    except OSError:
        raw = os.path.abspath(path)
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, key + ".json")


def load_cache(path: str):
    """读取缓存。命中返回 (chapters_list, title, extra_meta)，未命中返回 None。
    chapters_list 是 [(start, end, title), ...] 的列表。
    """
    key = _cache_key(path)
    cp = _cache_path(key)
    if not os.path.exists(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
        chapters = [(c["start"], c["end"], c["title"]) for c in data["chapters"]]
        title = data.get("title", "")
        extra_meta = data.get("extra_meta", None)
        # 更新访问时间（用于 LRU 淘汰）
        os.utime(cp, None)
        return chapters, title, extra_meta
    except Exception:
        # 缓存损坏，删掉重建
        try:
            os.remove(cp)
        except OSError:
            pass
        return None


def save_cache(path: str, chapters, title: str, extra_meta=None):
    """将解析结果写入缓存。
    chapters 接受两种格式：
      - [(start, end, title), ...]   来自 textio / epub
      - Chapter 对象列表（有 .start .end .title 属性）来自 LazyPager
    extra_meta 保存 extra dict 中不含二进制数据的轻量字段（mode / blocks 摘要等）。
    """
    key = _cache_key(path)
    cp = _cache_path(key)
    try:
        ch_list = []
        for c in chapters:
            if isinstance(c, (list, tuple)):
                s, e, t = c[0], c[1], c[2]
            else:
                s, e, t = c.start, c.end, c.title
            ch_list.append({"start": s, "end": e, "title": t})
        data = {"title": title, "chapters": ch_list, "extra_meta": extra_meta}
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        _evict()
    except Exception:
        pass


def _evict():
    """超出上限时删除最旧（按 mtime）的缓存文件。"""
    try:
        files = [os.path.join(CACHE_DIR, fn) for fn in os.listdir(CACHE_DIR)
                 if fn.endswith(".json")]
        if len(files) <= MAX_CACHE_FILES:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for old in files[:len(files) - MAX_CACHE_FILES]:
            os.remove(old)
    except Exception:
        pass
