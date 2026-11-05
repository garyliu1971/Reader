"""西文换行 / 段落回归：按词断行、超长词强制断开、CJK 逐字不变；<p> 内硬换行合并、<br> 保留、中文 txt 一行一段不合并。"""
import sys
import pytest
from PySide6.QtGui import QFont, QFontMetricsF
from PySide6.QtWidgets import QApplication
from pyreader.model import wrap_line
from pyreader.textio import _html_to_text, decode_plain_text, load_epub
from test_epub_mobi import make_epub

_app = QApplication.instance() or QApplication(sys.argv[:1] + ["-platform", "offscreen"])


@pytest.fixture(scope="module")
def fm():
    f = QFont("Courier New")
    f.setStyleHint(QFont.Monospace)
    f.setPixelSize(20)
    return QFontMetricsF(f)


def lines_of(fm, text, w):
    out, rest = [], text
    while rest:
        ln, rest = wrap_line(fm, rest, w)
        out.append(ln)
    return out


def width_for(fm, chars):
    return fm.horizontalAdvance("m") * chars + 0.5


def test_latin_wraps_on_word_boundary(fm):
    text = "She was the youngest of the two daughters of a most affectionate, indulgent father"
    ls = lines_of(fm, text, width_for(fm, 30))
    assert "".join(ls) == text and len(ls) > 1
    words = set(text.split())
    for ln in ls:
        assert all(w in words for w in ln.split()), ls     # 每行只含完整单词
        assert not ln.startswith(" ")


def test_apostrophe_hyphen_and_quotes_not_split(fm):
    text = "esteeming Miss Taylor’s judgment, twenty-one years “very little” to distress"
    for n in range(14, 40):
        ls = lines_of(fm, text, width_for(fm, n))
        assert "".join(ls) == text
        assert not any(ln.endswith("Taylor") or ln.startswith("’s") for ln in ls), (n, ls)


def test_hyphen_may_break_after_hyphen(fm):
    ls = lines_of(fm, "twenty-one years in the world", width_for(fm, 8))
    assert ls[0] == "twenty-"


def test_overlong_word_is_forced_to_break(fm):
    w = "x" * 50
    ls = lines_of(fm, w, width_for(fm, 12))
    assert "".join(ls) == w and len(ls) >= 4 and all(len(l) <= 12 for l in ls)
    ls = lines_of(fm, "ab " + w, width_for(fm, 12))
    assert ls[0] == "ab " and "".join(ls) == "ab " + w


def test_cjk_wraps_per_character_unchanged(fm):
    text = "他沿着走廊慢慢往前走，心里反复想着刚才发生的一切，却始终得不到答案。" * 3
    w = fm.horizontalAdvance("他") * 10 + 0.5
    ls = lines_of(fm, text, w)
    assert "".join(ls) == text
    # 与旧算法（纯按字符二分）逐行一致
    old, rest = [], text
    while rest:
        lo, hi = 1, len(rest)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fm.horizontalAdvance(rest[:mid]) <= w:
                lo = mid
            else:
                hi = mid - 1
        old.append(rest[:lo]); rest = rest[lo:]
    assert ls == old


def test_mixed_latin_cjk(fm):
    text = "我们使用 PyReader 阅读 Emma 这本小说，感觉非常不错"
    for n in range(4, 20):
        w = fm.horizontalAdvance("我") * n + 0.5
        ls = lines_of(fm, text, w)
        assert "".join(ls) == text
        for tok in ("PyReader", "Emma"):
            assert any(tok in l for l in ls) or len(tok) * fm.horizontalAdvance("m") > w, (n, ls)


# ---------- 段落 ----------
def test_html_p_hard_newlines_merged():
    raw = (b"<html><body><p>\nEmma Woodhouse, handsome, clever, and rich, with a comfortable home and happy\n"
           b"disposition, seemed to unite some of the best blessings of existence; and had\n"
           b"lived nearly twenty-one years.\n</p>\n\n<p>\nShe was the youngest\nof the two daughters.\n</p></body></html>")
    text, _ = _html_to_text(raw)
    paras = [p for p in text.split("\n") if p]
    assert paras == [
        "Emma Woodhouse, handsome, clever, and rich, with a comfortable home and happy disposition, "
        "seemed to unite some of the best blessings of existence; and had lived nearly twenty-one years.",
        "She was the youngest of the two daughters."]


def test_html_br_and_pre_preserved():
    text, _ = _html_to_text(b"<p>Roses are red,<br/>Violets are blue</p><pre>a\n  b\nc</pre>")
    assert "Roses are red,\nViolets are blue" in text
    assert text.endswith("a\nb\nc")      # <pre> 内的换行保留（缩进沿用既有的空白折叠）


def test_html_cjk_source_newline_joined_without_space():
    text, _ = _html_to_text("<p>他沿着走廊慢慢往前走，\n心里反复想着。</p>".encode("utf-8"))
    assert text == "他沿着走廊慢慢往前走，心里反复想着。"


def test_epub_p_hard_newlines_merged(tmp_path):
    body = "<h1>CHAPTER I</h1><p>\nline one of the\nparagraph here\n</p><p>second para</p>"
    p = make_epub(tmp_path / "e.epub",
                  {"OEBPS/c.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>' + body + "</body></html>"},
                  opf_extra_items='<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
                  spine='<itemref idref="c"/>')
    text, _, _ = load_epub(p)
    assert "line one of the paragraph here" in text.split("\n")


GUT = ("It is a truth universally acknowledged, that a single man in possession of a\n"
       "good fortune, must be in want of a wife.\n\n"
       "However little known the feelings or views of such a man may be on his first\n"
       "entering a neighbourhood, this truth is so well fixed.\n")


def test_txt_latin_hard_wrap_unwrapped_keeps_paragraph_breaks():
    t = decode_plain_text(GUT.encode("utf-8"))
    assert t.split("\n\n")[0] == ("It is a truth universally acknowledged, that a single man in possession of a "
                                  "good fortune, must be in want of a wife.")
    assert len(t.split("\n\n")) == 2


def test_txt_poetry_not_merged():
    poem = "Roses are red\nViolets are blue\nSugar is sweet\nAnd so are you\n\nSecond stanza here\nWith short lines too\n"
    assert decode_plain_text(poem.encode()) == poem


def test_txt_chinese_one_line_per_paragraph_untouched():
    cn = "他沿着走廊慢慢往前走，心里反复想着刚才发生的一切，却始终得不到答案，只好停下脚步。\n\n" * 3
    cn2 = "第一章 开始\n这是第一行正文，足够长的一行用来模拟网文里一行一段的情况，字数要超过四十个字符才算数。\n这是第二行正文，同样是一行一段，不能被合并成一个段落，否则排版会被破坏掉。\n"
    assert decode_plain_text(cn.encode("utf-8")) == cn
    assert decode_plain_text(cn2.encode("utf-8")) == cn2
    mixed = ("第一章\n\n" + "这是中文正文一行一段，" * 8 + "\n" + "这是中文正文又一行，" * 8 + "\n\n") + "English word " * 5
    assert decode_plain_text(mixed.encode("utf-8")) == mixed


def test_parser_version_bumped_for_layout_change():
    from pyreader import cache
    assert cache.PARSER_VERSION >= 3
