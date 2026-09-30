#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/macos-runtime.sh
omi_require_apple_silicon "$PWD/scripts/local-mac.sh" "$@"
if [ "${1:-}" = install ]; then
  exec bash scripts/install-local-mac.sh
fi
export PROVIDER_MODE=offline OMI_ENV_STAGE=offline
export OMI_DEV_HOST=127.0.0.1 OMI_DEV_BIND_HOST=127.0.0.1
export OMI_LOCAL_INSTANCE="${OMI_LOCAL_INSTANCE:-ngrok}"
export OMI_HARNESS_PORT_OFFSET="${OMI_HARNESS_PORT_OFFSET:-12000}"
omi_macos_path
export PYTHON="${PYTHON:-$PWD/backend/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo 'Backend runtime missing. Run: bash scripts/local-mac.sh install' >&2
  exit 1
fi
export PYTHONPATH="$PWD/scripts/dev-harness"
if [ "${1:-}" = launcher ]; then
  shift
  exec "$PYTHON" -m dev_harness.local_launcher "$@"
fi
exec "$PYTHON" -m dev_harness.local_mac "$@"
