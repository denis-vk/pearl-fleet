"""Miner bundle updater: download -> verify sha256 -> swap -> restart.

On-disk layout under the rig root:

    bundles/<version>/            one extracted bundle per version
        manifest.json             {"version": ..., ...}
        p40-miner/                PyInstaller onedir
    current                       NTFS junction (or symlink) -> bundles/<version>
    bundles/<version>.zip         downloaded archives (kept for rollback/audit)

The swap is: stop miner -> retarget the `current` junction -> start miner.
No machine reboot; the previous version stays on disk for instant rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile

_IS_WIN = os.name == "nt"


class Updater:
    def __init__(self, root, keep_versions=3):
        self.root = root
        self.bundles_dir = os.path.join(root, "bundles")
        self.current_link = os.path.join(root, "current")
        self.keep_versions = keep_versions
        os.makedirs(self.bundles_dir, exist_ok=True)

    # -------------------------------------------------------------- current
    def read_manifest(self):
        """Current bundle's manifest.json ({} when no bundle is installed)."""
        try:
            with open(os.path.join(self.current_link, "manifest.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def engine(self):
        """Bundle engine: 'p40' (our open NVIDIA miner) or an external one
        ('krig' for AMD, 'blockz' for Intel Arc/CPU, ...)."""
        return self.read_manifest().get("engine", "p40")

    def intensity_mode(self):
        """'file' = the miner throttles itself (p40 fleet build, live %);
        'process_duty' = the agent suspend/resumes the process (external engines)."""
        m = self.read_manifest()
        return m.get("intensity", {}).get(
            "mode", "file" if self.engine == "p40" else "process_duty")

    @property
    def miner_exe(self):
        entry = self.read_manifest().get("entry") or os.path.join(
            "p40-miner", "p40-miner.exe" if _IS_WIN else "p40-miner")
        return os.path.join(self.current_link, entry)

    def miner_cmd(self, wallet, worker, pool_url):
        """Full miner command line from the bundle manifest.

        p40 bundles carry no 'args' -> the miner's own --wallet/--worker flags.
        External-engine bundles carry an args template with {wallet}/{worker}/
        {pool} placeholders (e.g. krig: --url stratum+ssl://{pool} --user
        {wallet}/{worker}), so any vendor miner runs unmodified.
        """
        m = self.read_manifest()
        cmd = [self.miner_exe]
        if m.get("args"):
            cmd += [a.format(wallet=wallet, worker=worker, pool=pool_url)
                    for a in m["args"]]
        else:
            cmd += ["--wallet", wallet, "--worker", worker]
        return cmd

    @property
    def intensity_file(self):
        return os.path.join(self.current_link, "p40-miner", "run", "intensity.txt")

    def current_version(self):
        try:
            with open(os.path.join(self.current_link, "manifest.json")) as f:
                return str(json.load(f).get("version"))
        except (OSError, ValueError):
            return None

    def read_intensity(self, default=100):
        try:
            with open(self.intensity_file) as f:
                return int(float(f.read().strip()))
        except (OSError, ValueError):
            return default

    def write_intensity(self, pct):
        path = self.intensity_file
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(int(pct)))

    # ------------------------------------------------------------- pool (p40)
    @property
    def pool_file(self):
        return os.path.join(self.current_link, "p40-miner", "run", "pool.txt")

    def read_pool(self, default=None):
        try:
            with open(self.pool_file) as f:
                return f.read().strip() or default
        except OSError:
            return default

    def write_pool(self, url):
        path = self.pool_file
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(url))

    # ----------------------------------------------------------------- swap
    def _retarget_current(self, version):
        target = os.path.join(self.bundles_dir, version)
        if not os.path.isdir(target):
            raise FileNotFoundError(f"bundle dir missing: {target}")
        intensity = self.read_intensity()     # carry the panel's % across versions
        # remove the old link WITHOUT touching its target (rmdir on a junction
        # removes the reparse point only)
        if os.path.islink(self.current_link) or os.path.isdir(self.current_link):
            if _IS_WIN:
                subprocess.run(["cmd", "/c", "rmdir", self.current_link],
                               check=True, capture_output=True)
            else:
                os.remove(self.current_link)
        if _IS_WIN:
            # junctions need no admin rights, unlike symlinks
            subprocess.run(["cmd", "/c", "mklink", "/J", self.current_link, target],
                           check=True, capture_output=True)
        else:
            os.symlink(target, self.current_link)
        self.write_intensity(intensity)

    def _cleanup_old_versions(self):
        keep = {self.current_version()}
        dirs = sorted((d for d in os.listdir(self.bundles_dir)
                       if os.path.isdir(os.path.join(self.bundles_dir, d))),
                      key=lambda d: os.path.getmtime(os.path.join(self.bundles_dir, d)),
                      reverse=True)
        for d in dirs[self.keep_versions:]:
            if d not in keep:
                shutil.rmtree(os.path.join(self.bundles_dir, d), ignore_errors=True)

    # ---------------------------------------------------------------- update
    def apply(self, version, panel, stop_miner, start_miner, log=print):
        """Bring the rig to `version`. Returns True if the miner was restarted."""
        manifest = panel.bundle_manifest(version)
        sha = manifest.get("sha256")
        dest = os.path.join(self.bundles_dir, f"{version}.zip")
        if not (os.path.exists(dest) and sha and _file_sha256(dest) == sha):
            log(f"downloading bundle {version} ...")
            panel.download_bundle(version, dest, expected_sha256=sha)
        staging = os.path.join(self.bundles_dir, f".staging-{version}")
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(dest) as z:
            z.extractall(staging)
        final = os.path.join(self.bundles_dir, version)
        shutil.rmtree(final, ignore_errors=True)
        os.rename(staging, final)
        unblock_tree(final)       # no SmartScreen on agent-delivered updates

        intensity = self.read_intensity()
        stop_miner()
        self._retarget_current(version)
        self.write_intensity(intensity)   # carry the panel's setting across versions
        self._cleanup_old_versions()
        start_miner()
        log(f"updated to {version} and restarted the miner (no reboot)")
        return True

    def rollback(self, stop_miner, start_miner, log=print):
        """Point `current` back to the most recent other version on disk."""
        versions = sorted((d for d in os.listdir(self.bundles_dir)
                           if os.path.isdir(os.path.join(self.bundles_dir, d))),
                          key=lambda d: os.path.getmtime(os.path.join(self.bundles_dir, d)),
                          reverse=True)
        cur = self.current_version()
        for v in versions:
            if v != cur:
                stop_miner()
                self._retarget_current(v)
                start_miner()
                log(f"rolled back to {v}")
                return v
        log("no other version available to roll back to")
        return None


def _file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def unblock_tree(root):
    """Strip Mark-of-the-Web from every file we deployed, so Windows/SmartScreen
    never blocks an update the agent brought in."""
    if os.name != "nt":
        return
    import ctypes
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            try:
                ctypes.windll.kernel32.DeleteFileW(ctypes.c_wchar_p(p + ":Zone.Identifier"))
            except Exception:
                pass
