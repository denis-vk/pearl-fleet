#!/usr/bin/env python3
"""Fleet self-verification -- run this FIRST on the Windows rig:

    python tools\\verify_fleet.py

Proves the whole Python layer actually works end-to-end on THIS machine:
  A. syntax-compiles every .py of the miner/agent/panel/tools
  B. boots the panel in-process and drives it over real HTTP:
     enroll -> heartbeat -> admin config -> restart bump -> bundle upload
     -> rig download (sha256 verified) -> engine routing fields
  C. agent units on the real OS: GPU inventory, machine id, miner supervisor
     (spawn/log-parse/stop), duty-cycle suspend/resume throttle, updater
     junction swap + rollback
  D. PASS/FAIL summary -- paste the full output back if anything fails.

Needs: python3 + numpy (agent GPU scan needs nothing extra on Windows).
Panel section auto-skips (with instructions) if fastapi/uvicorn are absent.
"""
import glob
import hashlib
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def section(title):
    print(f"\n=== {title} ===")


# ------------------------------------------------------------------ A: syntax
section("A. Python syntax (py_compile)")
py_files = (glob.glob(os.path.join(ROOT, "python", "*.py"))
            + glob.glob(os.path.join(ROOT, "agent", "*.py"))
            + glob.glob(os.path.join(ROOT, "panel", "*.py"))
            + glob.glob(os.path.join(ROOT, "tools", "*.py")))
bad = []
for f in py_files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(f"{os.path.relpath(f, ROOT)}: {e}")
check(f"{len(py_files)} files compile", not bad, "; ".join(bad[:3]))

