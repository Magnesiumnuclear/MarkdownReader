# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包設定。

用法（單一 exe、隱藏主控台）：
    py -3.13 -m PyInstaller --noconfirm --clean build.spec

要改成啟動較快的資料夾版，把下方的 ONEFILE 改成 False 再重新打包。

【檔名為什麼是 build.spec 而不是 MarkdownReader.spec】
用 CLI 形式打包（帶 --name MarkdownReader）時，PyInstaller 會自動產生一份
MarkdownReader.spec 並覆蓋同名檔案。取名 build.spec 就不會被蓋掉。
"""

import os

# True  = 單一 exe（方便散布，但每次啟動都要把整包解壓到暫存目錄）
# False = 資料夾版（不需解壓，啟動快很多，適合設成 .md 的預設開啟程式）
#
# 可用環境變數覆寫，不必改檔案：
#     $env:MDREADER_ONEDIR = "1"; py -3.13 -m PyInstaller --noconfirm --clean build.spec
ONEFILE = os.environ.get("MDREADER_ONEDIR", "") != "1"

import re

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


def _read_version() -> tuple:
    """版本單一來源 app/__init__.py；只接受純數字三段（版本資源只吃數字）。"""
    root = globals().get("SPECPATH") or os.getcwd()
    with open(os.path.join(root, "app", "__init__.py"), encoding="utf-8") as handle:
        match = re.search(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', handle.read(), re.M)
    if not match:
        raise SystemExit("app/__init__.py 找不到純數字三段的 __version__（例如 1.0.0）")
    return tuple(int(part) for part in match.groups())


_VERSION = _read_version()
_VERSION_TEXT = "%d.%d.%d.0" % _VERSION
# exe 的版本資源（檔案總管「內容 › 詳細資料」看得到）。本體與安裝檔的版本都從
# 同一個 __version__ 來：安裝檔那邊由 build.ps1 -Installer 讀同一個檔再傳給 ISCC。
VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_VERSION + (0,), prodvers=_VERSION + (0,)),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "SamHo"),
                StringStruct("FileDescription", "Markdown Reader"),
                StringStruct("FileVersion", _VERSION_TEXT),
                StringStruct("ProductName", "Markdown Reader"),
                StringStruct("ProductVersion", _VERSION_TEXT),
                StringStruct("OriginalFilename", "MarkdownReader.exe"),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# 用不到的 Python 模組。
# 特別注意 PIL / numpy：pygments.formatters.img 會 import PIL，PIL 的 hook 又
# 把 numpy 一起拉進來，光這兩個就會讓 exe 多出約 12MB（含 20MB 的 OpenBLAS）。
# 本程式只用 pygments 產生 HTML，不會產生圖片格式輸出，排除掉完全沒有影響。
EXCLUDES = [
    "PIL",
    "numpy",
    "pyqtgraph",
    "tkinter",
    # OpenSSL 的三個使用者：_ssl 被 http.client／urllib／asyncio 這些用不到的標準庫
    # 拉進來（它們都用 try/except 包 import ssl，少了不會壞）；_hashlib 是 hashlib
    # 的 OpenSSL 後端，沒有它 hashlib.sha1 會退回內建實作，本程式只在 Mermaid 快取
    # 鍵用到，結果相同。三個一起排除，libcrypto／libssl 就沒有人再需要。
    "_ssl",
    "ssl",
    "_hashlib",
    "unittest",
    "pydoc_data",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuick3D",
    "PyQt6.QtQuickWidgets",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtBluetooth",
    "PyQt6.QtNfc",
    "PyQt6.QtPositioning",
    "PyQt6.QtSerialPort",
    "PyQt6.QtSql",
    "PyQt6.QtTest",
    "PyQt6.QtDesigner",
    "PyQt6.QtHelp",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
    "PyQt6.Qt3DCore",
    "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization",
]

# Qt 的 hook 會連帶複製一些本程式用不到的大型 DLL，這些要在 binaries 階段過濾。
#   opengl32sw.dll  20MB  Mesa 軟體 OpenGL 後備。純 widget 程式走 raster 繪製，
#                         不需要它；若日後在極舊的顯卡或虛擬機出現空白視窗，
#                         把這一行拿掉重新打包即可。
#   Qt6Pdf.dll       4.6MB QtPdf 模組，本程式沒有用到。
#   libcrypto/libssl 6.8MB 【來源不是 Qt，是 Python 自己的 _ssl 與 _hashlib】
#                         Python 的 DLLs\ 裡叫 libcrypto-3.dll，PyInstaller 收進來
#                         時會改名成 libcrypto-3-x64.dll——以前這裡寫的是改名前的
#                         名字，用完全比對，於是一個都沒濾掉，1.0.0 與 1.1.0 的安裝檔
#                         都白背了它。現在改用前綴比對，並在模組層就把 _ssl／ssl／
#                         _hashlib 排除（見 EXCLUDES）：本程式不連網、hashlib 沒有
#                         _hashlib 時會退回內建的 _sha1 等實作，功能不受影響。
#   plugins/tls      0.7MB QtNetwork 的 TLS 後端外掛（schannel／openssl），同理用不到。
#                         注意 Qt6Network.dll 本身不能排除，那是 QLocalSocket 的家。
EXCLUDE_BINARY_PREFIXES = (
    "opengl32sw",
    "qt6pdf",
    "libcrypto-",
    "libssl-",
)
EXCLUDE_BINARY_DIRS = (
    "pyqt6/qt6/plugins/tls/",
)

# Qt 的翻譯檔整包 6.6MB、96 個語言，程式只會載入 qtbase_zh_TW.qm（126KB）：
# language.install_qt_translator 只認 zh_TW，英文走源字串不需要 .qm。
KEEP_TRANSLATIONS = ("qtbase_zh_tw.qm",)


def _strip_binaries(binaries):
    """濾掉用不到的 DLL 與外掛（依前綴與目錄比對，不依賴改名前後的完整檔名）。"""
    kept = []
    for entry in binaries:
        dest = entry[0].replace("\\", "/").lower()
        name = os.path.basename(dest)
        if name.startswith(EXCLUDE_BINARY_PREFIXES):
            continue
        if any(folder in dest for folder in EXCLUDE_BINARY_DIRS):
            continue
        kept.append(entry)
    return kept


def _strip_datas(datas):
    """Qt 翻譯檔只留程式會載入的那一個。"""
    kept = []
    for entry in datas:
        dest = entry[0].replace("\\", "/").lower()
        if "/translations/" in dest and os.path.basename(dest) not in KEEP_TRANSLATIONS:
            continue
        kept.append(entry)
    return kept


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # SVG 圖示是外部檔案，一定要打包進去；程式端一律透過
    # app.resources.resource_path() 讀取，會自動處理 sys._MEIPASS
    # languages/*.json 同理，透過 app.resources.resource_path() 讀取
    datas=[("assets", "assets"), ("languages", "languages")],
    # 搭配 main.py 頂端的顯式 import，確保 Qt 的 SVG 插件
    # （qsvg.dll / qsvgicon.dll）一併被收錄。
    # QtNetwork 供單一實例的具名管道使用，寫在這裡避免日後被誤判成沒用到。
    hiddenimports=["PyQt6.QtSvg", "PyQt6.QtNetwork"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

a.binaries = _strip_binaries(a.binaries)
a.datas = _strip_datas(a.datas)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="MarkdownReader",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX 壓 Qt DLL 常導致無法啟動
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,      # 隱藏終端機視窗
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=["assets/app.ico"],
        version=VERSION_INFO,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="MarkdownReader",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=["assets/app.ico"],
        version=VERSION_INFO,
    )
    # 資料夾刻意命名為 MarkdownReader-onedir，而不是預設的 MarkdownReader：
    # 單一 exe 版的輸出是 dist\MarkdownReader.exe，兩者若只差一個副檔名而相鄰，
    # 很容易在清理或搬移時誤刪對方。
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="MarkdownReader-onedir",
    )
