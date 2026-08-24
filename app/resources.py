"""資源路徑解析。

開發時資源位於專案目錄；經 PyInstaller 打包後（--onefile）資源會被解壓到
sys._MEIPASS 指向的暫存目錄，因此所有讀取資源的程式碼都必須經過這裡。
"""

from __future__ import annotations

import os
import sys


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


def user_data_dir() -> str:
    """回傳可寫入的使用者資料目錄（存放錯誤紀錄）。"""
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(root, "MarkdownReader")
    os.makedirs(path, exist_ok=True)
    return path
