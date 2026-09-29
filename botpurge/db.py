"""SQLite storage for both modules.

Module A and Module B share a file but never a row: every Module B table is
keyed by ``tenant_id`` and every query filters on it (tenant isolation).
The audit log is append-only: triggers refuse UPDATE and DELETE, and each row
carries the hash of the previous one so tampering is detectable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------------- Module A: personal cleaner ----------------
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model_json TEXT NOT NULL DEFAULT '{}',
    rescan TEXT NOT NULL DEFAULT 'off',            -- off | weekly | monthly
    next_rescan_at TEXT,
    guardian_of TEXT,                               -- parent/guardian scanning a teen, with consent
    consent_at TEXT,
    plan TEXT NOT NULL DEFAULT 'free',              -- free | cleanup | protect
    plan_activated_at TEXT,
    plan_renews_at TEXT,
    license_id TEXT,                                -- the store license that paid for the plan
    license_key TEXT
);

-- The store's records (only on the server that sells licenses).
CREATE TABLE IF NOT EXISTS licenses (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    plan TEXT NOT NULL,
    session_id TEXT UNIQUE,
    stripe_customer TEXT,
    stripe_subscription TEXT,
    payment_intent TEXT,
    expires_at TEXT,                                -- NULL: never (Cleanup)
    status TEXT NOT NULL,                           -- active | cancelled | refunded
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS licenses_email ON licenses(email);
CREATE INDEX IF NOT EXISTS licenses_sub ON licenses(stripe_subscription);
CREATE TABLE IF NOT EXISTS billing_events (id TEXT PRIMARY KEY, type TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_learned (         -- buttons a person showed the agent when it couldn't find them
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL, action TEXT NOT NULL, step INTEGER NOT NULL,
    label TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (user_id, platform, action, step)
);
CREATE TABLE IF NOT EXISTS stripe_prices (plan TEXT PRIMARY KEY, price_id TEXT NOT NULL);   -- each plan's Stripe Price

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    at TEXT NOT NULL,
    payment_ref TEXT
);

-- Raw-ish connection detail from imports/APIs. Purged after the raw-data TTL (24h).
CREATE TABLE IF NOT EXISTS connections (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    data_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (user_id, platform, account_id, direction)
);

-- What we keep: scores, labels, decisions, and just enough identity to show and undo.
CREATE TABLE IF NOT EXISTS flags (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    handle TEXT, name TEXT, profile_url TEXT, avatar_hash TEXT,
    connected_at TEXT,
    score REAL NOT NULL,
    label TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    is_clone INTEGER NOT NULL DEFAULT 0,
    clone_of TEXT,
    ring_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',          -- active | whitelisted | pending | removed | failed
    scanned_at TEXT NOT NULL,
    PRIMARY KEY (user_id, platform, account_id, direction)
);

CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    counts_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS removal_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    mode TEXT NOT NULL,                             -- one_click | assisted | guided
    state TEXT NOT NULL,                            -- running | paused | done | cancelled
    created_at TEXT NOT NULL,
    pace_seconds REAL NOT NULL,
    next_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS removal_items (
    job_id TEXT NOT NULL REFERENCES removal_jobs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    action TEXT NOT NULL,                           -- unfriend | remove_follower | unfollow
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',          -- queued | opened | pending | removed | blocked | reported | failed | skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT,
    reason TEXT,                                    -- report reason: spam | fake | impersonation | scam
    PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    event TEXT NOT NULL,                            -- flagged | removed | failed | whitelisted | confirmed_bot | readded
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    handle TEXT, name TEXT, profile_url TEXT,
    score REAL, reasons TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    platform TEXT, account_id TEXT, link TEXT,
    read INTEGER NOT NULL DEFAULT 0
);

-- Messages, comments and email (read from exports / mailbox files), one row per item.
CREATE TABLE IF NOT EXISTS msg_items (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,                             -- dm | comment | live | email
    platform TEXT NOT NULL,                         -- tiktok | instagram | facebook | x | linkedin | email | other
    sender_id TEXT NOT NULL,
    sender_name TEXT, sender_handle TEXT,
    text TEXT NOT NULL,
    at TEXT,
    context TEXT,                                   -- post/video id, live stream id, or email subject
    score REAL, label TEXT, reasons_json TEXT,
    extra_json TEXT,                                -- email headers and links
    imported_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);

-- Who sent them: the thing the user acts on (block, report, unsubscribe, remove).
CREATE TABLE IF NOT EXISTS msg_senders (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT, sender_handle TEXT,
    items INTEGER NOT NULL,
    score REAL NOT NULL, label TEXT NOT NULL, reasons_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',          -- active | whitelisted | removed
    first_at TEXT, last_at TEXT,
    PRIMARY KEY (user_id, platform, sender_id)
);

CREATE TABLE IF NOT EXISTS apps (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    permissions TEXT, approved_at TEXT, app_status TEXT,
    score REAL NOT NULL, label TEXT NOT NULL, reasons_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',          -- active | revoked | trusted
    PRIMARY KEY (user_id, platform, name)
);

CREATE TABLE IF NOT EXISTS agent_consents (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    consented_at TEXT NOT NULL,
    consent_text TEXT NOT NULL,                     -- exactly what the customer agreed to
    PRIMARY KEY (user_id, platform)
);

CREATE TABLE IF NOT EXISTS canaries (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, token)
);

CREATE TABLE IF NOT EXISTS lg_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    channel TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    state TEXT NOT NULL,                            -- running | stopped
    started_at TEXT NOT NULL,
    stopped_at TEXT
);

CREATE TABLE IF NOT EXISTS lg_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES lg_sessions(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_name TEXT,
    text TEXT,
    score REAL NOT NULL,
    action TEXT NOT NULL,                           -- none | delete | timeout | ban
    reasons_json TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,             -- 1 when carried out on the platform
    undone INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS secrets (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sealed BLOB NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS mail_links (           -- live inbox connections (Gmail, Outlook), read-only
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    last_sync TEXT,
    last_error TEXT,
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    verifier TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instructions (
    id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruction_flags (
    instruction_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (instruction_id, user_id)
);

-- ---------------- Module B: platform purge console ----------------
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewers (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS t_accounts (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    score REAL, label TEXT, reasons_json TEXT, ring_id TEXT,
    state TEXT NOT NULL DEFAULT 'active',           -- active | challenged | restricted | suspended | removed
    exempt INTEGER NOT NULL DEFAULT 0,
    human_reviewed INTEGER NOT NULL DEFAULT 0,
    state_changed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, account_id)
);

CREATE TABLE IF NOT EXISTS t_rules (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS t_batches (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    state TEXT NOT NULL,                            -- running | paused | done | rolled_back
    plan_json TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    batch_size INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS t_actions (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    account_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    prev_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    rolled_back INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS t_notices (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    tier INTEGER NOT NULL,
    action TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    appeal_deadline TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS t_appeals (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    notice_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    contact TEXT,
    status TEXT NOT NULL,                           -- open | approved | denied
    created_at TEXT NOT NULL,
    review_due TEXT NOT NULL,
    reviewer TEXT,
    decided_at TEXT,
    decision_note TEXT,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS t_audit (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, seq)
);

CREATE TRIGGER IF NOT EXISTS t_audit_no_update BEFORE UPDATE ON t_audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS t_audit_no_delete BEFORE DELETE ON t_audit
WHEN (SELECT COUNT(*) FROM tenants WHERE id = OLD.tenant_id) > 0
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS t_adapters (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    sealed BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS t_signups (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    ip TEXT, subnet TEXT, asn TEXT, fingerprint TEXT, email TEXT,
    decision TEXT NOT NULL,
    score REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS t_signups_idx ON t_signups(tenant_id, at);
"""


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("BOTPURGE_DB", "botpurge.sqlite3")
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(removal_jobs)")}
        if "finished_at" not in cols:
            self.conn.execute("ALTER TABLE removal_jobs ADD COLUMN finished_at TEXT")
        icols = {r["name"] for r in self.conn.execute("PRAGMA table_info(removal_items)")}
        if "reason" not in icols:
            self.conn.execute("ALTER TABLE removal_items ADD COLUMN reason TEXT")
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for col, ddl in (("plan", "TEXT NOT NULL DEFAULT 'free'"), ("plan_activated_at", "TEXT"), ("plan_renews_at", "TEXT"),
                         ("license_id", "TEXT"), ("license_key", "TEXT")):
            if col not in ucols:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple | list = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).rowcount


def dumps(v: Any) -> str:
    return json.dumps(v, default=str, separators=(",", ":"))


def loads(s: str | None, default: Any = None) -> Any:
    return json.loads(s) if s else default
