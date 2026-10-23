#!/usr/bin/env bash
# provenance: drafted-by=dsh/deepseek-flash  reviewed-by=—  Unverified: 未经人复核
#
# 打发布包：跑自检 → 把源码与文档收进 zip → 生成 SHA256SUMS。
# 用法：bash tools/release.sh
# 产物：dist/desktop-todo-v<版本>.zip、dist/SHA256SUMS
#
# 约定：正式发布前先提交，产物必须对应某个 commit（脚本会警告工作区不干净）。

set -eu

cd "$(dirname "$0")/.."

VERSION=$(python3 - <<'PY'
import re
src = open('desktop_todo.py', encoding='utf-8').read()
m = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
if not m:
    raise SystemExit("在 desktop_todo.py 里找不到 APP_VERSION")
print(m.group(1))
PY
)

echo "== 版本 v$VERSION"

# 1) 自检必须通过，否则拒绝打包
if ! python3 desktop_todo.py --check | tail -1 | grep -q "SELFTEST OK"; then
    echo "!! 自检失败，拒绝打包（先跑 python3 desktop_todo.py --check 看原因）" >&2
    exit 1
fi
echo "== 自检 SELFTEST OK"

# 2) 产物应对应一个 commit（未跟踪文件不影响产物内容，只警告已跟踪改动）
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "!! 警告：工作区有未提交改动 —— 现在打的包不对应任何 commit，别当正式版发" >&2
fi

# 3) 收集
STAGE="dist/desktop-todo-v$VERSION"
ZIP="dist/desktop-todo-v$VERSION.zip"
rm -rf "$STAGE" "$ZIP" dist/SHA256SUMS
mkdir -p "$STAGE"
cp desktop_todo.py LICENSE README.md SECURITY.md CHANGELOG.md "$STAGE"/
cp preview*.png "$STAGE"/ 2>/dev/null || true

# 4) 打包（用标准库，不依赖 zip 命令）
python3 - "$STAGE" "$ZIP" <<'PY'
import os, sys, zipfile
stage, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(stage):
        for name in sorted(files):
            path = os.path.join(root, name)
            z.write(path, os.path.relpath(path, os.path.dirname(stage)))
PY
rm -rf "$STAGE"

# 5) 哈希
(cd dist && sha256sum "desktop-todo-v$VERSION.zip" > SHA256SUMS)

echo "== 产物"
ls -lh "$ZIP"
cat dist/SHA256SUMS
echo
echo "下一步：确认工作区已提交 → git tag v$VERSION → 把 zip 与 SHA256SUMS 传到 Releases"
