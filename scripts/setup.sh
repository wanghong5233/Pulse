#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${PULSE_VENV_DIR:-$PROJECT_DIR/.venv}"

echo "=== Pulse Setup ==="
echo ""

# 1) PostgreSQL
echo "[1/3] PostgreSQL..."
if ! command -v psql >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y -qq postgresql postgresql-contrib
fi
sudo pg_ctlcluster 16 main start 2>/dev/null || true
"$SCRIPT_DIR/setup-pg.sh"

# 2) Python venv + deps (driven by pyproject.toml)
echo ""
echo "[2/3] Python dependencies..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -e "$PROJECT_DIR"

# 3) Playwright
echo ""
echo "[3/3] Playwright Chromium..."
python -m playwright install chromium

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Startup (two terminals):"
echo "  Terminal 1: cd $PROJECT_DIR && ./scripts/start.sh pg"
echo "  Terminal 2: cd $PROJECT_DIR && ./scripts/start.sh backend"
echo ""
echo "Or one command:"
echo "  cd $PROJECT_DIR && ./scripts/start.sh all"
