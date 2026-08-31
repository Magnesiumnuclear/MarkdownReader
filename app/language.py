"""介面語言（i18n）。

字串放在 languages/<code>.json，程式端一律透過 t("鍵") 取用。用 JSON 查表而不是
Qt 的 .ts/.qm 工具鏈：全部只有一百多條字串，純文字檔看得懂 diff、改完存檔就生效，
不必多養一道 lrelease 編譯步驟。

【鍵是扁平的，不是巢狀的】
分類靠鍵的前綴（app. / titleBar. / tab. / find. / settings. / status. /
dialog. / welcome. / error. / encoding.），不靠檔案結構。理由有三：
  * 兩份語言檔必須逐鍵對齊，扁平檔一句 sorted(a) == sorted(b) 就驗得完，
    巢狀要遞迴走訪（回歸測試裡那條「語言檔完整性」就是這樣寫的）。
  * git diff 一行一條字串，改了哪句一眼看出；巢狀檔的 diff 只看得到縮排後的值。
  * t() 就是一次 dict 查表，沒有路徑切割，也沒有「中間節點是字串還是字典」的歧義。

【一句話一個鍵，絕不在程式碼裡拼接】
中英語序不同（「切換到{target}主題」/「Switch to {target} theme」），拼接會讓
英文語序永遠是錯的。全形標點（（）～／：·）也一律由翻譯字串整條承載——
英文用半形，那是翻譯的一部分，不是程式該決定的事。

【和 theme.py 的對照】
本模組刻意做成和 theme.py 同形狀（system_* / resolve / current），只少了
connect_system_changes：Windows 改顯示語言本來就要登出才生效，不會發訊號給
執行中的行程，Qt 也沒有對應的 hint 訊號。少那一段是結論，不是漏掉。
"""

from __future__ import annotations

import json

from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator

from . import config, resources

# 語言碼 -> 字串表。只有兩種語言，載入後就留著（比照 document._CONVERTERS）。
_CATALOGS: dict[str, dict[str, str]] = {}
_CURRENT: str | None = None

# Qt 自己的翻譯（右鍵選單、訊息框按鈕）。一定要用模組層變數接住：
# QTranslator 交給 installTranslator 之後 Qt 並不持有 Python 端的參照，
# 沒接住的話下一次 GC 就把它收走，介面文字會無聲地退回英文。
_QT_TRANSLATOR: QTranslator | None = None


def _catalog(code: str) -> dict[str, str]:
    """載入並快取一份字串表；讀不到就回空表（由 t() 的 fallback 接手）。"""
    cached = _CATALOGS.get(code)
    if cached is not None:
        return cached
    path = resources.resource_path(config.LANGUAGE_DIR, f"{code}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    _CATALOGS[code] = data
    return data


def current() -> str:
    """目前實際套用的語言碼（永遠是具體語言，不會是 "system"）。"""
    if _CURRENT is None:
        set_current(config.DEFAULT_LANGUAGE)
    return _CURRENT or config.DEFAULT_LANGUAGE


def t(key: str, /, **kwargs: object) -> str:
    """取目前語言的字串，並以具名參數格式化。

    key 是 positional-only：翻譯字串本身就可能有 {key} 佔位符，
    當成一般參數會和它撞名。

    找不到鍵時退回 zh_TW（原文，永遠是完整的），再找不到才回傳鍵本身。
    退回原文而不是直接回鍵：翻譯漏了幾條時，介面是「大致英文夾雜幾個中文詞」
    ——難看但可用；顯示 settings.hint.theme 則是壞掉。回傳鍵只是最後一道保險，
    回歸測試裡的「語言檔完整性」就是在確保它永遠走不到。
    """
    code = current()
    template = _catalog(code).get(key)
    if template is None and code != config.DEFAULT_LANGUAGE:
        template = _catalog(config.DEFAULT_LANGUAGE).get(key)
    if template is None:
        return key
    if not kwargs:
        return template
    try:
        return template.format_map(kwargs)
    except (KeyError, IndexError, ValueError):
        # 翻譯字串裡的佔位符名稱打錯時，回未格式化的原字串而不是讓例外漏出去。
        # t() 的呼叫散在 paintEvent / resizeEvent 的呼叫鏈上，PyQt6 對虛擬函式
        # 裡漏出去的例外是直接中止行程（0xC0000409），沒有第二次機會。
        return template


def system_language() -> str:
    """從 Windows 的顯示語言推斷要用哪一種介面語言。

    用 uiLanguages() 而不是 QLocale.system().name()：顯示語言設成英文、地區設成
    台灣的機器上 name() 會是 "zh_TW"，但我們要的是「介面語言」，不是數字與日期
    的格式偏好。

    zh-Hans（簡體）也對應到 zh_TW：沒有簡體字串表，而繁體遠比英文接近。
    """
    for tag in QLocale.system().uiLanguages():
        lowered = tag.lower()
        if lowered.startswith("zh"):
            return "zh_TW"
        if lowered.startswith("en"):
            return "en"
    return config.DEFAULT_LANGUAGE


def resolve(mode: str) -> str:
    """把語言模式換算成實際要套用的語言碼。

    "system" 這個字串只准出現在三個地方：config.LANGUAGE_MODES 的第三條、
    這裡的分支、以及 system_language()。其他程式碼一律比對解析後的語言碼，
    不要比對模式——這樣「跟隨系統」要拿掉時只需要刪這三處。
    （主題那邊把 "system" 洩漏到了 viewer._on_system_theme_changed，別學。）
    """
    if mode in {code for code, _key in config.LANGUAGE_MODES if code != "system"}:
        return mode
    return system_language()


def mode_from_settings(settings) -> str:
    """從 QSettings 讀語言模式，不在白名單就退回預設。"""
    mode = settings.value(config.KEY_LANGUAGE_MODE, config.DEFAULT_LANGUAGE_MODE)
    if mode not in {code for code, _key in config.LANGUAGE_MODES}:
        return config.DEFAULT_LANGUAGE_MODE
    return str(mode)


def install_qt_translator(code: str) -> bool:
    """載入 Qt 自己的翻譯，讓右鍵選單與訊息框按鈕跟著介面語言。

    這個專案原本從未載入 QTranslator，所以 Qt 內建元件一直顯示英文源字串
    （中文介面配英文右鍵選單）。.qm 檔本來就被 PyInstaller 收在
    PyQt6/Qt6/translations 底下，載入它不需要多打包任何東西。

    英文不需要載入（源字串就是英文），載入失敗也只是退回英文，不影響主流程。

    注意 QFileDialog 不在此列：Windows 上它走原生對話框，按鈕文字永遠跟隨
    系統語言，QTranslator 管不到（見 README 的已知限制）。
    """
    global _QT_TRANSLATOR

    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is None:
        return False
    if _QT_TRANSLATOR is not None:
        app.removeTranslator(_QT_TRANSLATOR)
        _QT_TRANSLATOR = None
    if code == "en":
        return True

    translator = QTranslator()
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not translator.load(f"qtbase_{code}", path):
        return False
    app.installTranslator(translator)
    _QT_TRANSLATOR = translator
    return True


def set_current(code: str) -> None:
    """切換目前語言（行程全域）。

    對相同語言早退：多視窗時每個視窗都會呼叫一次，沒有早退的話會重複拆裝
    QTranslator。
    """
    global _CURRENT

    if code == _CURRENT:
        return
    _CURRENT = code
    _catalog(code)
    install_qt_translator(code)


def init_from_settings(settings) -> str:
    """啟動時解析並套用語言，回傳實際套用的語言碼。"""
    code = resolve(mode_from_settings(settings))
    set_current(code)
    return code
