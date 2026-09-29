#!/usr/bin/env bash
# One-time setup on a fresh IONOS VPS (Ubuntu 22.04/24.04). Run as root:
#   curl -fsSL https://raw.githubusercontent.com/davidsbianchi1984/botpurge/main/deploy/setup-vps.sh | bash
set -euo pipefail
apt-get update -y
apt-get install -y ca-certificates curl git ufw
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
mkdir -p /opt && cd /opt
[ -d botpurge ] || git clone https://github.com/davidsbianchi1984/botpurge.git
cd botpurge/deploy
[ -f .env ] || { cp env.example .env; chmod 600 .env; }
echo
echo "Next: edit /opt/botpurge/deploy/.env (nano /opt/botpurge/deploy/.env), then run:"
echo "  cd /opt/botpurge/deploy && docker compose up -d --build"
