#!/usr/bin/env bash
# Finder convenience launcher. The CLI owns each lifecycle operation.
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == --help ]]; then
  echo 'Usage: ./start.command'
  echo 'Prepare the native Mac runtime, start services, and open the library.'
  echo 'For individual operations, run ./omiloc --help.'
  exit 0
fi
[[ $# == 0 ]] || { echo 'Run ./omiloc --help for diagnostic commands.' >&2; exit 2; }
until ./omiloc --runtime native bootstrap; do
  [[ -t 0 && -t 1 ]] || exit 1
  read -r -p 'Setup stopped. Fix the reported issue; Enter to retry, q to exit: ' retry || exit 1
  [[ "$retry" != q && "$retry" != Q ]] || exit 1
done
./omiloc --runtime native up
exec ./omiloc --runtime native open
