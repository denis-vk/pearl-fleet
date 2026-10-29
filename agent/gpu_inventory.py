"""Cross-vendor GPU inventory (Windows-first, with portable fallbacks).

NVIDIA cards get rich telemetry through nvidia-smi; every other vendor (AMD,
Intel, ...) is detected through WMI/CIM so the panel always sees the full GPU
list even on machines the miner cannot currently mine on.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess

_IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if _IS_WIN else 0

_PCI_VENDORS = {"10de": "nvidia", "1002": "amd", "8086": "intel", "1022": "amd"}
_NAME_HINTS = (("nvidia", "nvidia"), ("geforce", "nvidia"), ("tesla", "nvidia"),
               ("quadro", "nvidia"), ("radeon", "amd"), ("amd", "amd"), ("arc", "intel"),
               ("intel", "intel"))


def _run(cmd, timeout=10):
    try:
        kw = {"creationflags": _CREATE_NO_WINDOW} if _IS_WIN else {}
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL,
                                       timeout=timeout, **kw)
    except Exception:
        return ""


# ------------------------------------------------------------------ machine
def get_antivirus():
    """AV products installed on this rig (WMI SecurityCenter2). The heartbeat
    reports them so the panel shows where Defender/Kaspersky/Avast live."""
    if not _IS_WIN:
        return []
    out = _run(["wmic", "/namespace:\\\\root\\SecurityCenter2", "path",
               "AntiVirusProduct", "get", "displayName,productState"], timeout=60)
    av = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("displayname"):
            continue
        m = re.match(r"^(.*?)\s+(\d+)$", line)
        if m:
            state = int(m.group(2))
            # WSC productState bits: 0x1000 = real-time protection on,
            # 0x10 = definitions up to date (heuristics; vendors vary slightly)
            av.append({"name": m.group(1).strip(),
                       "state": state,
                       "enabled": bool(state & 0x1000),
                       "updated": bool(state & 0x10)})
    return av


def machine_id() -> str:
    """Stable per-machine identifier (SMBIOS UUID / MachineGuid / machine-id)."""
    if _IS_WIN:
        out = _run(["wmic", "csproduct", "get", "UUID"])
        m = re.search(r"([0-9A-Fa-f-]{36})", out)
        if m:
            return m.group(1)
        out = _run(["reg", "query",
                    r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"])
        m = re.search(r"([0-9A-Fa-f-]{30,})", out)
        if m:
            return m.group(1)
    elif platform.system() == "Darwin":
        out = _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        if m:
            return m.group(1)
    else:
        try:
            with open("/etc/machine-id") as f:
                mid = f.read().strip()
            if mid:
                return mid
        except OSError:
            pass
    return "unknown-" + platform.node()


def os_name() -> str:
    return f"{platform.system()} {platform.release()}"


# --------------------------------------------------------------------- GPUs
def _vendor_from_pnp(pnp: str, name: str) -> str:
    if pnp:
        m = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
        if m and m.group(1).lower() in _PCI_VENDORS:
            return _PCI_VENDORS[m.group(1).lower()]
    lname = (name or "").lower()
    for hint, vendor in _NAME_HINTS:
        if hint in lname:
            return vendor
    return "unknown"


def _nvidia_details():
    """index -> {model, compute_cap, vram_mb, driver, temp_c, fan_pct, power_w, util_pct}"""
    out = _run(["nvidia-smi",
                "--query-gpu=index,name,compute_cap,memory.total,driver_version,"
                "temperature.gpu,fan.speed,power.draw,utilization.gpu",
                "--format=csv,noheader,nounits"])
    details = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9 or not parts[0].isdigit():
            continue
        def _num(i):
            try:
                return float(parts[i])
            except ValueError:
                return None
        details[int(parts[0])] = {
            "model": parts[1], "compute_cap": parts[2],
            "vram_mb": int(_num(3) or 0), "driver": parts[4],
            "temp_c": _num(5), "fan_pct": _num(6), "power_w": _num(7),
            "util_pct": _num(8),
        }
    return details


def _wmi_video_controllers():
    """All display adapters via wmic, falling back to PowerShell CIM."""
    rows = []
    out = _run(["wmic", "path", "win32_VideoController",
                "get", "Name,DriverVersion,AdapterRAM,PNPDeviceID", "/format:csv"])
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) >= 2:
        header = [h.strip() for h in lines[0].split(",")]
        for line in lines[1:]:
            cells = line.split(",")
            if len(cells) != len(header):
                continue
            row = dict(zip(header, (c.strip() for c in cells)))
            rows.append(row)
    if not rows:
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object "
                    "Name,DriverVersion,AdapterRAM,PNPDeviceID | ConvertTo-Json"],
                   timeout=20)
        try:
            import json
            data = json.loads(out)
            rows = data if isinstance(data, list) else [data]
        except ValueError:
            pass
    return rows


def _mac_gpus():
    out = _run(["system_profiler", "SPDisplaysDataType"], timeout=20)
    gpus = []
    for block in out.split("        Graphics/Displays:")[1:]:
        model = re.search(r"Chipset Model:\s*(.+)", block)
        vram = re.search(r"VRAM \(.*\):\s*([\d.]+)\s*(GB|MB)", block)
        name = model.group(1).strip() if model else "unknown"
        vram_mb = 0
        if vram:
            vram_mb = int(float(vram.group(1)) * (1024 if vram.group(2) == "GB" else 1))
        gpus.append({"index": len(gpus), "vendor": _vendor_from_pnp("", name),
                     "model": name, "vram_mb": vram_mb, "driver": "",
                     "temp_c": None, "fan_pct": None, "power_w": None,
                     "util_pct": None, "compute_cap": None})
    return gpus


def get_gpus():
    """Full GPU list with the richest telemetry available for each vendor."""
    if platform.system() == "Darwin":
        return _mac_gpus()

    nvidia = _nvidia_details()  # {} on machines without nvidia-smi
    gpus = []
    for row in _wmi_video_controllers():
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        pnp = str(row.get("PNPDeviceID", ""))
        try:
            vram = int(str(row.get("AdapterRAM") or 0))
        except ValueError:
            vram = 0
        gpus.append({
            "index": len(gpus),
            "vendor": _vendor_from_pnp(pnp, name),
            "model": name,
            "vram_mb": vram,          # WMI uint32 caps at 4 GiB; smi value wins below
            "driver": str(row.get("DriverVersion", "")),
            "temp_c": None, "fan_pct": None, "power_w": None, "util_pct": None,
            "compute_cap": None,
        })
    # Linux without WMI: nvidia-smi is the only source
    if not gpus and nvidia:
        for idx, d in sorted(nvidia.items()):
            gpus.append({"index": len(gpus), "vendor": "nvidia", "model": d["model"],
                         "vram_mb": d["vram_mb"], "driver": d["driver"],
                         "temp_c": d["temp_c"], "fan_pct": d["fan_pct"],
                         "power_w": d["power_w"], "util_pct": d["util_pct"],
                         "compute_cap": d["compute_cap"]})
        return gpus
    # enrich NVIDIA rows (match by model-name substring; smi order == PCI order)
    smi_rows = list(nvidia.values())
    for g in gpus:
        if g["vendor"] == "nvidia":
            for d in smi_rows:
                if d["model"].lower() in g["model"].lower() or \
                   g["model"].lower() in d["model"].lower():
                    g.update({k: d[k] for k in ("compute_cap", "vram_mb", "driver",
                                                "temp_c", "fan_pct", "power_w",
                                                "util_pct")})
                    smi_rows.remove(d)
                    break
    return gpus


def has_p40_gpu(gpus):
    """Fleet p40 bundles mine on NVIDIA (CUDA backend) AND AMD (HIP backend)."""
    return any(g["vendor"] in ("nvidia", "amd") for g in gpus)


def has_mineable_gpu(gpus):
    """Deprecated alias kept for older call sites."""
    return has_p40_gpu(gpus)


def get_antivirus():
    """Installed AV products via Windows Security Center (works for any vendor:
    Defender, Kaspersky, Avast, ESET...). [] on non-Windows / no AV."""
    if not _IS_WIN:
        return []
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName "
                "AntiVirusProduct | Select-Object displayName, productState, "
                "timestamp | ConvertTo-Json"], timeout=20)
    import json
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    av = []
    for p in data or []:
        name = str(p.get("displayName", "")).strip()
        if not name:
            continue
        # productState hex: enabled/up-to-date bits (0x1000 = enabled,
        # 0x10 = up to date); surfaced raw for the panel.
        state = p.get("productState")
        try:
            enabled = (int(state) & 0x1000) == 0x1000
            updated = (int(state) & 0x10) == 0x10
        except (TypeError, ValueError):
            enabled = updated = None
        av.append({"name": name, "enabled": enabled, "updated": updated})
    return av
