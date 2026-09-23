#!/usr/bin/env bash
#
# Install everything the notebook needs and open it in JupyterLab.
#
#   scripts/lab.sh                 install, then open the notebook
#   scripts/lab.sh --acquire       also download ERCOT vintages first
#   scripts/lab.sh --install-only  set the environment up and stop
#
# Any further arguments are passed through to `jupyter lab`, so
# `scripts/lab.sh --no-browser --port 8889` works as you would expect.
#
# Safe to re-run: the virtualenv, the dependency install and the ERCOT
# download are all idempotent.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
NOTEBOOK="notebooks/exploration.ipynb"
EXTRAS='.[dev,notebook]'

acquire=0
install_only=0
passthrough=()
for argument in "$@"; do
    case "$argument" in
        --acquire)      acquire=1 ;;
        --install-only) install_only=1 ;;
        -h|--help)      sed -n '3,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              passthrough+=("$argument") ;;
    esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

cd "$REPO"

# --- environment ------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    say "Installing dependencies with uv"
    [ -d "$VENV" ] || uv venv --python 3.12
    uv pip install -e "$EXTRAS"
else
    say "uv not found; falling back to python3 -m venv"
    if [ ! -d "$VENV" ]; then
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
            echo "error: Python 3.11+ is required (found $(python3 -V))." >&2
            echo "       Install uv from https://docs.astral.sh/uv/ and re-run." >&2
            exit 1
        }
        python3 -m venv "$VENV"
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -e "$EXTRAS"
fi

# --- data -------------------------------------------------------------------
if [ "$acquire" -eq 1 ]; then
    say "Downloading ERCOT vintages (public MIS endpoint, no credentials)"
    "$VENV/bin/forecast-spine" acquire
fi

vintages=$(find data/raw -name '*_csv.zip' 2>/dev/null | wc -l | tr -d ' ')
say "Environment ready"
if [ "$vintages" -gt 0 ]; then
    echo "  $vintages ERCOT vintages on disk — the notebook will run against live data."
else
    cat <<'MSG'
  No ERCOT vintages on disk. The notebook will fall back to synthetic
  fixtures and say so: the charts render, but the forecast revision
  structure they are about will be absent.

  Run `scripts/lab.sh --acquire` for the real thing (~12 MB, ~3 minutes,
  no credentials). Note that ERCOT's public listing retains only about
  7 days of forecast vintages, so a fresh download covers a different
  window than the one recorded in MEMO.md.
MSG
fi

if [ "$install_only" -eq 1 ]; then
    echo
    echo "  Open it yourself with: .venv/bin/jupyter lab $NOTEBOOK"
    exit 0
fi

say "Starting JupyterLab — press Ctrl-C to stop"
exec "$VENV/bin/jupyter" lab "$NOTEBOOK" "${passthrough[@]+"${passthrough[@]}"}"
