# -*- mode: python ; coding: utf-8 -*-
# PyReader 打包脚本（已做体积优化：剔除 QtWebEngine 及未用到的 Qt 模块）
#
# 构建：  pyinstaller PyReader.spec
# 安装包：ISCC.exe PyReader-setup.iss
from PyInstaller.utils.hooks import collect_all
import os

datas = []
binaries = []
hiddenimports = ['pymupdf', 'sherpa_onnx', 'mobi', 'piper', 'loguru', 'edge_tts', 'docx', 'pptx', 'lxml', 'lxml.etree', 'lxml._elementpath',
                 'PySide6.QtMultimedia', 'PySide6.QtNetwork']
tmp_ret = collect_all('pymupdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pptx')     # python-pptx 模板/XML 资源
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')      # 3D 翻页透视扭曲
binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('numpy')    # 快照转数组
binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('winsdk')   # Windows 系统语音（WinRT SpeechSynthesizer，离线）
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('comtypes')   # SAPI5 语音（NaturalVoiceSAPIAdapter 桥接的自然语音）
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# ---- 未用到的 Qt 模块（排除后可省几百 MB）----
# 应用只用 QtWidgets + QtGui + QtCore + QtMultimedia + QtNetwork。
qt_excludes = [
    # WebEngine / Quick / QML（知识图谱已改为默认浏览器打开）
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtWebChannel', 'PySide6.QtWebChannelQuick',
    'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQuick3D',
    'PySide6.QtQml', 'PySide6.QtQmlModels', 'PySide6.QtQmlCore',
    'PySide6.QtQmlLocalStorage', 'PySide6.QtQmlWorkerScript', 'PySide6.QtQmlXmlListModel',
    'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtPdfQuick',
    # 3D / 图表 / 可视化 / 地图 / 其它不用的模块
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras',
    'PySide6.QtCharts', 'PySide6.QtChartsQml',
    'PySide6.QtDataVisualization', 'PySide6.QtDataVisualizationQml',
    'PySide6.QtGraphs', 'PySide6.QtGraphsWidgets',
    'PySide6.QtLocation', 'PySide6.QtPositioning',
    'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtHelp',
    'PySide6.QtMultimediaQuick', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtOpenGLWidgets', 'PySide6.QtOpenGL',
    'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtSensors',
    'PySide6.QtSerialBus', 'PySide6.QtSerialPort', 'PySide6.QtSpatialAudio',
    'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech', 'PySide6.QtVirtualKeyboard',
    'PySide6.QtWebSockets', 'PySide6.QtBluetooth', 'PySide6.QtNfc',
    'PySide6.QtDesigner', 'PySide6.QtUiTools',
    'PySide6.QtXml', 'PySide6.QtConcurrent', 'PySide6.QtSvg', 'PySide6.QtSvgWidgets',
    'PySide6.QtHttpServer', 'PySide6.QtNetworkAuth', 'PySide6.QtPrintSupport',
    'PySide6.QtDBus',
]

a = Analysis(
    ['reader.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=qt_excludes,
    noarchive=False,
    optimize=0,
)

# ---- 过滤未用到的 Qt DLL（兜底：即便钩子递归分析带入也一并剔除）----
_DROP_DLL_PREFIXES = (
    'Qt63D', 'Qt6Bluetooth', 'Qt6CanvasPainter', 'Qt6Charts', 'Qt6Concurrent',
    'Qt6DBus', 'Qt6DataVisualization', 'Qt6Designer', 'Qt6Graphs', 'Qt6Help',
    'Qt6HttpServer', 'Qt6Labs', 'Qt6Location', 'Qt6Lottie', 'Qt6MultimediaQuick',
    'Qt6MultimediaWidgets', 'Qt6NetworkAuth', 'Qt6Nfc', 'Qt6OpenGL',
    'Qt6OpenGLWidgets', 'Qt6Pdf', 'Qt6Positioning', 'Qt6PrintSupport', 'Qt6Qml',
    'Qt6Quick', 'Qt6RemoteObjects', 'Qt6Scxml', 'Qt6Sensors', 'Qt6SerialBus',
    'Qt6SerialPort', 'Qt6ShaderTools', 'Qt6SpatialAudio', 'Qt6Sql',
    'Qt6StateMachine', 'Qt6Svg', 'Qt6Test', 'Qt6TextToSpeech', 'Qt6UiTools',
    'Qt6VirtualKeyboard', 'Qt6WebChannel', 'Qt6WebEngine', 'Qt6WebSockets',
    'Qt6WebView', 'Qt6Xml',
)
_DROP_DLL_NAMES = ('opengl32sw.dll', 'pyside6qml.abi3.dll')

def _drop_qt_dll(name: str) -> bool:
    base = os.path.basename(name)
    return base.startswith(_DROP_DLL_PREFIXES) or base in _DROP_DLL_NAMES

a.binaries = [b for b in a.binaries if not _drop_qt_dll(b[0])]

# ---- 自检：播放器（有声书 QMediaPlayer）与朗读（QAudioSink）都靠 Qt Multimedia 的 ffmpeg 后端 ----
# Qt6Multimedia / Qt6Network 与 FFmpeg DLL、plugins/multimedia/ffmpegmediaplugin.dll 缺任何一个，包里就「能启动但放不出声」，
# 而且只有在用户机器上才会发现；上面的前缀过滤误伤（例如以后给 'Qt6Multimedia' 加了前缀）时让构建直接失败。
_NEED_BINARIES = ('Qt6Multimedia.dll', 'Qt6Network.dll', 'ffmpegmediaplugin.dll')
_NEED_PREFIXES = ('avcodec', 'avformat', 'avutil', 'swresample', 'swscale')
_have = [os.path.basename(b[0]) for b in a.binaries]
_missing = ([n for n in _NEED_BINARIES if n not in _have]
            + [p for p in _NEED_PREFIXES if not any(h.startswith(p) for h in _have)])
if _missing:
    raise SystemExit('PyReader.spec: 音频播放需要的 Qt Multimedia / FFmpeg 文件被剔除或没被收集：' + ', '.join(_missing))

# ---- 过滤 Qt 多语言翻译 / QML / WebEngine 资源（应用为中文，无需 186 种翻译）----
def _drop_qt_data(name: str) -> bool:
    n = name.replace('\\', '/')
    return n.startswith(('PySide6/translations/', 'PySide6/qml/', 'PySide6/resources/'))

a.datas = [d for d in a.datas if not _drop_qt_data(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PyReader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PyReader',
)
