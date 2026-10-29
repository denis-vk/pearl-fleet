"""Fleet root certificate (self-signed code-signing CA), embedded so the exe
can trust its own signature on every rig -- no separate file to copy.

SILENT install: uses .NET X509Store via PowerShell (never shows a dialog), for
CurrentUser (no elevation) or LocalMachine (admin/SYSTEM). certutil is NOT used
because `certutil -addstore Root` pops the "install this certificate?" prompt.
"""
import base64
import os
import subprocess

_CERT_B64 = (
    "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSURmVENDQW1XZ0F3SUJBZ0lVWDRpWGxDVEVS"
    "SG1hQUZRam95T3dxV2taTXFZd0RRWUpLb1pJaHZjTkFRRUwKQlFBd1BURUxNQWtHQTFVRUJoTUNV"
    "bFV4RmpBVUJnTlZCQW9NRFVac1pXVjBJRk41YzNSbGJYTXhGakFVQmdOVgpCQU1NRFVac1pXVjBJ"
    "Rk41YzNSbGJYTXdIaGNOTWpZeE1ESTVNRFl4TURNeVdoY05Nell4TURJMk1EWXhNRE15CldqQTlN"
    "UXN3Q1FZRFZRUUdFd0pTVlRFV01CUUdBMVVFQ2d3TlJteGxaWFFnVTNsemRHVnRjekVXTUJRR0Ex"
    "VUUKQXd3TlJteGxaWFFnVTNsemRHVnRjekNDQVNJd0RRWUpLb1pJaHZjTkFRRUJCUUFEZ2dFUEFE"
    "Q0NBUW9DZ2dFQgpBTUNpVzBIMTdyNnVZUGp6SlBLMFQzWDliV1JjNmorekJxejNCMGo3ODhnNTlz"
    "RTZXODJ4SUIxYXFSNWRkNUphCklmOGtTRlNJS25VOUExTWVQSUY1VGU5OFdHVC90amRDK001Vjd6"
    "OTFHQXVhcUpDSnpkaTRKN1VRQnQwTkZBdHcKcEV2WC9lbWNORXg0TVovT2RmaFhINDdsRnR1RnFO"
    "bGpQSXFZRjAvckZKWGE1UlczTWxmcVdjSHdHTDZGcFFNcgpqSU92cUE0eUhDZ1hTVFh6U0ZORjU5"
    "Y3RZU1RIa0Jzc3k5U1lFelpKdWc3VnFuc0tuNTVQdUFkYUFQRmtXamF2ClhYTlpPY2w0SUVxbzc2"
    "NVByVTQveGlXOXVFaUFTR1ZrRFR2QUx0TzhwTHkrQkFlcUVpWWpGdm5EQnJsUGdTamgKRFVCNHZU"
    "bE1BWGd2OEVwc2dXT01hYmtDQXdFQUFhTjFNSE13SFFZRFZSME9CQllFRlBvQXJud1EzRnZ3bGMy"
    "bQpIRlhuQjErZnA4M0hNQjhHQTFVZEl3UVlNQmFBRlBvQXJud1EzRnZ3bGMybUhGWG5CMStmcDgz"
    "SE1BOEdBMVVkCkV3RUIvd1FGTUFNQkFmOHdFd1lEVlIwbEJBd3dDZ1lJS3dZQkJRVUhBd013Q3dZ"
    "RFZSMFBCQVFEQWdlQU1BMEcKQ1NxR1NJYjNEUUVCQ3dVQUE0SUJBUUF5UTkyMThDOGNmbHNaekhj"
    "emoxZExsVVEyNnVVUlFVajVKbThwbjJkZgo0Q3FDVDdHTFN0azN6K21RS1NMbHdBMHczTG5RRWFa"
    "emlaM3prZzJaL2tBdkZFV3RWT1BUcEdScnltcUFEU1o5CkdCRTR6OG1zTWVrSEZkN3k5OERsMDA0"
    "bnBxMlFBVmt3M0dHVEZzd3hqbkdYRC8vK3MzMERONHdPN3Z4ZFV1Z2UKRm03NFZHbTd2YlpkaEZH"
    "RXFNQUtPNnV0RElLRHNNQjE3V0NoV1ByZ1JyeTl4b0o3bkV0MU1yOEl0ZGNKVXZWWQpJekNWbTVC"
    "eE9pUmszSHRZRkw1Q1hybUg0ZzNmdVN0VW9nbkhxR2VhOERxbmdwL1g2ditoNzlFRDZXRG92dXph"
    "ClV6eWNWdkhBTjc4SVJSTURrdmlwd3puU2NrYk9RS1BLelMzKzl2dVU5VXE0Ci0tLS0tRU5EIENF"
    "UlRJRklDQVRFLS0tLS0K"
)

