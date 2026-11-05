"""在线书源：浏览 / 搜索目录、挑一个可下载格式。下载本身走 sync.download_url。
实现：OpdsSource（OPDS 1.x Atom，自建书库如 Calibre-Web / Kavita / Komga）、GutenbergCatalogSource（Gutenberg 离线目录 CSV）。
OPDS 2（JSON）以后另写一个 BookSource 子类，在 source_from_cfg 里按 type 分发即可。"""
from typing import Optional

from .base import Acquisition, BookEntry, BookPage, BookSource, BookSourceError, book_filename, pick_acquisition
from .gutenberg import GutenbergCatalogSource
from .opds import OpdsSource, enabled_sources, opds_source_from_cfg

SOURCE_TYPES = ("opds", "gutenberg_catalog")      # 书源条目的 type 字段；缺省 = opds（向后兼容）


def source_from_cfg(src: dict) -> Optional[BookSource]:
    if not isinstance(src, dict) or not (src.get("url") or "").strip():
        return None
    if src.get("type") == "gutenberg_catalog":
        return GutenbergCatalogSource(src.get("name", ""), src["url"].strip(),
                                      allow_insecure=bool(src.get("allow_insecure", False)))
    return opds_source_from_cfg(src)


__all__ = ["Acquisition", "BookEntry", "BookPage", "BookSource", "BookSourceError", "book_filename", "pick_acquisition",
           "OpdsSource", "GutenbergCatalogSource", "source_from_cfg", "enabled_sources", "SOURCE_TYPES"]
