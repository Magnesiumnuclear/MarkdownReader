"""Windows 原生 API 的薄封裝（ctypes）。

只包含幾個 Qt 無法妥善處理、或原生作法明顯較佳的操作。
所有函式在非 Windows 平台或呼叫失敗時都會安靜地退回中性值（動作類回傳 False，
查詢類回傳 0），不影響主流程。
"""

from __future__ import annotations

import ctypes
import sys

from . import config

IS_WINDOWS = sys.platform == "win32"

# SetWindowPos 用的常數
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010

# SetWindowDisplayAffinity 用的常數
WDA_NONE = 0x00
WDA_MONITOR = 0x01               # 舊版 Windows 的退路：擷取畫面裡變成一塊黑
WDA_EXCLUDEFROMCAPTURE = 0x11    # Windows 10 2004（19041）起：擷取畫面裡完全不存在

# SHChangeNotify 用的常數
_SHCNE_ASSOCCHANGED = 0x08000000
_SHCNF_IDLIST = 0x0000


# 【務必保留這些 argtypes 宣告】
# 64 位元 Windows 的 HWND 是 64-bit，但 ctypes 在沒有 argtypes 時會把 Python int
# 當成 32-bit 的 C int 傳遞。SetWindowPos 的 HWND_TOPMOST（-1）因此被截斷成無效的
# 視窗代碼，函式直接失敗、而且不會設定 LastError，從外面完全看不出哪裡有問題
# ——「釘選最上層按鈕有反應但視窗沒有置頂」就是這樣來的。
if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32

    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,      # hWnd
        wintypes.HWND,      # hWndInsertAfter
        ctypes.c_int,       # X
        ctypes.c_int,       # Y
        ctypes.c_int,       # cx
        ctypes.c_int,       # cy
        ctypes.c_uint,      # uFlags
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL

    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

    _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowDisplayAffinity.restype = wintypes.BOOL

    _shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
    _shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT

    _shell32.SHChangeNotify.argtypes = [
        ctypes.c_long,      # wEventId
        ctypes.c_uint,      # uFlags
        ctypes.c_void_p,    # dwItem1
        ctypes.c_void_p,    # dwItem2
    ]
    _shell32.SHChangeNotify.restype = None

    _kernel32 = ctypes.windll.kernel32
    _ole32 = ctypes.windll.ole32

    # SHParseDisplayName / SHOpenFolderAndSelectItems（「在檔案總管中顯示」用）。
    # PIDL 是 64-bit 指標，一樣必須宣告 argtypes，否則會被截斷（見檔頭說明）。
    _shell32.SHParseDisplayName.argtypes = [
        wintypes.LPCWSTR,               # pszName
        ctypes.c_void_p,                # pbc
        ctypes.POINTER(ctypes.c_void_p),  # ppidl
        ctypes.c_ulong,                 # sfgaoIn
        ctypes.POINTER(ctypes.c_ulong),   # psfgaoOut
    ]
    _shell32.SHParseDisplayName.restype = ctypes.c_long
    _shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,                # pidlFolder
        ctypes.c_uint,                  # cidl
        ctypes.c_void_p,                # apidl
        wintypes.DWORD,                 # dwFlags
    ]
    _shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.restype = None

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    # WindowFromPoint 的 POINT 是「傳值」的結構參數，不是指標。沒有 argtypes 的話
    # ctypes 會把它拆成兩個 int 推上堆疊，函式讀到的座標是垃圾、回傳的視窗代碼
    # 也是垃圾——和檔頭說的 HWND 截斷是同一類問題，一樣不會設定 LastError。
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND

    # window_stack_at 用的：沿 z-order 往下走、逐一判斷視窗是否真的蓋在那個點上
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    for _name in ("IsWindowVisible", "IsIconic"):
        getattr(_user32, _name).argtypes = [wintypes.HWND]
        getattr(_user32, _name).restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    # DwmGetWindowAttribute：cloaked（別的虛擬桌面、暫停的 UWP）與不含隱形邊框的
    # 真實外框。dwmapi 在支援的 Windows 上一定有；載不到就退回 GetWindowRect。
    try:
        _dwmapi = ctypes.windll.dwmapi
        _dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except OSError:  # pragma: no cover - 沒有 dwmapi 的環境
        _dwmapi = None
else:  # pragma: no cover - 只在非 Windows 平台走到
    wintypes = None
    _user32 = None
    _shell32 = None
    _kernel32 = None
    _dwmapi = None

# GetWindowLongPtrW 用的常數
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TRANSPARENT = 0x00000020

# GetAncestor 用的常數
_GA_ROOT = 2

# GetWindow 用的常數：z-order 往下的下一個視窗
_GW_HWNDNEXT = 2

# DwmGetWindowAttribute 用的常數
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14

# window_stack_at 最多往下走幾個視窗。桌面上的頂層視窗（含隱藏的輔助視窗）
# 通常幾十到兩三百個；這只是防止異常環境下每次滑鼠移動都掃到天荒地老。
_STACK_WALK_LIMIT = 512


