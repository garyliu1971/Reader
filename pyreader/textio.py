import re
import os
import io
import html
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from collections import namedtuple
from urllib.parse import unquote
from typing import List, Tuple, Optional
from .comic import IMAGE_EPUB_MESSAGE, ImageEpubError, is_image_epub
from .config import CHUNK

# ============ 数字解析（中文 / 全角 / 大写 / 罗马 / 英文单词） ============
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
             "九": 9, "两": 2, "兩": 2, "壹": 1, "贰": 2, "貳": 2, "叁": 3, "參": 3, "肆": 4, "伍": 5,
             "陆": 6, "陸": 6, "柒": 7, "捌": 8, "玖": 9}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_SPECIAL = {"廿": 20, "卅": 30, "卌": 40}

def cn2int(s: str) -> Optional[int]:
    """中文/阿拉伯/全角/大写数字 → int；无法解析返回 None。
    支持 十一 / 二十三 / 一百零五 / 一千二百 / 一万二千 / 廿一 / 卅 / 壹贰叁 / 二〇二 / ０１２。"""
    if not s:
        return None
    s = s.strip().translate(_FW_DIGITS)
    if not s:
        return None
    if all(c in "0123456789" or c in _CN_DIGIT for c in s):   # 纯数字串：按位拼接（二〇二 → 202）
        return int("".join(c if c in "0123456789" else str(_CN_DIGIT[c]) for c in s))
    total = section = num = 0
    for c in s:
        if c in "0123456789":
            num = int(c)
        elif c in _CN_DIGIT:
            num = _CN_DIGIT[c]
        elif c in _CN_UNIT:
            section += (num or 1) * _CN_UNIT[c]
            num = 0
        elif c in _CN_SPECIAL:
            section += _CN_SPECIAL[c]
            num = 0
        elif c == "万":
            section += num
            total += (section or 1) * 10000
            section = num = 0
        else:
            return None
    return total + section + num

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

