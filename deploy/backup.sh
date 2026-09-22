#!/bin/bash
# RADIANT-LLM 备份脚本（原生部署）
# 打包 SQLite 数据库（evidence / memory / review / durable-checkpoint）与 artifacts/ 目录
# 到 $BACKUP_DIR（默认 <repo>/backups/）下的 tar.gz。
#
# 用法:  ./deploy/backup.sh [--out DIR]
# 环境:  BACKUP_DIR 默认 $BASE/backups
#        RADIANT_EVIDENCE_DB / RADIANT_MEMORY_DB / RADIANT_REVIEW_DB / RADIANT_DURABLE_DB
#        用于定位非默认路径的数据库（与运行时同一套环境变量）。
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$BASE/backups}"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) BACKUP_DIR="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# 加载 .env（拿到 RADIANT_EVIDENCE_DB 等路径配置）
ENV_FILE=$BASE/Docker_Executable/.env
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/radiant-backup-$TS.tar.gz"
mkdir -p "$BACKUP_DIR"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/db" "$STAGE/artifacts"

PY=$BASE/runtime/bin/python3.12

collect_db() {
  # $1=逻辑名 $2=路径（可能为空）
  # 用 SQLite 在线备份 API（而非 cp）：WAL 模式下主文件可能不含已提交数据，
  # cp 会丢 -wal 里的内容；backup API 产出一致的单文件快照。
  local name="$1" path="$2"
  if [ -n "$path" ] && [ -f "$path" ]; then
    "$PY" - "$path" "$STAGE/db/$name.db" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PYEOF
    echo "  + db/$name.db <- $path"
  else
    echo "  - $name: 不存在，跳过（${path:-未配置}）"
  fi
}

echo "收集 SQLite 数据库:"
collect_db evidence "${RADIANT_EVIDENCE_DB:-$BASE/evidence.db}"
collect_db memory "${RADIANT_MEMORY_DB:-$BASE/memory.db}"
collect_db review "${RADIANT_REVIEW_DB:-$BASE/review_queue.db}"
collect_db durable "${RADIANT_DURABLE_DB:-$BASE/durable.db}"

if [ -d "$BASE/artifacts" ]; then
  echo "收集 artifacts/ （$(du -sh "$BASE/artifacts" 2>/dev/null | cut -f1)）:"
  cp -a "$BASE/artifacts/." "$STAGE/artifacts/"
else
  echo "  - artifacts/ 不存在，跳过"
fi

cat > "$STAGE/MANIFEST.txt" <<EOF
radiant-llm backup
created: $(date -Iseconds)
host: $(hostname)
repo: $BASE
contents: db/*.db + artifacts/
restore: ./deploy/restore.sh $(basename "$ARCHIVE")
EOF

tar -czf "$ARCHIVE" -C "$STAGE" .
echo "备份完成: $ARCHIVE （$(du -h "$ARCHIVE" | cut -f1)）"
