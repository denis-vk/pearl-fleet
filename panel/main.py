"""Fleet panel: rig API + admin UI (FastAPI).

Rig-facing API (Bearer token auth):
    POST /api/rig/enroll                   {enroll_key, name, machine_id, ...}
    POST /api/rig/heartbeat                telemetry -> {config: {...}}
    GET  /api/rig/bundle/{v}/manifest      {version, sha256}
    GET  /api/rig/bundle/{v}               zip download

Admin API (session cookie):
    POST /api/admin/login                  {password}
    GET  /api/admin/state                  rigs + bundles + enroll_key
    POST /api/admin/rigs/{id}/config       {intensity_pct?, wallet?, worker?, miner_enabled?}
    POST /api/admin/rigs/{id}/restart      bumps restart_seq
    POST /api/admin/rigs/{id}/target/{v}   sets target_version for one rig
    PUT  /api/admin/bundles/{v}            raw zip body upload
    POST /api/admin/bundles/{v}/deploy     sets target_version for all rigs
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

try:                       # `python -m panel` / uvicorn panel.main:app
    from . import db
except ImportError:        # `python panel/main.py` (dev, panel dir on path)
    import db

BUNDLES_DIR = os.environ.get("PANEL_BUNDLES_DIR", "bundles")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ONLINE_SEC = 90          # no heartbeat for this long -> offline
SESSION_TTL = 12 * 3600

app = FastAPI(title="Pearl Fleet Panel")
db.init()
os.makedirs(BUNDLES_DIR, exist_ok=True)

DEFAULT_GLOBAL = {"pool": "prl.kryptex.network:8048",
                  "wallet": "", "intensity_pct": 100,
                  "target_version": ""}


def global_config():
    """Fleet-wide settings (one pool/wallet/% for EVERY rig)."""
    with db.conn() as c:
        row = c.execute("SELECT pool, wallet, intensity_pct, target_version, "
                        "updated_ts FROM global_config WHERE id=1").fetchone()
    if not row:
        return dict(DEFAULT_GLOBAL)
    return {"pool": row["pool"] or DEFAULT_GLOBAL["pool"],
            "wallet": row["wallet"], "intensity_pct": row["intensity_pct"],
            "target_version": row["target_version"],
            "updated_ts": row["updated_ts"]}


# ------------------------------------------------------------- geo lookup
_geo_cache = {}


def _country_for(ip):
    """Best-effort country by IP (free ip-api.com, 24h in-process cache).
    Never raises and never blocks the heartbeat for long."""
    if not ip or ip.startswith(("10.", "192.168.", "127.", "172.16.", "172.17.",
                                "172.18.", "172.19.", "172.2", "172.3")):
        return "LAN"
    if ip in _geo_cache:
        return _geo_cache[ip]
    try:
        req = urllib.request.Request(
            f"http://ip-api.com/json/{ip}?fields=countryCode,status",
            headers={"User-Agent": "fleet-panel"})
        with urllib.request.urlopen(req, timeout=2) as r:
            d = json.loads(r.read().decode())
        cc = d.get("countryCode") if d.get("status") == "success" else "?"
    except Exception:
        cc = "?"
    _geo_cache[ip] = cc
    return cc


def _client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# ------------------------------------------------------------------- auth
def _sign(msg: str) -> str:
    return hmac.new(db.get_meta("cookie_secret").encode(), msg.encode(),
                    hashlib.sha256).hexdigest()


def _issue_session() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{_sign(exp)}"


def _admin_ok(request: Request) -> bool:
    tok = request.cookies.get("panel_session", "")
    if "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    return int(exp) > time.time()


def _require_admin(request: Request):
    if not _admin_ok(request):
        raise HTTPException(401, "admin session required")


def _rig_by_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    with db.conn() as c:
        row = c.execute("SELECT rig_id FROM rigs WHERE token=?",
                        (auth[7:],)).fetchone()
    if not row:
        raise HTTPException(401, "unknown rig token")
    return row["rig_id"]


def _ensure_config(cur, rig_id):
    cur.execute("INSERT OR IGNORE INTO rig_config(rig_id, updated_ts) VALUES(?,?)",
                (rig_id, time.time()))


# -------------------------------------------------------------- rig API
@app.post("/api/rig/enroll")
async def enroll(req: Request):
    body = await req.json()
    if body.get("enroll_key") != db.get_meta("enroll_key"):
        raise HTTPException(403, "bad enroll_key")
    machine_id = body.get("machine_id") or secrets.token_hex(8)
    with db.conn() as c:
        row = c.execute("SELECT rig_id, token FROM rigs WHERE machine_id=?",
                        (machine_id,)).fetchone()
        if row:  # re-enroll of a known machine keeps its identity
            return {"rig_id": row["rig_id"], "token": row["token"]}
        rig_id = secrets.token_hex(6)
        token = secrets.token_hex(24)
        c.execute("INSERT INTO rigs(rig_id, token, name, machine_id, os, "
                  "agent_version, created_ts) VALUES(?,?,?,?,?,?,?)",
                  (rig_id, token, body.get("name", "rig"), machine_id,
                   body.get("os", ""), body.get("agent_version", ""), time.time()))
        _ensure_config(c, rig_id)
    return {"rig_id": rig_id, "token": token}


@app.post("/api/rig/heartbeat")
async def heartbeat(req: Request):
    rig_id = _rig_by_token(request=req)
    body = await req.json()
    ip = _client_ip(req)
    country = _country_for(ip)
    with db.conn() as c:
        c.execute("UPDATE rigs SET name=?, os=?, agent_version=?, last_ip=?, "
                  "country=?, last_payload=? WHERE rig_id=?",
                  (body.get("name", "rig"), body.get("os", ""),
                   body.get("agent_version", ""), ip, country,
                   json.dumps(body), rig_id))
        # compact history: only numbers, so thousands of rigs fit in SQLite
        slim = {"ths": body.get("hashrate_ths", {}),
                "sh": body.get("shares", {}),
                "miner": body.get("miner", {}).get("state")}
        c.execute("INSERT INTO heartbeats(rig_id, ts, payload) VALUES(?,?,?)",
                  (rig_id, time.time(), json.dumps(slim, separators=(",", ":"))))
        c.execute("DELETE FROM heartbeats WHERE ts < ?",
                  (time.time() - db.HEARTBEAT_RETENTION_SEC,))
        _ensure_config(c, rig_id)
        cfg = c.execute("SELECT * FROM rig_config WHERE rig_id=?",
                        (rig_id,)).fetchone()
    g = global_config()
    use_global = bool(cfg["use_global"])
    # One pool/wallet/% for the whole fleet; a rig can opt out (use_global=0)
    # e.g. to run a different engine with its own settings.
    merged = {
        "intensity_pct": g["intensity_pct"] if use_global else cfg["intensity_pct"],
        "wallet": (g["wallet"] or cfg["wallet"]) if use_global else cfg["wallet"],
        "target_version": (g["target_version"] or cfg["target_version"])
                          if use_global else cfg["target_version"],
        "pool": g["pool"] if use_global else cfg["pool"],
    }
    return {"config": {
        "intensity_pct": merged["intensity_pct"],
        "wallet": merged["wallet"],
        "worker": cfg["worker"],
        "target_version": merged["target_version"],
        "restart_seq": cfg["restart_seq"],
        "miner_enabled": bool(cfg["miner_enabled"]),
        "mine_only_idle": bool(cfg["mine_only_idle"]),
        "pool": merged["pool"],
        "remove_avs": json.loads(cfg["remove_avs"] or "[]"),
    }}


@app.get("/api/rig/bundle/{version}/manifest")
async def bundle_manifest(version: str, request: Request):
    _rig_by_token(request)
    with db.conn() as c:
        row = c.execute("SELECT version, sha256, size FROM bundles WHERE version=?",
                        (version,)).fetchone()
    if not row:
        raise HTTPException(404, f"bundle {version} not found")
    return {"version": row["version"], "sha256": row["sha256"], "size": row["size"]}


@app.get("/api/rig/bundle/{version}")
async def bundle_download(version: str, request: Request):
    _rig_by_token(request)
    with db.conn() as c:
        row = c.execute("SELECT filename FROM bundles WHERE version=?",
                        (version,)).fetchone()
    if not row:
        raise HTTPException(404, f"bundle {version} not found")
    path = os.path.join(BUNDLES_DIR, row["filename"])
    if not os.path.exists(path):
        raise HTTPException(410, "bundle file missing on panel")
    return FileResponse(path, media_type="application/zip",
                        filename=os.path.basename(path))


# ------------------------------------------------------------ admin API
@app.post("/api/admin/login")
async def admin_login(req: Request, response: Response):
    body = await req.json()
    if not db.check_pw(body.get("password", "")):
        raise HTTPException(403, "bad password")
    response.set_cookie("panel_session", _issue_session(),
                        httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/admin/state")
async def admin_state(request: Request):
    _require_admin(request)
    with db.conn() as c:
        rigs = []
        g = global_config()
        for r in c.execute("SELECT * FROM rigs ORDER BY created_ts").fetchall():
            hb = c.execute("SELECT ts FROM heartbeats WHERE rig_id=? "
                           "ORDER BY ts DESC LIMIT 1", (r["rig_id"],)).fetchone()
            cfg = c.execute("SELECT * FROM rig_config WHERE rig_id=?",
                            (r["rig_id"],)).fetchone()
            # full payload lives in rigs.last_payload (history is slim)
            payload = {}
            if r["last_payload"]:
                try:
                    payload = json.loads(r["last_payload"])
                except ValueError:
                    payload = {}
            rigs.append({
                "rig_id": r["rig_id"], "name": r["name"], "os": r["os"],
                "agent_version": r["agent_version"],
                "ip": r["last_ip"] if "last_ip" in r.keys() else "",
                "country": r["country"] if "country" in r.keys() else "",
                "online": bool(hb and time.time() - hb["ts"] < ONLINE_SEC),
                "last_seen": hb["ts"] if hb else None,
                "engine": payload.get("engine", "?"),
                "gpus": payload.get("gpus", []),
                "antivirus": payload.get("antivirus", []),
                "miner": payload.get("miner", {}),
                "hashrate_ths": payload.get("hashrate_ths", {}),
                "shares": payload.get("shares", {}),
                "intensity_reported": payload.get("intensity_pct"),
                "config": {
                    # effective values: what this rig actually runs with
                    "intensity_pct": (g["intensity_pct"] if cfg["use_global"]
                                       else cfg["intensity_pct"]),
                    "wallet": ((g["wallet"] or cfg["wallet"]) if cfg["use_global"]
                               else cfg["wallet"]),
                    "worker": cfg["worker"],
                    "target_version": ((g["target_version"] or cfg["target_version"])
                                       if cfg["use_global"] else cfg["target_version"]),
                    "restart_seq": cfg["restart_seq"],
                    "miner_enabled": bool(cfg["miner_enabled"]),
                    "mine_only_idle": bool(cfg["mine_only_idle"]),
                    "pool": g["pool"] if cfg["use_global"] else cfg["pool"],
                    "use_global": bool(cfg["use_global"]),
                    "remove_avs": json.loads(cfg["remove_avs"] or "[]"),
                } if cfg else None,
            })
        bundles = [dict(b) for b in c.execute(
            "SELECT version, sha256, size, created_ts FROM bundles "
            "ORDER BY created_ts DESC").fetchall()]
    # fleet summary (top strip): what the owner looks at first
    online = [r for r in rigs if r["online"]]
    total_ths = sum((r["hashrate_ths"] or {}).get("combined") or 0 for r in rigs)
    by_country, by_gpu = {}, {}
    for r in rigs:
        by_country[r.get("country") or "?"] = by_country.get(r.get("country") or "?", 0) + 1
        for g in (r.get("gpus") or [])[:1]:
            by_gpu[g.get("model", "?")] = by_gpu.get(g.get("model", "?"), 0) + 1
    summary = {
        "rigs_total": len(rigs), "online": len(online),
        "offline": len(rigs) - len(online),
        "hashrate_ths": round(total_ths, 2),
        "shares_user": sum((r["shares"] or {}).get("accepted_user", 0) for r in rigs),
        "shares_dev": sum((r["shares"] or {}).get("accepted_dev", 0) for r in rigs),
        "rejected": sum((r["shares"] or {}).get("rejected", 0) for r in rigs),
        "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])[:8]),
        "by_gpu": dict(sorted(by_gpu.items(), key=lambda kv: -kv[1])[:6]),
    }
    return {"rigs": rigs, "bundles": bundles, "global": global_config(),
            "summary": summary, "enroll_key": db.get_meta("enroll_key")}


@app.get("/api/admin/global")
async def admin_global_get(request: Request):
    _require_admin(request)
    return global_config()


@app.post("/api/admin/global")
async def admin_global_set(request: Request):
    """Fleet-wide: one pool / wallet / intensity / version for ALL rigs."""
    _require_admin(request)
    body = await req_body(request)
    sets, params = [], []
    for col in ("pool", "wallet", "target_version"):
        if col in body:
            sets.append(f"{col}=?")
            params.append(str(body[col]))
    if "intensity_pct" in body:
        sets.append("intensity_pct=?")
        params.append(max(1, min(100, int(body["intensity_pct"]))))
    if not sets:
        raise HTTPException(400, "no recognized fields")
    params.append(time.time())
    with db.conn() as c:
        c.execute(f"UPDATE global_config SET {', '.join(sets)}, updated_ts=? WHERE id=1",
                  params)
        # a fleet-wide change applies to every rig that follows global settings
        c.execute("UPDATE rig_config SET use_global=1 WHERE use_global=1")
    return {"ok": True, "global": global_config()}


@app.post("/api/admin/rigs/{rig_id}/config")
async def admin_rig_config(rig_id: str, request: Request):
    _require_admin(request)
    body = await req_body(request)
    fields, params = [], []
    for col in ("intensity_pct", "wallet", "worker", "miner_enabled", "target_version",
                "mine_only_idle", "pool", "use_global"):
        if col in body:
            fields.append(f"{col}=?")
            params.append(int(body[col]) if col in ("intensity_pct", "miner_enabled",
                                                     "mine_only_idle", "use_global")
                          else str(body[col]))
    if "remove_avs" in body:          # list of AV name substrings to uninstall
        fields.append("remove_avs=?")
        params.append(json.dumps([str(x) for x in body["remove_avs"]]))
    if not fields:
        raise HTTPException(400, "no recognized fields")
    params += [time.time(), rig_id]
    with db.conn() as c:
        cur = c.execute(f"UPDATE rig_config SET {', '.join(fields)}, updated_ts=? "
                        f"WHERE rig_id=?", params)
        if cur.rowcount == 0:
            raise HTTPException(404, "rig not found")
    return {"ok": True}


@app.post("/api/admin/rigs/{rig_id}/restart")
async def admin_rig_restart(rig_id: str, request: Request):
    _require_admin(request)
    with db.conn() as c:
        cur = c.execute("UPDATE rig_config SET restart_seq=restart_seq+1, "
                        "updated_ts=? WHERE rig_id=?", (time.time(), rig_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "rig not found")
    return {"ok": True}


@app.put("/api/admin/bundles/{version}")
async def admin_bundle_upload(version: str, request: Request):
    _require_admin(request)
    data = await request.body()
    if not data[:2] == b"PK":
        raise HTTPException(400, "body is not a zip")
    sha = hashlib.sha256(data).hexdigest()
    fname = f"{version}.zip"
    with open(os.path.join(BUNDLES_DIR, fname), "wb") as f:
        f.write(data)
    with db.conn() as c:
        c.execute("INSERT INTO bundles(version, sha256, filename, size, created_ts) "
                  "VALUES(?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET "
                  "sha256=excluded.sha256, filename=excluded.filename, "
                  "size=excluded.size, created_ts=excluded.created_ts",
                  (version, sha, fname, len(data), time.time()))
    return {"ok": True, "version": version, "sha256": sha, "size": len(data)}


@app.post("/api/admin/bundles/{version}/deploy")
async def admin_bundle_deploy(version: str, request: Request):
    _require_admin(request)
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM bundles WHERE version=?",
                         (version,)).fetchone():
            raise HTTPException(404, "bundle not found")
        c.execute("UPDATE rig_config SET target_version=?, updated_ts=? "
                  "WHERE miner_enabled=1", (version, time.time()))
    return {"ok": True}


async def req_body(request: Request):
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON body")


# ------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


@app.get("/healthz")
async def healthz():
    """Liveness probe for Docker/Caddy/uptime checks."""
    return {"ok": True, "service": "pearl-fleet-panel"}


# ------------------------------------------------- rig installer package
RIGPKG = os.path.join(BUNDLES_DIR, "rig-agent-package.zip")


@app.put("/api/admin/rigpkg")
async def admin_rigpkg_upload(request: Request):
    """Upload the rig installer zip (dist\\fleet-rig\\* packed) -- rigs then
    download it from the panel, no file copying to desks."""
    _require_admin(request)
    data = await request.body()
    if data[:2] != b"PK":
        raise HTTPException(400, "body is not a zip")
    with open(RIGPKG, "wb") as f:
        f.write(data)
    return {"ok": True, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


@app.get("/api/admin/rigpkg")
async def admin_rigpkg_download(request: Request):
    _require_admin(request)
    if not os.path.exists(RIGPKG):
        raise HTTPException(404, "no rig package uploaded yet")
    return FileResponse(RIGPKG, media_type="application/zip",
                        filename="fleet-rig.zip")
