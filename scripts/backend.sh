#!/usr/bin/env bash
# OptionsAji FastAPI 本地启停（后台 nohup + PID 文件）
# 用法:
#   ./scripts/backend.sh install # 首次：升级 pip 并安装 requirements.txt
#   ./scripts/backend.sh start    # 后台启动（默认端口 8787）
#   ./scripts/backend.sh stop     # 停止
#   ./scripts/backend.sh restart  # 重启
#   ./scripts/backend.sh status   # 是否运行
#   ./scripts/backend.sh log      # tail -f 日志
#   ./scripts/backend.sh fg       # 前台运行（调试用，Ctrl+C 退出）
#
# 环境变量:
#   PORT=8787          监听端口
#   HOST=0.0.0.0       监听地址
#   UVICORN_EXTRA      额外参数，例如: --log-level debug

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PID_FILE="${ROOT}/.backend.pid"
LOG_FILE="${ROOT}/.backend.log"
PORT="${PORT:-8787}"
HOST="${HOST:-0.0.0.0}"

if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  PY="python"
fi

check_deps() {
  if "$PY" -c "import uvicorn, fastapi" 2>/dev/null; then
    return 0
  fi
  echo "当前解释器未安装依赖（例如缺少 uvicorn）。请先执行：" >&2
  echo "  ${ROOT}/scripts/backend.sh install" >&2
  echo "或：" >&2
  echo "  cd ${ROOT} && ${PY} -m pip install -U pip setuptools wheel && ${PY} -m pip install -r requirements.txt" >&2
  exit 1
}

cmd_install() {
  echo "使用解释器: $PY"
  "$PY" -m pip install -U pip setuptools wheel
  "$PY" -m pip install -r "${ROOT}/requirements.txt"
  echo "依赖已安装。启动: ./scripts/backend.sh start"
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

cmd_start() {
  check_deps
  if is_running; then
    echo "已在运行 PID=$(cat "$PID_FILE") 端口=${PORT} 日志=${LOG_FILE}"
    exit 0
  fi
  rm -f "$PID_FILE"
  echo "$(date -Iseconds) 启动 uvicorn app.main:app ${HOST}:${PORT}" >>"$LOG_FILE"
  # shellcheck disable=SC2086
  nohup env PYTHONPATH="${ROOT}" PORT="${PORT}" \
    "$PY" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}" ${UVICORN_EXTRA:-} \
    >>"${LOG_FILE}" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 0.3
  if is_running; then
    echo "已后台启动 PID=$(cat "$PID_FILE") http://${HOST}:${PORT}/health 日志=${LOG_FILE}"
  else
    echo "启动可能失败，请查看: tail -n 50 ${LOG_FILE}" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
}

cmd_stop() {
  if ! is_running; then
    echo "未运行（无有效 PID 文件）"
    rm -f "$PID_FILE"
    exit 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if kill -0 "$pid" 2>/dev/null; then
      sleep 0.2
    else
      break
    fi
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "进程未退出，发送 SIGKILL: $pid" >&2
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "已停止"
}

cmd_status() {
  if is_running; then
    echo "运行中 PID=$(cat "$PID_FILE") 端口=${PORT} 日志=${LOG_FILE}"
    curl -sS -m 2 "http://127.0.0.1:${PORT}/health" | head -c 200 || echo "(health 请求失败)"
    echo ""
  else
    echo "未运行"
    rm -f "$PID_FILE"
    exit 1
  fi
}

cmd_log() {
  exec tail -n 100 -f "$LOG_FILE"
}

cmd_fg() {
  check_deps
  # shellcheck disable=SC2086
  exec env PYTHONPATH="${ROOT}" PORT="${PORT}" \
    "$PY" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}" ${UVICORN_EXTRA:-}
}

case "${1:-}" in
  install) cmd_install ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  log) cmd_log ;;
  fg) cmd_fg ;;
  *)
    echo "用法: $0 {install|start|stop|restart|status|log|fg}" >&2
    echo "当前目录: $ROOT" >&2
    exit 1
    ;;
esac
