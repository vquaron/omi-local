#!/usr/bin/env bash
# Backend/runtime only. App generation and iOS builds are separate operations.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
source scripts/macos-runtime.sh
omi_require_apple_silicon "$PWD/scripts/install-local-mac.sh" "$@"
for input in backend/.python-version backend/pylock.macos.toml package.json package-lock.json firebase.json backend/scripts/sync-python-deps.sh; do
  test -s "$input" || { echo "Missing installer input: $input" >&2; exit 1; }
done
quiet=false
case "${1:-}" in
  '') ;;
  --quiet) quiet=true ;;
  *) echo 'Usage: install-local-mac.sh [--quiet]' >&2; exit 1 ;;
esac
# Only dependency installation is logged; pairing and keys are handled later.
[[ ! -L .local && ! -L .local/install.log && ! -L .local/install.ready && ! -L .local/install.lock ]] || { echo 'Unsafe installer log path.' >&2; exit 1; }
mkdir -p .local
exec 9>.local/install.lock
if ! lockf -s -t 0 9; then
  echo 'Native preparation is already running. Wait for it to finish.' >&2
  exit 1
fi
install_inputs=$(omi_install_fingerprint)
# Any interruption, including SIGKILL, leaves the installation incomplete.
rm -f .local/install.ready
install_log="$PWD/.local/install.log"
: >>"$install_log"
chmod 600 "$install_log"
printf '\nInstallation started: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >>"$install_log"
installer_file=''
finish_install() {
  local result=$?
  printf 'Installation exit status: %s\n' "$result" >>"$install_log"
  [[ -z "$installer_file" ]] || rm -f "$installer_file"
  return "$result"
}
trap finish_install EXIT
run_step() {
  local status=0
  if [[ "$quiet" == true ]]; then
    "$@" >>"$install_log" 2>&1 || status=$?
  else
    "$@" 2>&1 | tee -a "$install_log" || status=$?
  fi
  if [[ "$status" != 0 ]]; then
    echo "Failed: $install_stage. $install_remedy" >&2
    echo 'Details: .local/install.log' >&2
  fi
  return "$status"
}
omi_macos_path
install_stage='prepare Homebrew'
install_remedy='Check networking and Xcode command line tools: xcode-select --install.'
if ! command -v brew >/dev/null 2>&1; then
  echo 'Installing Homebrew. Follow its prompts in this terminal.'
  installer_file="$(mktemp -t omi-homebrew)"
  run_step curl --fail --location --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$installer_file"
  # Preserve the terminal and record output; do not enable input recording (-k).
  /usr/bin/script -q -a "$install_log" /bin/bash "$installer_file" || {
    echo 'Homebrew is not installed. Complete setup: https://brew.sh' >&2
    exit 1
  }
  omi_macos_path
fi
for formula in uv node@22 openjdk@21 redis opus ffmpeg jq; do
  if ! brew list --versions "$formula" >/dev/null 2>&1; then
    install_stage="install $formula"
    install_remedy="Check networking; retry with: brew install $formula."
    echo "Installing $formula..."
    run_step brew install "$formula"
  fi
done

omi_macos_path
echo 'Preparing native application dependencies...'
install_stage='prepare Python'
install_remedy='Check networking and free disk space, then retry bootstrap.'
run_step bash backend/scripts/sync-python-deps.sh
# npm ci is skipped only when the complete install was attested to these inputs.
npm_inputs="$(shasum -a 256 package.json package-lock.json)"
if [ ! -f node_modules/.omi-install-inputs ] || [ "$(cat node_modules/.omi-install-inputs)" != "$npm_inputs" ]; then
  install_stage='prepare Firebase CLI'
  rm -f node_modules/.omi-install-inputs
  run_step npm ci --no-audit --no-fund
  printf '%s\n' "$npm_inputs" > node_modules/.omi-install-inputs
fi
export PYTHON="$PWD/backend/.venv/bin/python"
transport=$(PYTHONPATH=scripts/dev-harness "$PYTHON" -m dev_harness.local_transport)
if [[ "$transport" == ngrok ]]; then
if ! command -v ngrok >/dev/null 2>&1; then
  install_stage='install ngrok'
  install_remedy='Retry with: brew install --cask ngrok.'
  echo 'Installing ngrok...'
  run_step brew install --cask ngrok
fi
fi
install_stage='download Firebase emulator'
PYTHONPATH=scripts/dev-harness run_step "$PYTHON" -m dev_harness.local_mac prepare-emulator
echo 'Checking native service prerequisites...'
install_stage='check native service prerequisites'
install_remedy='Diagnostics: bash scripts/local-mac.sh check.'
run_step bash scripts/local-mac.sh check
[[ "$install_inputs" == "$(omi_install_fingerprint)" ]] || { echo 'Installer inputs changed; run installation again.' >&2; exit 1; }
ready_file=$(mktemp .local/install.ready.XXXXXX)
printf '%s\n' "$install_inputs" > "$ready_file"
mv -f "$ready_file" .local/install.ready
if [[ "$quiet" != true ]]; then echo 'Installation checked. Log: .local/install.log'; fi
