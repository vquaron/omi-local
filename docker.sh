#!/usr/bin/env bash
# Docker adapter. No host Python, Node, Java or model runtime required.
set -euo pipefail
umask 077
cd "$(dirname "$0")"
command="${1:-help}"
[[ $# == 0 ]] || shift
fail() { echo "$*" >&2; exit 1; }
usage() { echo 'Run ./omiloc --help. Docker settings are saved by bootstrap in .env.docker.'; }
case "$command" in help|-h|--help) usage; exit 0 ;; esac
case "$command" in bootstrap|up|dev|down|status|logs|doctor|open|pair|config|configure|tunnel) ;;
  *) usage >&2; exit 2 ;;
esac
# Read a deliberately small, non-executable format. Never source an env file.
OMI_DOCKER_DEVICE=${OMI_DOCKER_DEVICE:-cpu}
OMI_DOCKER_GPU_ID=${OMI_DOCKER_GPU_ID:-0}
OMI_DOCKER_TRANSPORT=${OMI_DOCKER_TRANSPORT:-local}
STT_MODEL=${STT_MODEL:-Systran/faster-whisper-small}
STT_REVISION=${STT_REVISION:-main}
STT_THREADS=${STT_THREADS:-4}
config_file=.env.docker
[[ ! -L "$config_file" ]] || fail '.env.docker must not be a symlink.'
if [[ -e "$config_file" ]]; then
  [[ -f "$config_file" && $(wc -c < "$config_file") -le 4096 ]] || fail 'Invalid .env.docker file.'
  seen=' '
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" && "$line" != \#* ]] || continue
    [[ "$line" == *=* ]] || fail 'Use NAME=value lines in .env.docker.'
    name=${line%%=*}; value=${line#*=}
    case "$name" in OMI_DOCKER_DEVICE|OMI_DOCKER_GPU_ID|OMI_DOCKER_TRANSPORT|STT_MODEL|STT_REVISION|STT_THREADS) ;;
      *) fail 'Unknown field in .env.docker. See docs/DOCKER.md.' ;;
    esac
    [[ "$seen" != *" $name "* ]] || fail 'Duplicate field in .env.docker.'
    [[ "$value" =~ ^[a-zA-Z0-9_./-]+$ ]] || fail 'Use unquoted literal values in .env.docker.'
    seen+="$name "
    printf -v "$name" '%s' "$value"
  done < "$config_file"
fi
follow=false
inference=false
while [[ $# -gt 0 ]]; do
  option=$1; shift
  case "$command:$option" in
    logs:-f) follow=true ;;
    doctor:--inference) inference=true ;;
    bootstrap:--device|bootstrap:--gpu|bootstrap:--transport|bootstrap:--model|bootstrap:--revision|bootstrap:--threads)
      [[ $# -gt 0 && -n "$1" ]] || fail "Missing value for $option."
      case "$option" in
        --device) OMI_DOCKER_DEVICE=$1 ;;
        --gpu) OMI_DOCKER_GPU_ID=$1 ;;
        --transport) OMI_DOCKER_TRANSPORT=$1 ;;
        --model) STT_MODEL=$1 ;;
        --revision) STT_REVISION=$1 ;;
        --threads) STT_THREADS=$1 ;;
      esac
      shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
