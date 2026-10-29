# build_fleet.ps1 — one-command fleet build. RUN ON A WINDOWS MACHINE.
#
# Prereqs (see docs/FLEET.md): CUDA Toolkit 12.x + CUTLASS headers, MSVC 2022,
# Python 3.12 with: pip install numpy blake3 py-pearl-mining pyinstaller
#
# Produces:
#   dist\p40-miner\                      fleet miner (onedir, pool locked)
#   dist\bundle-<version>\               uploadable bundle (manifest.json + p40-miner\)
#   dist\bundle-<version>.zip            zipped bundle -> upload to the panel
#   dist\rig-agent\                      RigAgent (onedir)
#   dist\fleet-rig\                      rig install dir (rig-agent\ + install_task.ps1)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File packaging\build_fleet.ps1 -Version 1.5.fleet-1
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$CudaPath = $env:CUDA_PATH
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# ---- 1. CUDA library (p40cuda.dll) ----------------------------------------
if (-not (Test-Path "p40cuda.dll")) {
    Write-Host "== building p40cuda.dll"
    & packaging\build_capi.bat
    if ($LASTEXITCODE -ne 0) { throw "build_capi.bat failed" }
} else {
    Write-Host "== p40cuda.dll exists, skipping CUDA build (delete it to rebuild)"
}

# ---- 2. miner bundle -------------------------------------------------------
Write-Host "== freezing the miner (PyInstaller onedir)"
pyinstaller packaging\p40-miner-lite.spec --noconfirm --distpath dist --workpath build_pyi
if ($LASTEXITCODE -ne 0) { throw "miner pyinstaller failed" }

$bundleDir = "dist\bundle-$Version"
if (Test-Path $bundleDir) { Remove-Item -Recurse -Force $bundleDir }
New-Item -ItemType Directory -Path $bundleDir | Out-Null

@{
    version = $Version
    created = (Get-Date).ToUniversalTime().ToString("o")
    entry   = "p40-miner/p40-miner.exe"
} | ConvertTo-Json | Set-Content (Join-Path $bundleDir "manifest.json")

Copy-Item -Recurse "dist\p40-miner" (Join-Path $bundleDir "p40-miner")
Compress-Archive -Path "$bundleDir\*" -DestinationPath "dist\bundle-$Version.zip" -Force
$sha = (Get-FileHash "dist\bundle-$Version.zip" -Algorithm SHA256).Hash
Write-Host "== bundle ready: dist\bundle-$Version.zip (sha256 $sha)"

# ---- 3. rig agent ----------------------------------------------------------
Write-Host "== freezing the agent (PyInstaller onedir)"
pyinstaller packaging\agent.spec --noconfirm --distpath dist --workpath build_pyi_agent
if ($LASTEXITCODE -ne 0) { throw "agent pyinstaller failed" }

if (Test-Path "dist\fleet-rig") { Remove-Item -Recurse -Force "dist\fleet-rig" }
New-Item -ItemType Directory -Path "dist\fleet-rig" | Out-Null
Copy-Item -Recurse "dist\rig-agent" "dist\fleet-rig\rig-agent"
Copy-Item "agent\install_task.ps1" "dist\fleet-rig\rig-agent\"

Write-Host ""
Write-Host "== DONE =="
Write-Host "Panel upload:  PUT /api/admin/bundles/$Version with dist\bundle-$Version.zip"
Write-Host "Rig install:   copy dist\fleet-rig\* to <rig-root>\, edit agent.json,"
Write-Host "               then (admin powershell): schtasks via rig-agent\install_task.ps1"
