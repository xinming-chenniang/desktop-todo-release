#!/usr/bin/env bash
# provenance: drafted-by=dsh/deepseek-flash  reviewed-by=—  Unverified: 未经人复核
#
# 打 .deb：双击就能装，装完在启动器里有个图标，点开即用（不需要终端）。
# 用法：bash tools/make-deb.sh
# 产物：dist/desktop-todo_<版本>_all.deb
#
# 设计取舍：
#   * 程序装到 /opt/desktop-todo/，/usr/bin/desktop-todo 只是三行 wrapper；
#   * 依赖声明 python3-tk —— 缺 tkinter 时由包管理器装，而不是让用户看到 ImportError；
#   * 卸载不删用户数据（~/.local/share/desktop-todo），在说明里写清楚。

set -eu

cd "$(dirname "$0")/.."
VERSION=$(python3 - <<'PY'
import re
print(re.search(r'^APP_VERSION = "([^"]+)"', open('desktop_todo.py', encoding='utf-8').read(), re.M).group(1))
PY
)
PKG="desktop-todo"
STAGE="dist/deb/${PKG}_${VERSION}_all"
OUT="dist/${PKG}_${VERSION}_all.deb"

echo "== 版本 v$VERSION"

if ! python3 desktop_todo.py --check | tail -1 | grep -q "SELFTEST OK"; then
    echo "!! 自检失败，拒绝打包" >&2
    exit 1
fi
echo "== 自检 SELFTEST OK"

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/$PKG" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/doc/$PKG"

install -m 755 desktop_todo.py                 "$STAGE/opt/$PKG/desktop_todo.py"
install -m 755 packaging/desktop-todo          "$STAGE/usr/bin/$PKG"
install -m 644 packaging/desktop-todo.desktop  "$STAGE/usr/share/applications/$PKG.desktop"
install -m 644 packaging/desktop-todo.svg      "$STAGE/usr/share/icons/hicolor/scalable/apps/$PKG.svg"
install -m 644 README.md SECURITY.md CHANGELOG.md "$STAGE/usr/share/doc/$PKG/"
install -m 755 packaging/postinst              "$STAGE/DEBIAN/postinst"
install -m 755 packaging/postrm                "$STAGE/DEBIAN/postrm"

cat > "$STAGE/usr/share/doc/$PKG/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: desktop-todo
Source: https://github.com/xinming-chenniang/desktop-todo

Files: *
Copyright: 2026 YanH
License: MIT
 见 /usr/share/doc/$PKG/README.md 与随包 LICENSE。
EOF

SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.7), python3-tk, libx11-6
Installed-Size: $SIZE
Maintainer: YanH <284022719@qq.com>
Homepage: https://github.com/xinming-chenniang/desktop-todo
Description: 桌面待办清单挂件（统信 UOS / Deepin）
 常驻桌面右上角的待办清单：无标题栏、可拖动、回车即记，支持三态（未完成 /
 等待中 / 已完成）、子步骤、优先级、情境标签与到点提醒。
 .
 单文件、只用 Python 标准库、不联网、无账号；数据保存在本机
 ~/.local/share/desktop-todo/todos.json。
EOF

dpkg-deb --build --root-owner-group "$STAGE" "$OUT" >/dev/null
rm -rf "dist/deb"

echo "== 校验"
dpkg-deb -I "$OUT" | sed -n '1,12p'
echo "-- 内容"
dpkg-deb -c "$OUT" | awk '{print $6}' | sed "s|^\./||" | grep -v '^$'
(cd dist && sha256sum "$(basename "$OUT")" > SHA256SUMS.deb)
echo "-- 哈希"
cat dist/SHA256SUMS.deb
