// MarkdownOpen —— 極小的原生轉交器（不連結 Qt，不用 Python）
//
// 【它解決什麼】
// 本體是 PyInstaller 打包的 Python 程式。即使新行程唯一要做的事只是「把路徑
// 送給既有視窗然後結束」，也得先載入 python313.dll 與整套 PyQt6 的 .pyd，
// 實測資料夾版要 169 ms、單一 exe 要 1064 ms。而這件事使用者每雙擊一個 .md
// 就付一次，是日常最有感的操作。
//
// 這支程式只做三件事：
//   1. 試著連上具名管道，連得上就把路徑（可以有多個）送過去、結束。
//   2. 連不上就啟動本體，把參數原封不動傳過去。
//   3. 等管道出現後才退場，讓同時被雙擊的其他檔案排在後面轉交，
//      而不是各自開一個視窗。
//
// 【為什麼可以直接用 Win32 講話】
// Qt 的 QLocalServer 在 Windows 上就是具名管道，名稱是 \\.\pipe\<serverName>，
// 位元組模式。所以不需要連結 Qt，CreateFile + WriteFile 就能當它的客戶端。
// 協定也很單純：UTF-8 的路徑加上一個換行當結尾（見 app/single_instance.py）。
//
// 【前景權限】
// 使用者剛剛在檔案總管雙擊，前景權限在「我們」手上，不在既有視窗那邊。
// 不呼叫 AllowSetForegroundWindow 轉讓的話，對方的 SetForegroundWindow 會被
// Windows 擋掉，症狀是「檔案開了，但視窗只在工作列閃爍」。

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>  // CommandLineToArgvW

// --- 可調參數 --------------------------------------------------------------

// 和 app/config.py 的 IPC_SERVER_BASE 一致，改了要兩邊一起改（測試會比對）。
// 實際的管道名稱在執行時補上登入工作階段編號（見 pipe_path）：具名管道是整台
// 機器共用的，沒有 kGateName 那種 Local\ 前綴可用，不加編號的話兩個使用者同時
// 登入，後登入的人雙擊 .md 會把路徑送進前一個人看不到的視窗裡。
static const wchar_t *kPipeBase = L"\\\\.\\pipe\\MarkdownReader.SingleInstance";

// 本體的檔名。轉交器會在自己旁邊找它。
static const wchar_t *kAppName = L"MarkdownReader.exe";

// 資料夾版的輸出目錄名（轉交器放在 dist\ 時要能找到子目錄裡的本體）。
static const wchar_t *kOneDirName = L"MarkdownReader-onedir\\";

// 排隊用的互斥鎖。加 Local\ 前綴，限定在同一個登入工作階段內，
// 才不會影響同一台機器上的其他使用者。
static const wchar_t *kGateName = L"Local\\MarkdownReader.LauncherGate";

static const DWORD kPipeBusyWaitMs = 200;   // 管道忙碌時單次等待的長度
// 管道「存在但所有實例都忙」時，總共願意等多久。這個狀態代表本體一定活著
// （管道會隨行程消失），只是還沒回到事件迴圈接下一條連線——例如正在渲染大檔，
// 或剛 rebuild 完、快取全冷。這裡放棄的代價非常高：會誤判「沒有實例」而
// 再生一個完整的本體行程（600 ms 起跳），所以等待上限要放得比直覺長。
static const DWORD kPipeBusyTotalMs = 3000;
static const DWORD kGateWaitMs = 12000;     // 排隊等前一個轉交器最久多久
static const DWORD kAppStartWaitMs = 15000; // 等本體把管道開起來最久多久
static const DWORD kPollIntervalMs = 25;

// 本體的 GUI 已經閒置（視窗都畫出來了）之後，再等管道最多這麼久。
// 管道是在建立視窗「之前」開的，GUI 都閒下來還沒有管道，幾乎可以斷定
// 本體的 listen() 失敗了（它會安靜退化成不做單一實例）——那就別再等了，
// 否則會握著排隊鎖空等滿 15 秒。取 4 秒而不是更短，是因為單一 exe 版的
// 啟動器行程可能在解壓期間就被判定閒置，太短會誤判。
static const DWORD kIdleGraceMs = 4000;

