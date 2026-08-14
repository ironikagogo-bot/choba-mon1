#!/bin/sh
# 納品zipを作る。2026-08-14の事故(テストがzipに入っておらずコンテナ消失で失われかけた)を受けて、
# 配布物だけでなく tests/ も必ず含める構成にした。マニフェストはリポジトリ内で版管理する。
#   使い方: sh scripts/pack.sh v241  →  /tmp/mon1-v241.zip
set -e
VER="${1:?版数を指定 (例: v241)}"
cd "$(dirname "$0")/.."
OUT="/tmp/mon1-${VER}.zip"
rm -f "$OUT"
zip -q "$OUT" -@ < scripts/ziplist.txt
echo "$OUT"
unzip -l "$OUT" | tail -1
# 節目では git bundle も一緒に渡す(2MB台で全履歴。コンテナ消失からの完全復元用)
git bundle create "/tmp/chouba_git_${VER}.bundle" --all >/dev/null 2>&1 \
  && echo "/tmp/chouba_git_${VER}.bundle"
