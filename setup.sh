#!/usr/bin/env bash
# One-shot setup for a new machine. Idempotent — safe to re-run.
#
#   ./setup.sh
#
# Installs Python deps, prepares the token directory, and runs the doctor so you
# can see exactly what's left (extension login, Schwab auth, .env keys).

set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-python3}
VENV=${VENV:-.venv}

echo "== Gödel research toolkit setup =="

# 1. Python
if ! command -v "$PY" >/dev/null; then
  echo "✗ python3 not found. Install Python 3.11+ first (https://www.python.org)."
  exit 1
fi
echo "✓ $("$PY" --version)"

# 2. Dependencies, into a venv.
# Most Linux distros (Debian/Ubuntu/Mint 24.04+, Fedora) ship PEP 668-marked
# Pythons where a bare `pip install` into the system interpreter is refused
# outright -- so always build a venv rather than assuming macOS's more
# permissive default.
echo "Installing Python dependencies…"
if [ ! -d "$VENV" ]; then
  if command -v uv >/dev/null; then uv venv "$VENV"
  else "$PY" -m venv "$VENV"; fi
fi
VPY="$VENV/bin/python"
[ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"   # git-bash on Windows
if command -v uv >/dev/null; then
  VIRTUAL_ENV="$VENV" uv pip install -q -r requirements.txt
else
  "$VPY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
  "$VPY" -m pip install -q -r requirements.txt
fi
echo "✓ dependencies installed into $VENV"
PY="$VPY"

# 3. Token directory
mkdir -p "$HOME/.godel_rest"
chmod 700 "$HOME/.godel_rest" 2>/dev/null || true
echo "✓ token directory ~/.godel_rest"

# 4. .env presence (don't print contents)
ENV_FILE="../.env"
if [ -f "$ENV_FILE" ]; then echo "✓ .env found at $(cd .. && pwd)/.env";
else echo "•  .env not found — copy yours to $(cd .. && pwd)/.env (Massive, OpenRouter, Schwab keys)"; fi

scripts_exec() { chmod +x services.sh setup.sh 2>/dev/null || true; }
scripts_exec

echo
echo "== Health check =="
"$PY" -m doctor || true

echo
cat <<EOF
Next steps (see SETUP.md for detail):
  0. Activate the venv:          source $VENV/bin/activate
  1. Load the Chrome extension:  chrome://extensions -> Load unpacked -> godel_rest/extension
  2. Start streaming:            ./services.sh start   (then log into app.godelterminal.com)
  3. Schwab (optional):          python3 -m broker.setup_auth
  4. Verify:                     python3 -m tests.test_framework
EOF
