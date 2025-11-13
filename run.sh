#!/usr/bin/env bash
# run.sh
# Setup a venv, install deps, and run either:
#   - umd2.py  (default, CLI backend)
#   - gui.py   (when --gui is passed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/umd2.py"
GUI_SCRIPT="${SCRIPT_DIR}/gui.py"
VENV_DIR="${SCRIPT_DIR}/.venv_umd2"

usage() {
cat <<'USAGE'
Usage:
  ./run.sh [--install-only] [--force-reinstall] [--gui] -- <args passed to umd2.py>

Examples (backend / CLI):
  ./run.sh -- --file LatheG4_displacement_file.txt --emit onstep
  cat LatheG4_displacement_file.txt | ./run.sh -- --fs 1000 --decimate 4 --out csv
  ./run.sh -- --serial /dev/tty.usbmodem1101 --baud 921600 --stepnm 79.124

GUI mode:
  ./run.sh --gui
  (GUI will call umd2.py under the hood. No extra args needed.)

Notes:
  - Anything after the first literal -- is passed directly to umd2.py.
  - Use --install-only to just create/update the venv and exit.
  - Use --force-reinstall to rebuild the venv from scratch.
USAGE
}

# Parse script-level flags (before the --)
INSTALL_ONLY=0
FORCE_REINSTALL=0
RUN_GUI=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-only)     INSTALL_ONLY=1; shift ;;
    --force-reinstall)  FORCE_REINSTALL=1; shift ;;
    --gui)              RUN_GUI=1; shift ;;
    --help|-h)          usage; exit 0 ;;
    --)                 shift; break ;;  # everything after this goes to umd2.py
    *)                  break ;;         # treat remaining as umd2.py args (without explicit --)
  esac
done

# Choose a Python
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "ERROR: python3 (or python) not found on PATH." >&2
  exit 1
fi

# (Re)create venv if needed
if [[ $FORCE_REINSTALL -eq 1 && -d "$VENV_DIR" ]]; then
  rm -rf "$VENV_DIR"
fi
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Upgrade pip and install baseline deps
python -m pip install --upgrade pip >/dev/null
# pyserial is only needed for --serial mode; install unconditionally (small).
python -m pip install --quiet pyserial

# Install GUI deps if requested
ensure_gui_deps() {
  # PySide6 + pyqtgraph are the only extras
  python -m pip install --quiet PySide6 pyqtgraph
}

if [[ $RUN_GUI -eq 1 ]]; then
  # Sanity checks for GUI
  if [[ ! -f "$GUI_SCRIPT" ]]; then
    echo "ERROR: Cannot find gui.py at: $GUI_SCRIPT" >&2
    exit 1
  fi
  if [[ ! -f "$PY_SCRIPT" ]]; then
    echo "ERROR: Cannot find umd2.py at: $PY_SCRIPT (GUI calls it under the hood)" >&2
    exit 1
  fi
  ensure_gui_deps
fi

if [[ $INSTALL_ONLY -eq 1 ]]; then
  echo "Virtualenv ready at: $VENV_DIR"
  if [[ $RUN_GUI -eq 1 ]]; then
    echo "GUI deps installed. Launch with: ./run.sh --gui"
  else
    echo "Example: ./run.sh -- --file LatheG4_displacement_file.txt --emit onstep"
  fi
  exit 0
fi

if [[ $RUN_GUI -eq 1 ]]; then
  # Run GUI
  exec python "$GUI_SCRIPT"
else
  # Backend mode: sanity check umd2.py
  if [[ ! -f "$PY_SCRIPT" ]]; then
    echo "ERROR: Cannot find umd2.py at: $PY_SCRIPT" >&2
    exit 1
  fi
  # Run umd2.py with any remaining args
  exec python "$PY_SCRIPT" "$@"
fi
