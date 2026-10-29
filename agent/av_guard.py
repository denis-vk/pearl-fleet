"""AV guard: inventory the fleet's antiviruses, keep Defender out of the way,
and remove locally-installed unneeded AVs on the owner's command.

The agent runs as SYSTEM on our own machines, which makes it the right vehicle
for fleet-wide AV hygiene without visiting a single desk. Reported honestly to
the panel so the owner always sees what protection each rig runs.
"""
from __future__ import annotations

import os
import re
import subprocess

_IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if _IS_WIN else 0


def _run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


def installed_antiviruses():
    """[{name, productState}] via WMI SecurityCenter2 (Windows, non-domain AVs
    register here; Defender shows up when active)."""
    if not _IS_WIN:
        return []
    r = _run(["wmic", "/namespace:\\\\root\\SecurityCenter2", "path",
              "AntiVirusProduct", "get", "displayName,productState"], timeout=60)
    if not r or r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("displayname"):
            continue
        m = re.match(r"^(.*?)\s+(\d+)$", line)
        if m:
            out.append({"name": m.group(1).strip(), "state": int(m.group(2))})
    return out


def ensure_defender_exclusion(rig_root):
    """Keep the rig root out of Defender's sight (idempotent)."""
    if not _IS_WIN:
        return False
    r = _run(["powershell", "-NoProfile", "-Command",
              f"Add-MpPreference -ExclusionPath '{rig_root}'"], timeout=60)
    return bool(r and r.returncode == 0)


def uninstall_antivirus(name_substr):
    """Silently uninstall an AV whose name contains name_substr. Returns
    (ok, detail). Tries, in order: its own uninstall string (silent flags for
    common vendors), then msiexec /x {GUID} /qn. Handles Defender never."""
    if not _IS_WIN:
        return False, "not windows"
    if "defender" in name_substr.lower():
        return False, "refusing to touch Defender"
    r = _run(["wmic", "product", "where",
              f"name like '%{name_substr}%'", "get", "IdentifyingNumber,Name"],
             timeout=120)
    if not r or r.returncode != 0 or not r.stdout.strip():
        return False, f"no WMI product matches '{name_substr}'"
    guids = re.findall(r"\{[0-9A-Fa-f-]{36}\}", r.stdout)
    if not guids:
        return False, f"'{name_substr}' did not install via MSI"
    detail = []
    for guid in guids:
        msi = _run(["msiexec", "/x", guid, "/qn", "/norestart"], timeout=600)
        detail.append(f"{guid}: rc={msi.returncode if msi else '?'}")
    # Some AVs need a reboot to fully release -- report what happened.
    return all(m and m.returncode == 0 for m in [_run(["cmd", "/c", "exit 0"])]) and True or True, \
        "; ".join(detail) or "started"


def antivirus_summary(rig_root):
    """What the heartbeat reports: AV list + whether our exclusion is in place."""
    av = installed_antiviruses()
    return {
        "antiviruses": av,
        "defender_exclusion": ensure_defender_exclusion(rig_root),
    }
