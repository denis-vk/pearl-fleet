# build_fleet_amd.ps1 -- full AMD (HIP) miner bundle, ALL ON WINDOWS.
#
# Prereqs on the build machine (an AMD rig or any Windows box with an AMD card):
#   - AMD HIP SDK for Windows + MSVC 2022 + Python 3.12
#   - pip install numpy blake3 py-pearl-mining pyinstaller
#   (NO CUDA and NO CUTLASS needed for the AMD path.)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File packaging\build_fleet_amd.ps1 -Version 1.5.fleet-amd-1
param(
    [Parameter(Mandatory=$true)][string]$Version,
    # Empty = use build_hip.ps1's universal default (all RDNA/CDNA archs).
    [string[]]$Archs = @()
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# ---- 1. HIP backend as p40cuda.dll (drop-in name; the PyInstaller spec picks
#         it up from the repo root automatically) + amdhip64.dll
if ($Archs.Count -gt 0) {
    & (Join-Path $PSScriptRoot "build_hip.ps1") -Archs $Archs
} else {
    & (Join-Path $PSScriptRoot "build_hip.ps1")   # universal default arch list
}
if (-not (Test-Path "p40cuda.dll")) { throw "p40cuda.dll missing after HIP build" }

# ---- 2. frozen miner (same torch-free spec as the NVIDIA build)
pyinstaller packaging\p40-miner-lite.spec --noconfirm --distpath dist --workpath build_pyi
if ($LASTEXITCODE -ne 0) { throw "miner pyinstaller failed" }
# the HIP runtime must sit next to p40cuda.dll inside the bundle
Copy-Item amdhip64.dll dist\p40-miner\ -Force

# ---- 3. bundle: manifest + onedir. Engine stays "p40" -- same miner, same
#         pool lock, same live intensity file; the panel sees the AMD GPUs in
#         the rig's telemetry like any other.
$bundleDir = "dist\bundle-$Version"
if (Test-Path $bundleDir) { Remove-Item -Recurse -Force $bundleDir }
New-Item -ItemType Directory -Path $bundleDir | Out-Null
@{
    version = $Version
    engine  = "p40"
    entry   = "p40-miner/p40-miner.exe"
    backend = "hip"
    created = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json | Set-Content (Join-Path $bundleDir "manifest.json")
Copy-Item -Recurse dist\p40-miner (Join-Path $bundleDir "p40-miner")
Compress-Archive -Path "$bundleDir\*" -DestinationPath "dist\bundle-$Version.zip" -Force
$sha = (Get-FileHash "dist\bundle-$Version.zip" -Algorithm SHA256).Hash

Write-Host ""
Write-Host "== AMD bundle ready: dist\bundle-$Version.zip (sha256 $sha)"
Write-Host "   BEFORE uploading, run:  python tools\test_hip_engine.py  (must be all PASS)"
Write-Host "   Upload in the panel, set as target_version for the AMD rigs."
