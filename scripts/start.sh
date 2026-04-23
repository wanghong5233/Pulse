#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

BACKEND_PID=""
BOSS_MCP_PID=""
MONITOR_PID=""

cleanup() {
  echo ""
  echo "Stopping all services..."
  [[ -n "$MONITOR_PID" ]] && kill "$MONITOR_PID" 2>/dev/null || true
  [[ -n "$BACKEND_PID" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "$BOSS_MCP_PID" ]] && kill "$BOSS_MCP_PID" 2>/dev/null || true
  # patchright/chrome 孤儿由 boss_mcpctl.sh stop 语义兜底; 这里退路保险
  pkill -9 -f 'patchright/driver/package/cli.js' 2>/dev/null || true
  pkill -9 -f 'chrome-linux64/chrome.*/root/.pulse/boss_browser_profile' 2>/dev/null || true
  wait 2>/dev/null
  echo "All stopped."
}
trap cleanup EXIT INT TERM

load_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  set -a
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    [[ "$line" != *=* ]] && continue
    eval "export $line"
  done < "$ENV_FILE"
  set +a
}

# ---------- individual starters ----------

start_pg() {
  if command -v pg_ctlcluster >/dev/null 2>&1; then
    echo "[PG] Starting PostgreSQL cluster..."
    sudo pg_ctlcluster 16 main start 2>/dev/null || true
    echo "[PG] Ready (expected port: 5432)"
  else
    echo "[PG] Skip: pg_ctlcluster not found."
  fi
}

run_backend() {
  load_env
  bash "$SCRIPT_DIR/start_backend.sh" 2>&1 | while IFS= read -r l; do echo "[BE] $l"; done
}

run_boss_mcp() {
  load_env
  bash "$SCRIPT_DIR/start_boss_mcp.sh" 2>&1 | while IFS= read -r l; do echo "[BOSS_MCP] $l"; done
}

# 清场: 停掉 Pulse 自己管理的 gateway (daemon 或手动前台启的都匹配, 因为
# 两者最终都 `exec python -m pulse.mcp_servers.boss_platform_gateway`).
# 这不是"启发式兜底": `boss_mcpctl.sh stop` 通过 pgrep -f
# "python.*-m pulse.mcp_servers.boss_platform_gateway" 精确匹配自家进程,
# 不会误杀非 Pulse 程序; 于是 "stop → assert_port_free" 的顺序天然把
# 问题分成两类:
#   A) 旧 Pulse gateway    → stop 清掉, 继续启动
#   B) 非 Pulse 占 8811    → stop 无效, 下一步 assert_port_free 直接 fail-loud
# 两个动作不能合并、也不能换序, 否则会把 B 类问题吞掉.
cleanup_stale_boss_mcp() {
  [[ -x "$SCRIPT_DIR/boss_mcpctl.sh" ]] || return 0
  # quiet no-op when nothing to stop; noisy line prefix 方便用户关联上下文
  bash "$SCRIPT_DIR/boss_mcpctl.sh" stop 2>&1 \
    | while IFS= read -r l; do echo "[PREFLIGHT] $l"; done
  # stop 之后给 OS 一点时间释放 TIME_WAIT 前的半关连接, 避免 assert 误报
  sleep 1
}

# Fail-loud: cleanup 之后 8811 仍被占 → 必然是**非 Pulse** 进程, 直接终止
# 'all' 启动流程. 不做"让新 MCP 复用旧端口"或"静默跳过绑定"的兜底.
# 参见 ADR-001 §6 P3c-env 与 code-review-checklist §B (补丁式兼容零容忍).
assert_boss_mcp_port_free() {
  local port pids
  port="${PULSE_BOSS_MCP_GATEWAY_PORT:-8811}"
  if command -v ss >/dev/null 2>&1; then
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      return 0
    fi
  else
    echo "[SYS] WARN: 'ss' unavailable, skipping port preflight (port=$port)."
    return 0
  fi

  echo "[SYS] FATAL: port $port still in use after cleanup_stale_boss_mcp."
  echo "[SYS] Pulse-managed gateway was already stopped, so the occupier is"
  echo "[SYS] a non-Pulse process (or a differently-named Python entrypoint)."
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
    [[ -n "$pids" ]] && echo "[SYS] listening pid(s): $pids"
  fi
  echo "[SYS] Free the port manually (or change PULSE_BOSS_MCP_GATEWAY_PORT) then retry."
  return 1
}

