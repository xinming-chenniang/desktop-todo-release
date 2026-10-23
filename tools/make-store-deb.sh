#!/usr/bin/env bash
# provenance: drafted-by=dsh/deepseek-flash  reviewed-by=—  Unverified: 未经人复核
#
# 按统信/deepin 商店打包规范出包（https://docs.deepin.org → 应用打包规范）：
#   * 包名 = appid，倒置域名；
#   * 全部文件只放在 /opt/apps/<appid>/ 下（entries + files + info）；
#   * 不带 postinst / prerm（规范：带系统修改钩子的包无法上架）；
#   * 版本号四段纯数字（0.2.1 → 0.2.1.0）；
#   * amd64 / arm64 各出一个包。
#
# 用法：bash tools/make-store-deb.sh
# 产物：dist/store/<appid>_<版本>_<arch>.deb

set -eu

cd "$(dirname "$0")/.."

APPID="com.github.xinming-chenniang.desktoptodo"
APP_NAME="桌面待办"
VERSION3=$(python3 - <<'PY'
import re
print(re.search(r'^APP_VERSION = "([^"]+)"', open('desktop_todo.py', encoding='utf-8').read(), re.M).group(1))
PY
)
VERSION4="${VERSION3}.0"          # 商店要求四段纯数字
ARCHS="amd64 arm64"

echo "== appid $APPID / 版本 $VERSION4"

if ! python3 desktop_todo.py --check | tail -1 | grep -q "SELFTEST OK"; then
    echo "!! 自检失败，拒绝打包" >&2
    exit 1
fi
echo "== 自检 SELFTEST OK"

rm -rf dist/store
mkdir -p dist/store

for ARCH in $ARCHS; do
    ROOT="dist/store/${APPID}_${VERSION4}_${ARCH}"
    APP="$ROOT/opt/apps/$APPID"
    mkdir -p "$ROOT/DEBIAN" \
             "$APP/entries/applications" \
             "$APP/entries/icons/hicolor/scalable/apps" \
             "$APP/files/bin" \
             "$APP/files/doc"

    install -m 755 desktop_todo.py "$APP/files/desktop_todo.py"
    install -m 644 packaging/desktop-todo.svg "$APP/entries/icons/hicolor/scalable/apps/$APPID.svg"
    install -m 644 README.md SECURITY.md CHANGELOG.md "$APP/files/doc/"

    # 启动脚本：只普通用户权限，不做任何提权
    cat > "$APP/files/bin/desktop-todo" <<EOF
#!/bin/sh
exec python3 /opt/apps/$APPID/files/desktop_todo.py "\$@"
EOF
    chmod 755 "$APP/files/bin/desktop-todo"

    # 桌面入口：Exec 指向自己目录，StartupWMClass 与程序里的 WM_CLASS 一致
    cat > "$APP/entries/applications/$APPID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Name[en]=Desktop To-do
GenericName=待办清单
Comment=常驻桌面的待办清单挂件
Exec=/opt/apps/$APPID/files/bin/desktop-todo
Icon=$APPID
Terminal=false
Categories=Utility;
StartupWMClass=desktop-todo
StartupNotify=true
X-Deepin-AppID=$APPID
EOF

    # info：权限按实际用到的申报（只写本机数据、允许自启，不联网、不读账户）
    cat > "$APP/info" <<EOF
{
  "appid": "$APPID",
  "name": "$APP_NAME",
  "version": "$VERSION4",
  "arch": ["$ARCH"],
  "permissions": {
    "autostart": true,
    "notification": false,
    "trayicon": false,
    "clipboard": false,
    "account": false,
    "bluetooth": false,
    "camera": false,
    "audio_record": false,
    "installed_apps": false
  }
}
EOF

    SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
    cat > "$ROOT/DEBIAN/control" <<EOF
Package: $APPID
Version: $VERSION4
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.7), python3-tk, libx11-6
Installed-Size: $SIZE
Maintainer: YanH <284022719@qq.com>
Homepage: https://github.com/xinming-chenniang/desktop-todo
Description: $APP_NAME —— 常驻桌面的待办清单挂件
 常驻桌面右上角的待办清单：无标题栏、可拖动、回车即记，支持三态（未完成 /
 等待中 / 已完成）、子步骤、优先级、情境标签与到点提醒。
 .
 单文件、只用 Python 标准库、不联网、无账号；数据保存在本机。
EOF

    OUT="dist/store/${APPID}_${VERSION4}_${ARCH}.deb"
    dpkg-deb --build --root-owner-group "$ROOT" "$OUT" >/dev/null
    rm -rf "$ROOT"
    echo "-- $OUT"
    dpkg-deb -c "$OUT" | awk '{print $6}' | sed 's|^\./||' | grep -v '^$' | sed 's/^/   /'
done

(cd dist/store && sha256sum *.deb > SHA256SUMS)
echo "== 哈希"
cat dist/store/SHA256SUMS
