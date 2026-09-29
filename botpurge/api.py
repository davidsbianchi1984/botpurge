"""HTTP API and web app for both modules.

    /                 Personal Cleaner (Module A) web app
    /console          Platform Purge Console (Module B)
    /appeal/{token}   Public appeals portal for affected users

Run: ``python -m botpurge`` (or ``uvicorn --factory botpurge.api:create_app``).
"""

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import security as sec
from .connectors import mail as mailapi
from .connectors import x as xapi
from .db import DB
from .importers import EXPORT_HELP, ImportError_, detect_platform, import_export
from .instructions_store import InstructionStore
from .models import Platform
from .agent.runner import AgentService
from .apps import import_apps, import_app_names
from . import __version__
from .billing import Billing, BillingError
from .release import check_for_update, store_url
from .inbox import AppsService, InboxService
from .liveguard import ChatMessage, LiveGuardService
from .messages import detect_message_source, import_messages, parse_paste
from .personal import NotFound, PersonalService
from .plans import PlanRequired, Plans, catalog
from .purge.console import PurgeService, Rule
from .purge.signals import SiteAccount
from .removal import RemovalService, modes_for

WEB = Path(__file__).parent / "web"


class Services:
    def __init__(self, db: DB, purge_enforcer=None, x_http=None):
        self.db = db
        self.personal = PersonalService(db)
        self.plans = Plans(db)
        self.inbox = InboxService(db)
        self.apps = AppsService(db)
        self.liveguard = LiveGuardService(db, secrets_get=self.get_secret, http=x_http)
        self.instructions = InstructionStore(db)
        self.x_http = x_http
        self.removal = RemovalService(db, self.personal, x_client_for=self.x_client_for)
        self.purge = PurgeService(db, enforcer=purge_enforcer, http=x_http)
        self.agent = AgentService(db, self.removal, self.personal)
        self.billing = Billing(db, http=x_http)

    def sync_mail(self, user_id: str, provider: str) -> dict:
        """Pull the last 30 days of a connected inbox into the email scanner."""
        from .messages import parse_email_bytes

        p = mailapi.PROVIDERS[provider]
        rt = self.get_secret(user_id, f"mail_{provider}_refresh")
        if not rt:
            raise ValueError(f"{p['name']} isn't connected")
        try:
            raws = mailapi.FETCHERS[provider](self.mail_token(user_id, provider), http=self.x_http)
        except mailapi.MailError as exc:
            self.db.x("UPDATE mail_links SET last_error=? WHERE user_id=? AND provider=?", (str(exc), user_id, provider))
            raise
        msgs = []
        for raw, folder in raws:
            m = parse_email_bytes(raw)
            if m:
                m.headers["x-provider"], m.headers["x-folder"] = provider, folder     # where it lives, for cleanup
                msgs.append(m)
        result = self.inbox.store(user_id, msgs) if msgs else {"messages": 0}
        self.db.x("UPDATE mail_links SET last_sync=?, last_error=NULL WHERE user_id=? AND provider=?", (sec.iso(), user_id, provider))
        return {"provider": provider, "fetched": len(raws), **result}

    def mail_token(self, user_id: str, provider: str) -> str:
        p = mailapi.PROVIDERS[provider]
        rt = self.get_secret(user_id, f"mail_{provider}_refresh")
        if not rt:
            raise ValueError(f"{p['name']} isn't connected")
        link = self.db.one("SELECT can_clean FROM mail_links WHERE user_id=? AND provider=?", (user_id, provider))
        tok = mailapi.refresh(provider, rt, os.environ.get(p["client_env"], ""), os.environ.get(p["secret_env"]), http=self.x_http,
                              cleanup=bool(link and link["can_clean"]))
        if tok.get("refresh_token"):
            self.save_secret(user_id, f"mail_{provider}_refresh", tok["refresh_token"])
        return tok["access_token"]

    def clean_mail(self, user_id: str, to: str = "trash", senders: Optional[list] = None) -> dict:
        """Move the emails of flagged senders (or the ones picked) to Trash or Spam, in every mailbox that allows it."""
        if to not in ("trash", "spam"):
            raise ValueError("to must be trash or spam")
        if senders is None:
            senders = [r["sender_id"] for r in self.db.q(
                "SELECT sender_id FROM msg_senders WHERE user_id=? AND platform='email' AND status='active'"
                " AND label IN ('likely_bot','suspicious')", (user_id,))]
        want = set(senders)
        links = {r["provider"]: r for r in self.db.q("SELECT * FROM mail_links WHERE user_id=? AND can_clean=1", (user_id,))}
        if not links:
            raise ValueError("Connect a mailbox with cleanup allowed first")
        ids: dict[str, list] = {}
        in_inbox, in_spam = set(), set()
        for r in self.db.q("SELECT sender_id, extra_json FROM msg_items WHERE user_id=? AND kind='email'", (user_id,)):
            h = (json.loads(r["extra_json"] or "{}") or {}).get("headers", {})
            if r["sender_id"] not in want or h.get("x-provider") not in links or not h.get("Message-ID"):
                continue
            if h.get("x-folder", "Inbox") == "Inbox":
                ids.setdefault(h["x-provider"], []).append(h["Message-ID"])
                in_inbox.add(r["sender_id"])
            else:
                in_spam.add(r["sender_id"])                       # already out of the way
        moved, errors = {}, {}
        for provider, mids in ids.items():
            try:
                moved[provider] = mailapi.CLEANERS[provider](self.mail_token(user_id, provider), mids, to, http=self.x_http)
            except mailapi.MailError as exc:
                errors[provider] = str(exc)
        if moved:
            with self.db.tx() as tx:
                for sid in want:
                    tx.execute("UPDATE msg_senders SET status='removed' WHERE user_id=? AND platform='email' AND sender_id=?", (user_id, sid))
        return {"moved": sum(moved.values()), "by_mailbox": moved, "senders": len(in_inbox) if moved else 0,
                "already_in_spam": len(in_spam - in_inbox), "to": to, "errors": errors}

    def refresh_licenses(self) -> int:
        """Once a day, fetch renewed Protect licenses from the store (desktop copies)."""
        store = "" if Billing.store_configured() else store_url()     # the store itself needs no refresh
        if not store:
            return 0
        last = self.db.one("SELECT at FROM billing_events WHERE id='refresh-marker'")
        if last and (sec.now() - sec.parse_iso(last["at"])).total_seconds() < 86400:
            return 0
        self.db.x("INSERT OR REPLACE INTO billing_events(id,type,at) VALUES ('refresh-marker','refresh',?)", (sec.iso(),))
        client = self.x_http or httpx.Client(timeout=20)

        def fetch(key: str) -> str:
            r = client.post(store + "/api/billing/refresh", json={"license": key})
            r.raise_for_status()
            return r.json()["license"]

        return self.plans.refresh_licenses(fetch)

    def x_client_for(self, user_id: str):
        tok = self.db.one("SELECT sealed FROM secrets WHERE user_id=? AND name='x_access_token'", (user_id,))
        me = self.db.one("SELECT sealed FROM secrets WHERE user_id=? AND name='x_user_id'", (user_id,))
        if not tok or not me:
            raise RuntimeError("X is not connected")
        return (xapi.XClient(sec.unseal(tok["sealed"], f"{user_id}:x_access_token"), http=self.x_http),
                sec.unseal(me["sealed"], f"{user_id}:x_user_id"))

    def get_secret(self, user_id: str, name: str) -> Optional[str]:
        r = self.db.one("SELECT sealed FROM secrets WHERE user_id=? AND name=?", (user_id, name))
        return sec.unseal(r["sealed"], f"{user_id}:{name}") if r else None

    def save_secret(self, user_id: str, name: str, value: str) -> None:
        self.db.x("INSERT OR REPLACE INTO secrets VALUES (?,?,?)", (user_id, name, sec.seal(value, f"{user_id}:{name}")))

    def sync_x(self, user_id: str) -> dict:
        client, me = self.x_client_for(user_id)
        conns = client.connections(me)
        from . import vision

        http = self.x_http or httpx.Client(timeout=15, follow_redirects=True)

        def fetch(url: str):
            if not url.startswith("https://pbs.twimg.com/"):
                return None  # only X's own image host
            try:
                r = http.get(url)
                return r.content if r.status_code == 200 and len(r.content) < 2_000_000 else None
            except httpx.HTTPError:
                return None

        vision.enrich(conns, fetch)
        rec = self.personal.store_connections(user_id, Platform.x, conns)
        return {"imported": len(conns), "reconciled": rec, **self.personal.scan(user_id, "x")}

    # ---- background work ---------------------------------------------------------------

    def tick(self) -> dict:
        """One pass of the worker: one-click removals, purge batches, retention, rescans."""
        done = {"one_click": 0, "batches": 0, "purged_raw": 0, "rescans": 0}
        for j in self.db.q("SELECT id, user_id FROM removal_jobs WHERE state='running' AND mode='one_click'"):
            try:
                self.removal.run_one_click(j["user_id"], j["id"], max_items=10)
                done["one_click"] += 1
            except Exception:
                pass
        for b in self.db.q("SELECT tenant_id, id, actor FROM t_batches WHERE state='running'"):
            self.purge.step(b["tenant_id"], b["id"], b["actor"])
            done["batches"] += 1
        done["purged_raw"] = self.personal.purge_raw() + self.inbox.purge_raw()
        from .protect import send_weekly_reports

        done["reports"] = send_weekly_reports(self.db, self.plans.features)
        done["licenses"] = self.refresh_licenses()
        day_ago = sec.iso(sec.now() - timedelta(days=1))
        for m in self.db.q("SELECT user_id, provider FROM mail_links WHERE last_sync IS NULL OR last_sync<?", (day_ago,)):
            if "email" in self.plans.features(m["user_id"]):      # daily inbox checks are part of Protect
                try:
                    self.sync_mail(m["user_id"], m["provider"])
                    done["mail"] = done.get("mail", 0) + 1
                except Exception:
                    pass
        for uid in self.personal.due_rescans():
            done["rescans"] += 1
            if self.db.one("SELECT 1 FROM secrets WHERE user_id=? AND name='x_access_token'", (uid,)):
                try:
                    self.sync_x(uid)
                except Exception:
                    pass
        return done


