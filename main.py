"""Markdown 閱讀器進入點。

Windows 在雙擊 .md 檔時，會以「檔案的絕對路徑」作為第一個命令列參數啟動
本程式，因此這裡直接取 sys.argv[1] 當作要開啟的檔案。
沒有參數時（例如直接執行 exe）會顯示歡迎頁，而不是直接結束。

程式採單一實例：若已經有一個在跑，新啟動的行程會把路徑透過具名管道交給它，
在既有視窗開成新分頁，然後自己結束。

【為什麼所有重量級匯入都寫在 main() 裡面】
雙擊第二個 .md 檔時，新行程唯一要做的事就是「把路徑送出去然後結束」，
使用者感受到的延遲＝這段轉交的時間。若在模組頂層匯入 QtWidgets / QtGui /
app.viewer（連帶 markdown、pygments），即使根本不會開視窗也得先付這些錢。
QLocalSocket 只需要 QtCore + QtNetwork，而且不需要 QApplication 就能運作，
因此把轉交放在最前面、其餘匯入延後到確定要開視窗之後。

用法：
    py -3.13 main.py                     顯示歡迎頁
    py -3.13 main.py "D:\\docs\\note.md"   開啟指定檔案
    py -3.13 main.py a.md b.md c.md      一次開多個（只有第一個會立刻渲染）
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

from app import config


def _log_path() -> str:
    from app import resources

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

        # 轉交路徑上根本沒載入 QtWidgets，這時不該為了顯示對話框把它拉進來，
        # 否則一個小失誤會變成「等好幾百毫秒才看到錯誤」。
        if "PyQt6.QtWidgets" in sys.modules:
            from PyQt6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                from app.language import t

                box.setWindowTitle(
                    t("dialog.crash.title", app=t("app.displayName"))
                )
                box.setIcon(QMessageBox.Icon.Critical)
                box.setText(t("dialog.crash.text"))
                box.setInformativeText(t("dialog.crash.detail", path=_log_path()))
                box.setDetailedText(detail)
                box.exec()
                return

        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle


def _target_paths(argv: list[str]) -> list[str]:
    """從命令列參數取出要開啟的檔案路徑（可以有多個）。

    以前只取第一個，於是 `MarkdownReader.exe a.md b.md` 會安靜地丟掉 b.md。
    相對路徑（手動執行時可能出現）一律正規化為絕對路徑。
    """
    return [
        os.path.abspath(argument)
        for argument in argv[1:]
        if not argument.startswith("-")
    ]


def main() -> int:
    _install_exception_hook()

    targets = _target_paths(sys.argv)
    target = targets[0] if targets else None

    # === 轉交路徑（不開視窗）================================================
    # 這之前不要放任何 QtGui / QtWidgets 的匯入，見模組開頭說明。
    from app import single_instance

    if single_instance.send_all_to_existing(targets):
        return 0

    # === 開視窗路徑 ==========================================================
    # PyQt6.QtSvg 在程式中是透過 app.icons 間接使用。PyInstaller 以靜態分析判斷
    # 相依，精簡打包時很容易漏掉 SVG 的底層插件（qsvg.dll / qsvgicon.dll），
    # 導致「原始碼執行時圖示正常、打包後全部空白」。這裡顯式匯入以確保被收錄，
    # build.spec 的 hiddenimports 也有一份，兩道保險。
    import PyQt6.QtSvg  # noqa: F401

    from PyQt6.QtWidgets import QApplication

    from app import icons, win32
    from app.window_manager import WindowManager

    # 讓工作列正確辨識為獨立應用程式（無邊框視窗尤其需要）
    win32.set_app_user_model_id()

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    # 語言要在 setApplicationDisplayName 之前解析：產品名稱本身會隨語言變。
    # 【windowTitle 一定要以 applicationDisplayName 結尾】Qt 在 Windows 上
    # 若發現視窗標題沒有以 displayName 收尾，會自動補一段 " - <displayName>"。
    # 這裡與 viewer._update_titles 的後綴必須是同一個 t("app.displayName")，
    # 否則工作列會出現「note.md — Markdown Reader - Markdown 閱讀器」。
    from app import language

    from app import resources

    # 設定存哪裡（登錄檔或可攜版的 ini）由 resources.make_settings 一處決定
    language.init_from_settings(resources.make_settings())
    app.setApplicationDisplayName(language.t("app.displayName"))
    app.setOrganizationName(config.ORG_NAME)
    app.setWindowIcon(icons.app_icon())

    server = single_instance.InstanceServer()
    if not server.listen():
        # 監聽不起來（權限或環境限制）就退化成各自開視窗，功能不受影響
        server = None

    # 多視窗管理器：分頁可以拖出去拆成新視窗、拖回來合併（見 window_manager.py）。
    # 視窗各自帶 WA_DeleteOnClose（在 create_window 裡設），關閉即就地銷毀——
    # 否則視窗會活到直譯器結束後才被拆除，那時 QApplication 可能已先消失，
    # Qt 內部會踩到已釋放的記憶體（實測約六成機率在結束時存取違規）。
    manager = WindowManager(parent=app)
    if server is not None:
        # 【不要把 server 掛在視窗底下】
        # 視窗帶著 WA_DeleteOnClose，關閉時 Qt 會連同子物件一起銷毀，
        # QLocalServer 的 C++ 物件就跟著沒了；app.exec() 之後的 server.close()
        # 一碰就是「wrapped C/C++ object has been deleted」。掛在 app 底下，
        # 生命週期才會涵蓋 close() 之後，直到 main() 返回。
        server.setParent(app)
        # 轉交進來的路徑由管理器路由到最後作用中的視窗
        server.pathReceived.connect(manager.route_external_open)
        # 最後一個視窗關閉的瞬間就停止監聽：此時行程正在退出，晚一步送進來的
        # 雙擊若還連得上管道，檔案會被一個垂死的行程吞掉、什麼都不開。
        # 管道一關，新行程連不上就會自己開視窗——正確的退化。close 可重入，
        # 與 app.exec() 之後那次不衝突。
        app.lastWindowClosed.connect(server.close)
    window = manager.create_window(target)
    # 命令列一次給了好幾個檔案：第一個已經由 create_window 載入並顯示，
    # 其餘只建延後載入的分頁，不必為了「開起來」就先渲染十份文件。
    if len(targets) > 1:
        window.open_paths(targets[1:], activate_first=False)
    if targets:
        # 【冷啟動多選】沒有實例在跑時多選 N 個檔案，第一個是轉交器放在命令列上、
        # 由這裡開的，沒有經過 route_external_open；其餘 N-1 個等管道開起來才
        # 轉交進來。不在這裡種下連發狀態的話，第一條轉交會被當成「新的一批」而
        # 切過去——十個檔案開起來停在第二個。時間要從「第一份文件渲染完」起算，
        # 所以這行必須在 create_window 之後。
        manager.note_batch_started(window)

    exit_code = app.exec()
    if server is not None:
        server.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
