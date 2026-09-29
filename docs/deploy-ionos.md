# Putting Bot Purge online at www.botpurge.online (IONOS)

The website, phone app, store (Stripe) and Purge Console all run from one small server.
IONOS **Web Hosting** plans only run PHP, so Bot Purge needs an **IONOS VPS** (VPS Linux XS or S is plenty).

## Already have a server with other sites on it? (Recommended for 74.208.19.30)
Bot Purge can share it without touching your other sites. Its app listens only on the server itself, and your existing Caddy
forwards www.botpurge.online to it:
```bash
curl -fsSL https://raw.githubusercontent.com/davidsbianchi1984/botpurge/main/deploy/add-to-existing-server.sh | bash
```
The script backs up your Caddyfile before adding the Bot Purge site, and undoes the change if Caddy rejects it. Then do steps 2, 4, 5 and 6 below.
Automatic updates (step 5) work the same way.

## 1. Get the server (IONOS)
1. In IONOS: **Servers & Cloud → VPS → Linux**. Pick **Ubuntu 24.04**.
2. Note the server's **IP address** and **root password** (IONOS shows them after setup).

## No domain yet?

Run the install with a stand-in name for the server's own IP; it gets a real HTTPS certificate too:

```
curl -fsSL https://raw.githubusercontent.com/davidsbianchi1984/botpurge/main/deploy/add-to-existing-server.sh | SITE_HOST=74-208-19-30.sslip.io bash
```

When the domain is bought, do step 2 below and run the script again without `SITE_HOST`; then change
`BOTPURGE_PUBLIC_URL` in `.env` to `https://www.botpurge.online` and `docker compose up -d`.

If the site answers **502** after a `docker compose up -d`, the rebuilt container fell off Caddy's
network. `bash /opt/botpurge/deploy/attach-to-docker-caddy.sh` puts it back and, from then on, keeps it
there across restarts (it writes `docker-compose.caddy-net.yml` and adds it to `COMPOSE_FILE` in `.env`).

## 2. Point the domain at it (IONOS → Domains & SSL → botpurge.online → DNS)
| Type | Host name | Points to |
|---|---|---|
| A | `@` | your VPS IP |
| A | `www` | your VPS IP |

Leave the MX (email) records alone, so david@botpurge.online keeps working. DNS changes take a few minutes to an hour.

## 3. Install Bot Purge on the server (one time)
Log in (IONOS has a browser console under the VPS, or `ssh root@YOUR-IP`), then run:
```bash
curl -fsSL https://raw.githubusercontent.com/davidsbianchi1984/botpurge/main/deploy/setup-vps.sh | bash
nano /opt/botpurge/deploy/.env        # fill in the settings (see below), save with Ctrl+O, exit with Ctrl+X
cd /opt/botpurge/deploy && docker compose up -d --build
```
The site is live at **https://www.botpurge.online** a minute later. The HTTPS certificate is issued and renewed automatically.
(The repository must be public for the one-line install, or clone it with a GitHub access token.)

### Settings in `.env`
- `BOTPURGE_PUBLIC_URL=https://www.botpurge.online` (already filled in)
- `BOTPURGE_BETA=1` keeps every plan free; set `0` when the beta ends.
- Stripe: `STRIPE_SECRET_KEY` (Dashboard → Developers → API keys) and `STRIPE_WEBHOOK_SECRET` (from step 4).
- License keys: run `docker compose exec app python -m botpurge.billing keygen`. Put the private half in `BOTPURGE_LICENSE_PRIVATE`
  and set the public half as the GitHub repository variable `LICENSE_PUBLIC_KEY`, so desktop builds can check keys.
- Email: your IONOS mailbox password in `BOTPURGE_SMTP_URL` (for "Email me my key").

After changing `.env`: `cd /opt/botpurge/deploy && docker compose up -d`.

## 4. Stripe webhook
Stripe Dashboard → Developers → Webhooks → **Add endpoint**:
- URL: `https://www.botpurge.online/api/billing/webhook`
- Events: `checkout.session.completed`, `invoice.paid`, `customer.subscription.deleted`, `charge.refunded`

Copy its **signing secret** (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`.

## 5. Automatic updates from GitHub (optional)
Every merge to `main` can redeploy by itself. On the server, make a deploy key:
```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/deploy && cat ~/.ssh/deploy.pub >> ~/.ssh/authorized_keys && cat ~/.ssh/deploy
```
In GitHub → the repository → Settings → Secrets and variables → Actions, add:
- `VPS_HOST`: the server IP
- `VPS_SSH_KEY`: the private key that was just printed

## 6. Sign-in return addresses (only for the features you turn on)
- X: `https://www.botpurge.online/api/x/callback`
- Gmail and Outlook: `https://www.botpurge.online/api/mail/callback`

## Backups
The database lives in the `botpurge-data` Docker volume. To copy it off the server:
`docker compose cp app:/data/botpurge.sqlite3 ./botpurge-backup.sqlite3`
