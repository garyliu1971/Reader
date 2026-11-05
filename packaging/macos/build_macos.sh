#!/usr/bin/env bash
# 在 macOS 上构建 PyReader：dist/PyReader.app 和 dist/PyReader-<ver>-macos-<arch>.dmg
# 用法：  bash packaging/macos/build_macos.sh [版本号]
# 可选环境变量（有则签名 / 公证；没有则只产出未签名的 app，首次打开需右键 → 打开）：
#   MAC_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE="notarytool 保存的 keychain profile 名"（xcrun notarytool store-credentials 创建）
# arm64 / x86_64 取决于运行的机器（PyInstaller 不能交叉编译）。
set -euo pipefail
cd "$(dirname "$0")/../.."
VER="${1:-${PYREADER_VERSION:-0.4.0}}"
ARCH="$(uname -m)"
export PYREADER_VERSION="$VER"

python3 -m pip install -r requirements-build.txt
# Intel Mac：onnxruntime / PyMuPDF 新版没有 macOS x86_64 wheel，需要回退版本
if [ "$ARCH" = "x86_64" ]; then
  python3 -m pip install "onnxruntime<=1.23.2" "pymupdf<=1.28.2"
fi

# 图标：没有 .icns 时从 ico 生成
if [ ! -f assets/PyReader.icns ]; then
  python3 - <<'PY'
import subprocess, tempfile, os
from PIL import Image
im = Image.open("assets/PyReader.ico"); im.load(); im = im.convert("RGBA")
d = os.path.join(tempfile.mkdtemp(), "PyReader.iconset"); os.makedirs(d)
for s in (16, 32, 128, 256, 512):
    im.resize((s, s)).save(f"{d}/icon_{s}x{s}.png")
    im.resize((s * 2, s * 2)).save(f"{d}/icon_{s}x{s}@2x.png")
subprocess.check_call(["iconutil", "-c", "icns", d, "-o", "assets/PyReader.icns"])
PY
fi

python3 -m PyInstaller PyReader-posix.spec --noconfirm --clean
APP="dist/PyReader.app"
[ -d "$APP" ] || { echo "未生成 $APP"; exit 1; }

if [ -n "${MAC_CODESIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp --sign "$MAC_CODESIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict "$APP"
fi

DMG="dist/PyReader-${VER}-macos-${ARCH}.dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"; cp -a "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "PyReader" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [ -n "${MAC_CODESIGN_IDENTITY:-}" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
  codesign --force --sign "$MAC_CODESIGN_IDENTITY" "$DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi
echo "==> $DMG"
