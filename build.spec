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

# True  = 單一 exe（方便散布，但每次啟動要解壓到暫存目錄，冷啟約 1~2 秒）
# False = 資料夾版（啟動 <0.5 秒，適合設成 .md 的預設開啟程式）
ONEFILE = True

# 用不到的 Python 模組。
# 特別注意 PIL / numpy：pygments.formatters.img 會 import PIL，PIL 的 hook 又
# 把 numpy 一起拉進來，光這兩個就會讓 exe 多出約 12MB（含 20MB 的 OpenBLAS）。
# 本程式只用 pygments 產生 HTML，不會產生圖片格式輸出，排除掉完全沒有影響。
EXCLUDES = [
    "PIL",
    "numpy",
    "pyqtgraph",
    "tkinter",
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
#   libcrypto/libssl 6MB   QtNetwork 的 TLS 後端，本程式不連網。
EXCLUDE_BINARIES = (
    "opengl32sw.dll",
    "qt6pdf.dll",
    "libcrypto-3.dll",
    "libssl-3.dll",
)


def _strip_binaries(binaries):
    """濾掉 EXCLUDE_BINARIES 指定的 DLL。"""
    kept = []
    for entry in binaries:
        name = os.path.basename(entry[0]).lower()
        if name in EXCLUDE_BINARIES:
            continue
        kept.append(entry)
    return kept


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # SVG 圖示是外部檔案，一定要打包進去；程式端一律透過
    # app.resources.resource_path() 讀取，會自動處理 sys._MEIPASS
    datas=[("assets", "assets")],
    # 搭配 main.py 頂端的顯式 import，確保 Qt 的 SVG 插件
    # （qsvg.dll / qsvgicon.dll）一併被收錄
    hiddenimports=["PyQt6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

a.binaries = _strip_binaries(a.binaries)

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
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="MarkdownReader",
    )
