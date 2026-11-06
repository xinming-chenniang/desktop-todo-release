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

# 3) 收集（只用演示数据截图；根目录 preview*.png 含真实内容，绝不入包）
STAGE="dist/desktop-todo-v$VERSION"
ZIP="dist/desktop-todo-v$VERSION.zip"
rm -rf "$STAGE" "$ZIP" dist/SHA256SUMS
mkdir -p "$STAGE/design/rewrite/shots"
cp desktop_todo.py LICENSE README.md SECURITY.md CHANGELOG.md MANUAL.md "$STAGE"/
cp design/rewrite/shots/*.png "$STAGE/design/rewrite/shots/"

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

# 5) 哈希（zip 与源码各一行；源码行对照仓库根目录的 desktop_todo.py）
(cd dist && sha256sum "desktop-todo-v$VERSION.zip" > SHA256SUMS)
printf '%s  %s\n' "$(sha256sum desktop_todo.py | cut -d' ' -f1)" "desktop_todo.py" >> dist/SHA256SUMS

echo "== 产物"
ls -lh "$ZIP"
cat dist/SHA256SUMS
# 6) 签名（有 GPG 私钥才签；没有就明确跳过，不伪造）
if gpg --list-secret-keys --with-colons 2>/dev/null | grep -q '^sec'; then
    bash tools/sign-release.sh dist/SHA256SUMS
else
    echo "== 跳过 GPG 签名（本机没有私钥；见 tools/sign-release.sh）"
fi

# 7) Release 正文（粘到 Gitee / GitHub 的 Release 页面）
python3 - "$VERSION" > dist/release-notes.md <<'PY'
import io
import re
import sys

version = sys.argv[1]
lines = io.open("CHANGELOG.md", encoding="utf-8").read().splitlines()
start = None
for index, line in enumerate(lines):
    if line.startswith("## v%s" % version):
        start = index + 1
        break
section_lines = []
if start is not None:
    for line in lines[start:]:
        if line.startswith("## "):
            break
        section_lines.append(line)
section = "\n".join(section_lines).strip() or "（CHANGELOG 里没有对应段）"
su = io.open("dist/SHA256SUMS", encoding="utf-8").read().strip()
print("本版是 v%s。完整变更见 CHANGELOG。" % version)
print()
print("## 变更")
print(section)
print()
print("## 校验")
print("```")
print(su)
print("```")
print()
print("上传附件：包 + SHA256SUMS（+ .asc 签名）。校验：")
print("```")
print("sha256sum -c SHA256SUMS")
print("gpg --verify SHA256SUMS.asc SHA256SUMS   # 若发布了签名")
print("```")
PY
echo "== Release 正文：dist/release-notes.md"

echo
echo "下一步：正式对外发布走 tools/publish-release.sh（发布仓，源码 + .deb + 两行哈希）；"
echo "        zip 渠道仅在需要整包源码时使用。"
