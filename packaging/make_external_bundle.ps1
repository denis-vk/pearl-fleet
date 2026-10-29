# make_external_bundle.ps1 -- wrap a vendor miner (krig for AMD, Blockz for
# Intel Arc/CPU, ...) into a fleet bundle the RigAgent can deploy and supervise.
#
# The vendor miner is used UNMODIFIED: the bundle only adds a manifest that
# tells the agent which exe to run and how to pass wallet/worker/pool.
#
# Example (krig, from your krig-miner-1.5.12-win-x64 folder):
#   powershell -ExecutionPolicy Bypass -File packaging\make_external_bundle.ps1 `
#       -Version krig-1.5.12 `
#       -MinerDir C:\downloads\krig-miner-1.5.12-win-x64 `
#       -Entry "krig/krig-miner.exe" -Engine krig
#
# Result: dist\bundle-krig-1.5.12.zip -> upload in the panel, target AMD rigs.
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [Parameter(Mandatory=$true)][string]$MinerDir,   # folder with the vendor miner
    [Parameter(Mandatory=$true)][string]$Entry,      # exe path inside the bundle, e.g. "krig/krig-miner.exe"
    [string]$Engine = "external",
    # krig-style by default; for another miner pass its own CLI, e.g. Blockz:
    #   '["--url","stratum+ssl://{pool}","--user","{wallet}"]'
    [string]$ArgsJson = '["--url","{pool}","--user","{wallet}/{worker}"]',
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path $MinerDir)) { throw "MinerDir not found: $MinerDir" }
$dirName = ($Entry -split "[\\/]")[0]
$exeCheck = Join-Path $MinerDir (Split-Path $Entry -Leaf)
if (-not (Test-Path $exeCheck)) {
    throw "entry exe '$($Split-Path $Entry -Leaf)' not found inside $MinerDir"
}

$bundle = Join-Path $OutDir "bundle-$Version"
if (Test-Path $bundle) { Remove-Item -Recurse -Force $bundle }
New-Item -ItemType Directory -Path $bundle -Force | Out-Null
Copy-Item -Recurse $MinerDir (Join-Path $bundle $dirName)

$args = $ArgsJson | ConvertFrom-Json
@{
    version    = $Version
    engine     = $Engine
    entry      = $Entry
    args       = $args
    intensity  = @{ mode = "process_duty" }   # agent suspend/resumes the process
    created    = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $bundle "manifest.json")

$zip = Join-Path $OutDir "bundle-$Version.zip"
Compress-Archive -Path "$bundle\*" -DestinationPath $zip -Force
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash
Write-Host "== external bundle ready: $zip"
Write-Host "   engine=$Engine entry=$Entry sha256=$sha"
Write-Host "   upload in the panel and set it as target_version for the rigs"
Write-Host "   that should run this engine (e.g. AMD rigs for krig)."
