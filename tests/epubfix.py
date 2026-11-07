"""测试用 epub 构造器：全部用 zipfile 在代码里拼，不带任何真实书。图片字节默认是占位（纯逻辑测试不解码）。
真实漫画的形状：封面是 spine 第一项（CoverPage.xhtml 引用 images/cover.jpg），其余每页一个 xhtml 引用同目录的 N.jpeg，
所以按文件名排序封面会排到最后、图片还会被拆进 OEBPS 与 OEBPS/images 两个「章」。"""
import zipfile

FAKE = b"not-really-an-image"
_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def page_xhtml(body, title="p"):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
            '  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n<html xmlns="http://www.w3.org/1999/xhtml">'
            f'<head><title>{title}</title><style>.x{{color:red}}</style></head><body>{body}</body></html>')


def img_body(src, text=""):
    return f'<div><img src="{src}" alt="x"/></div>' + (f"<p>{text}</p>" if text else "")


def svg_body(href):
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
            f'width="100%" height="100%" viewBox="0 0 800 1200"><image width="800" height="1200" xlink:href="{href}"/></svg>')


def make_epub(path, pages, images=None, spine=None, ppd=None, book_type=None, opf_dir="OEBPS", linear_no=(),
              container=True, image_types=()):
    """pages: {xhtml 文件名（相对 OPF 目录）: <body> 里的 html}；images: {相对 OPF 目录的路径: 字节}；
    spine: xhtml 文件名的顺序（默认 pages 的顺序）；image_types: 直接放进 spine 的图片项（路径，相对 OPF 目录）。"""
    images = images or {}
    spine = list(pages) if spine is None else list(spine)
    items, refs = [], []
    for n, name in enumerate(pages):
        items.append(f'<item id="x{n}" href="{name}" media-type="application/xhtml+xml"/>')
    for n, name in enumerate(images):
        items.append(f'<item id="i{n}" href="{name}" media-type="{_MIME.get(name.rsplit(".", 1)[-1].lower(), "image/png")}"/>')
    ids = {name: f"x{n}" for n, name in enumerate(pages)}
    ids.update({name: f"i{n}" for n, name in enumerate(images)})
    for name in list(spine) + list(image_types):
        lin = ' linear="no"' if name in linear_no else ""
        refs.append(f'<itemref idref="{ids[name]}"{lin}/>')
    meta = f'<meta name="book-type" content="{book_type}"/>' if book_type else ""
    attr = f' page-progression-direction="{ppd}"' if ppd else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        if container:
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       f'<rootfiles><rootfile full-path="{opf_dir}/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr(f"{opf_dir}/book.opf",
                   '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>t</dc:title>{meta}</metadata>'
                   f'<manifest>{"".join(items)}</manifest><spine{attr}>{"".join(refs)}</spine></package>')
        for name, body in pages.items():
            z.writestr(f"{opf_dir}/{name}", page_xhtml(body))
        for name, data in images.items():
            z.writestr(f"{opf_dir}/{name}", data)
    return str(path)


def comic_epub(path, n=20, png=None, **kw):
    """真实漫画的形状：封面 images/cover.jpg 排在 spine 第一，之后 Chapter_001.html … 各引用 0.jpeg … 。
    png 给定时图片用它（可被 Qt 解码），否则占位字节；扩展名仍是 .jpeg / .jpg（Qt 按内容识别）。"""
    data = png or FAKE
    pages = {"CoverPage.xhtml": img_body("images/cover.jpg"), "Cover.html": "<h1>书名</h1>"}
    images = {"images/cover.jpg": data}
    spine = ["CoverPage.xhtml"]
    for i in range(n):
        pages[f"Chapter_{i + 1:03d}.html"] = img_body(f"{i}.jpeg")
        images[f"{i}.jpeg"] = data
        spine.append(f"Chapter_{i + 1:03d}.html")
    return make_epub(path, pages, images, spine=spine, **kw)


def text_epub(path, n=12, chars=400, plates=(), **kw):
    """文字书：n 页正文（每页 chars 个字）；plates 里的页序号换成整页插图（只有图）。"""
    pages, images = {}, {}
    for i in range(n):
        if i in plates:
            pages[f"c{i}.xhtml"] = img_body(f"pl{i}.jpg")
            images[f"pl{i}.jpg"] = FAKE
        else:
            pages[f"c{i}.xhtml"] = "<p>" + "字" * chars + "</p>"
    return make_epub(path, pages, images, **kw)
