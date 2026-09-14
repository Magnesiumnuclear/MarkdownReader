"""資源路徑解析，以及「設定與紀錄存在哪裡」的唯一決定點。

開發時資源位於專案目錄；經 PyInstaller 打包後（--onefile）資源會被解壓到
sys._MEIPASS 指向的暫存目錄，因此所有讀取資源的程式碼都必須經過這裡。

【可攜模式】exe 旁邊有 portable.txt（可攜版 zip 附的標記檔）時，設定改存
<exe 目錄>\\data\\settings.ini、錯誤紀錄存 <exe 目錄>\\data\\error.log，拔掉
隨身碟不留痕跡；刪掉標記檔就回到一般模式（登錄檔＋ %LOCALAPPDATA%）。
環境變數 MDREADER_PORTABLE_ROOT 可強制指定可攜根目錄，給測試與從原始碼跑用。

【這個模組在 import 期必須是葉節點】QSettings 與 config 只在函式裡匯入。
main.py 的轉交快路徑（第二次雙擊 .md）只載入 app.config 與 app.single_instance，
崩潰紀錄的路徑（main._log_path → user_data_dir）也不能碰 QtCore；頂層多一個
import 就會把那兩條路徑拖慢或拖進 Qt。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

_PORTABLE_ENV = "MDREADER_PORTABLE_ROOT"
_PORTABLE_MARKER = "portable.txt"
_PORTABLE_DATA_DIRNAME = "data"
_APP_DIRNAME = "MarkdownReader"
# 「可攜目錄不可寫、退回一般模式」那一行 error.log 只記一次，
# 不然每次存取設定都往紀錄裡灌同一句。
_fallback_logged = False


def base_path() -> str:
    """回傳資源根目錄（打包後為 PyInstaller 的暫存解壓目錄）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return str(meipass)
    # 本檔案位於 <專案根>/app/resources.py，往上一層即專案根目錄
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts: str) -> str:
    """組出資源的絕對路徑，例如 resource_path("assets", "icons", "close.svg")。"""
    return os.path.join(base_path(), *parts)


def icon_path(name: str) -> str:
    """取得 assets/icons/<name>.svg 的絕對路徑。"""
    return resource_path("assets", "icons", f"{name}.svg")


def portable_root() -> str | None:
    """可攜模式的根目錄；不是可攜模式回 None。

    環境變數最優先（不看 frozen、不看標記檔）；其次是打包後 exe 旁邊有標記檔。
    從原始碼跑、沒設環境變數，一律是一般模式，行為與加這個功能之前完全相同。
    """
    override = os.environ.get(_PORTABLE_ENV)
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.isfile(os.path.join(exe_dir, _PORTABLE_MARKER)):
            return exe_dir
    return None


def _normal_data_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(root, _APP_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _portable_data_dir() -> str | None:
    """可攜且 data 目錄確實寫得進去才回它；否則退回一般模式並在一般位置記一行。

    用探針檔真的寫一次，不信 os.access：唯讀媒介上 makedirs 可能不報錯
    （目錄早就在）。探針檔名帶 PID，兩個可攜行程同時探同一個目錄才不會互刪；
    刪不掉探針不算失敗——能寫進去就已經證明可寫。
    """
    root = portable_root()
    if root is None:
        return None
    data = os.path.join(root, _PORTABLE_DATA_DIRNAME)
    try:
        os.makedirs(data, exist_ok=True)
        probe = os.path.join(data, f".write_probe.{os.getpid()}")
        with open(probe, "w", encoding="utf-8"):
            pass
        try:
            os.remove(probe)
        except OSError:
            pass
        return data
    except OSError as exc:
        global _fallback_logged
        if not _fallback_logged:
            _fallback_logged = True
            try:
                log = os.path.join(_normal_data_dir(), "error.log")
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(
                        f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
                        f"可攜模式的資料目錄無法寫入（{data}），已退回一般模式：{exc}\n"
                    )
            except OSError:
                pass
        return None


def user_data_dir() -> str:
    """回傳可寫入的使用者資料目錄（存放錯誤紀錄）。可攜模式下是 <根>\\data。"""
    return _portable_data_dir() or _normal_data_dir()


def make_settings():
    """全專案唯一的 QSettings 建構點。

    可攜模式回 <根>\\data\\settings.ini（IniFormat），否則回登錄式
    QSettings(ORG_NAME, APP_NAME)。兩者對 QByteArray（視窗幾何）與清單
    （分頁清單）的存回讀回都一致，呼叫端不必分辨。
    """
    from PyQt6.QtCore import QSettings

    from . import config

    data = _portable_data_dir()
    if data is not None:
        return QSettings(os.path.join(data, "settings.ini"), QSettings.Format.IniFormat)
    return QSettings(config.ORG_NAME, config.APP_NAME)