# 清场: 停掉旧的 Pulse backend (前台 start_backend.sh 遗留 / 上次 Ctrl+C 没
# 清干净). 精确匹配自家 entrypoint `pulse.core.server:create_app`, 不会误杀
# 任何非 Pulse uvicorn 实例. 对称 `cleanup_stale_boss_mcp` 的语义:
#   A) 旧 Pulse backend → pkill 清掉, 继续启动
#   B) 非 Pulse 占 8010 → pkill 无效, assert_backend_port_free fail-loud
#
# 2026-04-22 post-mortem: 该函数之前缺失, 导致旧 backend 持有 8010,
# 新 `run_backend` 的 uvicorn 启动失败([Errno 98] Address already in use),
# 但 `wait_ready` 打到**旧进程**的 /health 仍然 200, 监控看起来"一切正常",
# 实则 `mcp_transport_http` / `_write_turn_meta` 等新代码从未被加载 —
# 整个观测面陷入"半生效"的幽灵态. 参见 ADR-005 附录.
cleanup_stale_backend() {
  local port="${PULSE_PORT:-8010}"

  if ! command -v pkill >/dev/null 2>&1; then
    return 0
  fi

  # Step 1: 精确匹配自家 entrypoint, 杀 uvicorn master. 不会误伤任何非
  # Pulse 的 Python 进程.
  if pkill -TERM -f 'pulse\.core\.server:create_app' 2>/dev/null; then
    echo "[PREFLIGHT] [backend] SIGTERM sent to stale Pulse backend master(s)"
    sleep 1
    pkill -KILL -f 'pulse\.core\.server:create_app' 2>/dev/null || true
    sleep 1
  else
    echo "[PREFLIGHT] [backend] no stale backend master process found"
  fi

  # Step 2: uvicorn --reload 会 spawn worker (multiprocessing.spawn),
  # worker 的 cmdline 是 `python -c "from multiprocessing.spawn import
  # spawn_main; ..."` — 不含 `pulse.core.server:create_app`, Step 1
  # 的精确 pkill 扫不到. master 被杀后 worker 变孤儿继续 bind $port,
  # 下一步 assert_backend_port_free 就 fail-loud.
  #
  # 做法: 从 listening pid 读 /proc/<pid>/cmdline, 仅当 cmdline 含
  # Pulse 自家特征 (`pulse` 模块名 或 uvicorn 且工作目录匹配自家项目)
  # 才 kill. 非 Pulse 进程仍然被 fail-loud 拦截 — 对称 boss_mcp 的契约.
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  local pid cmdline matched=0
  for pid in $(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
    [[ -r "/proc/$pid/cmdline" ]] || continue
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")"
    # 自家特征: 命令行含 pulse 模块 OR (含 uvicorn 且 cwd 指向本项目)
    if [[ "$cmdline" == *"pulse"* ]] \
       || { [[ "$cmdline" == *"uvicorn"* ]] \
            && [[ "$(readlink -f /proc/$pid/cwd 2>/dev/null)" == *"$PROJECT_DIR"* ]]; }; then
      echo "[PREFLIGHT] [backend] SIGKILL orphan worker pid=$pid (cmdline: ${cmdline:0:160})"
      kill -KILL "$pid" 2>/dev/null || true
      matched=1
    fi
  done
  if (( matched == 1 )); then
    sleep 1
  fi
}

# 对称 assert_boss_mcp_port_free: cleanup 后 8010 仍被占 = 非 Pulse 进程,
# fail-loud 而不是让新 uvicorn 静默挂掉、旧 backend 继续响应 /health 骗过
# 下游健康检查(2026-04-22 post-mortem 的直接成因).
assert_backend_port_free() {
  local port pids
  port="${PULSE_PORT:-8010}"
  if command -v ss >/dev/null 2>&1; then
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      return 0
    fi
  else
    echo "[SYS] WARN: 'ss' unavailable, skipping port preflight (port=$port)."
    return 0
  fi

  echo "[SYS] FATAL: port $port still in use after cleanup_stale_backend."
  echo "[SYS] Pulse-managed backend was already stopped, so the occupier is"
  echo "[SYS] a non-Pulse process (or a differently-named Python entrypoint)."
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
    [[ -n "$pids" ]] && echo "[SYS] listening pid(s): $pids"
  fi
  echo "[SYS] Free the port manually (or change PULSE_PORT) then retry."
  return 1
}

http_code() {
  local url="$1"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)"
  if [[ -n "$code" ]]; then
    echo "$code"
  else
    echo "DOWN"
  fi
}

# 等待 BOSS MCP /health 就绪 (含 patchright 冷启, 容忍 30s)
wait_boss_mcp_ready() {
  local code i port
  port="${PULSE_BOSS_MCP_GATEWAY_PORT:-8811}"
  echo "[SYS] Waiting for BOSS MCP readiness (port=$port)..."
  for i in $(seq 1 30); do
    code="$(http_code "http://127.0.0.1:${port}/health")"
    if [[ "$code" == "200" ]]; then
      echo "[SYS] BOSS MCP ready: gateway=200"
      # 露出关键模式, fail-loud 让人一眼看到当前执行模式
      curl -sS --max-time 3 "http://127.0.0.1:${port}/health" \
        | sed 's/^/[SYS]   health: /' || true
      return 0
    fi
    sleep 1
  done
  echo "[SYS] WARN: BOSS MCP not ready in 30s (last code=$code)."
  echo "[SYS] backend 仍会启动; 但 BOSS tool 调用会 fail-loud 直到 gateway 恢复."
  return 1
}