// 一次最多轉交幾個路徑。檔案總管多選是一檔一次叫用，這個上限只擋
// 命令列給到離譜的情況；每個路徑各佔 MAX_PATH*2 個 wchar，放在堆疊上。
static const int kMaxPaths = 32;

// --- 小工具（不用 CRT，避免多拖一份執行期）--------------------------------

static size_t wlen(const wchar_t *text)
{
    size_t n = 0;
    while (text[n]) ++n;
    return n;
}

static void wappend(wchar_t *dest, size_t cap, const wchar_t *src)
{
    size_t n = wlen(dest);
    while (*src && n + 1 < cap) dest[n++] = *src++;
    dest[n] = 0;
}

// 完整的管道路徑：kPipeBase + "." + 登入工作階段編號。和 app/config.py 的
// IPC_SERVER_NAME 用同一個算法（兩邊都是 ProcessIdToSessionId 的結果），
// 第一次呼叫時算好，之後直接回傳。wsprintfW 在 user32 裡，不是 CRT。
static const wchar_t *pipe_path()
{
    static wchar_t path[128] = {0};
    if (path[0] == 0) {
        DWORD session = 0;
        ProcessIdToSessionId(GetCurrentProcessId(), &session);
        wsprintfW(path, L"%s.%lu", kPipeBase, session);
    }
    return path;
}

// --- 轉交 ------------------------------------------------------------------

// 把路徑送給既有實例。成功回傳 true，代表本行程可以直接結束。
// path 為空字串時只送一個換行，對方會把視窗叫到前景而不開檔（和 Python 端一致）。
static bool try_send(const wchar_t *path)
{
    HANDLE pipe = INVALID_HANDLE_VALUE;
    DWORD busy_waited = 0;
    for (;;) {
        pipe = CreateFileW(pipe_path(), GENERIC_READ | GENERIC_WRITE,
                           0, NULL, OPEN_EXISTING, 0, NULL);
        if (pipe != INVALID_HANDLE_VALUE) break;
        // 管道不存在 -> 沒有實例在跑，立刻放棄（這是最常見的冷啟動路徑，
        // 絕對不能在這裡等，否則每次冷啟動都要多花時間）。
        if (GetLastError() != ERROR_PIPE_BUSY) return false;
        // 存在但所有實例都忙 -> 本體活著，等它回來；上限見 kPipeBusyTotalMs。
        // WaitNamedPipe 在實例空出來時會提早返回，之後仍可能被別的客戶端
        // 搶先接走，所以要繞回去重試 CreateFile，不能只試一次。
        if (busy_waited >= kPipeBusyTotalMs) return false;
        WaitNamedPipeW(pipe_path(), kPipeBusyWaitMs);
        busy_waited += kPipeBusyWaitMs;
    }

    // 一定要在送出之前轉讓前景權限，見檔案開頭說明。
    AllowSetForegroundWindow(ASFW_ANY);

    char payload[8192];
    int bytes = 0;
    if (path && path[0]) {
        bytes = WideCharToMultiByte(CP_UTF8, 0, path, -1,
                                    payload, (int)sizeof(payload) - 2, NULL, NULL);
        if (bytes <= 0) {
            CloseHandle(pipe);
            return false;
        }
        bytes -= 1;  // 去掉 WideCharToMultiByte 補的結尾 NUL
    }
    payload[bytes++] = '\n';  // 收方靠換行判斷路徑到齊了

    DWORD written = 0;
    BOOL ok = WriteFile(pipe, payload, (DWORD)bytes, &written, NULL);
    // 確保資料真的離開本行程再關閉，不然對方可能什麼都讀不到
    FlushFileBuffers(pipe);
    CloseHandle(pipe);
    return ok && written == (DWORD)bytes;
}

