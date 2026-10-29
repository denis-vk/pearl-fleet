# make_selfsigned_cert.ps1 -- FREE code-signing identity for the fleet, no
# purchase, no KYC, no public CA. Creates a self-signed code-signing cert and
# exports two files:
#
#   fleet-root.cer      public cert -> installed into Trusted Root + Trusted
#                       Publishers on every rig (install_task.ps1 does this)
#   fleet-signing.pfx   private key -> used by build_fleet_universal.ps1 to sign
#
# On rigs where fleet-root.cer is trusted, WinVerifyTrust validates our
# signatures as a proper chain (so AV heuristics that check "is it signed?" see
# a signed, trusted publisher). SmartScreen reputation still won't apply -- that
# needs a public CA -- but for agent-delivered updates it doesn't matter.
#
# Usage (once, on the build machine):
#   powershell -ExecutionPolicy Bypass -File packaging\make_selfsigned_cert.ps1 `
#       -Name "Fleet Systems" -OutDir signing -PfxPassword "choose-a-pass"
param(
    [string]$Name = "Fleet Systems",
    [string]$OutDir = "signing",
    [Parameter(Mandatory=$true)][string]$PfxPassword,
    [int]$Years = 5
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# Code-signing cert with digitalSignature usage, valid for $Years.
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject "CN=$Name" `
    -FriendlyName "$Name code signing" `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyUsage DigitalSignature `
    -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddYears($Years) `
    -HashAlgorithm SHA256

$cer = Join-Path $OutDir "fleet-root.cer"
$pfx = Join-Path $OutDir "fleet-signing.pfx"
Export-Certificate -Cert $cert -FilePath $cer -Type CERT | Out-Null
Export-PfxCertificate -Cert $cert -FilePath $pfx `
    -Password (ConvertTo-SecureString -String $PfxPassword -Force -AsPlainText) | Out-Null

Write-Host "created self-signed code-signing cert (valid $Years years):"
Write-Host "  public : $cer   (deploy to rigs: Trusted Root + Trusted Publishers)"
Write-Host "  private: $pfx   (build machine only; password is yours)"
Write-Host ""
Write-Host "Build signed:  build_fleet_universal.ps1 -Version X ``"
Write-Host "                 -SignPfx $pfx -SignPfxPassword <pass>"
Write-Host "Rigs trust it: install_task.ps1 auto-imports fleet-root.cer when it"
Write-Host "               sits next to the agent (dist\fleet-rig\rig-agent\)."
