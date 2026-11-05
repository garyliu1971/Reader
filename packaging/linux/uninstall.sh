#!/usr/bin/env bash
# 卸载 PyReader（用户级和系统级都会清理；用户数据 ~/.pyreader 默认保留）
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
  rm -rf /opt/PyReader /usr/local/bin/pyreader /usr/share/applications/pyreader.desktop \
         /usr/share/icons/hicolor/256x256/apps/pyreader.png
fi
rm -rf "$HOME/.local/opt/PyReader" "$HOME/.local/bin/pyreader" \
       "$HOME/.local/share/applications/pyreader.desktop" \
       "$HOME/.local/share/icons/hicolor/256x256/apps/pyreader.png"
echo "已卸载。设置/书架数据在 ~/.pyreader，如需彻底清除请手动删除。"
