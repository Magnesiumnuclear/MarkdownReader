"""系統深淺色主題偵測。

主要走 Qt 6.5+ 的 QStyleHints.colorScheme()，它在 Windows 上直接對應
「設定 → 個人化 → 色彩 → 選擇您的預設應用程式模式」，而且會在使用者切換時
發出 colorSchemeChanged 訊號，可以做到即時同步。

若 Qt 因版本或平台回報 Unknown，就退回直接讀登錄檔的
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
        AppsUseLightTheme   0 = 深色, 1 = 淺色   <- 應用程式看的是這個值
        SystemUsesLightTheme                     <- 工作列與開始功能表，不使用
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication

from . import config

_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def _from_registry() -> str | None:
    """從登錄檔判斷系統配色；讀不到時回傳 None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH) as key:
            apps_use_light, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if apps_use_light else "dark"
    except (OSError, ValueError):
        return None


def system_color_scheme() -> str:
    """回傳目前系統配色："light" 或 "dark"。"""
    app = QGuiApplication.instance()
    if app is not None:
        hints = app.styleHints()
        if hasattr(hints, "colorScheme"):
            scheme = hints.colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"

    from_registry = _from_registry()
    if from_registry is not None:
        return from_registry
    return config.DEFAULT_THEME


def resolve(mode: str) -> str:
    """把主題模式換算成實際要套用的主題。

    mode 為 "light" / "dark" 時直接沿用；"system" 則以目前的系統配色為準。
    """
    if mode in ("light", "dark"):
        return mode
    return system_color_scheme()


def connect_system_changes(handler) -> bool:
    """訂閱系統配色變更。回傳是否成功連接。

    Windows 切換深淺色時 Qt 會發出這個訊號，讓「跟隨系統」能即時反應，
    不需要使用者重開程式。

    handler 必須是某個 QObject 的 bound method，直接連接它（而不是包一層
    lambda）Qt 才會把該物件當成連線的接收端，在它被銷毀時自動斷開。
    包 lambda 的話，訊號來源是 QApplication 的 styleHints，生命週期比視窗長，
    會在關閉程式時留下指向已刪除物件的連線而造成存取違規。
    """
    app = QGuiApplication.instance()
    if app is None:
        return False
    hints = app.styleHints()
    signal = getattr(hints, "colorSchemeChanged", None)
    if signal is None:
        return False
    signal.connect(handler)
    return True
