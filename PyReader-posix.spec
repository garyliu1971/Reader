# -*- mode: python ; coding: utf-8 -*-
# PyReader 的 macOS / Linux 打包脚本（Windows 用 PyReader.spec，互不影响）
#
# 构建：  pyinstaller PyReader-posix.spec --noconfirm
#   Linux → dist/PyReader/ （目录版，由 packaging/linux/build_linux.sh 再打成 tar.gz / AppImage）
#   macOS → dist/PyReader.app （由 packaging/macos/build_macos.sh 再打成 dmg）
# PyInstaller 不能交叉编译：哪个平台/架构的包就要在对应平台/架构上构建。
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

IS_MAC = sys.platform == 'darwin'

datas = []
binaries = []
hiddenimports = ['pymupdf', 'sherpa_onnx', 'mobi', 'piper', 'loguru', 'edge_tts', 'docx', 'pptx',
                 'lxml', 'lxml.etree', 'lxml._elementpath', 'PySide6.QtMultimedia', 'PySide6.QtNetwork']

def _collect(name, with_datas=True):
    try:
        d, b, h = collect_all(name)
    except Exception as e:      # 某个可选包没装：只警告，不让构建失败
        print('PyReader-posix.spec: skip %s (%s)' % (name, e))
        return
    if with_datas:
        datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)

for _pkg in ('pymupdf', 'onnxruntime', 'sherpa_onnx', 'pptx'):
    _collect(_pkg)
for _pkg in ('PIL', 'numpy'):
    _collect(_pkg, with_datas=False)
# winsdk / comtypes 仅 Windows，这里故意不收集

qt_excludes = [
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtWebChannel', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQuick3D',
    'PySide6.QtQml', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtGraphs', 'PySide6.QtLocation', 'PySide6.QtPositioning', 'PySide6.QtSql',
    'PySide6.QtTest', 'PySide6.QtHelp', 'PySide6.QtMultimediaQuick', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtOpenGLWidgets', 'PySide6.QtOpenGL', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml',
    'PySide6.QtSensors', 'PySide6.QtSerialBus', 'PySide6.QtSerialPort', 'PySide6.QtSpatialAudio',
    'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech', 'PySide6.QtVirtualKeyboard',
    'PySide6.QtWebSockets', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtDesigner',
    'PySide6.QtUiTools', 'PySide6.QtHttpServer', 'PySide6.QtNetworkAuth', 'PySide6.QtPrintSupport',
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

# 自检：音频播放依赖 Qt Multimedia 的 FFmpeg 后端（libQt6Multimedia / libffmpegmediaplugin）。
# 缺了包会「能启动但放不出声」，只有在用户机器上才发现，所以构建时直接失败。
_have = [os.path.basename(b[0]) for b in a.binaries]
_need = ('Qt6Multimedia', 'ffmpegmediaplugin')
_missing = [n for n in _need if not any(n in h for h in _have)]
if _missing:
    raise SystemExit('PyReader-posix.spec: 音频播放需要的文件未被收集：' + ', '.join(_missing))

# 应用为中文，不需要 Qt 的翻译 / QML 资源
a.datas = [d for d in a.datas
           if not d[0].replace('\\', '/').startswith(('PySide6/Qt/translations/', 'PySide6/translations/',
                                                      'PySide6/Qt/qml/', 'PySide6/qml/'))]

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
    upx=False,                  # UPX 会破坏 macOS 签名，Linux 上也不值得
    console=False,
    argv_emulation=False,
    target_arch=None,           # 本机架构（macOS universal2 需要 universal2 的 Python 和全部 wheel，这里不做）
    codesign_identity=os.environ.get('MAC_CODESIGN_IDENTITY') or None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='PyReader',
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name='PyReader.app',
        icon='assets/PyReader.icns' if os.path.exists('assets/PyReader.icns') else None,
        bundle_identifier='com.pyreader.app',
        info_plist={
            'CFBundleName': 'PyReader',
            'CFBundleDisplayName': 'PyReader',
            'CFBundleShortVersionString': os.environ.get('PYREADER_VERSION', '0.4.0'),
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '13.0',
            'CFBundleDocumentTypes': [{
                'CFBundleTypeName': 'E-Book / Text',
                'CFBundleTypeRole': 'Viewer',
                'LSItemContentTypes': ['public.plain-text', 'com.adobe.pdf', 'org.idpf.epub-container'],
            }],
        },
    )
