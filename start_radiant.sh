#!/bin/bash
# RADIANT-LLM 启动脚本（无 Docker 直跑版）
# 用法:  ./start_radiant.sh          前台运行（调试用）
#        ./start_radiant.sh -d       后台运行（启动后轮询 /health 并报告）
#        ./start_radiant.sh stop     停止
#        ./start_radiant.sh status   查看进程与健康状态
#
# 日志轮转: radiant-llm.out.log 超过 $LOG_MAX_MB（默认 50MB）时切割，
# 保留 $LOG_KEEP（默认 5）份（.1 最新）。纯 shell 实现，不依赖 logrotate。

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=$BASE/app
PY=$BASE/runtime/bin/python3.12
ENV_FILE=$BASE/Docker_Executable/.env
PIDFILE=$BASE/radiant-llm.pid
LOG=$BASE/radiant-llm.out.log
LOG_MAX_MB=${LOG_MAX_MB:-50}
LOG_KEEP=${LOG_KEEP:-5}
HEALTH_WAIT_S=${HEALTH_WAIT_S:-120}

health_url() { echo "http://127.0.0.1:${RADIANT_LLM_PORT:-8080}/health"; }

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null
}

stop_app() {
  if is_running; then
    kill "$(cat $PIDFILE)" && rm -f "$PIDFILE" && echo "已停止"
  else
    pkill -f "$BASE/runtime/bin/python3.12" && echo "已停止" || echo "没有在运行"
  fi
}

rotate_log() {
  [ -f "$LOG" ] || return 0
  local size_mb
  size_mb=$(( $(stat -c %s "$LOG" 2>/dev/null || echo 0) / 1048576 ))
  [ "$size_mb" -ge "$LOG_MAX_MB" ] || return 0
  local i
  for (( i=LOG_KEEP-1; i>=1; i-- )); do
    [ -f "$LOG.$i" ] && mv "$LOG.$i" "$LOG.$((i+1))"
  done
  mv "$LOG" "$LOG.1"
  rm -f "$LOG.$((LOG_KEEP+1))"
  echo "日志已轮转（${size_mb}MB >= ${LOG_MAX_MB}MB），保留最近 $LOG_KEEP 份"
}

wait_for_health() {
  # 启动后轮询 /health，超时报告失败并展示日志尾部。
  local waited=0
  while [ $waited -lt $HEALTH_WAIT_S ]; do
    if curl -sf --max-time 2 "$(health_url)" > /dev/null 2>&1; then
      echo "健康检查通过: $(health_url) （等待 ${waited}s）"
      return 0
    fi
    if ! is_running; then
      echo "错误: 进程已退出，健康检查未通过。日志尾部:" >&2
      tail -n 30 "$LOG" >&2
      return 1
    fi
    sleep 2
    waited=$((waited+2))
  done
  echo "错误: ${HEALTH_WAIT_S}s 内健康检查未通过。日志尾部:" >&2
  tail -n 30 "$LOG" >&2
  return 1
}

status_app() {
  if is_running; then
    echo "进程: 运行中 (PID $(cat $PIDFILE))"
  else
    echo "进程: 未运行"
  fi
  if curl -sf --max-time 2 "$(health_url)" > /dev/null 2>&1; then
    echo "健康: ok ($(health_url))"
  else
    echo "健康: 不可达 ($(health_url))"
  fi
  [ -f "$LOG" ] && echo "日志: $LOG ($(du -h "$LOG" | cut -f1))"
}

[ "$1" = "stop" ] && { stop_app; exit 0; }
[ "$1" = "status" ] && { RADIANT_LLM_PORT=${RADIANT_LLM_PORT:-8080}; status_app; exit 0; }

# 加载 API keys（.env 里 KEY=value 格式）
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

export LD_LIBRARY_PATH="$BASE/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$BASE/runtime/bin:$PATH"
# tesseract（OCR 备用路径，扫描版 PDF 用；不存在则跳过，pytesseract 为可选依赖）
TESSERACT_PREFIX="${TESSERACT_PREFIX:-/root/tesseract-env}"
[ -x "$TESSERACT_PREFIX/bin/tesseract" ] && export PATH="$TESSERACT_PREFIX/bin:$PATH"
export RADIANT_LLM_SKILLS_DIR="${RADIANT_LLM_SKILLS_DIR:-$APP/radiant_llm_skills}"
export RADIANT_LLM_SESSION_DIR="${RADIANT_LLM_SESSION_DIR:-$BASE/Docker_Executable/RADIANT_LLM_Sessions}"
export RADIANT_LLM_PORT="${RADIANT_LLM_PORT:-8080}"
export PYTHONUNBUFFERED=1

cd "$APP" || exit 1

rotate_log

if [ "$1" = "-d" ]; then
  if is_running; then
    echo "已在运行 (PID $(cat $PIDFILE))，先执行 $0 stop 再启动" >&2
    exit 1
  fi
  nohup "$PY" api.py > "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "已在后台启动 (PID $(cat $PIDFILE))，端口 $RADIANT_LLM_PORT"
  echo "日志: $LOG   （tail -f $LOG 查看）"
  echo "停止: $0 stop    状态: $0 status"
  wait_for_health
else
  exec "$PY" api.py
fi
