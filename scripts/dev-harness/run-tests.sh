#!/usr/bin/env bash
# Full standalone harness suite; no dependency installation or service startup.
# The transport CI lane runs the same inventory through make test-library.
set -euo pipefail

cd "$(dirname "$0")/../.."

tests=()
seen=' '
while IFS= read -r path || [[ -n "$path" ]]; do
  [[ -n "$path" && "$path" != \#* ]] || continue
  [[ "$path" == scripts/dev-harness/tests/test_*.py && -f "$path" ]] || {
    echo 'Invalid standalone test inventory; check scripts/dev-harness/standalone-tests.txt.' >&2
    exit 1
  }
  [[ "$seen" != *" $path "* ]] || { echo 'Duplicate entry in standalone test inventory.' >&2; exit 1; }
  seen+="$path "
  tests+=("$path")
done < scripts/dev-harness/standalone-tests.txt
[[ ${#tests[@]} -gt 0 ]] || { echo 'Standalone test inventory is empty.' >&2; exit 1; }

run_pytest() {
  # Tests choose their provider mode explicitly; Makefile's runtime default
  # must not change the fixtures that exercise real/offline policy boundaries.
  exec env -u PROVIDER_MODE "$@" -m pytest -q -p no:cacheprovider "${tests[@]}"
}

for py in \
  "${PYTHON:-}" \
  backend/.venv/bin/python \
  backend/.venv/Scripts/python.exe \
  backend/venv/bin/python \
  backend/venv/Scripts/python.exe \
  python3; do
  if [ -x "$py" ] || command -v "$py" >/dev/null 2>&1; then
    if "$py" -c 'import pytest, dotenv, httpx, yaml, numpy' >/dev/null 2>&1; then
      run_pytest "$py"
    fi
  fi
done

echo 'Harness test dependencies are unavailable. Prepare the backend with ./omiloc bootstrap, then retry.' >&2
exit 1
