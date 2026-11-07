import os
import zipfile
import pytest
from epubfix import comic_epub, text_epub
from pyreader.comic import ImageEpubError
from pyreader.textio import load_epub, load_mobi6, load_html_file

P = "这是一段用来填充章节内容的正文，没有任何特别含义，只是为了让章节足够长。" * 6


def xhtml(body, title="Section0001.xhtml"):
    return (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f'<head><title>{title}</title></head><body>{body}</body></html>')


def make_epub(path, files, opf_extra_items="", spine="", version="3.0", opf_dir="OEBPS"):
    """files: {zip 内全路径: 内容}；spine: itemref 的 XML 字符串。"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   f'<rootfiles><rootfile full-path="{opf_dir}/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr(f"{opf_dir}/content.opf",
                   f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="{version}">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试书</dc:title></metadata>'
                   f'<manifest>{opf_extra_items}</manifest><spine {"toc=" + chr(34) + "ncx" + chr(34) if version != "3.0" else ""}>{spine}</spine></package>')
        for name, data in files.items():
            z.writestr(name, data)
    return str(path)


def nav_doc(entries):
    lis = "".join(f'<li><a href="{h}">{t}</a></li>' for t, h in entries)
    return ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            f'<body><nav epub:type="toc"><ol>{lis}</ol></nav></body></html>')


def ncx_doc(entries):
    pts = "".join(f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{t}</text></navLabel>'
                  f'<content src="{h}"/></navPoint>' for i, (t, h) in enumerate(entries, 1))
    return ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f'<navMap>{pts}</navMap></ncx>')


def test_epub3_nav_single_big_xhtml_with_anchors(tmp_path):
    body = "".join(f'<h2 id="c{i}">第{i}章 标题{i}</h2><p>{P}</p>' for i in range(1, 6))
    files = {"OEBPS/book.xhtml": xhtml(body),
             "OEBPS/nav.xhtml": nav_doc([(f"第{i}章 标题{i}", f"book.xhtml#c{i}") for i in range(1, 6)])}
    items = ('<item id="b" href="book.xhtml" media-type="application/xhtml+xml"/>'
             '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    p = make_epub(tmp_path / "a.epub", files, items, '<itemref idref="nav"/><itemref idref="b"/>')
    text, chs, title = load_epub(p)
    assert title == "测试书"
    assert [t for _, t in chs] == [f"第{i}章 标题{i}" for i in range(1, 6)]
    for off, t in chs:
        assert text[off:].startswith(t)            # 锚点偏移精确落在标题上
    assert "nav" not in text.lower() or "第1章 标题1" not in text.split("第1章 标题1")[0]


def test_epub2_ncx_urlencoded_chinese_and_parent_paths(tmp_path):
    names = ["第一章", "第二章", "第三章"]
    files, items, spine, ents = {}, "", "", []
    for i, n in enumerate(names, 1):
        files[f"OEBPS/文本/{n}.xhtml"] = xhtml(f"<h1>{n} 序幕{i}</h1><p>{P}</p>")
        items += f'<item id="c{i}" href="%E6%96%87%E6%9C%AC/{n}.xhtml" media-type="application/xhtml+xml"/>'
        spine += f'<itemref idref="c{i}"/>'
        ents.append((f"{n} 序幕{i}", f"../%E6%96%87%E6%9C%AC/{n}.xhtml"))
    # ncx 放在子目录 OEBPS/toc/ 下，用 ../ 回指
    files["OEBPS/toc/toc.ncx"] = ncx_doc(ents)
    items += '<item id="ncx" href="toc/toc.ncx" media-type="application/x-dtbncx+xml"/>'
    p = make_epub(tmp_path / "b.epub", files, items, spine, version="2.0")
    text, chs, _ = load_epub(p)
    assert [t for _, t in chs] == [f"{n} 序幕{i}" for i, n in enumerate(names, 1)]
    assert chs[0][0] == 0 and all(text[o:].startswith(t) for o, t in chs)


def test_multiple_toc_entries_in_one_file_and_unlisted_spine_merges(tmp_path):
    a = "".join(f'<h2 id="s{i}">甲部{i}</h2><p>{P}</p>' for i in (1, 2, 3))
    files = {"OEBPS/a.xhtml": xhtml(a),
             "OEBPS/b.xhtml": xhtml(f"<h1>乙章</h1><p>{P}</p>"),
             "OEBPS/extra.xhtml": xhtml(f"<p>未列入目录的附页 {P}</p>"),
             "OEBPS/c.xhtml": xhtml(f"<h1>丙章</h1><p>{P}</p>"),
             "OEBPS/cover.xhtml": xhtml("<p>封面</p>"),
             "OEBPS/toc.ncx": ncx_doc([("甲一", "a.xhtml#s1"), ("甲二", "a.xhtml#s2"), ("甲三", "a.xhtml#s3"),
                                       ("乙", "b.xhtml"), ("丙", "c.xhtml")])}
    items = "".join(f'<item id="{k}" href="{k}.xhtml" media-type="application/xhtml+xml"/>'
                    for k in ("a", "b", "extra", "c", "cover"))
    items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    spine = ('<itemref idref="cover" linear="no"/><itemref idref="a"/><itemref idref="b"/>'
             '<itemref idref="extra"/><itemref idref="c"/>')
    text, chs, _ = load_epub(make_epub(tmp_path / "c.epub", files, items, spine, version="2.0"))
    assert [t for _, t in chs] == ["甲一", "甲二", "甲三", "乙", "丙"]
    assert "封面" not in text                     # linear=no 且不在目录里的封面被跳过
    extra_off = text.index("未列入目录的附页")
    ci = max(i for i, (o, _) in enumerate(chs) if o <= extra_off)
    assert chs[ci][1] == "乙"                    # 没列入目录的 spine 文件并入前一章


def test_filename_like_toc_and_titles_fall_back(tmp_path):
    files, items, spine = {}, "", ""
    for i in range(1, 5):
        files[f"OEBPS/Section000{i}.xhtml"] = xhtml(f"<p>{P}</p>" if i % 2 else f"<h2>真正的标题{i}</h2><p>{P}</p>",
                                                    title=f"Section000{i}.xhtml")
        items += f'<item id="s{i}" href="Section000{i}.xhtml" media-type="application/xhtml+xml"/>'
        spine += f'<itemref idref="s{i}"/>'
    files["OEBPS/toc.ncx"] = ncx_doc([(f"Section000{i}.xhtml", f"Section000{i}.xhtml") for i in range(1, 5)])
    items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    _, chs, _ = load_epub(make_epub(tmp_path / "d.epub", files, items, spine, version="2.0"))
    ts = [t for _, t in chs]
    assert len(ts) == 4
    assert not any(t.lower().endswith(".xhtml") or t.startswith("Section") for t in ts)
    assert ts[1] == "真正的标题2" and ts[0] == "第 1 章"


def test_epub_toc_all_one_position_falls_back_to_headings(tmp_path):
    body = "".join(f"<h2>第{i}节 名{i}</h2><p>{P}</p>" for i in range(1, 5))
    files = {"OEBPS/book.xhtml": xhtml(body),
             "OEBPS/toc.ncx": ncx_doc([("全书", "book.xhtml"), ("全书2", "book.xhtml"), ("全书3", "book.xhtml")])}
    items = ('<item id="b" href="book.xhtml" media-type="application/xhtml+xml"/>'
             '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
    _, chs, _ = load_epub(make_epub(tmp_path / "e.epub", files, items, '<itemref idref="b"/>', version="2.0"))
    assert [t for _, t in chs] == [f"第{i}节 名{i}" for i in range(1, 5)]


def test_epub_title_whitespace_cleaning(tmp_path):
    files, items, spine = {}, "", ""
    for i in range(1, 4):
        files[f"OEBPS/c{i}.xhtml"] = xhtml(f"<h1>第{i}章<br/><span>副题</span>\n  <small>x{i}</small></h1><p>{P}</p>")
        items += f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
        spine += f'<itemref idref="c{i}"/>'
    _, chs, _ = load_epub(make_epub(tmp_path / "f.epub", files, items, spine))
    assert chs[0][1] == "第1章 副题 x1"


# ---------------- MOBI6 ----------------
def mobi_html(n=6):
    out = ['<html><head><guide></guide></head><body><p height="1em" width="0pt" align="center">'
           '<font size="4"><b>封面</b></font></p>']
    out.append('<mbp:pagebreak/><a id="filepos100" /><p align="center"><font size="4"><b>序文：开场</b></font></p>'
               f'<p>{P}</p>')
    for i in range(1, n + 1):
        out.append(f'<mbp:pagebreak/><a id="filepos{1000 + i}" /><p align="center"><font size="4"><b>{i}</b></font></p>'
                   f'<p>{P}</p><p>{P}</p>')
    out.append(f'<mbp:pagebreak/><a id="filepos9999" /><p align="center"><b>译后记</b></p><p>{P}</p></body></html>')
    return "".join(out)


def test_mobi6_ncx_filepos(tmp_path):
    (tmp_path / "book.html").write_text(mobi_html(), encoding="utf-8")
    ents = [("序文：开场", "book.html#filepos100")] + [(str(i), f"book.html#filepos{1000 + i}") for i in range(1, 7)] \
        + [("译后记", "book.html#filepos9999")]
    (tmp_path / "toc.ncx").write_text(ncx_doc(ents), encoding="utf-8")
    text, chs, _ = load_mobi6(str(tmp_path / "book.html"))
    assert [t for _, t in chs] == ["序文：开场"] + [f"第 {i} 章" for i in range(1, 7)] + ["译后记"]
    assert chs[0][0] == 0                         # 开头只有「封面」碎片 → 并入首章
    assert "filepos" not in text                  # 标记已清除
    off = dict((t, o) for o, t in chs)
    assert text[off["第 3 章"]:].startswith("3\n")
    assert text[off["译后记"]:].startswith("译后记")


def test_mobi6_without_ncx_uses_pagebreak(tmp_path):
    (tmp_path / "book.html").write_text(mobi_html(), encoding="utf-8")
    _, chs, _ = load_mobi6(str(tmp_path / "book.html"))
    ts = [t for _, t in chs]
    assert ts[:3] == ["序文：开场", "第 1 章", "第 2 章"] and ts[-1] == "译后记" and len(ts) == 8


def test_mobi6_bad_ncx_falls_back(tmp_path):
    (tmp_path / "book.html").write_text(mobi_html(), encoding="utf-8")
    (tmp_path / "toc.ncx").write_text(ncx_doc([("一", "book.html#nope"), ("二", "book.html#nope2")]),
                                      encoding="utf-8")
    _, chs, _ = load_mobi6(str(tmp_path / "book.html"))
    assert len(chs) == 8


def test_mobi6_no_structure_falls_back_to_scan(tmp_path):
    body = "".join(f"<p>第{n}章 名{n}</p><p>{P}</p>" for n in "一二三")
    (tmp_path / "book.html").write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    _, chs, _ = load_mobi6(str(tmp_path / "book.html"))
    assert [t for _, t in chs][-3:] == ["第一章 名一", "第二章 名二", "第三章 名三"]


def test_html_file_headings(tmp_path):
    (tmp_path / "x.html").write_text(
        "<html><body>" + "".join(f"<h2>卷{i}</h2><p>{P}</p>" for i in range(1, 4)) + "</body></html>", encoding="utf-8")
    _, chs, _ = load_html_file(str(tmp_path / "x.html"))
    assert [t for _, t in chs] == ["卷1", "卷2", "卷3"]


# ---------------- 真实书籍（版权文件，不在仓库里；缺失则跳过） ----------------
SALAMAGO = r"C:\Users\GaryLiu\Downloads\(葡)若泽·萨拉马戈-失明症漫记.mobi"


@pytest.mark.skipif(not os.path.exists(SALAMAGO), reason="real mobi not available")
def test_real_mobi_salamago():
    pytest.importorskip("mobi")
    from pyreader.pdf import load_kindle        # pdf 模块依赖 Qt，真书测试才需要
    text, chs, _ = load_kindle(SALAMAGO)
    assert len(chs) >= 20                         # toc.ncx 实有 20 条
    assert not any(t.startswith("第 ") and t.endswith(" 部分") for _, t in chs)
    assert chs[0][0] == 0 and [o for o, _ in chs] == sorted(o for o, _ in chs)
    assert "第 1 章" in [t for _, t in chs] and "译后记" in [t for _, t in chs]


# ================================================================== 图片型 epub（漫画）：文字书路径的保险丝
def test_load_epub_refuses_image_only_epub_with_typed_error(tmp_path):
    p = comic_epub(tmp_path / "c.epub", n=30)
    with pytest.raises(ImageEpubError):
        load_epub(p)
    assert not issubclass(ImageEpubError, OSError)               # 不是文件错误：调用方要能把它和读文件失败区分开


def test_load_epub_still_reads_text_epub_and_illustrated_text_epub(tmp_path):
    text, chs, _ = load_epub(text_epub(tmp_path / "t.epub", n=8))
    assert text.count("字") == 8 * 400 and chs
    plated = text_epub(tmp_path / "p.epub", n=10, plates={2, 7})   # 带整页插图的文字书不是漫画
    assert load_epub(plated)[0].count("字") == 8 * 400


def _run_loader(path):
    pytest.importorskip("PySide6")
    from pyreader.loader import BookLoader
    got = {"loaded": [], "failed": []}
    ld = BookLoader(path)
    ld.loaded.connect(lambda *a: got["loaded"].append(a))
    ld.failed.connect(got["failed"].append)
    ld.run()
    return got


def test_loader_reports_image_epub_with_the_fixed_message_instead_of_loading(tmp_path):
    from pyreader.comic import IMAGE_EPUB_MESSAGE
    got = _run_loader(comic_epub(tmp_path / "c.epub", n=30))
    assert got == {"loaded": [], "failed": [IMAGE_EPUB_MESSAGE]}                   # MainWindow._on_load_failed 认这条消息，改用漫画阅读器


def test_loader_ignores_stale_text_cache_for_image_epub(tmp_path):
    from pyreader.cache import load_cache, save_cache
    p = comic_epub(tmp_path / "c.epub", n=30)
    save_cache(p, [(0, "旧章节")], "旧文字书缓存")                    # 老版本把它当文字书解析时留下的缓存
    assert load_cache(p) is not None                                  # 确实命中缓存，下面才是真的绕过了它
    from pyreader.comic import IMAGE_EPUB_MESSAGE
    got = _run_loader(p)
    assert got["failed"] == [IMAGE_EPUB_MESSAGE] and not got["loaded"]


def test_loader_text_epub_still_loads(tmp_path):
    got = _run_loader(text_epub(tmp_path / "t.epub", n=6))
    assert len(got["loaded"]) == 1 and not got["failed"]


def test_loader_image_epub_from_converted_kindle_is_a_plain_failure(tmp_path, monkeypatch):
    import pyreader.loader as ld
    def fake_kindle(path):
        raise ImageEpubError("转出的 epub 只有图片")
    monkeypatch.setattr(ld, "load_kindle", fake_kindle)
    got = _run_loader(str(tmp_path / "b.azw3"))
    assert got["failed"] == ["转出的 epub 只有图片"]                                   # 漫画阅读器打不开 azw3 原文件：普通失败，消息原样给用户
