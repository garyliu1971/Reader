# reader.py —— 基于 PySide6 的文本小说阅读器（入口）
# 双击 start.bat 或命令行:  python reader.py 小说.txt
# 代码按功能拆分在 pyreader/ 包内：config / model / markdown / textio / pager / pdf / ai / tts / loader / view / settings / mainwin
import sys
from PySide6.QtWidgets import QApplication
from pyreader import MainWindow


def main():
    app = QApplication(sys.argv)
    win = MainWindow(sys.argv[1] if len(sys.argv) > 1 else None)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
