#!/usr/bin/env bash
# Add Bot Purge to a server that already hosts other sites behind Caddy. Other sites are left untouched.
# Run as root:  curl -fsSL https://raw.githubusercontent.com/davidsbianchi1984/botpurge/main/deploy/add-to-existing-server.sh | bash
set -euo pipefail
PORT=8765
if ss -ltn | grep -q ":$PORT "; then echo "Port $PORT is already in use on this server; edit deploy/docker-compose.app-only.yml to pick another." >&2; exit 1; fi
command -v git >/dev/null || { apt-get update -y && apt-get install -y git; }
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

mkdir -p /opt && cd /opt
if [ -d botpurge ]; then git -C botpurge pull --ff-only; else git clone https://github.com/davidsbianchi1984/botpurge.git; fi
cd /opt/botpurge/deploy
if [ ! -f .env ]; then
  cp env.example .env && chmod 600 .env
  echo "COMPOSE_FILE=docker-compose.app-only.yml" >> .env      # this server uses its own Caddy
fi
docker compose up -d --build

# Tell the existing Caddy about www.botpurge.online (only if it isn't there yet).
CADDYFILE=/etc/caddy/Caddyfile
BLOCK="
# --- Bot Purge ---
www.botpurge.online {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
botpurge.online {
	redir https://www.botpurge.online{uri} permanent
}
"
if systemctl is-active --quiet caddy && [ -f "$CADDYFILE" ]; then
  if ! grep -q "www.botpurge.online" "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.before-botpurge"
    printf '%s' "$BLOCK" >> "$CADDYFILE"
    if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
      systemctl reload caddy && echo "Caddy now serves www.botpurge.online."
    else
      cp "$CADDYFILE.before-botpurge" "$CADDYFILE"; echo "Caddy didn't accept the change, so it was undone. Your sites are unaffected." >&2; exit 1
    fi
  else
    echo "Caddy already has www.botpurge.online."
  fi
else
  echo "Caddy isn't running as a system service here. Add this to your web server's config instead:"; echo "$BLOCK"
fi
echo
echo "Bot Purge is running. Fill in /opt/botpurge/deploy/.env (nano /opt/botpurge/deploy/.env), then: cd /opt/botpurge/deploy && docker compose up -d"