def _roman2int(s: str) -> Optional[int]:
    s = s.lower()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    # 拒绝 "iiii" / "mix" 这类不规范写法：回转成罗马数字比对
    out, n = "", total
    for val, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                     (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= val:
            out += sym
            n -= val
    return total if out == s and total > 0 else None

_EN_WORDS = {w: i for i, w in enumerate(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split(), 1)}

# ============ 章节扫描（多模式候选 + 序号连续性打分） ============
_NUMCH = "零〇一二三四五六七八九十百千万两兩廿卅壹贰貳叁參肆伍陆陸柒捌玖拾佰仟0-9０-９"
_UNITS = "章节節回卷集部篇话話幕"
_SEP = " \t　"
_ZH_RE = re.compile(rf"^【?第[{_SEP}]*([{_NUMCH}]{{1,12}})[{_SEP}]*([{_UNITS}])】?(.*)$")
_EN_RE = re.compile(r"^(chapter|chap\.?|part|book|section)[ \t]+(\d{1,4}|[ivxlcdm]{1,8}|"
                    + "|".join(_EN_WORDS) + r")\b[ \t]*(?:[.:\-–—)][ \t]*)?(.*)$", re.I)
_SPECIAL_RE = re.compile(
    r"^(序章|序言|前言|楔子|引子|引言|终章|終章|尾声|尾聲|后记|後記|番外篇|番外|附录|附錄|致谢|致謝|"
    r"作者的话|完本感言|序|跋|prologue|epilogue|preface|foreword|afterword|introduction|appendix)"
    rf"([{_SEP}]*[:：、\-—.．][{_SEP}]*|[{_SEP}]+)?(.*)$", re.I)
_NUMDOT_RE = re.compile(rf"^(\d{{1,4}})[{_SEP}]*[.．、][{_SEP}]*(\D.*|)$")
_DUN_RE = re.compile(rf"^([{_NUMCH}]{{1,6}})[{_SEP}]*[、．.][{_SEP}]*(.*)$")
_MD_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
# 预过滤：只取「可能是标题」的短行，避免对每一行都跑一遍 Python 分类
_PREFILTER_RE = re.compile(
    rf"^[{_SEP}]*(?:[【#第序前楔引终終尾后後番跋附致作完{_NUMCH}]|(?i:chap|part|book|section|prologue|"
    r"epilogue|preface|foreword|afterword|introduction|appendix))[^\n]{0,100}$", re.M)

_NO_BODY_START = "的了是，,;；"      # 「第三章的内容…」这种紧跟助词的是正文
_CLOSERS = {"”": "“", "」": "「", "』": "『", "）": "（", ")": "(", "》": "《", "】": "【", "’": "‘", '"': '"'}
_NO_JOIN_SPECIAL = {"番外", "番外篇", "附录", "附錄"}     # 这几个允许与标题直接相连（番外一）

Cand = namedtuple("Cand", "pos end title fam num")

def _is_sentence(s: str, allow_dot_end: bool = False) -> bool:
    """以句末标点结尾（或含句号）的行视为正文而不是标题。"""
    s = s.strip()
    if not s:
        return False
    if "。" in s:
        return True
    c = s[-1]
    if c in "！？…!?，,；;、":
        return True
    if c == "." and not allow_dot_end:
        return True
    op = _CLOSERS.get(c)
    if op is not None:
        # 「第一章 重生（上）」这类括号配对完整的标题不算；引号收尾而无开引号才算正文
        if c == '"':
            return s.count('"') % 2 == 1
        return op not in s
    return False

def _classify(line: str):
    """单行 → (family, num, title) 或 None。"""
    m = _ZH_RE.match(line)
    if m:
        tail = m.group(3)
        if tail and (tail[0] in _NO_BODY_START or len(tail.strip()) > 40
                     or _is_sentence(tail)):
            return None
        n = cn2int(m.group(1))
        if n is None:
            return None
        unit = {"節": "节", "話": "话"}.get(m.group(2), m.group(2))
        return "zh:" + unit, n, line
    m = _EN_RE.match(line)
    if m:
        tail = m.group(3)
        if tail and (len(tail) > 60 or _is_sentence(tail)):
            return None
        w = m.group(2).lower()
        n = int(w) if w.isdigit() else (_EN_WORDS.get(w) or _roman2int(w))
        if n is None:
            return None
        kind = "chapter" if m.group(1).lower().startswith("chap") else m.group(1).lower()
        return "en:" + kind, n, line
    m = _SPECIAL_RE.match(line)
    if m:
        kw, sep, tail = m.group(1), m.group(2), m.group(3)
        if tail and (len(tail) > 30 or _is_sentence(tail)):
            return None
        if tail and not sep and kw not in _NO_JOIN_SPECIAL:
            return None
        return "special", None, line
    m = _MD_RE.match(line)
    if m:
        return "md", None, m.group(1).strip()
    if _is_sentence(line):
        return None
    m = _NUMDOT_RE.match(line)
    if m:
        return "numdot", int(m.group(1)), line
    m = _DUN_RE.match(line)
    if m:
        n = cn2int(m.group(1))
        if n is not None and len(m.group(2)) <= 40:
            return "dun", n, line
    return None

def _collect_candidates(text: str) -> List[Cand]:
    out = []
    for m in _PREFILTER_RE.finditer(text):
        raw = m.group(0)
        line = raw.strip(_SEP)
        if not line or len(line) > 80:
            continue
        r = _classify(line)
        if r is None:
            continue
        fam, num, title = r
        out.append(Cand(m.start(), m.start() + len(raw.rstrip(_SEP)), title, fam, num))
    return out

def _drop_toc_clusters(cands: List[Cand], text_len: int) -> List[Cand]:
    """文首「目录页」：>=5 个标题行紧挨着（间隔 <60 字符）→ 整段丢弃，只保留正文里的真标题。"""
    limit = max(20000, text_len // 10)
    out, i, n = [], 0, len(cands)
    while i < n:
        j = i
        while j + 1 < n and cands[j + 1].pos - cands[j].end < 60:
            a, b = cands[j], cands[j + 1]
            if a.fam == b.fam and a.num is not None and b.num is not None and b.num <= a.num:
                break                      # 序号回绕：目录页结束，紧接着的是正文第一章
            j += 1
        if j - i + 1 >= 5 and cands[i].pos < limit:
            pass
        else:
            out.extend(cands[i:j + 1])
        i = j + 1
    return out

def _prune_outliers(cs: List[Cand]) -> List[Cand]:
    """序号突兀（如重复出现的「第三章」正文引用）而后一个又接得上前一个 → 视为误报。"""
    out = []
    for i, c in enumerate(cs):
        if out and c.num is not None and out[-1].num is not None:
            d = c.num - out[-1].num
            if d <= 0 and c.num != 1:
                continue                   # 重复 / 倒退（且不是新一卷从 1 重数）：正文里的引用
            if d > 2 and i + 1 < len(cs) and cs[i + 1].num is not None \
                    and cs[i + 1].num - out[-1].num in (1, 2):
                continue
        out.append(c)
    return out

def _family_stats(cs: List[Cand], text_len: int):
    n = len(cs)
    nums = [c.num for c in cs]
    if n >= 2 and all(x is not None for x in nums):
        ok = sum(1 for a, b in zip(nums, nums[1:]) if b - a in (1, 2) or b == 1)
        cont = ok / (n - 1)
    else:
        cont = 1.0
    gaps = [b.pos - a.end for a, b in zip(cs, cs[1:])] + [text_len - cs[-1].end]
    spacing = sum(1 for g in gaps if g >= 100) / len(gaps)
    return n, cont, spacing

def _find_chapters(text: str) -> List[Tuple[int, str]]:
    """多模式候选 → 按（序号连续性 × 间距合理性 × 命中数）挑出可信的家族；一个都不可信返回 []。"""
    cands = _drop_toc_clusters(_collect_candidates(text), len(text))
    if not cands:
        return []
    fams = {}
    for c in cands:
        fams.setdefault(c.fam, []).append(c)
    chosen, secondary = [], []
    for fam, cs in fams.items():
        if fam.startswith(("zh:", "en:")):
            cs = _prune_outliers(cs)
            n, cont, spacing = _family_stats(cs, len(text))
            if (n == 1 and cs[0].num == 1) or (n >= 2 and cont >= 0.6 and spacing >= 0.6):
                chosen.extend(cs)
        elif fam in ("special", "md"):
            chosen.extend(cs)
        else:                                   # numdot / dun：最容易与正文列表混淆，要求更严
            cs = _prune_outliers(cs)
            n, cont, spacing = _family_stats(cs, len(text))
            if n >= 3 and cont >= 0.7 and spacing >= 0.8 and len(text) / n >= 300:
                secondary.append((n, cs))
    if not any(c.fam not in ("special", "md") for c in chosen) and secondary:
        chosen.extend(max(secondary, key=lambda x: x[0])[1])
    if not chosen:
        return []
    chosen.sort(key=lambda c: c.pos)
    out = []
    for c in chosen:
        if out and out[-1][0] == c.pos:
            continue
        out.append((c.pos, c.title))
    return out

def _finalize(chapters: List[Tuple[int, str]], text: str, lead_min: int = 0, lead_title: str = "开篇"):
    """保证第一章从 0 开始：开头有实质内容时补一个「开篇」；只有零星空白/封面碎片则并入首章。
    lead_min=0 表示只要有非空白内容就补「开篇」（与旧行为一致）。"""
    if not chapters:
        return chapters
    if chapters[0][0] != 0:
        if len(text[:chapters[0][0]].strip()) > lead_min:
            chapters.insert(0, (0, lead_title))
        else:
            chapters[0] = (0, chapters[0][1])
    return chapters

def _chunk_chapters(text: str) -> List[Tuple[int, str]]:
    """没有任何章节标记：按 CHUNK 大小切成伪章节，但尽量落在空行/换行处，不把句子腰斩。"""
    n = len(text)
    out, start = [], 0
    while start < n:
        end = n
        if n - start > CHUNK * 5 // 4:
            lo, hi = start + CHUNK // 2, start + CHUNK
            cut = text.rfind("\n\n", lo, hi)
            if cut < 0:
                cut = text.rfind("\n", lo, hi)
            if cut < 0:
                cut = text.find("\n", hi, hi + CHUNK // 4)
            end = cut + 1 if cut >= 0 else hi
        out.append((start, f"第 {len(out) + 1} 部分"))
        start = end
    return out

def scan_chapters(text: str) -> List[Tuple[int, str]]:
    chapters = _find_chapters(text)
    if not chapters:
        return _chunk_chapters(text)
    return _finalize(chapters, text)

# ============ 编码自动识别（网文 txt 编码五花八门） ============
def _norm_newlines(t: str) -> str:
    """\\r\\n 与单独的 \\r 统一成 \\n，保证章节偏移量与存入 pager 的全文一致。"""
    return t.replace("\r\n", "\n").replace("\r", "\n") if "\r" in t else t

def decode_text(raw: bytes) -> str:
    """自动识别 UTF-8 / UTF-16 / GBK(GB18030) / Big5，乱码时选替换符最少的；换行统一为 \\n。"""
    if raw.startswith(b'\xff\xfe'):
        return _norm_newlines(raw.decode('utf-16-le', errors='replace').lstrip('﻿'))
    if raw.startswith(b'\xfe\xff'):
        return _norm_newlines(raw.decode('utf-16-be', errors='replace').lstrip('﻿'))
    if raw.startswith(b'\xef\xbb\xbf'):
        return _norm_newlines(raw.decode('utf-8', errors='replace').lstrip('﻿'))
    try:
        return _norm_newlines(raw.decode('utf-8'))            # 严格 UTF-8 优先
    except UnicodeDecodeError:
        pass
    best_text, best_count = None, None
    for enc in ('utf-8', 'gb18030', 'big5'):  # 混合/损坏文件：谁乱码少用谁
        t = raw.decode(enc, errors='replace')
        c = t.count('�')
        if best_count is None or c < best_count:
            best_text, best_count = t, c
    return _norm_newlines(best_text)

_BLANK_SPLIT_RE = re.compile(r"(\n[ \t]*\n+)")
_WRAP_MIN = 40      # 硬折行文本（Gutenberg 约 70 列）每个中间行都不会短于此；诗歌 / 短行列表不满足

def unwrap_latin_text(text: str) -> str:
    """西文 txt 的硬折行还原：空行分隔的段落内，单个换行合并为空格。
    判断依据：中文 txt 的约定是「一行一段」，单个 \n 就是段落分隔，必须保留，所以
    1) CJK 字符占比 >= 5% 的文本整体不处理；2) 没有空行分隔的文本（本身就是一行一段）不处理；
    3) 只有「除末行外每行都 >= 40 字符且后续行无缩进」的段落才合并（诗歌 / 缩进引文 / 短行列表原样保留）。"""
    if not re.search(r"\n[ \t]*\n", text):
        return text
    sample = text[:200000]
    letters = sum(1 for c in sample if c.isalpha())
    if letters and sum(1 for c in sample if _is_cjk(c) and c.isalpha()) / letters >= 0.05:
        return text
    parts = _BLANK_SPLIT_RE.split(text)
    for i in range(0, len(parts), 2):
        blk = parts[i]
        if "\n" not in blk:
            continue
        lines = blk.split("\n")
        if all(len(l) >= _WRAP_MIN for l in lines[:-1]) and not any(l[:1] in (" ", "\t") for l in lines[1:]):
            parts[i] = " ".join(l.strip() if k else l.rstrip() for k, l in enumerate(lines))
    return "".join(parts)

def decode_plain_text(raw: bytes) -> str:
    """纯文本书（txt 等）：解码 + 西文硬折行还原。"""
    return unwrap_latin_text(decode_text(raw))

# ============ epub / HTML 解析（标准库，零依赖） ============
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

_MK_OPEN, _MK_CLOSE = "", ""     # 私有区字符：转换期间在文本里插入的「锚点标记」
_MK_RE = re.compile(_MK_OPEN + r"([IHP])([^" + _MK_CLOSE + r"]*)" + _MK_CLOSE)
_MK_RUN_RE = re.compile("((?:" + _MK_OPEN + "[^" + _MK_CLOSE + "]*" + _MK_CLOSE + r")+)(\s+)")
_TAG_RE = re.compile(r"<[a-zA-Z][^<>]*>")
_ATTR_ID_RE = re.compile(r"""(?<![\w:-])id\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ATTR_NAME_RE = re.compile(r"""(?<![\w:-])name\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_BAD_TITLE_RE = re.compile(r"\.x?html?$|^section\d+|^part\d+", re.I)

def _clean_title(frag: str) -> str:
    """HTML 片段 → 单行标题：块级标签 / <br> 先换成空格再去标签，空白折叠。"""
    s = re.sub(r"(?is)<br\s*/?>|</?(?:p|div|h[1-6]|li|tr|td|th|blockquote|section|article)\b[^>]*>", " ", frag)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

_PRE_RE = re.compile(r"(?is)(<pre\b.*?</pre\s*>)")
_SRC_NL_RE = re.compile(r"[ \t\r]*\n\s*")

def _is_cjk(ch: str) -> bool:
    return ch >= "⺀"        # CJK / 假名 / 谚文 / 全角标点（含 、。）；U+201C/D 等西文弯引号不算

def _collapse_source_newlines(s: str) -> str:
    """HTML 里源码折行只是空白：<p> 内的硬换行（Gutenberg 约每 70 字符折一行）合并成空格；
    <br> / 块级结束标签产生的换行在后面单独处理，不受影响；<pre> 原样保留。两侧都是 CJK 时直接相连不插空格。"""
    def sub(seg):
        return _SRC_NL_RE.sub(lambda m: "" if (m.start() > 0 and m.end() < len(seg)
                              and _is_cjk(seg[m.start() - 1]) and _is_cjk(seg[m.end()])) else " ", seg)
    return "".join(part if i % 2 else sub(part) for i, part in enumerate(_PRE_RE.split(s)))

class _Doc:
    """一份 HTML 转换结果：text 全文；anchors {id: 偏移}；heads [(偏移, 标题)] (h1-h3)；
    breaks [偏移] (<mbp:pagebreak/>)；title 首个 h1-h6 否则 <title>；h_title / t_title 分别是两者。"""
    __slots__ = ("text", "anchors", "heads", "breaks", "title", "h_title", "t_title")

def _convert_html(raw: bytes) -> _Doc:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("utf-8", errors="replace")
    s = s.replace(_MK_OPEN, "").replace(_MK_CLOSE, "")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    d = _Doc()
    d.h_title = d.t_title = ""
    m = re.search(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", s)
    if m:
        d.h_title = _clean_title(m.group(1))
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", s)
    if m:
        d.t_title = _clean_title(m.group(1))
    d.title = d.h_title or d.t_title
    s = re.sub(r"(?is)<head\b.*?</head>", "", s)
    s = _collapse_source_newlines(s)
    heads_txt = []

    def _head(m):
        heads_txt.append(_clean_title(m.group(2)))
        return f"{_MK_OPEN}H{len(heads_txt) - 1}{_MK_CLOSE}" + m.group(0)
    s = re.sub(r"(?is)<h([1-3])\b[^>]*>(.*?)</h\1\s*>", _head, s)
    s = re.sub(r"(?i)<mbp:pagebreak\s*/?>", f"{_MK_OPEN}P{_MK_CLOSE}", s)

    def _ids(m):
        tag = m.group(0)
        mk = ""
        for rx in ((_ATTR_ID_RE, _ATTR_NAME_RE) if re.match(r"<a\b", tag, re.I) else (_ATTR_ID_RE,)):
            a = rx.search(tag)
            if a:
                v = html.unescape(a.group(1) if a.group(1) is not None else a.group(2))
                if v:
                    mk += f"{_MK_OPEN}I{v}{_MK_CLOSE}"
        return mk + tag
    s = _TAG_RE.sub(_ids, s)
    # 块级元素 → 换行
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|blockquote|section|article|dt|dd|figcaption)>", "\n", s)
    s = re.sub(r"(?i)</(td|th)>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s)
    # 标记后移到紧随的空白之后：让空白折叠不会被标记打断，锚点也总落在正文首字上
    s = _MK_RUN_RE.sub(lambda m: m.group(2) + m.group(1), s)
    s = re.sub(r"[ \t\r]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)          # 源码折行折叠成空格后，段首 / 段尾会残留空格；去掉以免段首被误判为「已缩进」
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    s = s.strip()
    # 取出标记并记录其在「纯文本」里的偏移
    parts, last, cur = [], 0, 0
    d.anchors, d.heads, d.breaks = {}, [], []
    for m in _MK_RE.finditer(s):
        seg = s[last:m.start()]
        parts.append(seg)
        cur += len(seg)
        last = m.end()
        kind, val = m.group(1), m.group(2)
        if kind == "I":
            d.anchors.setdefault(val, cur)
        elif kind == "H":
            t = heads_txt[int(val)]
            if t:
                d.heads.append((cur, t))
        else:
            d.breaks.append(cur)
    parts.append(s[last:])
    d.text = "".join(parts)
    return d

def _html_to_text(raw: bytes):
    """XHTML/HTML → (纯文本, 标题)。标题取第一个 h1-h6，其次 <title>。"""
    d = _convert_html(raw)
    return d.text, d.title

# ---- 目录（TOC）解析与校验 ----
def _norm_toc_title(t: str, idx: int) -> str:
    """目录标题归一：空白折叠；纯数字（"1"）→ 「第 1 章」。"""
    t = re.sub(r"\s+", " ", t or "").strip()
    if re.fullmatch(r"\d{1,4}", t):
        return f"第 {int(t)} 章"
    return t

def _parse_ncx(raw: bytes):
    """toc.ncx → [(标题, src, 层级)]，按文档顺序。"""
    root = ET.fromstring(raw)
    entries = []

    def walk(el, level):
        for ch in el:
            if _strip_ns(ch.tag) != "navPoint":
                continue
            label = src = ""
            for x in ch:
                t = _strip_ns(x.tag)
                if t == "navLabel":
                    label = "".join(y.text or "" for y in x.iter() if _strip_ns(y.tag) == "text")
                elif t == "content" and not src:
                    src = x.get("src") or ""
            entries.append((label, src, level))
            walk(ch, level + 1)
    for el in root.iter():
        if _strip_ns(el.tag) == "navMap":
            walk(el, 1)
            break
    return entries

_NAV_RE = re.compile(r"(?is)<nav\b([^>]*)>(.*?)</nav\s*>")
_NAV_TOK_RE = re.compile(r"(?is)<(/?)ol\b[^>]*>|<a\b([^>]*)>(.*?)</a\s*>")
_HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)

def _parse_nav(raw: bytes):
    """epub3 nav.xhtml → [(标题, href, 层级)]。优先 epub:type=toc 的 <nav>。"""
    s = raw.decode("utf-8", errors="replace")
    body = None
    for m in _NAV_RE.finditer(s):
        if re.search(r"""type\s*=\s*["'][^"']*\btoc\b""", m.group(1), re.I):
            body = m.group(2)
            break
        if body is None and not re.search(r"landmarks|page-list", m.group(1), re.I):
            body = m.group(2)
    entries, depth = [], 0
    for m in _NAV_TOK_RE.finditer(body or ""):
        if m.group(3) is None:
            depth = max(0, depth - 1) if m.group(1) else depth + 1
            continue
        h = _HREF_RE.search(m.group(2))
        if h:
            entries.append((_clean_title(m.group(3)), html.unescape(h.group(1) if h.group(1) is not None else h.group(2)),
                            max(1, depth)))
    return entries

def _toc_to_chapters(entries, resolve, text: str, lead_min: int):
    """entries [(标题, src, level)] + resolve(src)→偏移|None → 校验后的章节列表；不合格返回 None。
    规则：>=3 个不同位置；标题多数不像文件名；同一位置只留第一条。"""
    items = []
    for i, (title, src, _lvl) in enumerate(entries):
        off = resolve(src)
        t = _norm_toc_title(title, i)
        if off is None or not t or off >= len(text):
            continue
        items.append((off, t))
    items.sort(key=lambda x: x[0])           # 稳定排序：同一位置保留目录里靠前的
    out = []
    for off, t in items:
        if out and out[-1][0] == off:
            continue
        out.append((off, t))
    if len(out) < 3:
        return None
    if sum(1 for _, t in out if _BAD_TITLE_RE.search(t)) * 2 >= len(out):
        return None
    return _finalize(out, text, lead_min)

def _chapters_from_headings(doc: _Doc, lead_min: int = 0):
    out = []
    for off, t in doc.heads:
        if out and out[-1][0] == off:
            continue
        out.append((off, _norm_toc_title(t, len(out))))
    if len(out) < 3 or sum(1 for _, t in out if _BAD_TITLE_RE.search(t)) * 2 >= len(out):
        return None
    return _finalize(out, doc.text, lead_min)

def _chapters_from_breaks(doc: _Doc):
    """<mbp:pagebreak/> 后面紧跟的短行当章节标题；多数不是短行说明那是分页符而非章节 → None。"""
    text, out, short = doc.text, [], 0
    for b in doc.breaks:
        st = b
        while st < len(text) and text[st].isspace():
            st += 1
        if st >= len(text) or (out and out[-1][0] == st):
            continue
        nl = text.find("\n", st)
        line = text[st:nl if nl >= 0 else len(text)].strip()
        if 0 < len(line) <= 40:
            short += 1
            out.append((st, _norm_toc_title(line, len(out))))
        else:
            out.append((st, f"第 {len(out) + 1} 章"))
    if len(out) < 3 or short * 10 < len(out) * 6:
        return None
    return _finalize(out, text, 200)

def _fallback_html_chapters(doc: _Doc):
    return _chapters_from_headings(doc) or scan_chapters(doc.text)

def load_epub(path):
    """解析 epub：返回 (全文, 章节[(offset,标题)...], 书名)。
    章节优先取 epub3 nav / epub2 ncx 目录（支持 file#id 锚点）；目录不可信时退回 h1-h3 标题 / 正文扫描。
    只有图片的 epub（漫画）抛 ImageEpubError：当文字书读只会得到几乎空白的一页（正常路径在 MainWindow.load_book 就分流了，这里是保险）。"""
    if is_image_epub(path):
        raise ImageEpubError(IMAGE_EPUB_MESSAGE)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = None
        for el in container.iter():
            if _strip_ns(el.tag) == "rootfile":
                opf_path = el.get("full-path")
                break
        if not opf_path or opf_path not in names:
            raise RuntimeError("无效的 epub：找不到 OPF 清单文件")
        opf = ET.fromstring(z.read(opf_path))
        opf_dir = posixpath.dirname(opf_path)
        manifest, spine, title, nav_id, ncx_id, spine_toc = {}, [], "", None, None, None
        for el in opf.iter():
            t = _strip_ns(el.tag)
            if t == "item":
                i, href = el.get("id"), el.get("href")
                if i and href:
                    manifest[i] = href
                    if "nav" in (el.get("properties") or "").split() and nav_id is None:
                        nav_id = i
                    if el.get("media-type") == "application/x-dtbncx+xml" and ncx_id is None:
                        ncx_id = i
            elif t == "itemref":
                if el.get("idref"):
                    spine.append((el.get("idref"), (el.get("linear") or "yes").lower()))
            elif t == "spine":
                spine_toc = el.get("toc")
            elif t == "title" and not title:
                title = (el.text or "").strip()
        if spine_toc and spine_toc in manifest:
            ncx_id = spine_toc

        def full_of(item_id):
            href = manifest.get(item_id)
            return posixpath.normpath(posixpath.join(opf_dir, unquote(href))) if href else None

        # ---- 目录候选：nav 优先，其次 ncx ----
        tocs = []                                  # [(toc 文件全路径, entries)]
        for item_id, parser in ((nav_id, _parse_nav), (ncx_id, _parse_ncx)):
            tp = full_of(item_id) if item_id else None
            if tp and tp in names:
                try:
                    ents = parser(z.read(tp))
                except Exception:
                    ents = []
                if ents:
                    tocs.append((tp, ents))

        def split_src(tp, src):
            p, _, frag = src.partition("#")
            if not p:
                return None, unquote(frag)
            return posixpath.normpath(posixpath.join(posixpath.dirname(tp), unquote(p))), unquote(frag)

        toc_files = {split_src(tp, e[1])[0] for tp, ents in tocs for e in ents}
        nav_full = full_of(nav_id) if nav_id else None

        # ---- 正文：逐 spine 文件转换，记录每个文件的起点与锚点 ----
        parts, docs, file_start, anchors = [], [], {}, {}
        offset = 0
        for idref, linear in spine:
            full = full_of(idref)
            if not full or full not in names:
                continue
            if full not in toc_files and (linear == "no" or full == nav_full):
                continue                           # 封面 / 目录页：不在目录里就不要
            d = _convert_html(z.read(full))
            file_start[full] = offset
            anchors[full] = {k: v + offset for k, v in d.anchors.items()}
            if not d.text:
                continue
            docs.append((offset, full, d))
            parts.append(d.text)
            offset += len(d.text) + 2
        if not parts:
            raise RuntimeError("epub 中没有可读取的正文")
        text = "\n\n".join(parts)

        chapters = None
        for tp, ents in tocs:
            def resolve(src, tp=tp):
                p, frag = split_src(tp, src)
                if p is None or p not in file_start:
                    return None
                if frag and frag in anchors[p]:
                    a = anchors[p][frag]
                    return text.rfind("\n", 0, a) + 1 if a > 0 and text[a - 1:a] != "\n" else a
                return file_start[p]
            chapters = _toc_to_chapters(ents, resolve, text, 200)
            if chapters:
                break
        if not chapters:
            chapters = _epub_fallback(text, docs)
        return text, chapters, title

def _epub_fallback(text, docs):
    """目录缺失/不可信：每个正文文件一章（标题去掉文件名味）→ h1-h3 标题 → 正文扫描。"""
    files = []
    for off, _full, d in docs:
        t = d.h_title
        if not t and d.t_title and not _BAD_TITLE_RE.search(d.t_title):
            t = d.t_title
        files.append((off, t or f"第 {len(files) + 1} 章"))
    if len(files) >= 3:
        return files
    heads, seen = [], set()
    for off, _full, d in docs:
        for o, t in d.heads:
            if off + o not in seen:
                seen.add(off + o)
                heads.append((off + o, _norm_toc_title(t, len(heads))))
    if len(heads) >= 3:
        return _finalize(heads, text)
    found = _find_chapters(text)
    if found:
        return _finalize(found, text)
    return files

def load_html_file(path):
    """HTML 文件 → (全文, 章节, 标题)。"""
    with open(path, "rb") as f:
        raw = f.read()
    doc = _convert_html(raw)
    return doc.text, _fallback_html_chapters(doc), doc.title

def load_mobi6(html_path):
    """旧版 mobi/azw 解出来的 book.html → (全文, 章节, 标题)。
    章节来源依次为：同目录 toc.ncx（src=book.html#fileposNNN）→ <mbp:pagebreak/> 后的短标题行 →
    h1-h3 标题 → 正文扫描。纯数字标题（"1"）改为「第 1 章」。"""
    with open(html_path, "rb") as f:
        doc = _convert_html(f.read())
    chapters = None
    ncx = os.path.join(os.path.dirname(html_path), "toc.ncx")
    if os.path.isfile(ncx):
        try:
            with open(ncx, "rb") as f:
                ents = _parse_ncx(f.read())
        except Exception:
            ents = []
        base = os.path.basename(html_path).lower()

        def resolve(src):
            p, _, frag = src.partition("#")
            if p and os.path.basename(unquote(p)).lower() != base:
                return None
            a = doc.anchors.get(unquote(frag)) if frag else 0
            if a is None:
                return None
            return doc.text.rfind("\n", 0, a) + 1 if a > 0 and doc.text[a - 1:a] != "\n" else a
        chapters = _toc_to_chapters(ents, resolve, doc.text, 200)
    if not chapters:
        chapters = _chapters_from_breaks(doc) or _fallback_html_chapters(doc)
    return doc.text, chapters, doc.title

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