wait_ready() {
  local be_code i
  echo "[SYS] Waiting for backend readiness..."
  for i in $(seq 1 120); do
    be_code="$(http_code "http://127.0.0.1:8010/health")"
    if [[ "$be_code" == "200" ]]; then
      echo "[SYS] Backend ready: backend=200"
      return 0
    fi
    if (( i % 5 == 0 )); then
      echo "[SYS] Still starting... backend=$be_code (elapsed ${i}s)"
    fi
    sleep 1
  done
  echo "[SYS] Startup timeout. backend=$be_code"
  return 1
}

monitor_loop() {
  # State-change-only heartbeat:
  #   * first iteration prints the baseline (`last_be`/`last_mcp` empty)
  #   * subsequent iterations print ONLY when a status code changes
  # Purpose: terminal stays quiet while both services are healthy, but any
  # regression (200 → 5xx / 000 / 404) prints immediately — preserves the
  # fail-loud property without the 20-second "idle" spam.
  local be_code mcp_code ts port last_be="" last_mcp=""
  port="${PULSE_BOSS_MCP_GATEWAY_PORT:-8811}"
  while true; do
    be_code="$(http_code "http://127.0.0.1:8010/docs")"
    mcp_code="$(http_code "http://127.0.0.1:${port}/health")"
    if [[ "$be_code" != "$last_be" || "$mcp_code" != "$last_mcp" ]]; then
      ts="$(date '+%H:%M:%S')"
      echo "[SYS] $ts status | backend=$be_code boss_mcp=$mcp_code"
      last_be="$be_code"
      last_mcp="$mcp_code"
    fi
    sleep 20
  done
}

# ---------- main ----------

ACTION="${1:-all}"

case "$ACTION" in
  pg)
    start_pg
    ;;
  backend)
    exec bash "$SCRIPT_DIR/start_backend.sh"
    ;;
  boss_mcp)
    exec bash "$SCRIPT_DIR/start_boss_mcp.sh"
    ;;
  all)
    load_env
    echo ""
    echo "=========================================="
    echo "  Pulse — One command startup"
    echo "  Backend:   http://127.0.0.1:8010/docs"
    echo "  BOSS MCP:  http://127.0.0.1:${PULSE_BOSS_MCP_GATEWAY_PORT:-8811}/health"
    echo "  PULSE_ENVIRONMENT = ${PULSE_ENVIRONMENT:-dev}"
    echo "  Ctrl+C stops all services (incl. patchright chromium)"
    echo "=========================================="
    echo ""

    start_pg

    # Preflight 分两步 (顺序不可换):
    #   1) 清场 Pulse 自家服务 (旧 daemon 或手动前台启的) —— 用户常见情况
    #   2) fail-loud 非 Pulse 进程占用 —— 真问题必须暴露给人
    # Backend 和 boss_mcp 两条管线对称处理, 任一端残留旧进程都会让新进程
    # 启动失败, 但 /health 探测会被旧进程的 200 骗过 (2026-04-22 post-mortem).
    cleanup_stale_boss_mcp
    if ! assert_boss_mcp_port_free; then
      exit 2
    fi
    cleanup_stale_backend
    if ! assert_backend_port_free; then
      exit 2
    fi

    # BOSS MCP 先起 (patchright 冷启慢, 并行让 backend 不白等)
    run_boss_mcp &
    BOSS_MCP_PID=$!

    # 启动 2s 内检查子进程是否已 exit (典型: 端口突然占用、venv 坏、import error);
    # 若已死, 用它的真实退出码向上游 fail-loud, 而不是让 http 探测器误判
    # "旧 MCP 的 200 = 新实例成功".
    sleep 2
    if ! kill -0 "$BOSS_MCP_PID" 2>/dev/null; then
      # `wait` 拿退出码 (非阻塞: 进程已终结)
      wait "$BOSS_MCP_PID" 2>/dev/null
      mcp_rc=$?
      echo "[SYS] FATAL: BOSS MCP child (pid=$BOSS_MCP_PID) exited within 2s, rc=$mcp_rc."
      echo "[SYS] Aborting 'start.sh all'. Check [BOSS_MCP] lines above for the real error."
      exit "$mcp_rc"
    fi

    run_backend &
    BACKEND_PID=$!

    # 两个 ready 检查并行: MCP 先就绪不阻塞 backend, 反之亦然
    wait_boss_mcp_ready || true
    wait_ready || true
    monitor_loop &
    MONITOR_PID=$!

    wait
    ;;
  *)
    echo "Usage: $0 [all|pg|backend|boss_mcp]"
    exit 1
    ;;
esac