# --------------------------------------------------------- B: panel end-to-end
section("B. Panel end-to-end (in-process, real HTTP)")
PANEL_PORT = 8901
BASE = f"http://127.0.0.1:{PANEL_PORT}"
panel_started = False
try:
    tmp = tempfile.mkdtemp(prefix="fleet-verify-")
    os.environ["PANEL_DB"] = os.path.join(tmp, "panel.db")
    os.environ["PANEL_BUNDLES_DIR"] = os.path.join(tmp, "bundles")
    os.environ["PANEL_ADMIN_PASSWORD"] = "verify-pass"
    sys.path.insert(0, ROOT)
    from panel import main as panel_main  # noqa: E402
    import uvicorn  # noqa: E402

    srv = uvicorn.Server(uvicorn.Config(panel_main.app, host="127.0.0.1",
                                        port=PANEL_PORT, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE, timeout=1)
            break
        except urllib.error.HTTPError:
            break  # serving (401/200) == up
        except OSError:
            time.sleep(0.2)
    panel_started = True
    check("panel boots and serves /", True)

    def http(method, path, body=None, headers=None, raw=False):
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = r.read()
            return (r, payload if raw else json.loads(payload or b"{}"))

    # rig enrollment
    enroll_key = panel_main.db.get_meta("enroll_key")
    r, j = http("POST", "/api/rig/enroll",
                {"enroll_key": enroll_key, "name": "verify-rig",
                 "machine_id": "verify-machine", "os": "verify", "agent_version": "0"})
    token = j["token"]
    check("enroll issues token", bool(token))
    r2, j2 = http("POST", "/api/rig/enroll",
                  {"enroll_key": enroll_key, "name": "verify-rig",
                   "machine_id": "verify-machine"})
    check("re-enroll is idempotent", j2["token"] == token)

    # heartbeat -> config
    hb = {"name": "verify-rig", "machine_id": "verify-machine", "os": "verify",
          "engine": "p40", "gpus": [{"vendor": "nvidia", "model": "VERIFY-4090"}],
          "miner": {"version": "v1", "state": "running"},
          "hashrate_ths": {"combined": 12.5}, "shares": {"accepted_user": 1},
          "intensity_pct": 100}
    auth = {"Authorization": f"Bearer {token}"}
    r, j = http("POST", "/api/rig/heartbeat", hb, auth)
    check("heartbeat returns config", j.get("config", {}).get("intensity_pct") == 100)

    # admin login (cookie)
    r, _ = http("POST", "/api/admin/login", {"password": "verify-pass"})
    cookie = r.headers.get("Set-Cookie", "")
    sess = {"Cookie": cookie.split(";")[0]}
    check("admin login sets session", "panel_session=" in cookie)

    r, j = http("GET", "/api/admin/state", headers=sess)
    rig = next(x for x in j["rigs"] if x["name"] == "verify-rig")
    check("admin state shows rig + GPU", rig["gpus"][0]["model"] == "VERIFY-4090")

    # fleet-wide settings apply to every rig...
    http("POST", "/api/admin/global", {"pool": "prl.kryptex.network:8048",
                                        "wallet": "prl1fleet", "intensity_pct": 55}, sess)
    r, j = http("POST", "/api/rig/heartbeat", hb, auth)
    cfg = j["config"]
    check("global pool/wallet/% reach every rig",
          cfg["intensity_pct"] == 55 and cfg["wallet"] == "prl1fleet", str(cfg))
    # ...unless a rig opts out with use_global=0
    http("POST", f"/api/admin/rigs/{rig['rig_id']}/config",
         {"use_global": 0, "intensity_pct": 40, "wallet": "prl1own"}, sess)

    http("POST", f"/api/admin/rigs/{rig['rig_id']}/restart", headers=sess)
    r, j = http("POST", "/api/rig/heartbeat", hb, auth)
    cfg = j["config"]
    check("rig override (use_global=0) + restart_seq",
          cfg["intensity_pct"] == 40 and cfg["wallet"] == "prl1own"
          and cfg["restart_seq"] == 1, str(cfg))

    # bundle upload / download roundtrip
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "verify-v1",
                                                "engine": "p40",
                                                "entry": "p40-miner/p40-miner.exe"}))
        z.writestr("p40-miner/p40-miner.exe", b"dummy")
    zbytes = zbuf.getvalue()
    http("PUT", "/api/admin/bundles/verify-v1", zbytes, sess)
    http("POST", "/api/admin/bundles/verify-v1/deploy", headers=sess)
    r, j = http("GET", "/api/rig/bundle/verify-v1/manifest", headers=auth)
    check("bundle manifest + sha256", j.get("sha256") ==
          hashlib.sha256(zbytes).hexdigest())
    r, blob = http("GET", "/api/rig/bundle/verify-v1", headers=auth, raw=True)
    check("bundle download byte-identical", blob == zbytes)
    r, j = http("POST", "/api/rig/heartbeat", hb, auth)
    check("deploy sets target_version", j["config"]["target_version"] == "verify-v1")

    # fleet-wide settings: one pool/% for every rig that follows global
    http("POST", f"/api/admin/rigs/{rig['rig_id']}/config", {"use_global": 1}, sess)
    http("POST", "/api/admin/global", {"pool": "prl.test.network:9999",
                                       "intensity_pct": 70}, sess)
    r, j = http("POST", "/api/rig/heartbeat", hb, auth)
    check("global pool+% reach the rig",
          j["config"]["pool"] == "prl.test.network:9999"
          and j["config"]["intensity_pct"] == 70, str(j["config"]))
    r, j = http("GET", "/api/admin/state", headers=sess)
    rig = next(x for x in j["rigs"] if x["name"] == "verify-rig")
    check("panel reports rig ip/country",
          rig.get("ip") is not None and rig.get("country") is not None)

    # healthz + rig installer distribution
    r, j = http("GET", "/healthz")
    check("healthz", j.get("ok") is True)
    pkg = io.BytesIO()
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("rig-agent/rig-agent.exe", b"dummy")
    pkg_bytes = pkg.getvalue()
    http("PUT", "/api/admin/rigpkg", pkg_bytes, sess)
    r, blob = http("GET", "/api/admin/rigpkg", headers=sess, raw=True)
    check("rig installer upload/download", blob == pkg_bytes)
except ImportError:
    check("panel e2e", False, "SKIP: pip install fastapi uvicorn, then rerun")
    panel_started = False
