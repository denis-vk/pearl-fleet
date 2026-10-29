# Install RigAgent as a boot-start scheduled task under SYSTEM.
# Run ONCE from an elevated PowerShell, from the rig-agent directory:
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
#
# Layout: <rig-root>\rig-agent\  (this script + rig-agent.exe)
#         <rig-root>\agent.json  (created from template on first install)
param(
    [string]$TaskName = "RigAgent",
    [string]$AgentDir = $PSScriptRoot
)

$exe = Join-Path $AgentDir "rig-agent.exe"
if (-not (Test-Path $exe)) {
    # tolerate running from source (python -m agent) during testing
    $exe = Join-Path $AgentDir "..\python"
    if (-not (Test-Path (Join-Path $exe "python.exe"))) {
        Write-Error "rig-agent.exe not found in $AgentDir (build it with packaging\build_fleet.ps1)"
        exit 1
    }
}

$root = Split-Path -Parent $AgentDir
$cfg = Join-Path $root "agent.json"
if (-not (Test-Path $cfg)) {
    @{
        panel_url     = "https://your-panel.example.com"
        enroll_key    = "CHANGE_ME"
        rig_name      = $env:COMPUTERNAME
        heartbeat_sec = 30
    } | ConvertTo-Json | Set-Content $cfg
    Write-Host "template agent.json written to $cfg"
    Write-Host ">> edit panel_url and enroll_key, then: schtasks /Run /TN $TaskName"
}

# Fleet code-signing trust (free path): if the build shipped its self-signed
# public cert next to the agent, install it into Trusted Root + Trusted
# Publishers so our signed binaries validate as a proper chain on this rig.
$cer = Join-Path $AgentDir "fleet-root.cer"
if (Test-Path $cer) {
    try {
        Import-Certificate -FilePath $cer -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null
        Import-Certificate -FilePath $cer -CertStoreLocation "Cert:\LocalMachine\TrustedPublisher" | Out-Null
        Write-Host "fleet code-signing cert trusted on this rig"
    } catch {
        Write-Warning "could not import fleet-root.cer ($_)"
    }
} else {
    Write-Host "fleet-root.cer not shipped; binaries stay publisher-unverified"
}

# AV invisibility: exclude the rig root from Windows Defender BEFORE the first
# miner start. A GPU-mining workload + unsigned PyInstaller exe is exactly the
# pattern ML heuristics flag (PUA:CoinMiner); a path exclusion removes both
# scanning and notifications for everything under the rig root. On our own
# machines this is standard fleet practice (HiveOS-style).
# Third-party AVs (Kaspersky/Avast/...) need their own exclusion via their UI
# or admin policy -- the check below only covers Defender.
try {
    if (Get-Command Add-MpPreference -ErrorAction SilentlyContinue) {
        Add-MpPreference -ExclusionPath $root
        Add-MpPreference -ExclusionProcess (Join-Path $AgentDir "rig-agent\rig-agent.exe")
        Write-Host "Windows Defender exclusions added for: $root"
    }
} catch {
    Write-Warning "could not add Defender exclusions ($_) -- add manually if AV flags the miner"
}

schtasks /Create /TN $TaskName /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "`"$exe`"" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "schtasks /Create failed (need admin?)"; exit 1 }

if (Test-Path (Join-Path $AgentDir "rig-agent.exe")) {
    schtasks /Run /TN $TaskName | Out-Null
    Write-Host "RigAgent installed as '$TaskName' (auto-start at boot; started now)."
} else {
    Write-Host "Scheduled task '$TaskName' created (source mode; start manually)."
}
Write-Host "Miner log: $root\logs\miner.log"
Write-Host "Uninstall: schtasks /Delete /TN $TaskName /F"