def set_app_user_model_id(app_id: str = config.APP_USER_MODEL_ID) -> bool:
    """設定 AppUserModelID。

    無邊框視窗若沒有明確設定這個 ID，Windows 可能把它歸到 python.exe 底下，
    導致工作列圖示與分組錯誤。
    """
    if not IS_WINDOWS:
        return False
    try:
        _shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False


def set_topmost(window_id: int, enabled: bool) -> bool:
    """切換視窗是否置頂，回傳是否確實生效。

    比 Qt 的 setWindowFlag(WindowStaysOnTopHint) 好：後者在 Windows 上會重建
    原生視窗，造成畫面閃爍，而且容易掉失最大化狀態與焦點。

    回傳值會實際回頭檢查 WS_EX_TOPMOST，而不是只看 API 的回傳值——呼叫端要靠
    它決定是否改用 Qt 的旗標作為後備。
    """
    if not IS_WINDOWS or not window_id:
        return False
    try:
        handle = wintypes.HWND(int(window_id))
        insert_after = wintypes.HWND(_HWND_TOPMOST if enabled else _HWND_NOTOPMOST)
        _user32.SetWindowPos(
            handle,
            insert_after,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
        return is_topmost(window_id) == bool(enabled)
    except Exception:
        return False


def get_display_affinity(window_id: int) -> int:
    """視窗目前的擷取親和性（WDA_*）；查不到回 -1。"""
    if not IS_WINDOWS or not window_id:
        return -1
    try:
        value = wintypes.DWORD(0)
        if not _user32.GetWindowDisplayAffinity(wintypes.HWND(int(window_id)), ctypes.byref(value)):
            return -1
        return int(value.value)
    except Exception:
        return -1


def set_capture_excluded(window_id: int, excluded: bool) -> int:
    """讓錄影、截圖、螢幕分享擷取不到這個視窗（或恢復）。回傳實際生效的 WDA_* 值，失敗回 -1。

    排除時先試 WDA_EXCLUDEFROMCAPTURE（擷取畫面裡視窗完全不存在，看得到後面的
    東西），舊版 Windows 不認得就退回 WDA_MONITOR（擷取畫面裡是一塊黑）。回傳值
    一律回頭讀實際狀態，不信 API 的 BOOL——呼叫端靠它決定按鈕要不要亮、狀態列
    要怎麼說，不能讓 UI 說謊。

    只對頂層視窗有效；Qt 的子元件沒有自己的原生視窗，跟著頂層一起被排除。
    原生視窗被重建（setWindowFlag）後設定會掉，呼叫端要重套。
    """
    if not IS_WINDOWS or not window_id:
        return -1
    try:
        handle = wintypes.HWND(int(window_id))
        wanted = (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR) if excluded else (WDA_NONE,)
        for value in wanted:
            if _user32.SetWindowDisplayAffinity(handle, value):
                break
        return get_display_affinity(window_id)
    except Exception:
        return -1


def is_topmost(window_id: int) -> bool:
    """查詢視窗目前是否具有 WS_EX_TOPMOST 樣式。"""
    if not IS_WINDOWS or not window_id:
        return False
    try:
        style = _user32.GetWindowLongPtrW(wintypes.HWND(int(window_id)), _GWL_EXSTYLE)
        return bool(style & _WS_EX_TOPMOST)
    except Exception:
        return False


def top_level_hwnd_at(x: int, y: int) -> int:
    """回傳原生座標 (x, y) 底下最上層視窗的 HWND，問不出來回 0。

    要繞過 Qt 自己的 QApplication.topLevelAt，是因為它在 Windows 上走的是
    ChildWindowFromPointEx(desktop, pt, CWP_SKIPINVISIBLE)——只跳過隱藏的視窗，
    不跳過 WS_EX_TRANSPARENT 的視窗，於是拖曳幽靈（相對跟隨之後永遠蓋在游標
    正下方的那個頂層視窗）會被當成命中結果。WindowFromPoint 會跳過
    WS_EX_TRANSPARENT，游標壓在幽靈上時仍然答得出底下真正的視窗。
    實測 60 個落在幽靈內的點：topLevelAt 抓到幽靈 60/60，這裡 0/60。
    """
    if not IS_WINDOWS:
        return 0
    try:
        handle = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not handle:
            return 0
        # 回傳的可能是子視窗（有些元件有自己的原生視窗），要爬到頂層才對得上
        # QWidget.find——它只認得 Qt 頂層視窗的代碼
        return int(_user32.GetAncestor(handle, _GA_ROOT) or handle)
    except Exception:
        return 0


def _covers_point(handle, point) -> bool:
    """這個頂層視窗此刻是否真的蓋在原生座標 point 上。

    問的是「看不看得到」，不是「收不收輸入」：WindowFromPoint 會跳過停用的
    視窗，這裡不跳——停用的視窗照樣畫在畫面上、照樣遮住底下的分頁列。
    其餘複刻 WindowFromPoint 的排除規則：隱藏、最小化、WS_EX_TRANSPARENT
    （拖曳縮影就是這種，見 tab_bar.DragGhost）都不算。再加兩條它不需要而
    這裡需要的：cloaked（在別的虛擬桌面、或被系統暫停的 UWP——
    IsWindowVisible 仍是真）不算；外框用 DWM 的真實邊界，別的程式的視窗在
    Windows 10/11 四周各有七八個像素的隱形縮放邊框，用 GetWindowRect 會把
    那圈也算成「蓋住」。
    """
    if not _user32.IsWindowVisible(handle) or _user32.IsIconic(handle):
        return False
    if _user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE) & _WS_EX_TRANSPARENT:
        return False
    rect = wintypes.RECT()
    if _dwmapi is not None:
        cloaked = wintypes.DWORD(0)
        if _dwmapi.DwmGetWindowAttribute(
            handle, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        ) == 0 and cloaked.value:
            return False
        if _dwmapi.DwmGetWindowAttribute(
            handle, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        ) != 0 and not _user32.GetWindowRect(handle, ctypes.byref(rect)):
            return False
    elif not _user32.GetWindowRect(handle, ctypes.byref(rect)):  # pragma: no cover
        return False
    return rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom


