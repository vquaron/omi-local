#!/usr/bin/env bash
# Native adapter; bootstrap can run before the project Python exists.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
command="${1:-open}"
[[ $# == 0 ]] || shift
case "$command:$*" in
  logs:|logs:-f|doctor:|doctor:--inference|bootstrap:|up:|down:|status:|open:|pair:|--install:) ;;
  *) echo 'Unsupported native command or arguments. Run ./omiloc --help.' >&2; exit 2 ;;
esac
source scripts/macos-runtime.sh
omi_require_apple_silicon "$PWD/scripts/native-runtime.sh" "$command" "$@"
omi_macos_path
if [[ "$command" == bootstrap ]]; then
  if ! omi_dependencies_ready; then
    echo 'Preparing native dependencies. Installation log: .local/install.log'
    bash scripts/install-local-mac.sh --quiet
    omi_macos_path
  fi
elif [[ "$command" == up ]] && ! omi_dependencies_ready; then
  echo 'Native dependencies are incomplete or changed. Run: ./omiloc bootstrap' >&2
  exit 1
fi
export PROVIDER_MODE=offline OMI_ENV_STAGE=offline
export OMI_DEV_HOST=127.0.0.1 OMI_DEV_BIND_HOST=127.0.0.1
export OMI_LOCAL_INSTANCE="${OMI_LOCAL_INSTANCE:-ngrok}"
export OMI_HARNESS_PORT_OFFSET="${OMI_HARNESS_PORT_OFFSET:-12000}"
export PYTHON="${PYTHON:-$PWD/backend/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo 'Native runtime missing. Run: ./omiloc bootstrap' >&2; exit 1; }
export PYTHONPATH="$PWD/scripts/dev-harness"
exec "$PYTHON" -m dev_harness.local_setup "$command" "$@"
