#!/usr/bin/env bash
# Unified local iPhone launcher: debug, profile, or status.
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'HELP'
Usage: ./iphone.command [debug|profile|status] [--check|--build-only]
  debug         Run Omi Local Dev with interactive Flutter hot reload.
  profile       Install Omi Local for everyday launches from the app icon.
  status        Inspect existing iPhone builds and Flutter sessions.
  --check       Check tools, signing and device readiness without building.
  --build-only  Build Debug without installing or launching it.
Start the server separately with ./omiloc up. See docs/LOCAL_SETUP.md.
HELP
  exit 0
fi
source scripts/macos-runtime.sh
omi_require_apple_silicon "$PWD/iphone.command" "$@"
omi_macos_path
export PYTHONPATH="$PWD/scripts/dev-harness"
exec "${PYTHON:-$PWD/backend/.venv/bin/python}" -m dev_harness.ios_launcher "$@"