def window_stack_at(x: int, y: int) -> list[tuple[int, bool]] | None:
    """原生座標 (x, y) 底下由上而下的頂層視窗：[(HWND, 是否本行程), ...]。

    給「看穿自家視窗本體」的合併命中用：拖曳合併不該只看最上層那一個，
    來源視窗（或第三個自家視窗）的本體蓋住了別人的分頁列時，底下那條分頁列
    仍然是合法的合併目標。從 WindowFromPoint 答出的視窗起沿 z-order 往下走
    （GetWindow GW_HWNDNEXT 在頂層視窗之間就是 z-order），每個都用
    _covers_point 過濾。走到第一個**別的行程**的視窗就停，它也放進清單——
    被別的程式蓋住的分頁列是真的看不到，呼叫端看到它就不再往下找。
    問不出來回 None，呼叫端退回既有的幾何掃描。
    """
    if not IS_WINDOWS:
        return None
    try:
        point = wintypes.POINT(int(x), int(y))
        handle = _user32.WindowFromPoint(point)
        if not handle:
            return None
        handle = _user32.GetAncestor(handle, _GA_ROOT) or handle
        own_pid = _kernel32.GetCurrentProcessId()
        stack: list[tuple[int, bool]] = []
        first = True
        for _ in range(_STACK_WALK_LIMIT):
            if not handle:
                break
            # 第一個是 WindowFromPoint 親自答的，已經套過它的規則，不必再過濾
            if first or _covers_point(handle, point):
                pid = wintypes.DWORD(0)
                _user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                ours = pid.value == own_pid
                stack.append((int(handle), ours))
                if not ours:
                    break
            first = False
            handle = _user32.GetWindow(handle, _GW_HWNDNEXT)
        return stack
    except Exception:
        return None


def force_foreground(window_id: int) -> bool:
    """把視窗搶到前景。

    Windows 會擋掉非前景行程的 SetForegroundWindow，導致「雙擊檔案後視窗只在
    工作列閃爍」。把本執行緒暫時附加到目前前景視窗的輸入佇列就能繞過這個限制，
    這是處理單一實例喚醒時的標準作法。
    """
    if not IS_WINDOWS or not window_id:
        return False
    try:
        handle = wintypes.HWND(int(window_id))
        foreground = _user32.GetForegroundWindow()
        current = _kernel32.GetCurrentThreadId()
        target = _user32.GetWindowThreadProcessId(foreground, None)
        attached = False
        if target and target != current:
            attached = bool(_user32.AttachThreadInput(current, target, True))
        _user32.SetForegroundWindow(handle)
        if attached:
            _user32.AttachThreadInput(current, target, False)
        return True
    except Exception:
        return False


def reveal_in_explorer(path: str) -> bool:
    """在檔案總管中開啟檔案所在的資料夾，並把該檔案選取起來。

    用 SHOpenFolderAndSelectItems 而不是 `explorer /select,<path>`：
    後者對含逗號的路徑會解析錯誤，而且每次都硬開一個新視窗；
    Shell API 會重用已開著同一個資料夾的視窗，行為和檔案總管右鍵的
    「開啟檔案位置」一致。
    """
    if not IS_WINDOWS or not path:
        return False
    try:
        import os as _os

        target = _os.path.abspath(path)
        if not _os.path.exists(target):
            return False
        _ole32.CoInitialize(None)
        try:
            pidl = ctypes.c_void_p()
            flags = ctypes.c_ulong(0)
            result = _shell32.SHParseDisplayName(
                target, None, ctypes.byref(pidl), 0, ctypes.byref(flags)
            )
            if result != 0 or not pidl.value:
                return False
            try:
                return _shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
            finally:
                _ole32.CoTaskMemFree(pidl)
        finally:
            _ole32.CoUninitialize()
    except Exception:
        return False


def notify_association_changed() -> bool:
    """通知檔案總管重新讀取副檔名關聯，讓圖示立即更新。"""
    if not IS_WINDOWS:
        return False
    try:
        _shell32.SHChangeNotify(_SHCNE_ASSOCCHANGED, _SHCNF_IDLIST, None, None)
        return True
    except Exception:
        return False
