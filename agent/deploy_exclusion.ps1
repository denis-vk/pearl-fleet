# deploy_exclusion.ps1 -- mass-deploy the fleet's Defender exclusions to ALL
# machines at once (thousands, one command). Run from an admin PowerShell on a
# machine that has network access to the fleet.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File agent\deploy_exclusion.ps1 -Hosts hosts.txt -RigRoot C:\rig
#   powershell ... -Hosts hosts.txt -RigRoot C:\rig -WhatIf   (dry run)
#
# hosts.txt: one machine name or IP per line (# comments allowed).
#
# Alternative for domain fleets: the same two Add-MpPreference lines as a GPO
# (Computer Config > Admin Templates > Windows Components > Defender > Path
# exclusions) reaches every machine without any script.
param(
    [Parameter(Mandatory=$true)][string]$Hosts,   # path to hosts.txt
    [Parameter(Mandatory=$true)][string]$RigRoot,# rig root on each machine
    [string]$AgentProcess = "rig-agent\rig-agent.exe",
    [int]$Throttle = 50,                          # parallel machines
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Hosts)) { throw "hosts file not found: $Hosts" }
$targets = Get-Content $Hosts | Where-Object { $_ -and -not $_.TrimStart().StartsWith("#") } |
           ForEach-Object { $_.Trim() }
Write-Host "deploying Defender exclusions to $($targets.Count) machines (root $RigRoot)"

# The agent process lives inside the rig root, so the path exclusion already
# covers it; the process exclusion is belt-and-suspenders for renamed paths.
$script = {
    param($root, $proc)
    Add-MpPreference -ExclusionPath $root
    Add-MpPreference -ExclusionProcess $proc
    "ok"
}

if ($WhatIf) { Write-Host "WhatIf: would run Add-MpPreference on $($targets.Count) machines"; exit 0 }

$ok = $fail = 0
$failures = @()
$targets | ForEach-Object -ThrottleLimit $Throttle -Parallel {
    # PS 7+ ForEach-Object -Parallel; the scriptblock is re-declared inline for
    # the runspace. $using: carries the outer variables.
    $machine = $_
    try {
        $r = Invoke-Command -ComputerName $machine -ErrorAction Stop -ScriptBlock {
            param($root, $proc)
            Add-MpPreference -ExclusionPath $root
            Add-MpPreference -ExclusionProcess $proc
        } -ArgumentList $using:RigRoot, (Join-Path $using:RigRoot $using:AgentProcess)
        Write-Host "  ok   $machine"
    } catch {
        Write-Host "  FAIL $machine ($($_.Exception.Message))"
    }
}
Write-Host "done. Re-run for failures only (edit hosts.txt to the failed list)."
Write-Host "verify a machine:  Invoke-Command -ComputerName <host> (Get-MpPreference).ExclusionPath"
