"""独立测试：用 QWebEngineView 加载知识图谱 HTML，拦截 JS 控制台消息。"""
import sys, json, tempfile, os
sys.path.insert(0, os.path.dirname(__file__))

from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtCore import QUrl, QTimer

class DebugPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        lvl = ["INFO", "WARN", "ERR ", "UNKNOWN"][min(level.value if hasattr(level,'value') else level, 3)]
        print(f"[JS {lvl}] line {lineNumber}: {message}")

from pyreader.graph import build_graph_html

sample = json.dumps({
    "nodes": [
        {"id": "A", "label": "Alice", "type": "person",  "desc": "主角"},
        {"id": "B", "label": "London","type": "place",   "desc": "地点"},
        {"id": "C", "label": "Battle","type": "event",   "desc": "战役"},
    ],
    "edges": [
        {"from": "A", "to": "B", "label": "前往"},
        {"from": "A", "to": "C", "label": "参与"},
    ]
})

html = build_graph_html(sample, "Test")
tmp = os.path.join(tempfile.gettempdir(), "pyreader_graph_debug.html")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(html)
print("HTML written to:", tmp)
print("HTML size:", len(html), "bytes")

app = QApplication(sys.argv)
win = QMainWindow()
win.setWindowTitle("Graph Debug")
win.resize(900, 600)

view = QWebEngineView()
page = DebugPage(view)
view.setPage(page)
win.setCentralWidget(view)

view.load(QUrl.fromLocalFile(tmp))
print("Loading:", QUrl.fromLocalFile(tmp).toString())

# 5秒后截取页面内容检查
def check():
    page.runJavaScript(
        "JSON.stringify({"
        "  hasVis: typeof vis !== 'undefined',"
        "  hasNetwork: typeof network !== 'undefined',"
        "  nodeCount: (typeof nodes !== 'undefined') ? nodes.length : -1,"
        "  networkDiv: document.getElementById('network') ? document.getElementById('network').offsetWidth : -1,"
        "  bodyH: document.body.offsetHeight"
        "})",
        lambda r: print("JS state:", r)
    )
QTimer.singleShot(3000, check)

win.show()
sys.exit(app.exec())
