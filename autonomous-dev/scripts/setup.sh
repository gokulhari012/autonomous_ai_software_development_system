#!/usr/bin/env bash
# One-time setup on Linux/macOS. Run from the repository root: bash autonomous-dev/scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

[ -d .venv ] || python3 -m venv .venv
PY=.venv/bin/python
$PY -m pip install --upgrade pip
$PY -m pip install -r autonomous-dev/requirements.txt -r dashboard/requirements.txt
$PY -m pip install -r autonomous-dev/sample_apps/flask-inventory/requirements.txt -r autonomous-dev/sample_apps/pyside-notes/requirements.txt
$PY -m playwright install --with-deps chromium || $PY -m playwright install chromium

[ -f autonomous-dev/.env ] || { cp autonomous-dev/.env.example autonomous-dev/.env; echo "Created autonomous-dev/.env"; }

(cd dashboard/frontend && npm install --no-fund --no-audit && npm run build)
(cd autonomous-dev && ../$PY scripts/create_sample_workspace.py && ../$PY main.py doctor)

cat <<'EOF'

Setup complete. Next steps:
  1. cd dashboard && ../.venv/bin/python -m backend.manage set-password
  2. ../.venv/bin/python -m backend.app          # dashboard at http://127.0.0.1:8000
  3. cd ../autonomous-dev && ../.venv/bin/python main.py run
Headless Linux: Qt widget tests use the offscreen platform automatically; for PyAutoGUI tests run
under a virtual display, e.g. `xvfb-run -a ../.venv/bin/python main.py run`, and set
testing.pyside.allow_real_input_automation=true only on a dedicated test machine.
EOF