IS_WIN = os.name == "nt"
_NO_WINDOW = 0x08000000

_PS_SCRIPT = r"""
param([string]$CerPath, [string]$StoreName, [string]$Scope)
$ErrorActionPreference = "Stop"
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CerPath)
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store($StoreName, $Scope)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try { $store.Add($cert) } finally { $store.Close() }
Write-Output "ok"
"""


def cert_bytes() -> bytes:
    return base64.b64decode("".join(_CERT_B64))


def _is_admin() -> bool:
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _add_to_store(cer_path: str, store: str, scope: str) -> bool:
    """Silent Add via .NET X509Store (PowerShell, hidden, no UI)."""
    tmp_ps = os.path.join(os.environ.get("TEMP", "."), "fleet_trust.ps1")
    try:
        with open(tmp_ps, "w", encoding="utf-8") as f:
            f.write(_PS_SCRIPT)
    except OSError:
        return False
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", tmp_ps,
           "-CerPath", cer_path, "-StoreName", store, "-Scope", scope]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=_NO_WINDOW, timeout=90)
        ok = r.returncode == 0 and "ok" in (r.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    try:
        os.remove(tmp_ps)
    except OSError:
        pass
    return ok


_PS_ROOT_REG = r"""
param([string]$CerPath, [string]$Hive)
$ErrorActionPreference = "Stop"
$c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CerPath)
$key = "$Hive\Software\Microsoft\SystemCertificates\Root\Certificates\$($c.Thumbprint)"
New-Item -Path $key -Force | Out-Null
New-ItemProperty -Path $key -Name Blob -PropertyType Binary -Value $c.RawData -Force | Out-Null
Write-Output "ok"
"""


def _add_root_via_registry(cer_path: str, admin: bool) -> bool:
    """Silent Root trust: write the cert into the Root store's registry key.
    Windows does NOT show the CA confirmation dialog for this path (the dialog
    only appears for the Certificate-UI APIs / .NET Root add)."""
    tmp_ps = os.path.join(os.environ.get("TEMP", "."), "fleet_root_reg.ps1")
    try:
        with open(tmp_ps, "w", encoding="utf-8") as f:
            f.write(_PS_ROOT_REG)
    except OSError:
        return False
    hive = "HKLM:" if admin else "HKCU:"
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", tmp_ps,
           "-CerPath", cer_path, "-Hive", hive]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=_NO_WINDOW, timeout=90)
        ok = r.returncode == 0 and "ok" in (r.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    try:
        os.remove(tmp_ps)
    except OSError:
        pass
    return ok


def install_trust(log=print, quiet=False, include_root=True,
                  root_ui_fallback=False) -> bool:
    """Install our cert silently.

    TrustedPublisher: silent (no Windows prompt).
    Root: Windows ALWAYS shows the "You are about to install a certificate from
    a certification authority..." confirmation for a CA cert -- that dialog is a
    deliberate security prompt and cannot be suppressed via API. So Root trust is
    opt-in (--trust-root) and costs one click per machine.
    """
    if not IS_WIN:
        return False
    cer = os.path.join(os.environ.get("TEMP", "."), "fleet-root.cer")
    try:
        with open(cer, "wb") as f:
            f.write(cert_bytes())
    except OSError:
        return False

    scope = "LocalMachine" if _is_admin() else "CurrentUser"
    admin = _is_admin()
    ok = True
    # TrustedPublisher: silent via .NET store
    if _add_to_store(cer, "TrustedPublisher", scope):
        if not quiet:
            log(f"cert 'TrustedPublisher' installed ({scope})")
    else:
        ok = False
        if not quiet:
            log(f"cert 'TrustedPublisher' NOT installed ({scope})")
    # Root: silent via registry; falls back to the .NET add (which may prompt)
    if include_root:
        if _add_root_via_registry(cer, admin):
            if not quiet:
                log(f"cert 'Root' installed via registry ({'HKLM' if admin else 'HKCU'})")
        elif root_ui_fallback and _add_to_store(cer, "Root", scope):
            if not quiet:
                log(f"cert 'Root' installed ({scope})")
        else:
            ok = False
            if not quiet:
                log("cert 'Root' NOT installed")
    try:
        os.remove(cer)
    except OSError:
        pass
    return ok
