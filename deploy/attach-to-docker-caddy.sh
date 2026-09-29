#!/usr/bin/env bash
# When the server's Caddy runs in Docker (not as a system service), add the Bot Purge site to it:
# put the app container on Caddy's network, append a site block to the Caddyfile Caddy is using,
# and reload Caddy. Safe to run again; each step skips what is already done.
#   SITE_HOST=74-208-19-30.sslip.io bash attach-to-docker-caddy.sh      (stand-in name before the domain)
#   bash attach-to-docker-caddy.sh                                       (www.botpurge.online)
set -euo pipefail
SITE_HOST="${SITE_HOST:-www.botpurge.online}"

CADDY=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /caddy/ {print $1; exit}')
[ -n "$CADDY" ] || { echo "No Caddy container is running on this server." >&2; exit 1; }
APP=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /deploy-app|botpurge/ {print $1; exit}')
[ -n "$APP" ] || { echo "The Bot Purge container isn't running. Run the install script first." >&2; exit 1; }
NET=$(docker inspect "$CADDY" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)

# 1. Same Docker network, so Caddy can reach the app by its container name.
if ! docker inspect "$APP" --format '{{json .NetworkSettings.Networks}}' | grep -q "\"$NET\""; then
  docker network connect "$NET" "$APP"; echo "Joined $APP to Caddy's network ($NET)."
fi

# 2. The site block, appended to the Caddyfile Caddy is using (on the host, so it survives restarts).
BLOCK="
# --- Bot Purge ---
$SITE_HOST {
	encode gzip
	reverse_proxy $APP:8000
}
"
if [ "$SITE_HOST" = "www.botpurge.online" ]; then BLOCK="${BLOCK}botpurge.online {
	redir https://www.botpurge.online{uri} permanent
}
"; fi
SRC=$(docker inspect "$CADDY" --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')
if [ -n "$SRC" ] && [ -f "$SRC" ]; then
  if grep -q "^$SITE_HOST {" "$SRC"; then echo "Caddy already has $SITE_HOST."; else
    cp "$SRC" "$SRC.before-botpurge"; printf '%s' "$BLOCK" >> "$SRC"; echo "Added $SITE_HOST to $SRC"; fi
else
  echo "Couldn't find the Caddyfile on the host; adding the site inside the container only (re-run after a Caddy restart)." >&2
fi
docker exec "$CADDY" sh -c "grep -q '^$SITE_HOST {' /etc/caddy/Caddyfile 2>/dev/null || printf '%s' \"\$1\" >> /etc/caddy/Caddyfile" sh "$BLOCK" 2>/dev/null || true

# 3. Reload; if Caddy rejects the config, put the file back.
if docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
  echo "Caddy now serves https://$SITE_HOST (the certificate is issued on the first visit, give it a minute)."
else
  [ -n "${SRC:-}" ] && [ -f "$SRC.before-botpurge" ] && cp "$SRC.before-botpurge" "$SRC"
  echo "Caddy didn't accept the change, so it was undone. Your other sites are unaffected." >&2; exit 1
fi
