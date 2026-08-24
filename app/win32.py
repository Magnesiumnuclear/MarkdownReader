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


def set_app_user_model_id(app_id: str = config.APP_USER_MODEL_ID) -> bool:
    """設定 AppUserModelID。

    無邊框視窗若沒有明確設定這個 ID，Windows 可能把它歸到 python.exe 底下，
    導致工作列圖示與分組錯誤。
    """
    if not IS_WINDOWS:
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False


def set_topmost(window_id: int, enabled: bool) -> bool:
    """切換視窗是否置頂。

    比 Qt 的 setWindowFlag(WindowStaysOnTopHint) 好：後者在 Windows 上會重建
    原生視窗，造成畫面閃爍，而且容易掉失最大化狀態與焦點。
    """
    if not IS_WINDOWS or not window_id:
        return False
    try:
        insert_after = _HWND_TOPMOST if enabled else _HWND_NOTOPMOST
        return bool(
            ctypes.windll.user32.SetWindowPos(
                int(window_id),
                insert_after,
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        )
    except Exception:
        return False


def notify_association_changed() -> bool:
    """通知檔案總管重新讀取副檔名關聯，讓圖示立即更新。"""
    if not IS_WINDOWS:
        return False
    try:
        ctypes.windll.shell32.SHChangeNotify(
            _SHCNE_ASSOCCHANGED, _SHCNF_IDLIST, None, None
        )
        return True
    except Exception:
        return False
