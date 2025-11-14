#!/usr/bin/env bash
set -euo pipefail

# --- Paths ---
PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"
VENV="$PROJ_DIR/.venv_umd2"
REQ="$PROJ_DIR/requirements.txt"

# --- Make ./run symlink (and re-exec through it on first run for immediate use) ---
if [[ ! -e "$PROJ_DIR/run" ]]; then
  ln -s "run.sh" "run" 2>/dev/null || true
  if [[ "$(basename "$0")" = "run.sh" ]]; then
    exec "$PROJ_DIR/run" "$@"
  fi
fi

# --- Flags / Mode ---
FORCE=0
MODE="gui"  # default
case "${1:-}" in
  --force-install) FORCE=1; shift;;
esac
case "${1:-}" in
  --backend) MODE="backend"; shift;;
  --gui) MODE="gui"; shift;;
esac

# --- venv (no activation needed) ---
if [[ ! -d "$VENV" ]]; then
  echo "[RUN] Creating venv at $VENV"
  python3 -m venv "$VENV"
fi

# --- deps (install when requirements.txt changes or --force-install) ---
REQ_HASH_FILE="$VENV/.req_hash"
CUR_HASH="$("$VENV/bin/python" - <<'PY' "$REQ"
import sys, pathlib, hashlib
p = pathlib.Path(sys.argv[1])
print(hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "")
PY
)"
OLD_HASH="$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "")"

if [[ ! -f "$REQ" ]]; then
  echo "[RUN] WARNING: requirements.txt not found; skipping installs." >&2
elif [[ "$FORCE" -eq 1 || "$CUR_HASH" != "$OLD_HASH" ]]; then
  echo "[RUN] Installing/Updating deps from requirements.txt"
  "$VENV/bin/python" -m pip install -U pip >/dev/null
  "$VENV/bin/pip" install -r "$REQ"
  echo "$CUR_HASH" > "$REQ_HASH_FILE"
else
  echo "[RUN] Deps up-to-date (requirements.txt unchanged) — skipping install"
fi

# --- Launch ---
if [[ "$MODE" == "backend" ]]; then
  echo "[RUN] Backend mode → python umd2.py $*"
  exec "$VENV/bin/python" "$PROJ_DIR/umd2.py" "$@"
else
  echo "[RUN] GUI mode (default) → python gui.py"
  echo "[RUN] Tips:"
  echo "       • For backend: ./run.sh --backend --serial /dev/tty.usbmodemXXXX --baud 921600 --out jsonl"
  echo "       • Force reinstall deps: ./run.sh --force-install"
  exec "$VENV/bin/python" "$PROJ_DIR/gui.py" "$@"
fi
