"""Windows 原生 API 的薄封裝（ctypes）。

只包含幾個 Qt 無法妥善處理、或原生作法明顯較佳的操作。
所有函式在非 Windows 平台或呼叫失敗時都會安靜地回傳 False，不影響主流程。
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
else:  # pragma: no cover - 只在非 Windows 平台走到
    wintypes = None
    _user32 = None
    _shell32 = None
    _kernel32 = None

# GetWindowLongPtrW 用的常數
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008


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


def is_topmost(window_id: int) -> bool:
    """查詢視窗目前是否具有 WS_EX_TOPMOST 樣式。"""
    if not IS_WINDOWS or not window_id:
        return False
    try:
        style = _user32.GetWindowLongPtrW(wintypes.HWND(int(window_id)), _GWL_EXSTYLE)
        return bool(style & _WS_EX_TOPMOST)
    except Exception:
        return False


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
