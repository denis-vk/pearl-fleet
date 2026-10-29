/* FleetUninstaller.c — полностью удаляет Fleet Compute Service с машины.
 * Убивает процессы, удаляет задачу автозагрузки, ключи реестра,
 * исключение Defender, папку C:\rig и временные файлы.
 * Запуск: FleetUninstall.exe (двойной клик, без окон).
 * Build: x86_64-w64-mingw32-gcc -o FleetUninstall.exe FleetUninstaller.c -mwindows -s -Os
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <shellapi.h>
#include <tlhelp32.h>

#define RIG_DIR   L"C:\\rig"
#define TASK_NAME L"FleetAgent"

static void log_(const char *msg) {
    FILE *f = fopen("C:\\rig_uninstall.log", "a");
    if (!f) return;
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d] %s\n", st.wHour, st.wMinute, st.wSecond, msg);
    fclose(f);
}

static void kill_process(const wchar_t *name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W pe = {sizeof(pe)};
    if (Process32FirstW(snap, &pe)) {
        do {
            if (lstrcmpiW(pe.szExeFile, name) == 0) {
                HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, pe.th32ProcessID);
                if (h) { TerminateProcess(h, 0); CloseHandle(h);
                         log_("killed process"); }
            }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
}

static void remove_registry(void) {
    /* Run key (per-user) */
    HKEY key;
    if (RegOpenKeyExW(HKEY_CURRENT_USER,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0,
        KEY_SET_VALUE | KEY_QUERY_VALUE, &key) == ERROR_SUCCESS) {
        RegDeleteValueW(key, TASK_NAME);
        RegCloseKey(key);
        log_("registry Run key deleted");
    }
    /* cert from Root (both HKCU and HKLM) */
    const wchar_t *certPaths[] = {
        L"Software\\Microsoft\\SystemCertificates\\Root\\Certificates",
        L"Software\\Microsoft\\SystemCertificates\\TrustedPublisher\\Certificates",
        NULL
    };
    /* we need the thumbprint; easier via PowerShell */
    STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi;
    wchar_t cmd[1024];
    _snwprintf(cmd, 1024,
        L"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
        L"-NoProfile -NonInteractive -WindowStyle Hidden -Command "
        L"\"Get-ChildItem Cert:\\CurrentUser\\Root,CurrentUser\\TrustedPublisher,"
        L"LocalMachine\\Root,LocalMachine\\TrustedPublisher "
        L"-ErrorAction SilentlyContinue | Where-Object {$_.Subject -like '*Fleet*'} |"
        L" Remove-Item -Force\"");
    ZeroMemory(&pi, sizeof(pi));
    CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
    if (pi.hProcess) { WaitForSingleObject(pi.hProcess, 30000);
                       CloseHandle(pi.hProcess); CloseHandle(pi.hThread); }
    log_("certificates removed");
}

static void remove_defender(void) {
    STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi;
    wchar_t cmd[512];
    _snwprintf(cmd, 512,
        L"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
        L"-NoProfile -NonInteractive -WindowStyle Hidden -Command "
        L"\"try{Remove-MpPreference -ExclusionPath '%s'}catch{}\"", RIG_DIR);
    ZeroMemory(&pi, sizeof(pi));
    CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
    if (pi.hProcess) { WaitForSingleObject(pi.hProcess, 30000);
                       CloseHandle(pi.hProcess); CloseHandle(pi.hThread); }
    log_("defender exclusion removed");
}

static void remove_task(void) {
    STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi;
    wchar_t cmd[512];
    _snwprintf(cmd, 512,
        L"C:\\Windows\\System32\\cmd.exe /C schtasks /Delete /TN %s /F 2>nul", TASK_NAME);
    ZeroMemory(&pi, sizeof(pi));
    CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
    if (pi.hProcess) { WaitForSingleObject(pi.hProcess, 15000);
                       CloseHandle(pi.hProcess); CloseHandle(pi.hThread); }
    log_("scheduled task removed");
}

static void remove_startup_folder(void) {
    wchar_t path[MAX_PATH];
    _snwprintf(path, MAX_PATH, L"%s\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\%s.cmd",
               _wgetenv(L"APPDATA") ? _wgetenv(L"APPDATA") : L"", TASK_NAME);
    DeleteFileW(path);
    log_("startup folder cmd removed");
}

static void remove_dir_recursive(const wchar_t *path) {
    /* SHFileOperation with FOF_NOCONFIRMATION | FOF_SILENT */
    wchar_t buf[MAX_PATH + 2];
    memset(buf, 0, sizeof(buf));
    lstrcpyW(buf, path);
    buf[lstrlenW(buf) + 1] = 0;  /* double-null terminated */

    SHFILEOPSTRUCTW op = {0};
    op.wFunc = FO_DELETE;
    op.pFrom = buf;
    op.fFlags = FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI;
    int result = SHFileOperationW(&op);
    if (result == 0) log_("directory deleted");
    else log_("directory delete returned error");
}

static void remove_temp(void) {
    /* fleet temp files */
    const wchar_t *tempFiles[] = {
        L"C:\\rig\\_fleet.zip", L"C:\\rig\\_extract.ps1",
        L"C:\\rig\\install.log", L"C:\\rig\\logs",
        NULL
    };
    for (int i = 0; tempFiles[i]; i++) {
        DWORD attr = GetFileAttributesW(tempFiles[i]);
        if (attr != INVALID_FILE_ATTRIBUTES) {
            if (attr & FILE_ATTRIBUTE_DIRECTORY) remove_dir_recursive(tempFiles[i]);
            else DeleteFileW(tempFiles[i]);
        }
    }
    /* %TEMP% fleet files */
    wchar_t tmpPath[MAX_PATH];
    GetTempPathW(MAX_PATH, tmpPath);
    const wchar_t *tmpNames[] = { L"fleet_deploy.zip", L"fleet_crash.log",
                                   L"fleet_agent.log", L"fleet-root.cer", NULL };
    for (int i = 0; tmpNames[i]; i++) {
        wchar_t full[MAX_PATH];
        _snwprintf(full, MAX_PATH, L"%s%s", tmpPath, tmpNames[i]);
        DeleteFileW(full);
    }
    log_("temp files cleaned");
}

int WINAPI WinMain(HINSTANCE h, HINSTANCE p, LPSTR c, int show) {
    log_("=== FleetUninstaller starting ===");

    /* 1. kill processes */
    log_("stopping processes...");
    kill_process(L"FleetComputeService.exe");
    kill_process(L"krig-miner.exe");
    Sleep(2000);  /* let handles release */

    /* 2. remove autostart */
    remove_task();
    remove_registry();
    remove_startup_folder();

    /* 3. remove Defender exclusion */
    remove_defender();

    /* 4. remove temp files */
    remove_temp();

    /* 5. delete C:\rig */
    if (GetFileAttributesW(RIG_DIR) != INVALID_FILE_ATTRIBUTES) {
        log_("deleting rig directory...");
        remove_dir_recursive(RIG_DIR);
    }

    /* 6. delete the uninstaller itself (delayed) */
    wchar_t self[MAX_PATH];
    GetModuleFileNameW(NULL, self, MAX_PATH);
    STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi;
    wchar_t cmd[512];
    _snwprintf(cmd, 512,
        L"C:\\Windows\\System32\\cmd.exe /C timeout /t 3 >nul & del \"%s\" & del C:\\rig_uninstall.log",
        self);
    CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);

    log_("=== FleetUninstaller done ===");
    return 0;
}
