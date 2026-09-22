#!/bin/bash
# RADIANT-LLM 启动脚本（无 Docker 直跑版）
# 用法:  ./start_radiant.sh          前台运行（调试用）
#        ./start_radiant.sh -d       后台运行
#        ./start_radiant.sh stop     停止

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=$BASE/app
PY=$BASE/runtime/bin/python3.12
ENV_FILE=$BASE/Docker_Executable/.env
PIDFILE=$BASE/radiant-llm.pid
LOG=$BASE/radiant-llm.out.log

stop_app() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
    kill "$(cat $PIDFILE)" && rm -f "$PIDFILE" && echo "已停止"
  else
    pkill -f "$BASE/runtime/bin/python3.12" && echo "已停止" || echo "没有在运行"
  fi
}

[ "$1" = "stop" ] && { stop_app; exit 0; }

# 加载 API keys（.env 里 KEY=value 格式）
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

export LD_LIBRARY_PATH="$BASE/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$BASE/runtime/bin:$PATH"
export RADIANT_LLM_SKILLS_DIR="${RADIANT_LLM_SKILLS_DIR:-$BASE/radiant_llm_skills}"
export RADIANT_LLM_SESSION_DIR="${RADIANT_LLM_SESSION_DIR:-$BASE/Docker_Executable/RADIANT_LLM_Sessions}"
export RADIANT_LLM_PORT="${RADIANT_LLM_PORT:-8080}"
export PYTHONUNBUFFERED=1

cd "$APP" || exit 1

if [ "$1" = "-d" ]; then
  nohup "$PY" api.py > "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "已在后台启动 (PID $(cat $PIDFILE))，端口 $RADIANT_LLM_PORT"
  echo "日志: $LOG   （tail -f $LOG 查看）"
  echo "停止: $0 stop"
else
  exec "$PY" api.py
fi
