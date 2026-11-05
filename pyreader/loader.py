import os
from PySide6.QtCore import QObject, Signal
from .textio import load_epub, load_html_file, load_markdown, decode_plain_text, scan_chapters
from .pdf import load_pdf, load_kindle
from .docx import load_docx
from .pptx import load_pptx
from .cache import load_cache, save_cache
from .shelf import MEDIA_EXTS

# ============ 后台加载（不阻塞 UI） ============
class BookLoader(QObject):
    loaded = Signal(str, list, str, object)   # (全文, 章节, 书名, 版式块)
    failed = Signal(str)
    progress = Signal(int, str)       # (百分比, 说明)

    # 这些格式缓存章节后，重新打开仍需完整解析（因为有图片/渲染块 extra）
    _FULL_PARSE_EXTS = {".pdf", ".docx", ".pptx"}

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        extra = None
        try:
            ext = os.path.splitext(self.path)[1].lower()

            # ---- 纯文本类：缓存命中时只需重读文字，跳过章节扫描 ----
            if ext not in self._FULL_PARSE_EXTS:
                hit = load_cache(self.path)
                if hit is not None:
                    cached_chapters, cached_title, _ = hit
                    self.progress.emit(20, "读取文件（缓存命中）…")
                    text = self._read_text_only(ext)
                    if text is not None:
                        self.progress.emit(95, "完成")
                        self.loaded.emit(text, cached_chapters, cached_title, None)
                        return

            # ---- 完整解析 ----
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
            elif ext == ".pptx":
                self.progress.emit(10, "解析 PPTX…")
                text, chapters, title, extra = load_pptx(
                    self.path, progress=lambda p, m: self.progress.emit(p, m))
            elif ext in (".md", ".markdown"):
                self.progress.emit(10, "解析 Markdown…")
                text, chapters, title = load_markdown(self.path)
            elif ext == ".kfx":
                raise RuntimeError("暂不支持 KFX 格式，请先用 Calibre 转换为 EPUB 或 MOBI")
            elif ext in MEDIA_EXTS:          # 保险：有声书 / 漫画压缩包绝不能掉进下面「按文本整个读进内存」的兜底（正常由 MainWindow 分发到播放器）
                raise RuntimeError("阅读器不能打开这种格式（有声书 / 压缩包），请用系统应用打开")
            else:
                self.progress.emit(5, "读取文件…")
                with open(self.path, "rb") as f:
                    raw = f.read()
                self.progress.emit(40, "解码…")
                text = decode_plain_text(raw)
                self.progress.emit(65, "扫描章节…")
                chapters = scan_chapters(text)
                title = ""

            self.progress.emit(95, "完成")

            # ---- 写缓存（扫描版 PDF 跳过）----
            is_scan = isinstance(extra, dict) and extra.get("mode") == "scan"
            if not is_scan:
                save_cache(self.path, chapters, title)

            self.loaded.emit(text, chapters, title, extra)

        except Exception as e:
            self.failed.emit(str(e))

    def _read_text_only(self, ext: str):
        """仅读取全文字符串（缓存命中路径，跳过章节扫描）。"""
        try:
            if ext == ".epub":
                text, _, _ = load_epub(self.path)
                return text
            elif ext in (".html", ".htm"):
                text, _, _ = load_html_file(self.path)
                return text
            elif ext in (".mobi", ".azw", ".azw3", ".azw8", ".prc"):
                text, _, _ = load_kindle(self.path)
                return text
            elif ext in (".md", ".markdown"):
                text, _, _ = load_markdown(self.path)
                return text
            elif ext in MEDIA_EXTS:
                return None
            else:
                with open(self.path, "rb") as f:
                    raw = f.read()
                return decode_plain_text(raw)
        except Exception:
            return None
