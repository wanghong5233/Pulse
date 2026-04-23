#!/usr/bin/env bash
# 一键跑项目的 smoke 回归.
#
# 原则: 使用 .env 里声明的 PULSE_VENV_DIR 作为**唯一** Python 事实源,
# 不要混用系统 python3 / conda / pyenv / 另造 .venv (曾经踩过坑: 项目根下
# 的 .venv 是个断链, 服务实际跑的是 /root/.venvs/pulse).
#
# 用法:
#   ./scripts/run_smoke.sh              跑全部
#   ./scripts/run_smoke.sh router only  只跑 smoke_router_priority.py (模糊匹配)
set -euo pipefail

cd "$(dirname "$0")/.."

# 读 .env 里的 PULSE_VENV_DIR (如果存在), 不污染 shell 的其余变量.
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  PULSE_VENV_DIR_FROM_ENV="$(grep -E '^PULSE_VENV_DIR=' .env | tail -1 | cut -d= -f2- || true)"
  if [[ -n "${PULSE_VENV_DIR_FROM_ENV:-}" && -z "${PULSE_VENV_DIR:-}" ]]; then
    export PULSE_VENV_DIR="$PULSE_VENV_DIR_FROM_ENV"
  fi
fi

PY="${PULSE_VENV_DIR:-/root/.venvs/pulse}/bin/python3"
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

if [[ ! -x "$PY" ]]; then
  echo "FATAL: Python not found at $PY" >&2
  echo "Set PULSE_VENV_DIR in .env to the real venv path." >&2
  exit 2
fi

echo "[env] python=$PY"
echo "[env] PYTHONPATH=$PYTHONPATH"
echo

ALL_SMOKES=(
  scripts/smoke_imports.py
  scripts/smoke_router_priority.py
  scripts/smoke_domain_preference_dispatch.py
  scripts/smoke_job_greet_pipeline.py
  scripts/smoke_memory_boundary.py
  scripts/smoke_startup_check.py
  scripts/smoke_events.py
)

# 可选过滤: 把参数当子串, 只跑匹配的 smoke
selected=()
if [[ $# -gt 0 ]]; then
  pattern="$*"
  for s in "${ALL_SMOKES[@]}"; do
    if [[ "$s" == *"$pattern"* ]]; then
      selected+=("$s")
    fi
  done
  if [[ ${#selected[@]} -eq 0 ]]; then
    echo "no smoke matched pattern: $pattern" >&2
    echo "candidates:" >&2
    printf '  %s\n' "${ALL_SMOKES[@]}" >&2
    exit 3
  fi
else
  selected=("${ALL_SMOKES[@]}")
fi

for s in "${selected[@]}"; do
  echo "===== $s ====="
  "$PY" "$s"
  echo
done

echo "===== ALL PASS ====="
