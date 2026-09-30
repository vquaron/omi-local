#!/usr/bin/env bash
# Re-enter the same command natively when Terminal runs under Rosetta.
omi_require_apple_silicon() {
  if [[ "$(uname -s)" == Darwin ]]; then
    case "$(uname -m)" in
      arm64) return 0 ;;
      x86_64)
        if [[ "$(sysctl -in sysctl.proc_translated 2>/dev/null)" == 1 ]]; then
          exec /usr/bin/arch -arm64 /bin/bash "$@"
        fi
        ;;
    esac
  fi
  echo 'Native startup requires an Apple Silicon Mac. Use --runtime docker on Linux.' >&2
  return 1
}

# Prefer the native default installation, otherwise use the brew on PATH.
omi_macos_path() {
  if [[ -x /opt/homebrew/bin/brew ]]; then
    export PATH="/opt/homebrew/bin:$PATH"
  fi
  local brew_prefix
  if command -v brew >/dev/null 2>&1; then
    brew_prefix=$(brew --prefix) || return 1
    export PATH="$PWD/node_modules/.bin:$brew_prefix/opt/node@22/bin:$brew_prefix/opt/openjdk@21/bin:$brew_prefix/bin:$PATH"
  else
    export PATH="$PWD/node_modules/.bin:$PATH"
  fi
  # Resolve from this file: setup.sh calls us after changing into app/.
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -x "$repo_root/.local/toolchains/flutter/bin/flutter" ]]; then
    export PATH="$repo_root/.local/toolchains/flutter/bin:$PATH"
  fi
}

# The pinned Firebase CLI requires Java 21; macOS also ships a nonfunctional stub.
omi_java_ready() {
  local version_output version_pattern='version "([0-9]+)'
  version_output=$(java -Duser.language=en -version 2>&1) || return 1
  [[ "$version_output" =~ $version_pattern ]] || return 1
  (( ${BASH_REMATCH[1]} >= 21 ))
}

omi_install_fingerprint() {
  shasum -a 256 backend/.python-version backend/pylock.macos.toml \
    backend/scripts/sync-python-deps.sh package.json package-lock.json firebase.json \
    scripts/install-local-mac.sh scripts/macos-runtime.sh | shasum -a 256 | awk '{print $1}'
}

omi_install_ready() {
  [[ -f .local/install.ready && ! -L .local/install.ready ]] || return 1
  [[ "$(cat .local/install.ready)" == "$(omi_install_fingerprint)" ]]
}

omi_dependencies_ready() {
  omi_install_ready || return 1
  [[ -x backend/.venv/bin/python && -x node_modules/.bin/firebase ]] || return 1
  local tool transport
  for tool in uv node java redis-server ffmpeg jq brew; do
    command -v "$tool" >/dev/null 2>&1 || return 1
  done
  omi_java_ready || return 1
  brew list --versions opus >/dev/null 2>&1 || return 1
  transport=$(PYTHONPATH=scripts/dev-harness backend/.venv/bin/python -m dev_harness.local_transport) || return 1
  if [[ "$transport" == ngrok ]]; then command -v ngrok >/dev/null 2>&1 || return 1; fi
}