// 等管道出現。用 WaitNamedPipe 而不是 CreateFile：後者會真的建立一條連線，
// 收方會收到一個空訊息並把視窗叫到前景，白白閃一下。
//
// 等待以「本體行程的狀態」為界，而不是死板的時鐘：
//   - 本體死了（啟動就當機）  -> 立刻放棄，別空等。
//   - 本體的 GUI 已閒置卻始終沒有管道 -> 它的 listen() 失敗了，再等也不會有，
//     寬限 kIdleGraceMs 後放棄。整體上限 timeout_ms 仍是最後的保險。
// 沒有這兩個界限的話，「本體正常顯示視窗但沒在監聽」會讓每次雙擊都
// 握著排隊鎖空等滿 15 秒，第二次雙擊還要在鎖上再排 12 秒。
static bool wait_for_pipe(DWORD timeout_ms, HANDLE app_process)
{
    DWORD waited = 0;
    DWORD idle_for = 0;
    bool gui_idle = false;
    while (waited < timeout_ms) {
        if (WaitNamedPipeW(pipe_path(), 1)) return true;
        if (GetLastError() != ERROR_FILE_NOT_FOUND) {
            // 管道存在只是忙碌，也算已經起來了
            return true;
        }
        if (app_process) {
            if (WaitForSingleObject(app_process, 0) == WAIT_OBJECT_0) {
                return false;  // 本體已經死了，不會再有管道
            }
            if (!gui_idle && WaitForInputIdle(app_process, 0) == 0) {
                gui_idle = true;
            }
            if (gui_idle) {
                idle_for += kPollIntervalMs;
                if (idle_for >= kIdleGraceMs) return false;
            }
        }
        Sleep(kPollIntervalMs);
        waited += kPollIntervalMs;
    }
    return false;
}

// --- 找到並啟動本體 --------------------------------------------------------

static bool find_app(wchar_t *out, size_t cap)
{
    wchar_t dir[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(NULL, dir, (DWORD)(sizeof(dir) / sizeof(dir[0])));
    if (n == 0 || n >= sizeof(dir) / sizeof(dir[0])) return false;
    while (n > 0 && dir[n - 1] != L'\\') --n;
    dir[n] = 0;  // 只留目錄，含結尾反斜線

    // 1) 和轉交器同一層（資料夾版：兩者都在 MarkdownReader-onedir\ 裡）
    out[0] = 0;
    wappend(out, cap, dir);
    wappend(out, cap, kAppName);
    if (GetFileAttributesW(out) != INVALID_FILE_ATTRIBUTES) return true;

    // 2) 轉交器放在 dist\，本體在 dist\MarkdownReader-onedir\ 裡
    out[0] = 0;
    wappend(out, cap, dir);
    wappend(out, cap, kOneDirName);
    wappend(out, cap, kAppName);
    if (GetFileAttributesW(out) != INVALID_FILE_ATTRIBUTES) return true;

    return false;
}

// 取得自己命令列裡「參數的部分」，原封不動傳給本體。
// 自己重組參數容易把引號和空白弄壞，直接沿用原字串最保險。
static const wchar_t *args_tail(const wchar_t *cmdline)
{
    const wchar_t *p = cmdline;
    if (*p == L'"') {
        ++p;
        while (*p && *p != L'"') ++p;
        if (*p == L'"') ++p;
    } else {
        while (*p && *p != L' ' && *p != L'\t') ++p;
    }
    while (*p == L' ' || *p == L'\t') ++p;
    return p;
}

// 啟動本體。成功時回傳行程握把（呼叫端負責 CloseHandle），失敗回傳 NULL。
// 留著握把是為了讓 wait_for_pipe 能觀察本體的死活與 GUI 狀態。
static HANDLE launch_app(const wchar_t *app_path)
{
    wchar_t cmdline[MAX_PATH * 4];
    cmdline[0] = 0;
    wappend(cmdline, sizeof(cmdline) / sizeof(cmdline[0]), L"\"");
    wappend(cmdline, sizeof(cmdline) / sizeof(cmdline[0]), app_path);
    wappend(cmdline, sizeof(cmdline) / sizeof(cmdline[0]), L"\" ");
    wappend(cmdline, sizeof(cmdline) / sizeof(cmdline[0]), args_tail(GetCommandLineW()));

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    ZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);

    if (!CreateProcessW(app_path, cmdline, NULL, NULL, FALSE,
                        0, NULL, NULL, &si, &pi)) {
        return NULL;
    }
    CloseHandle(pi.hThread);
    return pi.hProcess;
}

