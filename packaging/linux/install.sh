#!/usr/bin/env bash
# PyReader Linux 安装脚本。
#   默认安装到当前用户（~/.local/opt/PyReader，无需 root）；加 --system 安装到 /opt/PyReader（需 sudo）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE=user
[ "${1:-}" = "--system" ] && MODE=system

if [ "$MODE" = system ]; then
  [ "$(id -u)" = 0 ] || { echo "--system 需要 root：请用 sudo bash install.sh --system"; exit 1; }
  APP=/opt/PyReader; BIN=/usr/local/bin; DESK=/usr/share/applications; ICON=/usr/share/icons/hicolor/256x256/apps
else
  APP="$HOME/.local/opt/PyReader"; BIN="$HOME/.local/bin"
  DESK="$HOME/.local/share/applications"; ICON="$HOME/.local/share/icons/hicolor/256x256/apps"
fi

mkdir -p "$BIN" "$DESK" "$ICON"
rm -rf "$APP"; mkdir -p "$APP"
cp -a "$HERE/PyReader/." "$APP/"
ln -sf "$APP/PyReader" "$BIN/pyreader"

# 图标：优先用包内预转好的 pyreader.png；没有的话再尝试从 ico 转（需要系统有 python3 + Pillow，否则用默认图标）
if [ -f "$HERE/pyreader.png" ]; then
  cp "$HERE/pyreader.png" "$ICON/pyreader.png"
elif command -v python3 >/dev/null && python3 -c "import PIL" 2>/dev/null && [ -f "$HERE/PyReader.ico" ]; then
  python3 - "$HERE/PyReader.ico" "$ICON/pyreader.png" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1]); im.load()
im.convert("RGBA").resize((256, 256)).save(sys.argv[2])
PY
fi
sed "s|^Exec=.*|Exec=$APP/PyReader %F|" "$HERE/pyreader.desktop" > "$DESK/pyreader.desktop"
if command -v update-desktop-database >/dev/null; then update-desktop-database "$DESK" 2>/dev/null || true; fi

echo "已安装到 $APP"
echo "命令行：pyreader   （确保 $BIN 在 PATH 中）"
# 运行库提示：PySide6 / Qt 的 xcb 平台插件和音频播放需要这些系统库（桌面发行版通常已带，最小化安装可能缺）
if command -v ldconfig >/dev/null; then
  _miss=""
  _ldc="$(ldconfig -p 2>/dev/null || true)"   # 先存下来再 grep：set -o pipefail 下 `ldconfig -p | grep -q` 会因 SIGPIPE 误报
  for _lib in libpulse.so.0 libxcb-cursor.so.0 libxkbcommon-x11.so.0 libxcb-icccm.so.4 libxcb-shape.so.0 libSM.so.6 libICE.so.6 libXrandr.so.2; do
    case "$_ldc" in *"$_lib"*) ;; *) _miss="$_miss $_lib" ;; esac
  done
  if [ -n "$_miss" ]; then
    echo "提示：系统缺少运行库：$_miss"
    echo "  Debian/Ubuntu: sudo apt install libpulse0 libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-util1 libxcb-xkb1 libxcb-shape0 libsm6 libice6 libxrandr2"
    echo "  Fedora:        sudo dnf install pulseaudio-libs xcb-util-cursor libxkbcommon-x11 xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil libSM libICE libXrandr"
  fi
fi
# 中文显示提示
if command -v fc-list >/dev/null && ! fc-list :lang=zh | grep -q .; then
  echo "提示：系统未检测到中文字体，界面和书籍中的中文会显示为方块。请安装，例如："
  echo "  Debian/Ubuntu: sudo apt install fonts-noto-cjk      Fedora: sudo dnf install google-noto-sans-cjk-fonts"
fi
