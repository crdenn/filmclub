#!/bin/bash
# Build the FilmClub bundle locally and deploy it through the existing
# Mac HTTP server -> Unraid wget -> deploy.sh workflow.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNRAID_HOST="${UNRAID_HOST:-your-unraid-host.local}"
UNRAID_USER="${UNRAID_USER:-root}"
UNRAID_SSH_PORT="${UNRAID_SSH_PORT:-22}"
REMOTE_DIR="${FILMCLUB_REMOTE_DIR:-/mnt/user/appdata/filmclub}"
HTTP_PORT="${FILMCLUB_HTTP_PORT:-8888}"
MAC_IP="${FILMCLUB_MAC_IP:-}"
SSH_TARGET="${UNRAID_USER}@${UNRAID_HOST}"
BUNDLE="filmclub-deploy.tar.gz"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

install_key() {
  local public_key="${FILMCLUB_SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"
  if [[ ! -f "$public_key" ]]; then
    echo "No SSH public key found at $public_key"
    echo "Create one with: ssh-keygen -t ed25519"
    exit 1
  fi
  echo ">> Installing the Mac SSH key on $SSH_TARGET (one password prompt)..."
  ssh -p "$UNRAID_SSH_PORT" -o StrictHostKeyChecking=accept-new \
    -o PubkeyAuthentication=no "$SSH_TARGET" \
    'umask 077; mkdir -p /root/.ssh; touch /root/.ssh/authorized_keys; IFS= read -r key; grep -qxF "$key" /root/.ssh/authorized_keys || printf "%s\n" "$key" >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys' \
    < "$public_key"
  echo ">> Key installed. Future deploys should not need a password."
}

MODE="deploy"
if [[ "${1:-}" == "--install-key" ]]; then
  install_key
  exit 0
fi
if [[ "${1:-}" == "--check" ]]; then
  MODE="check"
  shift
fi
if [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--check|--install-key]"
  exit 2
fi

cd "$PROJECT_DIR"

for command in python3 ssh tar curl shasum; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command"; exit 1; }
done
[[ -f .env ]] || { echo "Missing $PROJECT_DIR/.env"; exit 1; }

if ! ssh -p "$UNRAID_SSH_PORT" -o StrictHostKeyChecking=accept-new \
  -o BatchMode=yes -o ConnectTimeout=5 "$SSH_TARGET" true; then
  echo
  echo "SSH key access is not ready. Enable SSH on Unraid, then run:"
  echo "  ./deploy-from-mac.sh --install-key"
  exit 1
fi

if [[ -z "$MAC_IP" ]]; then
  MAC_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [[ -n "$MAC_IP" ]] || MAC_IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
fi
[[ -n "$MAC_IP" ]] || {
  echo "Could not detect the Mac LAN IP. Set FILMCLUB_MAC_IP and retry."
  exit 1
}

if [[ "$MODE" == "check" ]]; then
  echo "Ready: SSH key access to $SSH_TARGET:$UNRAID_SSH_PORT works."
  echo "Ready: Mac LAN address is $MAC_IP and the local deployment inputs exist."
  exit 0
fi

echo ">> Running focused checks..."
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js

echo ">> Building $BUNDLE..."
tar --disable-copyfile --exclude='**pycache**' -czf "$BUNDLE" \
  app static Dockerfile requirements.txt .env deploy.sh

local_hash="$(shasum -a 256 "$BUNDLE" | awk '{print $1}')"
served_hash=""
if fetched_hash="$(curl -fsS --max-time 3 \
    "http://127.0.0.1:$HTTP_PORT/$BUNDLE" 2>/dev/null \
    | shasum -a 256 | awk '{print $1}')"; then
  served_hash="$fetched_hash"
fi

if [[ "$served_hash" == "$local_hash" ]]; then
  echo ">> Reusing the existing HTTP server on port $HTTP_PORT."
elif [[ -n "$served_hash" ]]; then
  echo "Port $HTTP_PORT is serving a different file. Stop that server or set FILMCLUB_HTTP_PORT."
  exit 1
else
  echo ">> Starting a temporary HTTP server on port $HTTP_PORT..."
  python3 -m http.server "$HTTP_PORT" --bind 0.0.0.0 \
    >"${TMPDIR:-/tmp}/filmclub-deploy-http.log" 2>&1 &
  SERVER_PID=$!
  for _ in {1..20}; do
    if curl -fsS --max-time 1 "http://127.0.0.1:$HTTP_PORT/$BUNDLE" >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done
  kill -0 "$SERVER_PID" 2>/dev/null || {
    echo "Temporary HTTP server failed to start."
    exit 1
  }
fi

BUNDLE_URL="http://$MAC_IP:$HTTP_PORT/$BUNDLE"
echo ">> Deploying to $SSH_TARGET..."
ssh -p "$UNRAID_SSH_PORT" -o StrictHostKeyChecking=accept-new \
  "$SSH_TARGET" bash -s -- "$REMOTE_DIR" "$BUNDLE_URL" <<'REMOTE'
set -Eeuo pipefail
remote_dir="$1"
bundle_url="$2"
mkdir -p "$remote_dir"
cd "$remote_dir"
wget -O filmclub-deploy.tar.gz "$bundle_url"
tar xzf filmclub-deploy.tar.gz
bash deploy.sh

for _ in $(seq 1 30); do
  if wget -qO- http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo ">> Health check passed."
    exit 0
  fi
  sleep 1
done

echo ">> Container did not become healthy; recent logs:"
docker logs --tail 50 filmclub
exit 1
REMOTE

echo ">> Deployment complete: https://your-filmclub.example.com"
