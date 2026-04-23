#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
START_SCRIPT="$SCRIPT_DIR/start_backend.sh"
LOG_FILE="${PULSE_LOG_FILE:-/tmp/pulse.log}"
API_BASE="${PULSE_API_BASE:-http://127.0.0.1:8010}"

_is_up() {
  curl -sS --max-time 3 "$API_BASE/health" >/dev/null 2>&1
}

_process_pattern() {
  echo "uvicorn.*pulse.core.server:create_app|pulse start"
}

start() {
  if _is_up; then
    echo "[pulse] backend already running"
    status
    return 0
  fi

  echo "[pulse] starting backend..."
  nohup bash "$START_SCRIPT" >"$LOG_FILE" 2>&1 </dev/null &
  sleep 3

  if _is_up; then
    echo "[pulse] started"
    status
    return 0
  fi

  echo "[pulse] start failed, recent log:"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  return 1
}

stop() {
  local pattern pids
  pattern="$(_process_pattern)"
  pids="$(pgrep -f "$pattern" || true)"
  if [[ -z "$pids" ]]; then
    echo "[pulse] no backend process found"
    return 0
  fi

  echo "[pulse] stopping backend..."
  # shellcheck disable=SC2086
  kill $pids || true
  sleep 1
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "[pulse] force killing remaining backend processes..."
    pkill -9 -f "$pattern" || true
  fi
  echo "[pulse] stopped"
}

restart() {
  stop
  start
}

status() {
  local pattern
  pattern="$(_process_pattern)"
  echo "[pulse] process:"
  pgrep -af "$pattern" || echo "  (none)"
  echo "[pulse] health:"
  curl -sS --max-time 5 "$API_BASE/health" || echo "SERVICE_DOWN"
}

logs() {
  tail -n "${1:-120}" "$LOG_FILE" 2>/dev/null || echo "[pulse] no log file: $LOG_FILE"
}

usage() {
  cat <<EOF
Usage: bash $0 <command>

Commands:
  start        Start backend (loads .env via start_backend.sh)
  stop         Stop backend process
  restart      Restart backend
  status       Show process + /health
  logs [N]     Tail log file (default 120 lines)
EOF
}

cmd="${1:-status}"
case "$cmd" in
  start) start ;;
  stop) stop ;;
  restart) restart ;;
  status) status ;;
  logs) logs "${2:-120}" ;;
  *) usage; exit 1 ;;
esac
