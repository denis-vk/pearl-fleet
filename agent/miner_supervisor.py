"""Supervise the fleet miner process: spawn, parse its log, stop gracefully.

The miner (p40-miner, PyInstaller onedir) is started as ONE top-level process;
on multi-GPU machines it spawns its own per-GPU children (prefixed [gpuN]).
We only manage the top process and scrape the combined log lines.

Stop path on Windows: CTRL_BREAK_EVENT -> the miner's SIGBREAK handler turns it
into KeyboardInterrupt, which drains in-flight proofs and terminates children.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time

_IS_WIN = os.name == "nt"

# "[gpu2] 12:00:00 grid 5 done: 0 hits over 512 regions (12.34 TH/s, ...)"
_GPU_THS_RE = re.compile(r"\[gpu(\d+)\].*\(([\d.]+) TH/s")
# single-GPU in-process mode has no [gpuN] prefix
_THS_RE = re.compile(r"grid \d+ done:.*\(([\d.]+) TH/s")
# "[ALL] 3/3 GPUs up | combined ~36.01 TH/s | accepted 12"
_ALL_RE = re.compile(r"\[ALL\] (\d+)/(\d+) GPUs up \| combined ~([\d.]+) TH/s \| accepted (\d+)")
# "  *** SHARE ACCEPTED (you) *** you=3 dev=1" / "(DEV FEE)"
_SHARE_RE = re.compile(r"SHARE ACCEPTED \((you|DEV FEE)\)\*\* you=(\d+) dev=(\d+)")
# "p40 miner (torch-free) | fleet 1.5.fleet-3 | pool ..."
_VER_RE = re.compile(r"p40 miner \(torch-free\) \| fleet (\S+)")
_REJECT_RE = re.compile(r"reject|stale", re.IGNORECASE)
# generic hashrate fallback for external engines (krig, blockz, ...)
_GEN_THS_RE = re.compile(r"([\d.]+)\s*TH/s")


def suspend_process(pid):
    """Freeze every thread of a process (duty-cycle throttle for external engines)."""
    if _IS_WIN:
        import ctypes
        PROCESS_SUSPEND_RESUME = 0x0800
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not h:
            raise OSError(f"OpenProcess({pid}) failed")
        try:
            ctypes.windll.ntdll.NtSuspendProcess(h)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    else:
        os.kill(pid, signal.SIGSTOP)


def resume_process(pid):
    if _IS_WIN:
        import ctypes
        PROCESS_SUSPEND_RESUME = 0x0800
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not h:
            raise OSError(f"OpenProcess({pid}) failed")
        try:
            ctypes.windll.ntdll.NtResumeProcess(h)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    else:
        os.kill(pid, signal.SIGCONT)


class MinerSupervisor:
    def __init__(self, exe_path, cwd=None, log_path=None, engine="p40"):
        self.exe_path = exe_path
        self.cwd = cwd or os.path.dirname(exe_path)
        self.log_path = log_path
        self.engine = engine        # 'p40' parses its own log; others use generic
        self.proc = None
        self._lock = threading.Lock()
        self.throttle_pct = 100
        self._cycler = None
        self._cycler_stop = threading.Event()
        self.stats = {
            "state": "stopped",      # stopped|running|crashed|updating
            "pid": None, "version": None, "started_ts": None,
            "per_gpu_ths": {}, "combined_ths": None, "gpus_up": None,
            "accepted_user": 0, "accepted_dev": 0, "rejected": 0,
            "restarts": 0, "last_error": None,
        }

    # ------------------------------------------------------------------ life
    def start(self, cmd, env_extra=None):
        """Spawn the miner with a full command line (no-op if already running)."""
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return False
            self.exe_path = cmd[0]
            env = os.environ.copy()
            if env_extra:
                env.update(env_extra)
            # hidden mode: no console window for the miner (it runs under the
            # SYSTEM scheduled task, where no window exists anyway; the flag
            # matters when the agent is started manually from a terminal).
            creationflags = 0
            if _IS_WIN:
                creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP
                                 | subprocess.CREATE_NO_WINDOW)
            log_f = (open(self.log_path, "a", encoding="utf-8", errors="replace")
                     if self.log_path else None)
            self.proc = subprocess.Popen(
                cmd, cwd=self.cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags)
            self.stats.update({"state": "running", "pid": self.proc.pid,
                               "started_ts": time.time(), "last_error": None})
            threading.Thread(target=self._pump, args=(log_f,), daemon=True).start()
            return True

    def stop(self, grace=15):
        """Graceful stop (CTRL_BREAK/SIGINT), escalating to terminate/kill."""
        with self._lock:
            proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            self.stats["state"] = "stopped"
            return
        try:
            resume_process(proc.pid)   # a duty-cycled miner may be frozen
        except OSError:
            pass
        try:
            if _IS_WIN:
                # CTRL_BREAK needs the child to share our console (fine when run
                # interactively; scheduled tasks have none -> fall through to
                # terminate, losing at most one in-flight ~6s proof).
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass
            else:
                proc.send_signal(signal.SIGINT)
            try:
                proc.wait(grace)
                self.stats["state"] = "stopped"
                return
            except subprocess.TimeoutExpired:
                pass
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
        finally:
            self.stats["state"] = "stopped"

    def restart(self, cmd, env_extra=None):
        self.stop()
        ok = self.start(cmd, env_extra)
        if ok:
            with self._lock:
                self.stats["restarts"] += 1
        return ok

    # ------------------------------------------------------------------ logs
    def _pump(self, log_f):
        proc = self.proc
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if log_f is not None:
                    log_f.write(line + "\n")
                    log_f.flush()
                self._parse_line(line)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if log_f is not None:
                log_f.close()
            rc = proc.poll()
            with self._lock:
                if self.stats["state"] == "running":  # not a deliberate stop/update
                    self.stats["state"] = "crashed" if rc not in (0, None) else "stopped"
                    self.stats["last_error"] = f"exit code {rc}"

    def _parse_line(self, line):
        m = _VER_RE.search(line)
        if m:
            self.stats["version"] = m.group(1)
            return
        m = _GPU_THS_RE.search(line)
        if m and "done:" in line:
            self.stats["per_gpu_ths"][m.group(1)] = float(m.group(2))
            return
        m = _THS_RE.search(line)
        if m and "done:" in line:   # single-GPU in-process mode: no [gpuN] prefix
            self.stats["combined_ths"] = float(m.group(1))
            self.stats["per_gpu_ths"]["0"] = float(m.group(1))
            return
        m = _ALL_RE.search(line)
        if m:
            self.stats["gpus_up"] = f"{m.group(1)}/{m.group(2)}"
            self.stats["combined_ths"] = float(m.group(3))
            return
        # Generic share counting: works for both our p40 format and krig.
        # p40: "*** SHARE ACCEPTED (you) *** you=N dev=M"
        # krig: any line with "accepted" and not "rejected" counts as accepted
        m = _SHARE_RE.search(line)
        if m:
            self.stats["accepted_user"] = int(m.group(2))
            self.stats["accepted_dev"] = int(m.group(3))
            return
        # fallback for krig and other miners
        ll = line.lower()
        if "accepted" in ll and "reject" not in ll and "stale" not in ll:
            self.stats["accepted_user"] = self.stats.get("accepted_user", 0) + 1
            return
        # only count actual share rejections, not debug/info lines mentioning "reject"
        ll = line.lower()
        if ("share" in ll or "submit" in ll) and ("reject" in ll or "stale" in ll):
            self.stats["rejected"] += 1
            return
        if self.engine != "p40":
            m = _GEN_THS_RE.search(line)
            if m:
                self.stats["combined_ths"] = float(m.group(1))

    # -------------------------------------------------------------- throttle
    def set_throttle(self, pct):
        """Process-level duty cycle for engines without in-process intensity
        (krig, blockz, ...): run `pct`% of each 5 s cycle, freeze the rest.
        pct >= 100 disables throttling and resumes the process."""
        pct = min(100, max(0, int(pct)))
        self.throttle_pct = pct
        if pct >= 100:
            if self._cycler is not None and self._cycler.is_alive():
                self._cycler_stop.set()
                self._cycler.join(timeout=6)
                self._cycler_stop = threading.Event()
                self._cycler = None
            proc = self.proc
            if proc is not None and proc.poll() is None:
                try:
                    resume_process(proc.pid)
                except OSError:
                    pass
            return
        if self._cycler is None or not self._cycler.is_alive():
            self._cycler = threading.Thread(target=self._cycle, daemon=True)
            self._cycler.start()

    def _cycle(self):
        stop = self._cycler_stop
        cycle = 5.0
        while not stop.is_set():
            pct = self.throttle_pct
            proc = self.proc
            if proc is None or proc.poll() is not None or pct >= 100:
                stop.wait(cycle)
                continue
            try:
                resume_process(proc.pid)
                if stop.wait(cycle * pct / 100.0):
                    break
                suspend_process(proc.pid)
                stop.wait(cycle * (100 - pct) / 100.0)
            except OSError:
                stop.wait(cycle)

    # ----------------------------------------------------------------- state
    def snapshot(self):
        """Copy of stats + liveness check."""
        alive = self.proc is not None and self.proc.poll() is None
        snap = dict(self.stats)
        snap["per_gpu_ths"] = dict(self.stats["per_gpu_ths"])
        if not alive and snap["state"] == "running":
            snap["state"] = "crashed"
        return snap
