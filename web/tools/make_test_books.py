"""Generate sample books for manual / smoke testing (nothing here is copyrighted text).

    python web/tools/make_test_books.py OUTDIR

Writes: sample.pdf (6 pages), sample.epub (EPUB3 with nav + NCX, cover, inline image, internal links, 12 chapters),
        long.txt (UTF-8, ~60 chapters), long-gbk.txt (same text in GB18030), big.txt (~5 MB), notes.md, page.html.
"""
import os
import random
import struct
import sys
import zipfile
import zlib


def png(w, h, rgb):
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


SENT = ["夜色渐深，远处的灯火在雾气里晕开。", "他停下脚步，回头望了一眼来时的路。", "“你真的决定了吗？”她轻声问道。", "风从窗缝里钻进来，带着一丝凉意。",
        "桌上的茶早已凉透，却没有人想起去换。", "许多年以后，他仍然记得那个傍晚的钟声。", "这座城市从不等待任何人，只是默默地向前流淌。"]


def para(rng, n=6):
    return "".join(rng.choice(SENT) for _ in range(n))


def cn(n):
    d = "零一二三四五六七八九十"
    if n >= 100:
        return str(n)
    if n <= 10:
        return d[n]
    if n < 20:
        return "十" + d[n - 10]
    return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")


def make_epub(path, n_chapters=12, paras=14):
    rng = random.Random(7)
    chapters = []
    for i in range(1, n_chapters + 1):
        body = [f"<h1>第{cn(i)}章 测试章节{i}</h1>"]
        for k in range(paras):
            body.append(f"<p>{para(rng)}</p>")
            if k == 5 and i == 2:
                body.append('<p><img src="images/pic.png" alt="插图"/></p>')
            if k == 3 and i == 1:
                body.append('<p>跳转到<a href="ch03.xhtml#target">第三章里的某一处</a>。</p>')
            if k == 7 and i == 3:
                body.append('<p id="target">这里是被链接的位置。</p>')
        chapters.append((f"ch{i:02d}.xhtml", f"第{cn(i)}章 测试章节{i}", "\n".join(body)))
    xhtml = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
             '<head><title>{t}</title><style>p{{color:red}}</style><script>alert(1)</script></head><body>{b}<script>alert(2)</script></body></html>')
    manifest = "".join(f'<item id="c{i}" href="{f}" media-type="application/xhtml+xml"/>' for i, (f, _, _) in enumerate(chapters))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:uuid:1234</dc:identifier>'
           '<dc:title>测试之书</dc:title><dc:creator>测试作者</dc:creator><dc:language>zh-CN</dc:language><meta name="cover" content="cover-img"/></metadata>'
           '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           '<item id="cover-img" href="images/cover.png" media-type="image/png" properties="cover-image"/>'
           '<item id="pic" href="images/pic.png" media-type="image/png"/>' + manifest + '</manifest>'
           '<spine toc="ncx">' + spine + '</spine></package>')
    nav = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>nav</title></head><body>'
           '<nav epub:type="toc"><ol>' + "".join(f'<li><a href="{f}">{t}</a></li>' for f, t, _ in chapters) + '</ol></nav></body></html>')
    ncx = ('<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>'
           + "".join(f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{t}</text></navLabel><content src="{f}"/></navPoint>' for i, (f, t, _) in enumerate(chapters))
           + '</navMap></ncx>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/images/cover.png", png(300, 400, (180, 60, 60)))
        z.writestr("OEBPS/images/pic.png", png(400, 300, (60, 150, 90)))
        for f, t, b in chapters:
            z.writestr("OEBPS/" + f, xhtml.format(t=t, b=b))


def make_txt(rng, chapters, per):
    out = ["测试长篇\n\n作者：无名\n\n"]
    for i in range(1, chapters + 1):
        out.append(f"第{cn(i) if i < 100 else i}章 第{i}个故事\n\n")
        for _ in range(per):
            out.append("　　" + para(rng, 5) + "\n")
        out.append("\n")
    return "".join(out)


def make_pdf(path, pages=6):
    """Tiny hand-written PDF (Helvetica text, outline of 2 entries)."""
    objs = []

    def add(b):
        objs.append(b)
        return len(objs)
    cat = add(None)
    pg_tree = add(None)
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for i in range(pages):
        content = f"BT /F1 36 Tf 72 700 Td (Test PDF page {i + 1}) Tj ET".encode()
        c = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        p = add(f"<< /Type /Page /Parent {pg_tree} 0 R /MediaBox [0 0 612 792] /Contents {c} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>".encode())
        kids.append(p)
    outl = add(None)
    o1 = add(None)
    o2 = add(None)
    objs[o1 - 1] = f"<< /Title (Start) /Parent {outl} 0 R /Next {o2} 0 R /Dest [{kids[0]} 0 R /Fit] >>".encode()
    objs[o2 - 1] = f"<< /Title (Middle) /Parent {outl} 0 R /Prev {o1} 0 R /Dest [{kids[pages // 2]} 0 R /Fit] >>".encode()
    objs[outl - 1] = f"<< /Type /Outlines /First {o1} 0 R /Last {o2} 0 R /Count 2 >>".encode()
    objs[cat - 1] = f"<< /Type /Catalog /Pages {pg_tree} 0 R /Outlines {outl} 0 R >>".encode()
    objs[pg_tree - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {pages} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, b in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + b + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, x)
    open(path, "wb").write(bytes(out))


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    make_epub(os.path.join(outdir, "sample.epub"))
    make_epub(os.path.join(outdir, "big.epub"), 300, 200)        # ~300 chapters, ~25 MB uncompressed
    make_epub(os.path.join(outdir, "huge1.epub"), 1, 14000)      # one single 1.5M-char chapter (worst case for layout)
    rng = random.Random(11)
    t = make_txt(rng, 60, 40)
    open(os.path.join(outdir, "long.txt"), "w", encoding="utf-8", newline="").write(t)
    open(os.path.join(outdir, "long-gbk.txt"), "wb").write(t.encode("gb18030", errors="replace"))
    big = make_txt(random.Random(5), 400, 120)
    open(os.path.join(outdir, "big.txt"), "w", encoding="utf-8", newline="").write(big)
    open(os.path.join(outdir, "notes.md"), "w", encoding="utf-8").write("# 笔记\n\n第一段 **加粗** 与 `code`。\n\n## 小节\n\n- 项目一\n- 项目二\n\n<script>alert(1)</script>\n\n[链接](https://example.com)\n")
    open(os.path.join(outdir, "page.html"), "w", encoding="utf-8").write("<html><head><title>页面</title></head><body><h1>标题</h1><p onclick='alert(1)'>段落<iframe src='x'></iframe></p></body></html>")
    make_pdf(os.path.join(outdir, "sample.pdf"))
    print("wrote to", outdir, "big.txt", len(big))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "testbooks")