def create_app(db_path: Optional[str] = None, purge_enforcer=None, x_http=None, worker: Optional[bool] = None) -> FastAPI:
    svc = Services(DB(db_path), purge_enforcer=purge_enforcer, x_http=x_http)
    admin_token = os.environ.get("BOTPURGE_ADMIN_TOKEN")
    run_worker = worker if worker is not None else os.environ.get("BOTPURGE_WORKER", "1") == "1"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_worker:  # pragma: no cover - background thread
            def loop():
                while True:
                    try:
                        svc.tick()
                    except Exception:
                        pass
                    time.sleep(float(os.environ.get("BOTPURGE_TICK_SECONDS", "20")))

            threading.Thread(target=loop, daemon=True).start()
        yield

    app = FastAPI(title="Bot Purge", version=__version__, lifespan=lifespan)
    app.state.svc = svc

    # ---- errors ---------------------------------------------------------------------

    @app.exception_handler(NotFound)
    @app.exception_handler(LookupError)
    async def _nf(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc) or "not found"}, status_code=404)

    @app.exception_handler(PlanRequired)
    async def _plan(_: Request, exc: PlanRequired):
        return JSONResponse({"detail": str(exc), "plan": exc.plan, "feature": exc.feature}, status_code=402)

    @app.exception_handler(ValueError)
    async def _bad(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(mailapi.MailError)
    async def _mail(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(BillingError)
    async def _billing(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(PermissionError)
    async def _forbidden(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    # ---- auth -------------------------------------------------------------------------

    def user(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        uid = svc.personal.auth(token) if token else None
        if not uid:
            raise HTTPException(401, "Sign in first")
        return uid

    def admin(x_admin_token: str = Header(default="")) -> None:
        if not admin_token or not sec.token_matches(x_admin_token, sec.hash_token(admin_token)):
            raise HTTPException(401, "Admin token required")

    def tenant(x_api_key: str = Header(default="")) -> tuple[str, str, str]:
        found = svc.purge.auth(x_api_key) if x_api_key else None
        if not found:
            raise HTTPException(401, "API key required")
        return found

    def owner(t: tuple = Depends(tenant)) -> tuple[str, str, str]:
        if t[1] != "owner":
            raise HTTPException(403, "Owner key required")
        return t

    # ---- pages ----------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(WEB / "index.html")

    @app.get("/console", include_in_schema=False)
    def console_page():
        return FileResponse(WEB / "console.html")

    @app.get("/appeal/{token}", include_in_schema=False)
    def appeal_page(token: str):
        return FileResponse(WEB / "appeal.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        # Served from the root so it can cover the whole app.
        return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str):
        p = (WEB / name).resolve()
        if p.parent != WEB.resolve() or not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    @app.get("/api/app/version")
    def app_version():
        return check_for_update(svc.x_http)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # =========================== Module A ===========================

    class SignupIn(BaseModel):
        email: str
        guardian_of: Optional[str] = None
        teen_consent: bool = False

    @app.post("/api/signup")
    def signup(body: SignupIn):
        uid, token = svc.personal.register(body.email, body.guardian_of, body.teen_consent)
        return {"user_id": uid, "token": token}

    @app.get("/api/plans")
    def plans_catalog():
        own = Billing.store_configured()
        store = "" if own else store_url()                              # the store sells directly
        return {**catalog(), "store_url": store or ("" if not own else None), "can_buy": bool(store or own)}

    @app.get("/api/me/plan")
    def my_plan(uid: str = Depends(user)):
        return svc.plans.current(uid)

    class PlanIn(BaseModel):
        plan: str

    @app.post("/api/me/plan")
    def choose_plan(body: PlanIn, uid: str = Depends(user)):
        return svc.plans.activate(uid, body.plan)

    # ---- payments and licenses (see billing.py) --------------------------------------

    class LicenseIn(BaseModel):
        license: str = Field(max_length=4000)

    @app.post("/api/me/license")
    def enter_license(body: LicenseIn, uid: str = Depends(user)):
        return svc.plans.apply_license(uid, body.license)

    class CheckoutIn(BaseModel):
        plan: str
        email: Optional[str] = Field(default=None, max_length=320)

    @app.post("/api/billing/checkout")
    def checkout(body: CheckoutIn, authorization: str = Header(default="")):
        """This server is the store: a Stripe Checkout page (signed-in web users get the plan at once)."""
        token = authorization.removeprefix("Bearer ").strip()
        uid = svc.personal.auth(token) if token else None
        email = body.email
        if uid and not email:
            email = svc.db.one("SELECT email FROM users WHERE id=?", (uid,))["email"]
        return {"url": svc.billing.checkout(body.plan, email, uid)}

    @app.get("/buy/{plan}", include_in_schema=False)
    def buy(plan: str, email: Optional[str] = None):
        """What the desktop app's Buy buttons open in the browser."""
        return RedirectResponse(svc.billing.checkout(plan, email), status_code=303)

    @app.post("/api/billing/webhook", include_in_schema=False)
    async def stripe_webhook(request: Request, stripe_signature: str = Header(default="")):
        try:
            return svc.billing.handle_webhook(await request.body(), stripe_signature)
        except BillingError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/billing/license")
    def license_for_session(session_id: str = Query(max_length=300)):
        key = svc.billing.license_for_session(session_id)
        if not key:
            return JSONResponse({"detail": "Payment is still being confirmed"}, status_code=404)
        return {"license": key}

    @app.post("/api/billing/refresh")
    def refresh_license(body: LicenseIn):
        return {"license": svc.billing.refresh(body.license)}

    @app.post("/api/billing/portal")
    def billing_portal(body: LicenseIn):
        return {"url": svc.billing.portal(body.license)}

    def _store(path: str, payload: dict) -> Optional[dict]:
        """Desktop copies forward store requests server-side (the page can't call another origin)."""
        store = "" if Billing.store_configured() else store_url()       # never forward to ourselves
        if not store:
            return None
        r = (svc.x_http or httpx.Client(timeout=20)).post(store + path, json=payload)
        if r.status_code >= 400:
            raise BillingError((r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("detail")
                               or "The Bot Purge store couldn't be reached. Please try again")
        return r.json()

    @app.post("/api/me/billing/portal")
    def my_billing_portal(uid: str = Depends(user)):
        key = svc.db.one("SELECT license_key FROM users WHERE id=?", (uid,))["license_key"]
        if not key:
            raise ValueError("No subscription on this account")
        return _store("/api/billing/portal", {"license": key}) or {"url": svc.billing.portal(key)}

    class EmailIn(BaseModel):
        email: str = Field(max_length=320)

    @app.post("/api/billing/send-keys")
    def send_keys(body: EmailIn):
        if _store("/api/billing/send-keys", {"email": body.email}) is None:
            svc.billing.send_keys(body.email)
        return {"ok": True, "detail": "If that email bought Bot Purge, its license keys are on their way."}

    @app.get("/billing/success", include_in_schema=False)
    def billing_success():
        return FileResponse(WEB / "success.html")

    @app.get("/api/me/summary")
    def summary(uid: str = Depends(user)):
        return {**svc.personal.summary(uid), "plan": svc.plans.current(uid)}

    @app.get("/api/me/dashboard")
    def dashboard(uid: str = Depends(user)):
        d = svc.personal.dashboard(uid)
        d["x_connected"] = bool(svc.db.one("SELECT 1 FROM secrets WHERE user_id=? AND name='x_access_token'", (uid,)))
        return d

    @app.delete("/api/me")
    def delete_me(uid: str = Depends(user)):
        svc.personal.delete_everything(uid)
        return {"deleted": True}

    @app.get("/api/import/help")
    def import_help():
        return EXPORT_HELP

    @app.post("/api/import/auto")
    async def import_auto(file: UploadFile = File(...), uid: str = Depends(user)):
        """Upload the export exactly as downloaded (zip or file); we work out which network it's from."""
        data = await file.read()
        platform = detect_platform(file.filename or "", data)
        if not platform:
            raise HTTPException(400, "We couldn't tell which network this export is from. Pick the network and upload it again")
        return {"platform": platform.value, **_import(uid, platform, file.filename, data)}

    @app.post("/api/import/{platform}")
    async def import_file(platform: Platform, file: UploadFile = File(...), uid: str = Depends(user)):
        return _import(uid, platform, file.filename, await file.read())

    def _import(uid: str, platform: Platform, filename: Optional[str], data: bytes) -> dict:
        try:
            conns = import_export(platform, filename or "upload", data)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))
        finally:
            del data  # raw export is never persisted
        rec = svc.personal.store_connections(uid, platform, conns)
        result = svc.personal.scan(uid, platform.value)
        svc.inbox.scan(uid)  # newly flagged accounts strengthen the case against their messages
        return {"imported": len(conns), "reconciled": rec, **result}

    # X OAuth (PKCE)
    @app.get("/api/x/connect")
    def x_connect(request: Request, uid: str = Depends(user)):
        client_id = os.environ.get("X_CLIENT_ID")
        if not client_id:
            raise HTTPException(503, "X integration isn't configured on this server (set X_CLIENT_ID)")
        pkce = xapi.PKCE.new()
        svc.db.x("INSERT INTO oauth_pending VALUES (?,?,?,?)", (pkce.state, uid, pkce.verifier, sec.iso()))
        redirect = os.environ.get("X_REDIRECT_URI", str(request.url_for("x_callback")))
        return {"authorize_url": xapi.authorize_url(client_id, redirect, pkce)}

    @app.get("/api/x/callback", name="x_callback")
    def x_callback(request: Request, code: str, state: str):
        row = svc.db.one("SELECT * FROM oauth_pending WHERE state=?", (state,))
        if not row:
            raise HTTPException(400, "Unknown or expired sign-in attempt")
        svc.db.x("DELETE FROM oauth_pending WHERE state=?", (state,))
        redirect = os.environ.get("X_REDIRECT_URI", str(request.url_for("x_callback")))
        tok = xapi.XClient.exchange_code(os.environ["X_CLIENT_ID"], code, redirect, row["verifier"], http=svc.x_http)
        client = xapi.XClient(tok["access_token"], http=svc.x_http)
        me = client.me()
        svc.save_secret(row["user_id"], "x_access_token", tok["access_token"])
        if tok.get("refresh_token"):
            svc.save_secret(row["user_id"], "x_refresh_token", tok["refresh_token"])
        svc.save_secret(row["user_id"], "x_user_id", str(me["id"]))
        svc.sync_x(row["user_id"])
        return RedirectResponse("/?connected=x")

    @app.get("/api/mail")
    def mail_links(uid: str = Depends(user)):
        rows = {r["provider"]: dict(r) for r in svc.db.q("SELECT * FROM mail_links WHERE user_id=?", (uid,))}
        return [{"provider": k, "name": p["name"], "available": bool(os.environ.get(p["client_env"])),
                 "connected": k in rows, "last_sync": rows.get(k, {}).get("last_sync"), "last_error": rows.get(k, {}).get("last_error"),
                 "can_clean": bool(rows.get(k, {}).get("can_clean"))}
                for k, p in mailapi.PROVIDERS.items()]

    @app.post("/api/mail/{provider}/connect")
    def mail_connect(provider: str, request: Request, cleanup: bool = False, uid: str = Depends(user)):
        svc.plans.require(uid, "email")
        p = mailapi.PROVIDERS.get(provider)
        if not p:
            raise HTTPException(404, "unknown mail provider")
        client_id = os.environ.get(p["client_env"])
        if not client_id:
            raise HTTPException(503, f"{p['name']} isn't set up on this copy yet (set {p['client_env']})")
        pkce = xapi.PKCE.new()
        state = f"{provider}.{'clean' if cleanup else 'read'}.{pkce.state}"
        svc.db.x("INSERT INTO oauth_pending VALUES (?,?,?,?)", (state, uid, pkce.verifier, sec.iso()))
        redirect = os.environ.get("MAIL_REDIRECT_URI", str(request.url_for("mail_callback")))
        return {"authorize_url": mailapi.authorize_url(provider, client_id, redirect, state, pkce.challenge, cleanup=cleanup)}

    @app.get("/api/mail/callback", name="mail_callback")
    def mail_callback(request: Request, state: str, code: str = "", error: str = ""):
        row = svc.db.one("SELECT * FROM oauth_pending WHERE state=?", (state,))
        if not row:
            raise HTTPException(400, "Unknown or expired sign-in attempt")
        svc.db.x("DELETE FROM oauth_pending WHERE state=?", (state,))
        provider = state.split(".", 1)[0]
        if error or not code:
            return _mail_done(provider, False)
        p = mailapi.PROVIDERS[provider]
        redirect = os.environ.get("MAIL_REDIRECT_URI", str(request.url_for("mail_callback")))
        cleanup = state.split(".")[1] == "clean"
        tok = mailapi.exchange_code(provider, code, redirect, row["verifier"], os.environ[p["client_env"]],
                                    os.environ.get(p["secret_env"]), http=svc.x_http, cleanup=cleanup)
        if not tok.get("refresh_token"):
            return _mail_done(provider, False)
        svc.save_secret(row["user_id"], f"mail_{provider}_refresh", tok["refresh_token"])
        svc.db.x("INSERT OR REPLACE INTO mail_links(user_id,provider,connected_at,can_clean) VALUES (?,?,?,?)",
                 (row["user_id"], provider, sec.iso(), 1 if cleanup else 0))
        try:
            svc.sync_mail(row["user_id"], provider)
        except Exception:
            pass                                        # shown as last_error; the link itself is saved
        return _mail_done(provider, True)

    def _mail_done(provider: str, ok: bool) -> HTMLResponse:
        # Sign-in happens in the system browser (Google blocks sign-in inside app windows), so just say where to go next.
        name = mailapi.PROVIDERS[provider]["name"]
        msg = (f"{name} is connected. You can close this tab and go back to Bot Purge." if ok
               else f"{name} wasn't connected. Close this tab and try again from Bot Purge.")
        return HTMLResponse(f'<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">'
                            f'<link rel="stylesheet" href="/static/style.css"><main style="max-width:560px"><div class="panel">'
                            f'<h2>{"Connected ✅" if ok else "Not connected"}</h2><p>{msg}</p></div></main>')

    @app.post("/api/mail/{provider}/sync")
    def mail_sync(provider: str, uid: str = Depends(user)):
        svc.plans.require(uid, "email")
        if provider not in mailapi.PROVIDERS:
            raise HTTPException(404, "unknown mail provider")
        return svc.sync_mail(uid, provider)

    class MailCleanIn(BaseModel):
        to: str = "trash"                          # trash | spam; never deleted for good
        senders: Optional[list[str]] = None        # default: every flagged email sender

    @app.post("/api/mail/clean")
    def mail_clean(body: MailCleanIn, uid: str = Depends(user)):
        svc.plans.require(uid, "email")
        try:
            return svc.clean_mail(uid, body.to, body.senders)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/mail/{provider}")
    def mail_disconnect(provider: str, uid: str = Depends(user)):
        svc.db.x("DELETE FROM mail_links WHERE user_id=? AND provider=?", (uid, provider))
        svc.db.x("DELETE FROM secrets WHERE user_id=? AND name=?", (uid, f"mail_{provider}_refresh"))
        return {"disconnected": provider}

    @app.post("/api/x/sync")
    def x_sync(uid: str = Depends(user)):
        try:
            return svc.sync_x(uid)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/x")
    def x_disconnect(uid: str = Depends(user)):
        svc.db.x("DELETE FROM secrets WHERE user_id=? AND name LIKE 'x_%'", (uid,))
        return {"disconnected": True}

    class ScanIn(BaseModel):
        platform: Optional[Platform] = None

    @app.post("/api/scan")
    def scan(body: ScanIn, uid: str = Depends(user)):
        return svc.personal.scan(uid, body.platform.value if body.platform else None)

    @app.get("/api/flags")
    def flags(platform: Optional[Platform] = None, direction: Optional[str] = None, tab: str = "likely_bot",
              sort: str = "score", reason: Optional[str] = None, limit: int = Query(500, le=2000), offset: int = 0,
              uid: str = Depends(user)):
        return svc.personal.list_flags(uid, platform.value if platform else None, direction, tab, sort, reason, limit, offset)

    @app.get("/api/flags/{platform}/{direction}/{account_id}")
    def flag(platform: Platform, direction: str, account_id: str, uid: str = Depends(user)):
        return svc.personal.get_flag(uid, platform.value, account_id, direction)

    class FeedbackIn(BaseModel):
        platform: Platform
        account_id: str
        is_bot: bool

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn, uid: str = Depends(user)):
        return svc.personal.feedback(uid, body.platform.value, body.account_id, body.is_bot)

    class AccountRef(BaseModel):
        platform: Platform
        account_id: str
        direction: str = "follower"

    @app.post("/api/whitelist/remove")
    def unwhitelist(body: AccountRef, uid: str = Depends(user)):
        svc.personal.unwhitelist(uid, body.platform.value, body.account_id)
        return {"ok": True}

    class RemovalIn(BaseModel):
        platform: Platform
        accounts: list[dict]
        mode: Optional[str] = None

    @app.get("/api/removals/modes/{platform}")
    def removal_modes(platform: Platform):
        return {"modes": modes_for(platform.value)}

    @app.post("/api/removals")
    def create_removal(body: RemovalIn, uid: str = Depends(user)):
        svc.plans.require(uid, "remove")
        if body.mode == "agent":
            svc.plans.require(uid, "agent")
            if not svc.agent.has_consent(uid, body.platform.value):
                raise HTTPException(400, "Read and accept the done-for-you notice for this platform first")
        return svc.removal.create_job(uid, body.platform.value, body.accounts, body.mode)

    @app.get("/api/removals")
    def list_removals(uid: str = Depends(user)):
        return svc.removal.jobs(uid)

    @app.get("/api/removals/{job_id}")
    def get_removal(job_id: str, uid: str = Depends(user)):
        return svc.removal.job(uid, job_id)

    class StateIn(BaseModel):
        state: str

    @app.post("/api/removals/{job_id}/state")
    def removal_state(job_id: str, body: StateIn, uid: str = Depends(user)):
        return svc.removal.set_state(uid, job_id, body.state)

    @app.post("/api/removals/{job_id}/run")
    def removal_run(job_id: str, uid: str = Depends(user)):
        try:
            job = svc.removal.run_one_click(uid, job_id, max_items=10)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        if job["state"] == "done":
            try:
                job = svc.removal.recheck_x(uid, job_id)
            except Exception:
                pass
        return job

    @app.get("/api/removals/{job_id}/next")
    def removal_next(job_id: str, device: str = "web", uid: str = Depends(user)):
        return svc.removal.next_assisted(uid, job_id, device)

    class ConfirmIn(BaseModel):
        outcome: str

    @app.post("/api/removals/{job_id}/items/{idx}/confirm")
    def removal_confirm(job_id: str, idx: int, body: ConfirmIn, uid: str = Depends(user)):
        return svc.removal.confirm(uid, job_id, idx, body.outcome)

    @app.get("/api/removals/{job_id}/guided")
    def removal_guided(job_id: str, device: str = "web", uid: str = Depends(user)):
        return svc.removal.guided(uid, job_id, device)

    # Instructions (A6/A7)
    @app.get("/api/instructions")
    def instructions(platform: Optional[str] = None, device: Optional[str] = None, action: Optional[str] = None,
                     authorization: str = Header(default="")):
        uid = svc.personal.auth(authorization.removeprefix("Bearer ").strip()) if authorization else None
        return svc.instructions.list(platform, device, action, uid)

    @app.get("/api/instructions/best")
    def best_instructions(platform: str, device: str, action: str, uid: str = Depends(user)):
        return svc.instructions.best(platform, device, action, uid)

    class InstructionIn(BaseModel):
        platform: str
        device: str
        action: str
        steps: list[str]
        app_version: str = ""
        screenshots: list[str] = Field(default_factory=list)
        share: bool = False
        replaces: Optional[str] = None

    @app.post("/api/instructions")
    def submit_instructions(body: InstructionIn, uid: str = Depends(user)):
        return svc.instructions.submit(uid, **body.model_dump())

    @app.post("/api/instructions/{iid}/outdated")
    def flag_instructions(iid: str, uid: str = Depends(user)):
        return svc.instructions.flag_outdated(iid, uid)

    @app.get("/api/admin/instructions/pending", dependencies=[Depends(admin)])
    def pending_instructions():
        return svc.instructions.pending()

    class ModerateIn(BaseModel):
        approve: bool

    @app.post("/api/admin/instructions/{iid}/moderate", dependencies=[Depends(admin)])
    def moderate_instructions(iid: str, body: ModerateIn):
        return svc.instructions.moderate(iid, body.approve)

    # Inbox, comments, live chat and email
    @app.post("/api/inbox/import/{source}")
    async def inbox_import(source: str, file: UploadFile = File(...), uid: str = Depends(user)):
        svc.plans.require(uid, "messages")
        data = await file.read()
        if source == "auto":
            source = detect_message_source(file.filename or "", data) or ""
            if not source:
                raise HTTPException(400, "We couldn't tell what kind of export this is. Pick the type and upload it again")
        try:
            msgs = import_messages(source, file.filename or "upload", data)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))
        finally:
            del data
        return {"imported": len(msgs), "source": source, **svc.inbox.store(uid, msgs)}

    class PasteIn(BaseModel):
        platform: str = "tiktok"
        kind: str = "comment"
        context: str = ""
        text: str

    @app.post("/api/inbox/paste")
    def inbox_paste(body: PasteIn, uid: str = Depends(user)):
        svc.plans.require(uid, "messages")
        if body.kind not in ("comment", "live", "dm"):
            raise HTTPException(400, "kind must be comment, live or dm")
        msgs = parse_paste(body.text[:500_000], body.platform, body.kind, body.context)
        if not msgs:
            raise HTTPException(400, 'Paste one message per line as "handle: message"')
        return {"imported": len(msgs), **svc.inbox.store(uid, msgs)}

    @app.get("/api/inbox/senders")
    def inbox_senders(platform: Optional[str] = None, tab: str = "flagged", uid: str = Depends(user)):
        return svc.inbox.senders(uid, platform, tab)

    @app.get("/api/inbox/senders/{platform}/{sender_id}")
    def inbox_sender(platform: str, sender_id: str, uid: str = Depends(user)):
        return svc.inbox.sender_items(uid, platform, sender_id)

    class SenderFeedbackIn(BaseModel):
        platform: str
        sender_id: str
        is_bot: bool

    @app.post("/api/inbox/feedback")
    def inbox_feedback(body: SenderFeedbackIn, uid: str = Depends(user)):
        return svc.inbox.feedback(uid, body.platform, body.sender_id, body.is_bot)

    class SenderStatusIn(BaseModel):
        platform: str
        sender_id: str
        status: str

    @app.post("/api/inbox/status")
    def inbox_status(body: SenderStatusIn, uid: str = Depends(user)):
        if body.status == "removed":
            svc.plans.require(uid, "remove")
        svc.inbox.set_status(uid, body.platform, body.sender_id, body.status)
        return {"ok": True}

    # Connected apps
    @app.post("/api/apps/import/{platform}")
    async def apps_import(platform: str, file: Optional[UploadFile] = File(None), names: str = Form(""), uid: str = Depends(user)):
        """A connected-apps file from a data export, or the app names typed one per line."""
        try:
            if file is not None and file.filename:
                found = import_apps(platform, file.filename, await file.read())
            else:
                found = import_app_names(platform, names)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))
        return svc.apps.store(uid, found)

    @app.get("/api/apps")
    def apps_list(uid: str = Depends(user)):
        return svc.apps.list(uid)

    class AppStatusIn(BaseModel):
        platform: str
        name: str
        status: str

    @app.post("/api/apps/status")
    def apps_status(body: AppStatusIn, uid: str = Depends(user)):
        if body.status == "revoked":
            svc.plans.require(uid, "remove")
        svc.apps.set_status(uid, body.platform, body.name, body.status)
        return {"ok": True}

    class CanaryIn(BaseModel):
        label: str = ""
        phrase: Optional[str] = None

    @app.get("/api/canaries")
    def list_canaries(uid: str = Depends(user)):
        return svc.inbox.canaries(uid)

    @app.post("/api/canaries")
    def new_canary(body: CanaryIn, uid: str = Depends(user)):
        svc.plans.require(uid, "canary")
        out = svc.inbox.new_canary(uid, body.label, body.phrase)
        svc.inbox.scan(uid)
        return out

    @app.delete("/api/canaries")
    def delete_canary(phrase: str, uid: str = Depends(user)):
        svc.inbox.delete_canary(uid, phrase)
        return {"ok": True}

    # Live Guard (Protect): removes bots from live chat as they appear
    @app.put("/api/liveguard/credentials/{platform}")
    def liveguard_credentials(platform: str, body: dict[str, str], uid: str = Depends(user)):
        svc.plans.require(uid, "liveguard")
        need = {"twitch": {"client_id", "token", "login", "broadcaster_id", "moderator_id"}, "youtube": {"token"}}.get(platform)
        if need is None:
            raise HTTPException(400, "Credentials are only needed for twitch and youtube; TikTok and Instagram use the agent's moderator account")
        missing = need - set(body)
        if missing:
            raise HTTPException(400, f"Missing: {', '.join(sorted(missing))}")
        import json as _json

        svc.save_secret(uid, f"liveguard_{platform}", _json.dumps({k: body[k] for k in need}))
        return {"ok": True, "platform": platform}

    class LiveStartIn(BaseModel):
        platform: str
        channel: str = Field(description="Twitch channel name, YouTube liveChatId, or a label for TikTok/Instagram")
        policy: dict[str, Any] = Field(default_factory=dict)

    @app.post("/api/liveguard/sessions")
    def liveguard_start(body: LiveStartIn, uid: str = Depends(user)):
        svc.plans.require(uid, "liveguard")
        from .agent.chat import prefs_for

        defaults = prefs_for(svc, uid).get("live") or {}           # what the person told their agent
        return svc.liveguard.start(uid, body.platform, body.channel, {**defaults, **body.policy})

    @app.get("/api/liveguard/sessions")
    def liveguard_sessions(uid: str = Depends(user)):
        return svc.liveguard.sessions(uid)

    @app.get("/api/liveguard/sessions/{sid}")
    def liveguard_session(sid: str, uid: str = Depends(user)):
        return svc.liveguard.session(uid, sid)

    class ChatIn(BaseModel):
        author_id: str
        text: str
        author_name: str = ""
        message_id: str = ""
        badges: list[str] = Field(default_factory=list)

    class ChatBatchIn(BaseModel):
        messages: list[ChatIn]

    @app.post("/api/liveguard/sessions/{sid}/chat")
    def liveguard_chat(sid: str, body: ChatBatchIn, uid: str = Depends(user)):
        platform = svc.liveguard.session(uid, sid)["platform"]
        msgs = [ChatMessage(platform=platform, author_id=m.author_id, text=m.text[:2000], author_name=m.author_name,
                            message_id=m.message_id, badges=set(m.badges), at=sec.now()) for m in body.messages[:500]]
        return svc.liveguard.feed(uid, sid, msgs)

    class ModeIn(BaseModel):
        mode: str

    @app.post("/api/liveguard/sessions/{sid}/mode")
    def liveguard_mode(sid: str, body: ModeIn, uid: str = Depends(user)):
        return svc.liveguard.set_mode(uid, sid, body.mode)

    class WatchIn(BaseModel):
        url: str = Field(max_length=500)

    WATCH_HOSTS = {"tiktok": "tiktok.com", "instagram": "instagram.com", "facebook": "facebook.com", "kick": "kick.com"}

    @app.post("/api/liveguard/sessions/{sid}/watch")
    def liveguard_watch(sid: str, body: WatchIn, uid: str = Depends(user)):
        """Desktop app: the agent opens the live as the moderator account and moderates its chat."""
        svc.plans.require(uid, "liveguard")
        x = svc.liveguard.session(uid, sid)
        host = WATCH_HOSTS.get(x["platform"])
        if not host:
            raise HTTPException(400, "Twitch and YouTube chats are read through their official APIs; connect your moderator account instead")
        if host not in body.url.lower() or not body.url.lower().startswith("https://"):
            raise HTTPException(400, f"Paste the link to your live on {host}")
        if x["state"] != "running":
            raise HTTPException(400, "Start Live Guard first")
        if sid in svc.liveguard.watchers:
            return {"watching": True}
        if not svc.agent.has_consent(uid, x["platform"]):
            raise PermissionError(f"Accept the agent notice for {x['platform']} first")
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise HTTPException(503, "Live Guard on TikTok, Instagram, Facebook and Kick runs in the Bot Purge desktop app")

        def run():
            from .agent.cockpit import Cockpit, Stopped
            from .agent.livewatch import watch
            from .agent.runner import Executor, Pacing

            try:
                pw, ctx = _agent_browser()
            except Exception:
                svc.liveguard.watchers.discard(sid)
                return
            try:
                cockpit = Cockpit(ctx).install()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(body.url, wait_until="domcontentloaded")
                watch(svc.liveguard, uid, sid, page, x["platform"], cockpit=cockpit, custom=_own_steps(uid, x["platform"], LIVE_OWN),
                      executor=Executor(Pacing(0.3, 0.8, 0, 0), timeout_ms=4000, cockpit=cockpit, ask_help=False))
            except Stopped:
                svc.liveguard.stop(uid, sid)
            except Exception:
                pass
            finally:
                svc.liveguard.watchers.discard(sid)
                try:
                    ctx.close()
                    pw.stop()
                except Exception:
                    pass

        svc.liveguard.watchers.add(sid)
        threading.Thread(target=run, daemon=True, name=f"livewatch-{sid}").start()
        return {"watching": True, "note": "A Bot Purge window opened on your live. Keep it open while you stream."}

    @app.post("/api/liveguard/sessions/{sid}/stop")
    def liveguard_stop(sid: str, uid: str = Depends(user)):
        return svc.liveguard.stop(uid, sid)

    @app.post("/api/liveguard/sessions/{sid}/events/{event_id}/undo")
    def liveguard_undo(sid: str, event_id: int, uid: str = Depends(user)):
        return svc.liveguard.undo(uid, sid, event_id)

    LIVE_OWN = {"ban": "live_ban", "timeout": "live_timeout", "delete": "live_delete"}

    def _own_steps(uid: str, platform: str, actions: dict) -> dict:
        """The person's own written or taught steps, which win over the built-in ones."""
        out = {}
        for key, action in actions.items():
            mine = svc.instructions.best(platform, "web", action, uid)
            if mine.get("author_id") == uid and mine.get("steps"):
                out[key] = mine["steps"]
        return out

    # Done-for-you agent (Protect; runs in the desktop app's own browser)
    @app.get("/api/agent/consent/{platform}")
    def agent_consent_get(platform: str, uid: str = Depends(user)):
        return {"platform": platform, "text": svc.agent.consent_text(platform), "consented": svc.agent.has_consent(uid, platform)}

    @app.post("/api/agent/consent/{platform}")
    def agent_consent_give(platform: str, uid: str = Depends(user)):
        svc.plans.require(uid, "agent")
        return svc.agent.give_consent(uid, platform)

    @app.delete("/api/agent/consent/{platform}")
    def agent_consent_withdraw(platform: str, uid: str = Depends(user)):
        svc.agent.withdraw_consent(uid, platform)
        return {"platform": platform, "consented": False}

    from .agent import signin as agent_signin
    LOGIN_URLS = agent_signin.LOGIN_URLS

    def _saved_signin(uid: str, platform: str) -> Optional[dict]:
        return agent_signin.unpack(svc.get_secret(uid, agent_signin.secret_name(platform)))

    # Optional: save a sign-in so the agent can log in by itself (desktop app only; sealed on this computer)
    @app.get("/api/agent/signins")
    def agent_signins(uid: str = Depends(user)):
        out = []
        for p in LOGIN_URLS:
            c = _saved_signin(uid, p)
            out.append({"platform": p, "saved": bool(c), "username": agent_signin.masked(c["username"]) if c else None})
        return {"available": os.environ.get("BOTPURGE_DESKTOP") == "1", "signins": out}

    class SigninIn(BaseModel):
        username: str
        password: str

    @app.put("/api/agent/signins/{platform}")
    def agent_signin_save(platform: str, body: SigninIn, uid: str = Depends(user)):
        if platform not in LOGIN_URLS:
            raise HTTPException(400, "unknown platform")
        if os.environ.get("BOTPURGE_DESKTOP") != "1":
            raise HTTPException(403, "Saved sign-ins stay on your own computer, so they're only in the Bot Purge desktop app")
        svc.plans.require(uid, "agent")
        if not body.username.strip() or not body.password:
            raise HTTPException(400, "Enter both the username and the password")
        svc.save_secret(uid, agent_signin.secret_name(platform), agent_signin.pack(body.username.strip(), body.password))
        return {"platform": platform, "saved": True, "username": agent_signin.masked(body.username.strip())}

    @app.delete("/api/agent/signins/{platform}")
    def agent_signin_forget(platform: str, uid: str = Depends(user)):
        svc.db.x("DELETE FROM secrets WHERE user_id=? AND name=?", (uid, agent_signin.secret_name(platform)))
        return {"platform": platform, "saved": False}

    def _agent_browser():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise HTTPException(503, "The done-for-you agent runs in the Bot Purge desktop app")
        pw = sync_playwright().start()
        profile = os.environ.get("BOTPURGE_BROWSER_PROFILE", str(Path.home() / ".botpurge" / "browser"))
        kw = dict(headless=os.environ.get("BOTPURGE_AGENT_HEADLESS") == "1", viewport={"width": 1280, "height": 900})
        # Use the Chrome or Edge already on the computer; fall back to Playwright's own Chromium.
        for channel in (os.environ.get("BOTPURGE_AGENT_BROWSER"), "chrome", "msedge", None):
            try:
                ctx = pw.chromium.launch_persistent_context(profile, channel=channel, **kw) if channel \
                    else pw.chromium.launch_persistent_context(profile, **kw)
                return pw, ctx
            except Exception:
                continue
        pw.stop()
        raise HTTPException(503, "No browser found for the agent: install Google Chrome or Microsoft Edge")

    @app.post("/api/agent/login/{platform}")
    def agent_login(platform: str, uid: str = Depends(user)):
        """Opens the agent's browser at the platform's login page; the customer signs in themselves."""
        if platform not in LOGIN_URLS:
            raise HTTPException(400, "unknown platform")
        pw, ctx = _agent_browser()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        creds = _saved_signin(uid, platform)
        if creds:
            from .agent.cockpit import Cockpit

            ok = agent_signin.sign_in(page, platform, creds, Cockpit(ctx).install())
            return {"opened": LOGIN_URLS[platform], "signed_in": ok,
                    "note": "Signed in with your saved sign-in." if ok else "Couldn't finish signing in. Finish it in the window, then close it."}
        page.goto(LOGIN_URLS[platform])
        return {"opened": LOGIN_URLS[platform], "note": "Sign in in the window that opened, then close it."}

    @app.post("/api/agent/apps/{platform}")
    def agent_read_apps(platform: str, uid: str = Depends(user)):
        """The agent opens the platform's app-permissions page and reads the list; the person confirms it before scoring."""
        from .agent import apps as agent_apps

        svc.plans.require(uid, "agent")
        if platform not in agent_apps.APP_PAGES:
            raise HTTPException(400, "unknown platform")
        if not svc.agent.has_consent(uid, platform):
            raise PermissionError(f"Consent for the agent on {platform} is needed first")
        pw, ctx = _agent_browser()
        try:
            from .agent.cockpit import Cockpit

            cockpit = Cockpit(ctx).install()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return agent_apps.read_apps(page, platform, _saved_signin(uid, platform), cockpit)
        finally:
            ctx.close()
            pw.stop()

    @app.post("/api/removals/{job_id}/agent/run")
    def agent_run(job_id: str, uid: str = Depends(user)):
        svc.plans.require(uid, "agent")
        job = svc.removal.job(uid, job_id)
        if not svc.agent.has_consent(uid, job["platform"]):          # the notice comes before anything opens or signs in
            raise PermissionError(f"Consent for the agent on {job['platform']} is needed first")
        custom = _own_steps(uid, job["platform"], {a: a for a in {i["action"] for i in job["items"]}})  # the customer's own steps win
        from .agent.chat import PACES, prefs_for

        prefs = prefs_for(svc, uid)
        pw, ctx = _agent_browser()
        try:
            from .agent.cockpit import Cockpit
            from .agent.runner import Executor, Pacing

            cockpit = Cockpit(ctx).install()          # visible cursor; the person can take over any time
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            agent_signin.ensure_signed_in(page, job["platform"], _saved_signin(uid, job["platform"]), cockpit)
            return svc.agent.run_job(uid, job_id, page, executor=Executor(Pacing(*PACES[prefs["pace"]]), cockpit=cockpit),
                                     custom_steps=custom, max_items=prefs["max_per_run"])
        finally:
            ctx.close()
            pw.stop()

    class TeachIn(BaseModel):
        platform: str
        action: str
        account_id: Optional[str] = None        # a flagged account to demonstrate on
        start_url: Optional[str] = None         # live moderation: the person's live
        example_name: Optional[str] = None      # live moderation: the chatter they'll act on

    @app.post("/api/agent/teach")
    def agent_teach(body: TeachIn, uid: str = Depends(user)):
        """Teach mode: the person does a process once in the agent's window; it's saved as their own steps."""
        from .agent.chat import teachable
        from .agent.cockpit import Cockpit, Stopped, steps_from_recording

        svc.plans.require(uid, "agent")
        if body.action not in teachable(body.platform):
            raise HTTPException(400, f"{body.platform} doesn't support {body.action}")
        live = body.action.startswith("live_")
        if live:
            host = WATCH_HOSTS.get(body.platform, "")
            if not body.start_url or not body.start_url.lower().startswith("https://") or host not in body.start_url.lower():
                raise HTTPException(400, f"Paste the link to your live on {host}")
            if not (body.example_name or "").strip():
                raise HTTPException(400, "Type the name of the chatter you'll show it on")
        f = None
        if body.account_id:
            f = svc.db.one("SELECT handle, profile_url FROM flags WHERE user_id=? AND platform=? AND account_id=?",
                           (uid, body.platform, body.account_id))
        home = agent_signin.HOME_URLS[body.platform]
        pw, ctx = _agent_browser()
        try:
            cockpit = Cockpit(ctx).install()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            agent_signin.ensure_signed_in(page, body.platform, _saved_signin(uid, body.platform), cockpit)
            try:
                raw = cockpit.record(page, start_url=body.start_url if live else ((f and f["profile_url"]) or home))
            except Stopped:
                return {"saved": False, "steps": []}
        finally:
            ctx.close()
            pw.stop()
        lines = steps_from_recording(raw, {"author_name": body.example_name.strip()} if live else {"handle": f and f["handle"]})
        if len(lines) < 2:
            return {"saved": False, "steps": lines, "note": "Nothing was recorded. Try again and click through the steps."}
        saved = svc.instructions.submit(uid, body.platform, "web", body.action, lines)
        return {"saved": True, "steps": lines, "id": saved["id"],
                "note": "Saved as your own steps: the agent follows them for this action from now on. Edit them any time."}

    # Talk to your agent: fine-tune its settings, directions and objectives in plain words
    class ChatIn(BaseModel):
        text: str = Field(min_length=1, max_length=2000)
        platform: Optional[str] = None                # what the person is teaching, when they came from a failed step
        action: Optional[str] = None

    @app.post("/api/agent/chat")
    def agent_chat(body: ChatIn, uid: str = Depends(user)):
        from .agent.chat import converse

        return converse(svc, uid, body.text, {"platform": body.platform, "action": body.action}, http=svc.x_http or httpx.Client())

    @app.get("/api/agent/chat")
    def agent_chat_history(uid: str = Depends(user)):
        from .agent.chat import prefs_for, teachable

        rows = svc.db.q("SELECT role, text, at FROM agent_chat WHERE user_id=? ORDER BY rowid DESC LIMIT 40", (uid,))
        return {"messages": [dict(r) for r in rows][::-1], "prefs": prefs_for(svc, uid),
                "teachable": {p: list(teachable(p)) for p in ("tiktok", "instagram", "facebook", "linkedin", "x", "kick")},
                "engine": "claude" if os.environ.get("ANTHROPIC_API_KEY") else "built-in"}

    @app.delete("/api/agent/chat")
    def agent_chat_clear(uid: str = Depends(user)):
        svc.db.x("DELETE FROM agent_chat WHERE user_id=?", (uid,))
        return {"cleared": True}

    # Desktop app only: read texts straight from an iPhone backup on this computer
    @app.get("/api/local/iphone-backups")
    def iphone_backups(uid: str = Depends(user)):
        if os.environ.get("BOTPURGE_DESKTOP") != "1":
            raise HTTPException(404, "Only available in the Bot Purge desktop app")
        from .desktop import iphone_sms_backups

        return [{"path": p, "modified": sec.iso(datetime.fromtimestamp(os.path.getmtime(p), timezone.utc))} for p in iphone_sms_backups()]

    @app.post("/api/local/iphone-backups/scan")
    def iphone_scan(uid: str = Depends(user)):
        svc.plans.require(uid, "messages")
        found = iphone_backups(uid)
        if not found:
            raise HTTPException(404, "No iPhone backup found. Back up your iPhone to this computer in Finder or iTunes "
                                     "(unencrypted), then try again.")
        msgs = import_messages("sms", "sms.db", Path(found[0]["path"]).read_bytes())
        return {"imported": len(msgs), "backup": found[0]["modified"], **svc.inbox.store(uid, msgs)}

    # Section 10 metrics
    @app.get("/api/admin/metrics", dependencies=[Depends(admin)])
    def admin_metrics():
        from .metrics import product_metrics

        return product_metrics(svc.db)

    class RestrictionIn(BaseModel):
        platform: Platform
        note: str = ""

    @app.post("/api/me/restriction-report")
    def restriction_report(body: RestrictionIn, uid: str = Depends(user)):
        """A platform restricted the user's own account. The target is zero; every report is investigated."""
        svc.db.x("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,1)",
                 (sec.new_id("al_"), uid, sec.iso(), "platform_restriction",
                  f"You reported a restriction on {body.platform.value}: {body.note[:500]}", body.platform.value, None, None))
        return {"ok": True}

    # Alerts, schedule, export, undo
    @app.get("/api/alerts")
    def alerts(uid: str = Depends(user)):
        return svc.personal.alerts(uid)

    @app.post("/api/alerts/read")
    def alerts_read(uid: str = Depends(user)):
        svc.personal.mark_alerts_read(uid)
        return {"ok": True}

    class RescanIn(BaseModel):
        cadence: str

    @app.get("/api/me/report")
    def my_report(days: int = 7, uid: str = Depends(user)):
        from .protect import weekly_report

        svc.plans.require(uid, "reports")
        return weekly_report(svc.db, uid, max(1, min(days, 90)))

    @app.post("/api/rescan")
    def rescan(body: RescanIn, uid: str = Depends(user)):
        if body.cadence == "daily":
            svc.plans.require(uid, "monitor")
        return svc.personal.set_rescan(uid, body.cadence)

    @app.get("/api/export.csv")
    def export_csv(uid: str = Depends(user)):
        svc.plans.require(uid, "undo")
        return PlainTextResponse(svc.personal.export_csv(uid), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=botpurge-log.csv"})

    @app.get("/api/undo-log")
    def undo_log(event: Optional[str] = None, uid: str = Depends(user)):
        return svc.personal.undo_log(uid, event)

    @app.post("/api/undo/readded")
    def readded(body: AccountRef, uid: str = Depends(user)):
        svc.personal.mark_readded(uid, body.platform.value, body.account_id, body.direction)
        return {"ok": True}

    # =========================== Module B ===========================

    class TenantIn(BaseModel):
        name: str
        config: dict = Field(default_factory=dict)

    @app.post("/api/purge/tenants")
    def create_tenant(body: TenantIn, x_admin_token: str = Header(default="")):
        if admin_token and not sec.token_matches(x_admin_token, sec.hash_token(admin_token)):
            raise HTTPException(401, "Admin token required to create a tenant")
        tid, key = svc.purge.create_tenant(body.name, body.config)
        return {"tenant_id": tid, "api_key": key}

    @app.get("/api/purge/me")
    def purge_me(t=Depends(tenant)):
        name = svc.db.one("SELECT name FROM tenants WHERE id=?", (t[0],))["name"]
        return {"tenant_id": t[0], "name": name, "role": t[1], "actor": t[2]}

    @app.get("/api/purge/config")
    def get_config(t=Depends(tenant)):
        return svc.purge.config(t[0])

    @app.patch("/api/purge/config")
    def patch_config(patch: dict[str, Any], t=Depends(owner)):
        return svc.purge.set_config(t[0], patch)

    class ReviewerIn(BaseModel):
        name: str

    @app.post("/api/purge/reviewers")
    def add_reviewer(body: ReviewerIn, t=Depends(owner)):
        return svc.purge.add_reviewer(t[0], body.name)

    @app.post("/api/purge/accounts")
    def ingest(accounts: list[SiteAccount], t=Depends(owner)):
        return svc.purge.ingest(t[0], accounts)

    @app.post("/api/purge/accounts/csv")
    async def ingest_csv(file: UploadFile = File(...), t=Depends(owner)):
        text = (await file.read()).decode("utf-8-sig")
        return svc.purge.ingest(t[0], svc.purge.parse_csv(text))

    class AdapterIn(BaseModel):
        kind: str
        secret: str = Field(description="Discourse API key, Discord bot token, Shopify access token or WordPress Application Password")
        base_url: Optional[str] = None
        api_username: Optional[str] = None
        guild_id: Optional[str] = None
        verify_role_id: Optional[str] = None
        shop: Optional[str] = None
        api_version: Optional[str] = None
        username: Optional[str] = None
        member_role: Optional[str] = None
        reassign_to: Optional[str] = None

    @app.get("/api/purge/adapter")
    def get_adapter(t=Depends(owner)):
        return svc.purge.adapter_info(t[0])

    @app.put("/api/purge/adapter")
    def put_adapter(body: AdapterIn, t=Depends(owner)):
        settings = body.model_dump(exclude={"kind", "secret"}, exclude_none=True)
        return svc.purge.set_adapter(t[0], body.kind, settings, body.secret)

    @app.delete("/api/purge/adapter")
    def delete_adapter(t=Depends(owner)):
        svc.purge.remove_adapter(t[0])
        return {"ok": True}

    @app.post("/api/purge/adapter/sync")
    def sync_adapter(t=Depends(owner)):
        return svc.purge.sync_adapter(t[0])

    @app.post("/api/purge/scan")
    def purge_scan(t=Depends(owner)):
        return svc.purge.scan(t[0])

    @app.get("/api/purge/accounts")
    def explore(min_score: float = 0, max_score: float = 100, reason: Optional[str] = None, ring_id: Optional[str] = None,
                state: Optional[str] = None, signup_after: Optional[str] = None, signup_before: Optional[str] = None,
                limit: int = Query(200, le=2000), offset: int = 0, t=Depends(tenant)):
        return svc.purge.explore(t[0], min_score, max_score, reason, ring_id, state, signup_after, signup_before, limit, offset)

    @app.get("/api/purge/rings")
    def rings(t=Depends(tenant)):
        return svc.purge.rings(t[0])

    @app.get("/api/purge/sample")
    def sample(per_label: int = 10, t=Depends(tenant)):
        return svc.purge.sample(t[0], per_label)

    class ReviewIn(BaseModel):
        is_bot: bool
        note: str = ""

    @app.post("/api/purge/accounts/{account_id}/review")
    def review(account_id: str, body: ReviewIn, t=Depends(tenant)):
        return svc.purge.mark_reviewed(t[0], account_id, t[2], body.is_bot, body.note)

    @app.post("/api/purge/accounts/{account_id}/verified")
    def verified(account_id: str, t=Depends(owner)):
        return svc.purge.report_verified(t[0], account_id)

    @app.get("/api/purge/accounts/{account_id}/notice")
    def account_notice(account_id: str, t=Depends(tenant)):
        return svc.purge.notice_for_account(t[0], account_id)

    @app.get("/api/purge/rules")
    def list_rules(t=Depends(tenant)):
        return svc.purge.rules(t[0])

    @app.post("/api/purge/rules")
    def put_rule(rule: Rule, t=Depends(owner)):
        return svc.purge.put_rule(t[0], rule)

    @app.delete("/api/purge/rules/{rule_id}")
    def delete_rule(rule_id: str, t=Depends(owner)):
        svc.purge.delete_rule(t[0], rule_id)
        return {"ok": True}

    class DryRunIn(BaseModel):
        rules: list[Rule] = Field(default_factory=list)
        only_tiers: Optional[list[int]] = None

    @app.post("/api/purge/dry-run")
    def dry_run(body: DryRunIn, t=Depends(owner)):
        return svc.purge.dry_run(t[0], t[2], body.rules, body.only_tiers)

    @app.get("/api/purge/batches")
    def batches(t=Depends(tenant)):
        return svc.purge.batches(t[0])

    @app.get("/api/purge/batches/{bid}")
    def get_batch(bid: str, t=Depends(tenant)):
        return {**svc.purge.batch(t[0], bid), **svc.purge.batch_status(t[0], bid)}

    @app.post("/api/purge/batches/{bid}/execute")
    def execute(bid: str, t=Depends(owner)):
        return svc.purge.execute(t[0], bid, t[2])

    @app.post("/api/purge/batches/{bid}/step")
    def step(bid: str, t=Depends(owner)):
        return svc.purge.step(t[0], bid, t[2])

    @app.post("/api/purge/batches/{bid}/pause")
    def pause(bid: str, t=Depends(owner)):
        return svc.purge.pause(t[0], bid, t[2])

    @app.post("/api/purge/batches/{bid}/rollback")
    def rollback(bid: str, t=Depends(owner)):
        return svc.purge.rollback(t[0], bid, t[2])

    @app.get("/api/purge/feed")
    def feed(since: int = 0, t=Depends(owner)):
        return svc.purge.enforcement_feed(t[0], since)

    @app.get("/api/purge/appeals")
    def appeals(status: str = "open", t=Depends(tenant)):
        return svc.purge.appeal_queue(t[0], status)

    class DecideIn(BaseModel):
        approve: bool
        note: str = ""

    @app.post("/api/purge/appeals/{appeal_id}/decide")
    def decide(appeal_id: str, body: DecideIn, t=Depends(tenant)):
        return svc.purge.decide(t[0], appeal_id, t[2], body.approve, body.note)

    @app.get("/api/purge/removal-candidates")
    def removal_candidates(t=Depends(tenant)):
        return svc.purge.removal_candidates(t[0])

    class RemoveIn(BaseModel):
        account_ids: list[str]

    @app.post("/api/purge/remove-permanently")
    def remove_permanently(body: RemoveIn, t=Depends(tenant)):
        return svc.purge.remove_permanently(t[0], body.account_ids, t[2])

    @app.post("/api/purge/gate")
    def gate(signup: SiteAccount, t=Depends(owner)):
        return svc.purge.gate_signup(t[0], signup)

    @app.get("/api/purge/report")
    def report(days: int = 30, t=Depends(tenant)):
        return svc.purge.report(t[0], days)

    @app.get("/api/purge/audit")
    def audit(since: int = 0, account_id: Optional[str] = None, t=Depends(tenant)):
        return svc.purge.audit_log(t[0], since, account_id=account_id)

    @app.get("/api/purge/audit/verify")
    def audit_verify(t=Depends(tenant)):
        return svc.purge.verify_audit(t[0])

    # Public appeals portal
    @app.get("/api/appeal/{token}")
    def view_appeal(token: str):
        return svc.purge.view_notice(token)

    class AppealIn(BaseModel):
        statement: str
        contact: str = ""

    @app.post("/api/appeal/{token}")
    def submit_appeal(token: str, body: AppealIn):
        return svc.purge.submit_appeal(token, body.statement, body.contact)

    return app
