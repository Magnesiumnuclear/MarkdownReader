"""註冊／移除 .md 檔案關聯（只寫入 HKCU，不需系統管理員權限）。

執行後，Markdown 檔案的「開啟檔案」清單裡就會出現本程式，圖示也會套用。

用法：
    py -3.13 tools/install_association.py                 # 自動找 dist\\MarkdownReader.exe，找不到就用開發模式
    py -3.13 tools/install_association.py --target "D:\\app\\MarkdownReader.exe"
    py -3.13 tools/install_association.py --set-default   # 一併嘗試設為預設開啟程式
    py -3.13 tools/install_association.py --uninstall     # 完整移除

【關於「預設開啟程式」】
Windows 10/11 以 UserChoice 雜湊保護預設程式設定，第三方程式無法單靠寫入
登錄檔強制指定。若 .md 先前已被其他程式關聯，--set-default 不會生效，請改用
    右鍵 .md 檔 → 開啟檔案 → 選擇其他應用程式 → 勾選「一律使用此應用程式」
此時清單中已經有本程式（因為 ProgID 已註冊完成）。
"""

from __future__ import annotations

import argparse
import os
import sys
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, win32  # noqa: E402

PROG_ID = "MarkdownReader.md"
PROG_DESCRIPTION = "Markdown 文件"
CLASSES = r"Software\Classes"
# 只關聯 Markdown 專用副檔名，不動 .txt 以免影響記事本
EXTENSIONS = config.MARKDOWN_SUFFIXES


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_target(explicit: str | None) -> tuple[str, str]:
    """決定要註冊的執行指令，回傳 (顯示用說明, 完整命令列樣板)。"""
    if explicit:
        exe = os.path.abspath(explicit)
        if not os.path.isfile(exe):
            raise SystemExit(f"找不到指定的執行檔：{exe}")
        return exe, f'"{exe}" "%1"'

    root = _project_root()
    packaged = os.path.join(root, "dist", "MarkdownReader.exe")
    if os.path.isfile(packaged):
        return packaged, f'"{packaged}" "%1"'

    # 開發模式：用 pythonw.exe 執行 main.py，才不會跳出主控台視窗
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    script = os.path.join(root, "main.py")
    return f"{pythonw} {script}", f'"{pythonw}" "{script}" "%1"'


def _icon_source(target: str) -> str:
    """圖示來源：打包後用 exe 內嵌圖示，開發模式用 assets\\app.ico。"""
    if target.lower().endswith(".exe") and os.path.isfile(target):
        return f"{target},0"
    ico = os.path.join(_project_root(), "assets", "app.ico")
    return f"{ico},0"


def _set(key_path: str, value: str, name: str = "") -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _delete_tree(key_path: str) -> bool:
    """遞迴刪除登錄機碼（winreg 沒有現成的遞迴刪除）。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
        ) as key:
            children = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
    except FileNotFoundError:
        return False

    for child in children:
        _delete_tree(f"{key_path}\\{child}")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    return True


def install(target: str, command: str, set_default: bool) -> None:
    prog_key = f"{CLASSES}\\{PROG_ID}"
    _set(prog_key, PROG_DESCRIPTION)
    _set(f"{prog_key}\\DefaultIcon", _icon_source(target))
    _set(f"{prog_key}\\shell\\open", "以 Markdown 閱讀器開啟(&M)")
    _set(f"{prog_key}\\shell\\open\\command", command)

    print(f"已註冊 ProgID：HKCU\\{prog_key}")
    print(f"  開啟指令：{command}")
    print(f"  圖示    ：{_icon_source(target)}")

    for suffix in EXTENSIONS:
        # 加進「開啟檔案」清單，不覆寫使用者原本的預設程式
        _set(f"{CLASSES}\\{suffix}\\OpenWithProgids", "", name=PROG_ID)
        print(f"  已加入 {suffix} 的開啟方式清單")

    if set_default:
        for suffix in EXTENSIONS:
            _set(f"{CLASSES}\\{suffix}", PROG_ID)
        print(
            "\n已嘗試設為預設開啟程式。\n"
            "若雙擊後仍由其他程式開啟，代表 Windows 的 UserChoice 設定優先，\n"
            "請改用：右鍵 .md → 開啟檔案 → 選擇其他應用程式 → 勾選「一律使用此應用程式」。"
        )

    win32.notify_association_changed()
    print("\n完成。已通知檔案總管重新整理關聯設定。")


def uninstall() -> None:
    removed = _delete_tree(f"{CLASSES}\\{PROG_ID}")
    print(("已移除 ProgID" if removed else "找不到 ProgID，略過") + f"：{PROG_ID}")

    for suffix in EXTENSIONS:
        path = f"{CLASSES}\\{suffix}\\OpenWithProgids"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS
            ) as key:
                winreg.DeleteValue(key, PROG_ID)
                print(f"  已從 {suffix} 的開啟方式清單移除")
        except FileNotFoundError:
            continue
        except OSError:
            continue

        # 若這個副檔名的預設值指向本程式，一併清掉
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, f"{CLASSES}\\{suffix}", 0, winreg.KEY_ALL_ACCESS
            ) as key:
                current, _type = winreg.QueryValueEx(key, "")
                if current == PROG_ID:
                    winreg.DeleteValue(key, "")
                    print(f"  已清除 {suffix} 的預設關聯")
        except (FileNotFoundError, OSError):
            pass

    win32.notify_association_changed()
    print("\n完成。所有變更只影響目前使用者（HKCU）。")


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("這個腳本只能在 Windows 上執行。")

    parser = argparse.ArgumentParser(description="註冊或移除 .md 檔案關聯（僅 HKCU）")
    parser.add_argument("--target", help="要註冊的執行檔路徑（預設自動偵測）")
    parser.add_argument(
        "--set-default", action="store_true", help="一併嘗試設為預設開啟程式"
    )
    parser.add_argument("--uninstall", action="store_true", help="移除所有註冊內容")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
        return 0

    target, command = resolve_target(args.target)
    install(target, command, args.set_default)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