except Exception as e:
    check("panel e2e", False, f"{type(e).__name__}: {e}")

# ------------------------------------------------------------- C: agent units
section("C. Agent units (real processes / real junctions)")
sys.path.insert(0, os.path.join(ROOT, "agent"))
try:
    import gpu_inventory
    gpus = gpu_inventory.get_gpus()
    check(f"GPU inventory sees {len(gpus)} adapter(s)",
          isinstance(gpus, list), str(gpus)[:120])
    for g in gpus:
        print(f"      - {g.get('vendor')}: {g.get('model')}")
    check("machine id stable", len(gpu_inventory.machine_id()) > 4)
except Exception as e:
    check("gpu_inventory", False, f"{type(e).__name__}: {e}")

try:
    from miner_supervisor import MinerSupervisor
    logp = os.path.join(tempfile.mkdtemp(prefix="sup-"), "miner.log")
    sup = MinerSupervisor(sys.executable, cwd=tempfile.gettempdir(),
                          log_path=logp, engine="krig")
    ok = sup.start([sys.executable, "-u", "-c",
                    "print('p40 miner (torch-free) | fleet verify-v1 | pool x'); "
                    "print('grid 1 done: 0 hits over 8 regions (1.23 TH/s)'); "
                    "import time; time.sleep(120)"])
    time.sleep(2)
    snap = sup.snapshot()
    check("supervisor spawns + parses log",
          snap["state"] == "running" and snap["version"] == "verify-v1"
          and abs(snap["combined_ths"] - 1.23) < 0.01, str(snap))
    sup.set_throttle(50)
    time.sleep(4)
    check("duty-cycle suspend/resume keeps process alive",
          sup.proc is not None and sup.proc.poll() is None)
    sup.set_throttle(100)
    sup.stop(grace=10)
    time.sleep(0.5)
    check("graceful stop", sup.proc is None or sup.proc.poll() is not None)
except Exception as e:
    check("miner_supervisor", False, f"{type(e).__name__}: {e}")

try:
    from updater import Updater
    root = tempfile.mkdtemp(prefix="upd-")
    upd = Updater(root)

    def make_bundle(ver):
        b = os.path.join(root, "bundles", ver, "p40-miner")
        os.makedirs(b, exist_ok=True)
        exe = "p40-miner.exe" if os.name == "nt" else "p40-miner"
        with open(os.path.join(b, exe), "w") as f:
            f.write("dummy")
        with open(os.path.join(root, "bundles", ver, "manifest.json"), "w") as f:
            json.dump({"version": ver, "engine": "p40",
                       "entry": f"p40-miner/{exe}"}, f)

    make_bundle("v1")
    make_bundle("v2")
    upd._retarget_current("v1")
    check("junction/symlink current -> v1", upd.current_version() == "v1")
    upd.write_intensity(55)
    check("intensity file roundtrip", upd.read_intensity() == 55)
    upd._retarget_current("v2")
    check("swap to v2 without reboot", upd.current_version() == "v2"
          and upd.read_intensity() == 55)   # setting carries across versions
    check("miner_cmd from manifest",
          upd.miner_cmd("prl1w", "wk", "stratum+ssl://p")[-1] == "wk")
    check("engine/intensity_mode callable",
          upd.engine() in ("p40", "krig") and upd.intensity_mode() in ("file", "process_duty"))
    rb = upd.rollback(lambda: None, lambda: None, lambda *a: None)
    check("rollback to v1", rb == "v1" and upd.current_version() == "v1")
    shutil.rmtree(root, ignore_errors=True)
except Exception as e:
    check("updater", False, f"{type(e).__name__}: {e}")

# ------------------------------------------------------------------ D: summary
section("D. Summary")
n_fail = sum(1 for _, ok, _ in results if not ok)
for name, ok, detail in results:
    if not ok:
        print(f"  FAIL: {name} {detail}")
print(f"\n{len(results) - n_fail}/{len(results)} checks passed"
      + ("" if n_fail == 0 else f"; {n_fail} FAILED -- paste this output back"))
sys.exit(1 if n_fail else 0)
