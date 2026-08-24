"""Markdown 閱讀器進入點。

Windows 在雙擊 .md 檔時，會以「檔案的絕對路徑」作為第一個命令列參數啟動
本程式，因此這裡直接取 sys.argv[1] 當作要開啟的檔案。
沒有參數時（例如直接執行 exe）會顯示歡迎頁，而不是直接結束。

用法：
    py -3.13 main.py                     顯示歡迎頁
    py -3.13 main.py "D:\\docs\\note.md"   開啟指定檔案
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

# PyQt6.QtSvg 在程式中是透過 app.icons 間接使用。PyInstaller 以靜態分析判斷
# 相依，精簡打包時很容易漏掉 SVG 的底層插件（qsvg.dll / qsvgicon.dll），
# 導致「原始碼執行時圖示正常、打包後全部空白」。這裡顯式匯入以確保被收錄。
import PyQt6.QtSvg  # noqa: F401

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from app import config, icons, resources, win32
from app.viewer import MarkdownViewer


def _log_path() -> str:
    return os.path.join(resources.user_data_dir(), "error.log")


def _install_exception_hook() -> None:
    """攔截未處理的例外。

    以 --windowed 打包後沒有主控台，未攔截的例外會讓程式靜默閃退，
    使用者完全看不到原因，因此一律寫入紀錄檔並跳出對話框。
    """

    def handle(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        detail = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        try:
            with open(_log_path(), "a", encoding="utf-8") as handle_file:
                handle_file.write(
                    f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{detail}"
                )
        except OSError:
            pass

        if QApplication.instance() is not None:
            box = QMessageBox()
            box.setWindowTitle(f"{config.APP_DISPLAY_NAME} — 發生未預期的錯誤")
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText("程式發生未預期的錯誤，但已被攔截。")
            box.setInformativeText(f"錯誤紀錄已寫入：\n{_log_path()}")
            box.setDetailedText(detail)
            box.exec()
        else:
            sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle


def _target_path(argv: list[str]) -> str | None:
    """從命令列參數取出要開啟的檔案路徑。"""
    for argument in argv[1:]:
        if argument.startswith("-"):
            continue
        # 相對路徑（手動執行時可能出現）一律正規化為絕對路徑
        return os.path.abspath(argument)
    return None


def main() -> int:
    _install_exception_hook()

    # 讓工作列正確辨識為獨立應用程式（無邊框視窗尤其需要）
    win32.set_app_user_model_id()

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_DISPLAY_NAME)
    app.setOrganizationName(config.ORG_NAME)
    app.setWindowIcon(icons.app_icon())

    viewer = MarkdownViewer(_target_path(sys.argv))
    # 讓 Qt 在視窗關閉時就地銷毀它。否則視窗會活到直譯器結束後才被拆除，
    # 那時 QApplication 可能已經先一步消失，Qt 內部就會踩到已釋放的記憶體
    # （實測會有約六成機率在結束時發生存取違規）。
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
