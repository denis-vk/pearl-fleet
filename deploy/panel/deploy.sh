#!/usr/bin/env bash
# One-shot panel deploy on a fresh Ubuntu 22.04/24.04 Google Cloud VM.
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/panel/deploy.sh | bash
# or, from a cloned repo:   sudo bash deploy/panel/deploy.sh
#
# Installs Docker (if absent), writes .env (asks for domain + password on first
# run), builds and starts panel + Caddy (auto-HTTPS). Idempotent: rerun to
# update.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $EUID -ne 0 ]]; then echo "run as root (sudo)"; exit 1; fi

if ! command -v docker >/dev/null 2>&1; then
  echo "== installing docker"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

if [[ ! -f .env ]]; then
  read -rp "panel domain (A-record must point here): " DOMAIN
  read -rsp "admin password: " PASS; echo
  printf 'PANEL_DOMAIN=%s\nPANEL_ADMIN_PASSWORD=%s\n' "$DOMAIN" "$PASS" > .env
  chmod 600 .env
fi

echo "== building and starting"
docker compose up -d --build

IP=$(curl -s -4 ifconfig.me || true)
echo
echo "panel:  https://$(grep ^PANEL_DOMAIN= .env | cut -d= -f2)/   (VM ip: ${IP})"
echo "logs:   docker compose logs -f panel"
echo "update: git pull && sudo bash deploy/panel/deploy.sh"