case "$OMI_DOCKER_DEVICE" in cpu|cuda) ;; *) fail 'Device must be cpu or cuda; choose it with bootstrap --device.' ;; esac
case "$OMI_DOCKER_TRANSPORT" in local|ngrok|tailscale) ;; *) fail 'Transport must be local, tailscale or ngrok.' ;; esac
[[ "$OMI_DOCKER_GPU_ID" =~ ^([0-9]+|GPU-[a-fA-F0-9-]+)$ ]] || fail 'GPU must be an index or NVIDIA GPU UUID.'
[[ "$STT_MODEL" =~ ^[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+$ ]] || fail 'Model must be a Hugging Face owner/model ID.'
[[ "$STT_REVISION" =~ ^[a-zA-Z0-9_./-]+$ ]] || fail 'Invalid model revision.'
[[ "$STT_THREADS" =~ ^[1-9][0-9]*$ ]] || fail 'Threads must be a positive integer.'
export OMI_DOCKER_DEVICE OMI_DOCKER_GPU_ID OMI_DOCKER_TRANSPORT STT_MODEL STT_REVISION STT_THREADS
transport=$OMI_DOCKER_TRANSPORT
files=(-f compose.yaml)
# These files never import the native .env. The validated values above win.
compose() { docker compose --env-file /dev/null "${files[@]}" "$@"; }
tailscale_address() {
  local cli="${OMI_TAILSCALE_CLI:-}" status own selected a b c d
  if [[ -z "$cli" ]]; then
    cli=$(command -v tailscale || true)
    if [[ -z "$cli" && -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
      cli=/Applications/Tailscale.app/Contents/MacOS/Tailscale
    fi
  fi
  [[ -n "$cli" ]] || { echo 'Install and connect Tailscale on the Docker host first.' >&2; return 1; }
  status=$("$cli" status --json --peers=false 2>/dev/null) || {
    echo 'Cannot inspect Tailscale on the Docker host.' >&2; return 1;
  }
  # The CLI emits indented JSON. Inspect top-level state and Self, never peers.
  if ! awk '
    /^  "BackendState": "Running",?$/ { running=1 }
    /^  "Self": \{$/ { self=1; next }
    /^  },?$/ { self=0 }
    self && /^    "Online": true,?$/ { online=1 }
    END { exit !(running && online) }
  ' <<< "$status"; then
    echo 'Connect Tailscale on the Docker host before starting phone access.' >&2; return 1
  fi
  own=$("$cli" ip -4 2>/dev/null) || { echo 'Tailscale IPv4 is unavailable.' >&2; return 1; }
  selected="${OMI_TAILSCALE_IP:-$own}"
  if [[ ! "$selected" =~ ^100\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    echo 'OMI_TAILSCALE_IP must be the host Tailscale IPv4 in 100.64.0.0/10.' >&2; return 1
  fi
  IFS=. read -r a b c d <<< "$selected"
  if (( 10#$b < 64 || 10#$b > 127 || 10#$c > 255 || 10#$d > 255 )) || [[ "$selected" != "$own" ]]; then
    echo 'OMI_TAILSCALE_IP must match this Docker host, not the phone or another device.' >&2; return 1
  fi
  export OMI_TAILSCALE_IP="$selected"
}

require_local_docker() {
  local endpoint
  if [[ -n "${DOCKER_CONTEXT:-}" ]]; then
    endpoint=$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')
  elif [[ -n "${DOCKER_HOST:-}" ]]; then
    endpoint="$DOCKER_HOST"
  else
    endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}')
  fi
  case "$endpoint" in
    unix://*|npipe://*) ;;
    *) echo 'Run Docker commands on the Docker host with its local context; use SSH for a remote server.' >&2; return 1 ;;
  esac
}


if [[ "$command" == open ]]; then
  address=http://127.0.0.1:21001/
  echo "Library: $address"
  if [[ -t 1 && -z "${SSH_CONNECTION:-}${SSH_TTY:-}" ]]; then
    if [[ "$(uname -s)" == Darwin ]]; then open "$address"
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null; then xdg-open "$address"; fi
  fi
  exit 0
fi
command -v docker >/dev/null || fail 'Install Docker Engine/Desktop and Docker Compose first. See docs/DOCKER.md.'
version=$(docker compose version --short) || fail 'Docker Compose is unavailable.'
version=${version#v}
[[ "$version" =~ ^([0-9]+)\.([0-9]+) ]] || fail 'Cannot identify the Docker Compose version.'
(( BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 32) )) || fail 'Docker Compose 2.32 or later is required.'
# Recovery commands use the stable project identity, without GPU/VPN discovery.
case "$command" in
  down) exec docker compose --env-file /dev/null -f compose.yaml --profile tunnel down ;;
  status) compose --profile tunnel ps --all; exit ;;
  logs)
    if "$follow"; then compose --profile tunnel logs --tail 80 -f
    else compose --profile tunnel logs --tail 80; fi
    exit ;;
esac
require_local_docker
if [[ "$command" != bootstrap && ! -f "$config_file" ]]; then
  fail 'Docker is not prepared. Run: ./omiloc --runtime docker bootstrap'
fi
case "$command" in bootstrap|up|dev|doctor|pair|config)
  if [[ "$transport" == tailscale ]]; then
    tailscale_address
    files+=(-f compose.tailscale.yaml)
  fi ;;
esac
[[ "$OMI_DOCKER_DEVICE" != cuda ]] || files+=(-f compose.gpu.yaml)
case "$command" in dev) files+=(-f compose.dev.yaml) ;; esac
# CPU/GPU and transport choices stay fixed until an explicit bootstrap.
case "$command" in
  bootstrap)
    [[ ! -L .local ]] || fail 'Unsafe .local directory.'
    mkdir -p .local
    mkdir .local/docker-bootstrap.lock 2>/dev/null || fail 'Docker bootstrap is already running. See docs/DOCKER.md for interrupted-lock recovery.'
    pending=''
    trap '[[ -z "$pending" ]] || rm -f "$pending"; rmdir .local/docker-bootstrap.lock' EXIT
    # Changing a live image/model can interrupt recording. Prepare only when stopped.
    [[ -z "$(compose --profile tunnel ps --status running -q)" ]] || fail 'Stop this Docker stack with omiloc --runtime docker down before bootstrap.'
    : > .local/docker-bootstrap.pending
    docker info >/dev/null || fail 'Docker daemon is unavailable.'
    compose config --quiet
    echo "Preparing Docker runtime ($OMI_DOCKER_DEVICE)."
    compose build app stt download
    if [[ "$OMI_DOCKER_DEVICE" == cuda ]]; then
      # Exercise this image's CUDA driver path on the selected device, not just nvidia-smi on the host.
      compose run --rm --no-deps -T stt python3 -c 'import ctranslate2; assert ctranslate2.get_cuda_device_count() == 1, "Selected CUDA device is unavailable"'
    fi
    downloaded=$(compose run --rm --no-deps -T download)
    revision=$(printf '%s\n' "$downloaded" | sed -n 's/^Model revision: //p')
    [[ "$revision" =~ ^[a-f0-9]{40,64}$ ]] || fail 'Model download did not return an immutable revision; bootstrap is incomplete.'
    STT_REVISION=$revision
    pending=$(mktemp .env.docker.XXXXXX)
    {
      echo '# Docker host settings. Literal values only; no native credentials.'
      for name in OMI_DOCKER_DEVICE OMI_DOCKER_GPU_ID OMI_DOCKER_TRANSPORT STT_MODEL STT_REVISION STT_THREADS; do
        printf '%s=%s\n' "$name" "${!name}"
      done
    } > "$pending"
    chmod 600 "$pending"
    mv -f "$pending" "$config_file"; pending=''
    rm -f .local/docker-bootstrap.pending
    echo 'Docker bootstrap complete. Services are stopped. Run: ./omiloc --runtime docker up'
    ;;
  up|dev)
    [[ ! -e .local/docker-bootstrap.pending ]] || fail 'Previous bootstrap was interrupted. Run bootstrap again.'
    compose config --quiet
    echo "Starting Docker runtime ($OMI_DOCKER_DEVICE)."
    if [[ "$transport" == tailscale ]]; then compose --profile tunnel stop tunnel; fi
    if [[ "$command" == dev ]]; then
      compose up --no-build --pull never --watch
    else
      compose up -d --no-build --pull never --wait --wait-timeout 600 || fail 'Startup failed. Run doctor and logs; run bootstrap if images or models are missing.'
      compose exec -T app python docker/runtime.py health
      echo 'Docker services ready. Library: http://127.0.0.1:21001/'
    fi ;;
  doctor)
    docker info >/dev/null
    compose config --quiet
    echo 'Docker prerequisites and configuration passed.'
    if "$inference"; then
      compose exec -T app python docker/runtime.py health
      compose exec -T app python docker/runtime.py inference "$OMI_DOCKER_DEVICE"
    else
      compose --profile tunnel ps --all
      echo 'Run doctor --inference after up to verify synthetic transcription.'
    fi ;;
  pair)
    [[ -t 0 && -t 1 ]] || fail 'Run pair in your own interactive terminal; keys must not enter logs.'
    address=http://127.0.0.1:21000
    [[ "$transport" != ngrok ]] || address=''
    [[ "$transport" != tailscale ]] || address="http://$OMI_TAILSCALE_IP:21000"
    compose exec -e "OMI_PAIR_ADDRESS=$address" app python docker/runtime.py pair ;;
  config) compose config ;;
  configure) compose exec app python docker/runtime.py configure ;;
  tunnel)
    [[ "$transport" == ngrok ]] || fail 'Select ngrok with bootstrap --transport ngrok before starting its tunnel.'
    compose --profile tunnel up -d --no-build --pull never tunnel ;;
esac
