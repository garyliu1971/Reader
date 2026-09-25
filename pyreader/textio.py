import re
import os
import io
import html
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from urllib.parse import unquote
from typing import List, Tuple, Optional
from .config import CHUNK
from .markdown import md_chapters

# ============ 章节扫描（一次 C 级正则扫描） ============
CHAPTER_RE = re.compile(
    r"^\s*(?:第[零一二三四五六七八九十百千万0-9]{1,10}[章节回卷集部篇][^\n]{0,40}|"
    r"(?:序章|楔子|引子|终章|尾声|番外[^\n]{0,30})|#{1,6}[ \t]+[^\n]+)\s*$",
    re.MULTILINE)

def scan_chapters(text: str) -> List[Tuple[int, str]]:
    chapters = []
    for m in CHAPTER_RE.finditer(text):
        title = re.sub(r"^#{1,6}[ \t]+", "", m.group(0).strip())
        chapters.append((m.start(), title))
    if not chapters:
        # 无任何章节 → 按块切成伪章节，保证惰性分页可用
        chapters = [(i, f"第 {i // CHUNK + 1} 部分") for i in range(0, len(text), CHUNK)]
    elif chapters[0][0] != 0:
        chapters.insert(0, (0, "开篇"))
    return chapters
# ============ 编码自动识别（网文 txt 编码五花八门） ============
def decode_text(raw: bytes) -> str:
    """自动识别 UTF-8 / UTF-16 / GBK(GB18030) / Big5，乱码时选替换符最少的。"""
    if raw.startswith(b'\xff\xfe'):
        return raw.decode('utf-16-le', errors='replace').lstrip('\ufeff')
    if raw.startswith(b'\xfe\xff'):
        return raw.decode('utf-16-be', errors='replace').lstrip('\ufeff')
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw.decode('utf-8', errors='replace').lstrip('\ufeff')
    try:
        return raw.decode('utf-8')            # 严格 UTF-8 优先
    except UnicodeDecodeError:
        pass
    best_text, best_count = None, None
    for enc in ('utf-8', 'gb18030', 'big5'):  # 混合/损坏文件：谁乱码少用谁
        t = raw.decode(enc, errors='replace')
        c = t.count('\ufffd')
        if best_count is None or c < best_count:
            best_text, best_count = t, c
    return best_text
# ============ epub / HTML 解析（标准库，零依赖） ============
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def _html_to_text(raw: bytes):
    """XHTML/HTML → (纯文本, 标题)。标题取第一个 h1-h6。"""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("utf-8", errors="replace")
    # 去掉 script/style 块
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    # 标题
    title = ""
    m = re.search(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", s)
    if m:
        title = html.unescape(re.sub(r"(?s)<[^>]+>", "", m.group(1))).strip()
    if not title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", s)
        if m:
            title = html.unescape(re.sub(r"(?s)<[^>]+>", "", m.group(1))).strip()
    # 块级元素 → 换行
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|blockquote)>", "\n", s)
    # 去所有标签
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s)
    # 压缩空白
    s = re.sub(r"[ \t\r]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip(), title

def load_epub(path):
    """解析 epub：返回 (全文, 章节[(offset,标题)...], 书名)。"""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = None
        for el in container.iter():
            if _strip_ns(el.tag) == "rootfile":
                opf_path = el.get("full-path")
                break
        if not opf_path or opf_path not in z.namelist():
            raise RuntimeError("无效的 epub：找不到 OPF 清单文件")
        opf = ET.fromstring(z.read(opf_path))
        opf_dir = posixpath.dirname(opf_path)
        manifest, spine, title = {}, [], ""
        for el in opf.iter():
            t = _strip_ns(el.tag)
            if t == "item":
                i, href = el.get("id"), el.get("href")
                if i and href:
                    manifest[i] = href
            elif t == "itemref":
                if el.get("idref"):
                    spine.append(el.get("idref"))
            elif t == "title":
                title = (el.text or "").strip()
        parts, chapters = [], []
        offset = 0
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, unquote(href)))
            if full not in z.namelist():
                continue
            ch_text, ch_title = _html_to_text(z.read(full))
            if not ch_text:
                continue
            chapters.append((offset, ch_title or f"第 {len(chapters) + 1} 章"))
            parts.append(ch_text)
            offset += len(ch_text) + 2
        if not parts:
            raise RuntimeError("epub 中没有可读取的正文")
        return "\n\n".join(parts), chapters, title

def load_html_file(path):
    """HTML 文件 → (全文, 章节, 标题)。"""
    with open(path, "rb") as f:
        raw = f.read()
    text, title = _html_to_text(raw)
    chapters = scan_chapters(text)
    return text, chapters, title

MD_EXTS = (".md", ".markdown")
_MD_ATX_TITLE_RE = re.compile(r"^\s{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MD_SETEXT_TITLE_RE = re.compile(r"^([^\n]+)\n(=+)\s*$", re.MULTILINE)

def md_title(text: str) -> str:
    """从 markdown 提取书名：优先首个 ATX 标题，其次 setext(=) 标题。"""
    m = _MD_ATX_TITLE_RE.match(next((ln for ln in text.splitlines() if ln.strip()), ""))
    if m:
        return m.group(1).strip()
    m = _MD_SETEXT_TITLE_RE.search(text)
    return m.group(1).strip() if m else ""

def load_markdown(path):
    """Markdown 文件 → (原始正文, 章节, 书名)。

    正文保持原始 markdown 不变，交给 LazyPager(md=True) 做富文本渲染
    （标题分级 / 粗斜体 / 行内代码 / 代码块 / 列表 / 引用 / 表格 /
    分隔线 / 链接）；这里只额外提取书名。"""
    with open(path, "rb") as f:
        raw = f.read()
    text = decode_text(raw)
    chapters = scan_chapters(text)
    return text, chapters, md_title(text)