// --- 進入點 ----------------------------------------------------------------

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int)
{
    // 收集所有非旗標參數當作要開的檔案，並正規化成絕對路徑。
    // 檔案總管多選是「每個檔案叫一次」，所以這裡通常只有一個；但命令列
    // （或別的程式）一次給好幾個時，以前只送第一個、其餘無聲無息地消失。
    wchar_t paths[kMaxPaths][MAX_PATH * 2];
    int path_count = 0;
    {
        int count = 0;
        LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &count);
        if (argv) {
            for (int i = 1; i < count && path_count < kMaxPaths; ++i) {
                if (argv[i][0] == L'-') continue;
                wchar_t full[MAX_PATH * 2];
                DWORD n = GetFullPathNameW(argv[i],
                                           (DWORD)(sizeof(full) / sizeof(full[0])),
                                           full, NULL);
                paths[path_count][0] = 0;
                wappend(paths[path_count], sizeof(paths[0]) / sizeof(wchar_t),
                        (n > 0 && n < sizeof(full) / sizeof(full[0])) ? full : argv[i]);
                ++path_count;
            }
            LocalFree(argv);
        }
    }
    const wchar_t *path = path_count > 0 ? paths[0] : L"";

    // 快路徑：已經有實例在跑就直接送過去。一條連線送一個路徑（收方靠換行
    // 判斷到齊，不把多個塞進同一條訊息）；第一條送得出去就代表實例活著，
    // 後面幾條盡力送完即可。收方會把連續進來的路徑當成同一批處理。
    if (try_send(path)) {
        for (int i = 1; i < path_count; ++i) try_send(paths[i]);
        return 0;
    }

    // 慢路徑：要啟動本體。用互斥鎖排隊，否則一次選取多個 .md 按 Enter 時，
    // 每個行程都會發現「沒有實例」而各自開一個視窗。
    HANDLE gate = CreateMutexW(NULL, FALSE, kGateName);
    bool held = false;
    if (gate) {
        DWORD result = WaitForSingleObject(gate, kGateWaitMs);
        held = (result == WAIT_OBJECT_0 || result == WAIT_ABANDONED);
    }

    // 排隊等到的期間，前面那個可能已經把本體帶起來了，再試一次。
    // 這裡同樣要把剩下的路徑送完——只送第一個的話，冷啟動多選時排在後面的
    // 檔案會無聲無息地消失。
    if (held && try_send(path)) {
        for (int i = 1; i < path_count; ++i) try_send(paths[i]);
        ReleaseMutex(gate);
        CloseHandle(gate);
        return 0;
    }

    wchar_t app_path[MAX_PATH * 2];
    if (!find_app(app_path, sizeof(app_path) / sizeof(app_path[0]))) {
        MessageBoxW(NULL,
                    L"找不到 MarkdownReader.exe。\n\n"
                    L"請把這個轉交器放在本體旁邊（同一個資料夾）。",
                    L"Markdown 閱讀器", MB_ICONERROR | MB_OK);
        if (held) { ReleaseMutex(gate); }
        if (gate) CloseHandle(gate);
        return 1;
    }

    HANDLE app_process = launch_app(app_path);
    int exit_code = app_process ? 0 : 1;

    // 等本體把管道開起來才放行，後面排隊的轉交器才會走「送過去」而不是
    // 「再開一個視窗」。等不到就放行，最壞情況退化成現在的行為。
    if (app_process) {
        wait_for_pipe(kAppStartWaitMs, app_process);
        CloseHandle(app_process);
    }

    if (held) ReleaseMutex(gate);
    if (gate) CloseHandle(gate);
    return exit_code;
}
