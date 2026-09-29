#!/usr/bin/env bash
# Add Bot Purge to a server that already hosts other sites behind Caddy. Other sites are left untouched.
# Run as root:  curl -fsSL https://raw.githubusercontent.com/davidsbianchi1984/botpurge/main/deploy/add-to-existing-server.sh | bash
set -euo pipefail
PORT=8765
# The address the site answers on. Default is the real domain; before it's bought, a free stand-in for the
# server's own IP works and still gets a real HTTPS certificate:
#   curl -fsSL .../add-to-existing-server.sh | SITE_HOST=74-208-19-30.sslip.io bash
SITE_HOST="${SITE_HOST:-www.botpurge.online}"
if ss -ltn | grep -q ":$PORT "; then echo "Port $PORT is already in use on this server; edit deploy/docker-compose.app-only.yml to pick another." >&2; exit 1; fi
command -v git >/dev/null || { apt-get update -y && apt-get install -y git; }
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

mkdir -p /opt && cd /opt
if [ -d botpurge ]; then git -C botpurge pull --ff-only; else git clone https://github.com/davidsbianchi1984/botpurge.git; fi
cd /opt/botpurge/deploy
if [ ! -f .env ]; then
  cp env.example .env && chmod 600 .env
  echo "COMPOSE_FILE=docker-compose.app-only.yml" >> .env      # this server uses its own Caddy
  sed -i "s#^BOTPURGE_PUBLIC_URL=.*#BOTPURGE_PUBLIC_URL=https://$SITE_HOST#" .env
fi
docker compose up -d --build

# Tell the existing Caddy about the site (only if it isn't there yet).
CADDYFILE=/etc/caddy/Caddyfile
BLOCK="
# --- Bot Purge ---
$SITE_HOST {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
"
if [ "$SITE_HOST" = "www.botpurge.online" ]; then BLOCK="${BLOCK}botpurge.online {
	redir https://www.botpurge.online{uri} permanent
}
"; fi
if systemctl is-active --quiet caddy && [ -f "$CADDYFILE" ]; then
  if ! grep -q "$SITE_HOST" "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.before-botpurge"
    printf '%s' "$BLOCK" >> "$CADDYFILE"
    if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
      systemctl reload caddy && echo "Caddy now serves https://$SITE_HOST"
    else
      cp "$CADDYFILE.before-botpurge" "$CADDYFILE"; echo "Caddy didn't accept the change, so it was undone. Your sites are unaffected." >&2; exit 1
    fi
  else
    echo "Caddy already has $SITE_HOST."
  fi
elif docker ps --format '{{.Image}}' | grep -q caddy; then
  SITE_HOST="$SITE_HOST" bash /opt/botpurge/deploy/attach-to-docker-caddy.sh      # Caddy runs in Docker here
else
  echo "Caddy isn't running here. Add this to your web server's config instead:"; echo "$BLOCK"
fi
echo
echo "Bot Purge is running at https://$SITE_HOST. Fill in /opt/botpurge/deploy/.env (nano /opt/botpurge/deploy/.env), then: cd /opt/botpurge/deploy && docker compose up -d"
echo "Bought the domain later? Point its A records at this server and run this script again with SITE_HOST=www.botpurge.online"
