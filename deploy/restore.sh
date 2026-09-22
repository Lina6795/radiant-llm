#!/bin/bash
# RADIANT-LLM 恢复脚本（原生部署）
# 从 backup.sh 生成的 tar.gz 恢复 SQLite 数据库与 artifacts/。
#
# 用法:  ./deploy/restore.sh <backup.tar.gz> [-y]
#   -y  跳过确认提示。
# 恢复前: 校验归档存在且 tar 完整；列出将被覆盖的目标；默认需人工确认。
# 建议先 ./start_radiant.sh stop 再恢复，恢复后重启服务。
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "用法: $0 <backup.tar.gz> [-y]" >&2
  exit 2
fi
ARCHIVE="$1"
ASSUME_YES="${2:-}"

if [ ! -f "$ARCHIVE" ]; then
  # 允许只给文件名，到默认备份目录找
  if [ -f "$BASE/backups/$ARCHIVE" ]; then
    ARCHIVE="$BASE/backups/$ARCHIVE"
  else
    echo "错误: 备份文件不存在: $1" >&2
    exit 1
  fi
fi

echo "校验归档完整性: $ARCHIVE"
if ! tar -tzf "$ARCHIVE" > /dev/null 2>&1; then
  echo "错误: 归档损坏或不是合法的 tar.gz" >&2
  exit 1
fi

ENV_FILE=$BASE/Docker_Executable/.env
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$ARCHIVE" -C "$STAGE"

[ -f "$STAGE/MANIFEST.txt" ] && cat "$STAGE/MANIFEST.txt"

declare -A DB_TARGETS=(
  [evidence]="${RADIANT_EVIDENCE_DB:-$BASE/evidence.db}"
  [memory]="${RADIANT_MEMORY_DB:-$BASE/memory.db}"
  [review]="${RADIANT_REVIEW_DB:-$BASE/review_queue.db}"
  [durable]="${RADIANT_DURABLE_DB:-$BASE/durable.db}"
)

echo ""
echo "将恢复以下内容:"
for name in evidence memory review durable; do
  if [ -f "$STAGE/db/$name.db" ]; then
    echo "  db/$name.db -> ${DB_TARGETS[$name]}$([ -f "${DB_TARGETS[$name]}" ] && echo '  (覆盖现有文件)')"
  fi
done
if [ -d "$STAGE/artifacts" ] && [ -n "$(ls -A "$STAGE/artifacts" 2>/dev/null)" ]; then
  echo "  artifacts/ -> $BASE/artifacts/  (合并覆盖同名文件)"
fi

if [ "$ASSUME_YES" != "-y" ]; then
  echo ""
  read -r -p "确认恢复？建议先停止服务（./start_radiant.sh stop）。输入 yes 继续: " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

for name in evidence memory review durable; do
  if [ -f "$STAGE/db/$name.db" ]; then
    mkdir -p "$(dirname "${DB_TARGETS[$name]}")"
    cp "$STAGE/db/$name.db" "${DB_TARGETS[$name]}"
    echo "已恢复 ${DB_TARGETS[$name]}"
  fi
done
if [ -d "$STAGE/artifacts" ] && [ -n "$(ls -A "$STAGE/artifacts" 2>/dev/null)" ]; then
  mkdir -p "$BASE/artifacts"
  cp -a "$STAGE/artifacts/." "$BASE/artifacts/"
  echo "已恢复 artifacts/"
fi

echo "恢复完成。重启服务: ./start_radiant.sh -d"
