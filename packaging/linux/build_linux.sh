#!/usr/bin/env bash
# 在 Linux（x86_64 或 aarch64）上构建 PyReader：
#   dist/PyReader-<ver>-linux-<arch>.tar.gz   （内含 install.sh / uninstall.sh，最通用）
#   dist/PyReader-<ver>-linux-<arch>.AppImage （若 appimagetool 可用）
# 用法：  bash packaging/linux/build_linux.sh [版本号]
# 注意：aarch64 的 PySide6 wheel 要求 glibc >= 2.39（约 Ubuntu 24.04+），x86_64 要求 >= 2.34。
set -euo pipefail
cd "$(dirname "$0")/../.."
VER="${1:-${PYREADER_VERSION:-0.4.0}}"
ARCH="$(uname -m)"
OUT="dist/PyReader-${VER}-linux-${ARCH}"

python3 -m pip install -r requirements-build.txt
export PYREADER_VERSION="$VER"
python3 -m PyInstaller PyReader-posix.spec --noconfirm --clean

# ---- tar.gz（目录版 + 安装脚本）----
rm -rf "$OUT"; mkdir -p "$OUT"
cp -a dist/PyReader "$OUT/PyReader"
cp assets/PyReader.ico "$OUT/"
# 预先转好 png 图标放进包里：用户机器的系统 python3 通常没有 Pillow，install.sh 就无法自己转
python3 - "$OUT/pyreader.png" <<'PY'
import sys
from PIL import Image
im = Image.open("assets/PyReader.ico"); im.load()
im.convert("RGBA").resize((256, 256)).save(sys.argv[1])
PY
cp packaging/linux/install.sh packaging/linux/uninstall.sh packaging/linux/pyreader.desktop "$OUT/"
chmod +x "$OUT/install.sh" "$OUT/uninstall.sh" "$OUT/PyReader/PyReader"
tar -C dist -czf "$OUT.tar.gz" "$(basename "$OUT")"
echo "==> $OUT.tar.gz"

# ---- AppImage（可选）----
TOOL="${APPIMAGETOOL:-$(command -v appimagetool || true)}"
if [ -n "$TOOL" ]; then
  APPDIR="dist/PyReader.AppDir"
  rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/bin"
  cp -a dist/PyReader/. "$APPDIR/usr/bin/"
  sed 's|^Exec=.*|Exec=PyReader %F|' packaging/linux/pyreader.desktop > "$APPDIR/pyreader.desktop"
  # AppImage 要求根目录有 png 图标，从 ico 转一份
  python3 - "$APPDIR/pyreader.png" <<'PY'
import sys
from PIL import Image
im = Image.open("assets/PyReader.ico"); im.load()
im.convert("RGBA").resize((256, 256)).save(sys.argv[1])
PY
  cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/PyReader" "$@"
RUN
  chmod +x "$APPDIR/AppRun"
  # shellcheck disable=SC2086  # APPIMAGETOOL 可能带 --appimage-extract-and-run 参数
  ARCH="$ARCH" $TOOL "$APPDIR" "dist/PyReader-${VER}-linux-${ARCH}.AppImage"
  echo "==> dist/PyReader-${VER}-linux-${ARCH}.AppImage"
else
  echo "（未找到 appimagetool，跳过 AppImage；可设置 APPIMAGETOOL=/path/to/appimagetool）"
fi
