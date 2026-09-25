import os
from PySide6.QtCore import QObject, Signal
from .textio import load_epub, load_html_file, load_markdown, decode_text, scan_chapters
from .pdf import load_pdf, load_kindle
from .docx import load_docx

# ============ 后台加载（不阻塞 UI） ============
class BookLoader(QObject):
    loaded = Signal(str, list, str, object)   # (全文, 章节, 书名, 版式块)
    failed = Signal(str)
    progress = Signal(int, str)       # (百分比, 说明)
    def __init__(self, path):
        super().__init__()
        self.path = path
    def run(self):
        extra = None
        try:
            ext = os.path.splitext(self.path)[1].lower()
            if ext == ".epub":
                self.progress.emit(10, "解析 epub…")
                text, chapters, title = load_epub(self.path)
            elif ext in (".html", ".htm"):
                self.progress.emit(10, "读取文件…")
                text, chapters, title = load_html_file(self.path)
            elif ext in (".mobi", ".azw", ".azw3", ".azw8", ".prc"):
                self.progress.emit(10, "解析 Kindle 文件…")
                text, chapters, title = load_kindle(self.path)
            elif ext == ".pdf":
                self.progress.emit(10, "解析 PDF…")
                text, chapters, title, extra = load_pdf(
                    self.path, progress=lambda p, m: self.progress.emit(p, m))
            elif ext == ".docx":
                self.progress.emit(10, "解析 DOCX…")
                text, chapters, title, extra = load_docx(
                    self.path, progress=lambda p, m: self.progress.emit(p, m))
            elif ext in (".md", ".markdown"):
                self.progress.emit(10, "解析 Markdown…")
                text, chapters, title = load_markdown(self.path)
            elif ext == ".kfx":
                raise RuntimeError("暂不支持 KFX 格式，请先用 Calibre 转换为 EPUB 或 MOBI")
            else:
                self.progress.emit(5, "读取文件…")
                with open(self.path, "rb") as f:
                    raw = f.read()
                self.progress.emit(40, "解码…")
                text = decode_text(raw)
                self.progress.emit(65, "扫描章节…")
                chapters = scan_chapters(text)
                title = ""
            self.progress.emit(95, "完成")
            self.loaded.emit(text, chapters, title, extra)
        except Exception as e:
            self.failed.emit(str(e))
