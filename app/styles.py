"""樣式唯一來源。

專案規範：所有 CSS/QSS 一律集中在本模組宣告，其他模組不得散落 setStyleSheet。
- build_qss(theme)     -> PyQt 元件樣式（視窗外框、標題列、按鈕、捲軸、搜尋列）
- build_doc_css(theme) -> QTextDocument 的 Markdown 渲染樣式

兩者共用同一份色票與字型常數，因此切換主題時外殼與內文必定同步。

【重要】QSS 與 QTextDocument 的 CSS 是兩套完全不同的引擎：
  * QSS 支援 border-radius、rgba()、偽狀態（:hover）
  * QTextDocument 只支援 CSS 2.1 子集，區塊元素的 border/padding 完全無效，
    僅表格儲存格有效；字級必須用 em/%，用 px 會無法隨字級縮放。
"""

from __future__ import annotations

from string import Template

from . import config

# --- 字型（QSS 與文件 CSS 共用同一份定義） ----------------------------------
# 介面字型隨語言換 fallback 順序：微軟正黑體的西文字母字距偏鬆，英文介面下
# 讀起來不俐落；反過來 Segoe UI 沒有完整的中日韓字符。兩邊都把對方列為次選，
# 缺字時仍有東西可退。
FONT_UI_BY_LANGUAGE = {
    "zh_TW": '"Microsoft JhengHei", "微軟正黑體", "Segoe UI", sans-serif',
    "en": '"Segoe UI", "Microsoft JhengHei", sans-serif',
}
FONT_CODE = '"Cascadia Code", "Consolas", monospace'


def font_ui(language_code: str) -> str:
    return FONT_UI_BY_LANGUAGE.get(
        language_code, FONT_UI_BY_LANGUAGE[config.DEFAULT_LANGUAGE]
    )

# QTextDocument 的內文留白（由 viewer 套用到 document margin）
DOCUMENT_MARGIN = 30

# 程式碼高亮配色（Pygments）
PYGMENTS_STYLE = {"light": "default", "dark": "github-dark"}

# --- 色票 -------------------------------------------------------------------
PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "window_bg": "#ffffff",
        "surface": "#f6f8fa",
        "border": "#d1d9e0",
        "text": "#1f2328",
        "text_muted": "#59636e",
        "text_faint": "#818b98",
        "accent": "#0969da",
        "code_bg": "#f6f8fa",
        "code_inline_bg": "#eceef1",
        "quote_bar": "#d0d7de",
        "quote_text": "#59636e",
        "table_header_bg": "#f6f8fa",
        "hover_bg": "rgba(31, 35, 40, 0.08)",
        "pressed_bg": "rgba(31, 35, 40, 0.16)",
        "close_hover_bg": "#e81123",
        "close_pressed_bg": "#c50f1f",
        "icon": "#59636e",
        "icon_active": "#1f2328",
        "icon_on_accent": "#ffffff",
        "scrollbar": "#c9d1d9",
        "scrollbar_hover": "#9aa5b1",
        "selection_bg": "#b3d7ff",
        "selection_fg": "#1f2328",
        "find_match_bg": "#fff3c4",
        "find_current_bg": "#ffd33d",
    },
    "dark": {
        "window_bg": "#0d1117",
        "surface": "#161b22",
        "border": "#3d444d",
        "text": "#e6edf3",
        "text_muted": "#9198a1",
        "text_faint": "#6e7681",
        "accent": "#4493f8",
        "code_bg": "#161b22",
        "code_inline_bg": "#262c36",
        "quote_bar": "#3d444d",
        "quote_text": "#9198a1",
        "table_header_bg": "#1c2128",
        "hover_bg": "rgba(230, 237, 243, 0.10)",
        "pressed_bg": "rgba(230, 237, 243, 0.18)",
        "close_hover_bg": "#e81123",
        "close_pressed_bg": "#c50f1f",
        "icon": "#9198a1",
        "icon_active": "#e6edf3",
        "icon_on_accent": "#ffffff",
        "scrollbar": "#484f58",
        "scrollbar_hover": "#6e7681",
        "selection_bg": "#1f4b7f",
        "selection_fg": "#e6edf3",
        "find_match_bg": "#4a3f00",
        "find_current_bg": "#9e6a03",
    },
}


def palette(theme: str) -> dict[str, str]:
    """取得指定主題的色票（未知主題一律回退為淺色）。"""
    return PALETTES.get(theme, PALETTES["light"])


def other_theme(theme: str) -> str:
    """回傳另一個主題名稱，供切換按鈕使用。"""
    return "dark" if theme == "light" else "light"


def _parse_colour(value: str) -> tuple[int, int, int, float]:
    """把色票字串拆成 (r, g, b, alpha)。只認得本檔用到的兩種寫法。

    色票裡的 hover_bg／pressed_bg 是半透明的 rgba()，疊在別的顏色上才是
    眼睛看到的顏色。要在 QSS 裡鋪一塊「和分頁同色」的襯底就得先自己合成，
    QSS 本身算不出這個。
    """
    text = value.strip()
    if text.startswith("#"):
        digits = text[1:]
        return (
            int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 1.0
        )
    if text.startswith("rgba"):
        parts = [p.strip() for p in text[text.index("(") + 1:text.rindex(")")].split(",")]
        return int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3])
    raise ValueError(f"不認識的色票寫法：{value}")


def _blend(over: str, under: str) -> str:
    """把半透明的 over 疊在不透明的 under 上，算出等效的 #rrggbb。"""
    ored, ogreen, oblue, alpha = _parse_colour(over)
    ured, ugreen, ublue, _ = _parse_colour(under)
    mix = lambda a, b: round(a * alpha + b * (1 - alpha))  # noqa: E731
    return f"#{mix(ored, ured):02x}{mix(ogreen, ugreen):02x}{mix(oblue, ublue):02x}"


def _transparent(colour: str) -> str:
    """同一個顏色的全透明版本。

    漸層不能用 `transparent` 當起點：Qt 在非預乘的 RGBA 空間內插，
    透明黑漸變到不透明白會從灰色走過去，淺色主題上會看到一條髒邊。
    起點必須是「同色但 alpha=0」。
    """
    red, green, blue, _ = _parse_colour(colour)
    return f"rgba({red}, {green}, {blue}, 0)"


def _values(theme: str, language_code: str) -> dict[str, str]:
    """樣板替換值。

    語言是明確傳進來的，不讓這個模組去讀 language.current()：本專案從頭到尾
    把 theme 一路傳下來、不依賴環境狀態，語言照辦。代價只有四個呼叫端，
    換來的是這裡維持純函式、可以獨立測試。
    """
    values = dict(palette(theme))
    values["font_ui"] = font_ui(language_code)
    values["font_code"] = FONT_CODE
    # 關閉鈕襯底要和它蓋住的那塊分頁同色。分頁有兩種底色：非作用中被 hover
    # 時是半透明的 hover_bg 疊在分頁列的 surface 上（先合成才知道實色），
    # 作用中則直接是 window_bg。
    idle = _blend(values["hover_bg"], values["surface"])
    values["tab_close_bg"] = idle
    values["tab_close_bg_clear"] = _transparent(idle)
    values["tab_close_bg_active"] = values["window_bg"]
    values["tab_close_bg_active_clear"] = _transparent(values["window_bg"])
    # 漸層要在關閉鈕的左緣就收成實色，不然叉叉底下還是半透明、字會透出來。
    strip = config.TAB_CLOSE_FADE + config.TAB_CLOSE_SIZE + config.TAB_CLOSE_MARGIN
    values["tab_close_fade_stop"] = f"{config.TAB_CLOSE_FADE / strip:.3f}"
    return values


# --- PyQt 元件樣式（QSS） ---------------------------------------------------
# 用 string.Template（$name 佔位符）而非 f-string，避免與 CSS 的大括號打架。
_QSS = Template(
    """
/* ---------- 視窗外框 ---------- */
#rootFrame {
    background-color: $window_bg;
    border: 1px solid $border;
}

/* ---------- 自訂標題列 ---------- */
#titleBar {
    background-color: $surface;
    border: none;
    border-bottom: 1px solid $border;
}
#titleLabel {
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    padding: 0px 4px;
    background: transparent;
}
#titleIcon {
    background: transparent;
    padding-left: 6px;
}

/* ---------- 標題列與工具按鈕 ---------- */
QToolButton#navBtn, QToolButton#closeBtn {
    background-color: transparent;
    border: none;
    border-radius: 4px;
    margin: 0px 1px;
    padding: 0px;
}
QToolButton#navBtn:hover    { background-color: $hover_bg; }
QToolButton#navBtn:pressed  { background-color: $pressed_bg; }
QToolButton#navBtn:checked  { background-color: $pressed_bg; }
QToolButton#navBtn:disabled { background-color: transparent; }
QToolButton#closeBtn:hover   { background-color: $close_hover_bg; }
QToolButton#closeBtn:pressed { background-color: $close_pressed_bg; }

/* ---------- 分頁列 ---------- */
#tabBar {
    background-color: $surface;
    border: none;
    border-bottom: 1px solid $border;
}
#tabScroll, #tabStrip { background: transparent; border: none; }

/* 未選取：融入分頁列底色。border-top 保留透明佔位，切換時不會位移 */
#tab {
    background-color: transparent;
    border: none;
    border-right: 1px solid $border;
    border-top: 2px solid transparent;
}
#tab:hover { background-color: $hover_bg; }
#tabLabel {
    color: $text_muted;
    font-family: $font_ui;
    font-size: 12px;
    background: transparent;
}

/* 選取中：底色與內容區相同，視覺上和下方文件連成一片 */
#tabActive {
    background-color: $window_bg;
    border: none;
    border-right: 1px solid $border;
    border-top: 2px solid $accent;
}
#tabLabelActive {
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    background: transparent;
}

QToolButton#tabClose, QToolButton#tabNew {
    background-color: transparent;
    border: none;
    border-radius: 3px;
    padding: 0px;
}
QToolButton#tabClose:hover, QToolButton#tabNew:hover { background-color: $pressed_bg; }

/* 「＋」右側展開的選單。全專案只有這一處用 QMenu，樣式不寫的話會是系統原生
   外觀，和無邊框自繪的其他部分格格不入。QSS 沿 QObject 的親子鏈傳遞，選單
   的 parent 是分頁列上的按鈕，所以吃得到視窗這份樣式。 */
QMenu {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    background-color: transparent;
    color: $text;
    padding: 6px 14px;
    border-radius: 4px;
}
QMenu::item:selected { background-color: $hover_bg; }

/* 關閉鈕的襯底：右段實色蓋住檔名尾巴，左段漸層淡出，接回分頁底色。
   兩種底色分別對應「非作用中被 hover」與「作用中」，實色是先合成過的
   （hover_bg 是半透明的，見 _values）。關閉鈕本身仍是透明底，hover 時
   那塊半透明的 chip 疊在這層實色上，看起來和改版前一樣。 */
QFrame#tabCloseBackdrop {
    border: none;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $tab_close_bg_clear,
        stop:$tab_close_fade_stop $tab_close_bg,
        stop:1 $tab_close_bg);
}
#tabActive QFrame#tabCloseBackdrop {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $tab_close_bg_active_clear,
        stop:$tab_close_fade_stop $tab_close_bg_active,
        stop:1 $tab_close_bg_active);
}

/* 拖曳時標出分頁會插進哪個縫隙。這條規則一定要留在主視窗的 QSS 裡——
   指示線是主視窗的子元件，寫進拖曳幽靈那份（它是獨立頂層視窗、自己套一份
   樣式）等於完全沒有樣式，isVisible() 仍是 True 卻一個像素都不畫。 */
#tabInsertMarker { background: transparent; border: none; }
#tabInsertLine {
    background-color: $accent;
    border: none;
    border-radius: 1px;
}
#tabInsertCap { background: transparent; border: none; }

#tabScroll QScrollBar:horizontal {
    background: transparent;
    height: 6px;
    margin: 0px;
    border: none;
}
#tabScroll QScrollBar::handle:horizontal {
    background-color: $scrollbar;
    border-radius: 3px;
    min-width: 30px;
    margin: 1px;
}
#tabScroll QScrollBar::add-line:horizontal,
#tabScroll QScrollBar::sub-line:horizontal {
    width: 0px;
    background: transparent;
    border: none;
}
#tabScroll QScrollBar::add-page:horizontal,
#tabScroll QScrollBar::sub-page:horizontal {
    background: transparent;
}

/* ---------- 內容顯示區 ---------- */
#contentView {
    background-color: $window_bg;
    border: none;
    selection-background-color: $selection_bg;
    selection-color: $selection_fg;
}

/* ---------- 現代化捲軸：透明軌道、細身、圓角滑塊 ---------- */
#contentView QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0px;
    border: none;
}
#contentView QScrollBar::handle:vertical {
    background-color: $scrollbar;
    border-radius: 3px;
    min-height: 36px;
    margin: 2px;
}
#contentView QScrollBar::handle:vertical:hover {
    background-color: $scrollbar_hover;
}
#contentView QScrollBar::add-line:vertical,
#contentView QScrollBar::sub-line:vertical {
    height: 0px;
    background: transparent;
    border: none;
}
#contentView QScrollBar::add-page:vertical,
#contentView QScrollBar::sub-page:vertical {
    background: transparent;
}

#contentView QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0px;
    border: none;
}
#contentView QScrollBar::handle:horizontal {
    background-color: $scrollbar;
    border-radius: 3px;
    min-width: 36px;
    margin: 2px;
}
#contentView QScrollBar::handle:horizontal:hover {
    background-color: $scrollbar_hover;
}
#contentView QScrollBar::add-line:horizontal,
#contentView QScrollBar::sub-line:horizontal {
    width: 0px;
    background: transparent;
    border: none;
}
#contentView QScrollBar::add-page:horizontal,
#contentView QScrollBar::sub-page:horizontal {
    background: transparent;
}

/* ---------- 搜尋列（浮動面板，不在版面裡）---------- */
#findBar {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 8px;
}
#findGrip { background: transparent; }
#findInput {
    background-color: $window_bg;
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 3px 8px;
    selection-background-color: $selection_bg;
    selection-color: $selection_fg;
}
#findInput:focus {
    border: 1px solid $accent;
}
#findStatus {
    color: $text_muted;
    font-family: $font_ui;
    font-size: 11px;
    background: transparent;
    padding: 0px 6px;
}

/* ---------- 設定面板（覆蓋標題列以下的整個視窗）---------- */
#settingsPanel {
    background-color: $window_bg;
    border: none;
}
#settingsHeader {
    background-color: $surface;
    border: none;
    border-bottom: 1px solid $border;
}
#settingsTitle {
    color: $text;
    font-family: $font_ui;
    font-size: 15px;
    font-weight: bold;
    background: transparent;
}
#settingsScroll, #settingsContent {
    background: transparent;
    border: none;
}
#settingsSection {
    color: $text_muted;
    font-family: $font_ui;
    font-size: 11px;
    font-weight: bold;
    background: transparent;
}
#settingsSectionLine {
    background-color: $border;
    border: none;
}
#settingsRowLabel {
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    background: transparent;
}
#settingsHint {
    color: $text_muted;
    font-family: $font_ui;
    font-size: 11px;
    background: transparent;
}
#settingsValue {
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    background: transparent;
}

/* 分段選擇與開關（同一套外觀：未選為描邊、選中為強調色實心） */
QToolButton#segBtn {
    background-color: $window_bg;
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    border: 1px solid $border;
    border-radius: 5px;
    padding: 5px 14px;
    margin: 0px 2px 0px 0px;
}
QToolButton#segBtn:hover {
    background-color: $hover_bg;
}
QToolButton#segBtn:checked {
    background-color: $accent;
    color: $icon_on_accent;
    border: 1px solid $accent;
}

/* 字級加減 */
QToolButton#stepBtn {
    background-color: $window_bg;
    border: 1px solid $border;
    border-radius: 5px;
    padding: 0px;
    margin: 0px 2px 0px 0px;
}
QToolButton#stepBtn:hover   { background-color: $hover_bg; }
QToolButton#stepBtn:pressed { background-color: $pressed_bg; }

/* 恢復預設值 */
QToolButton#resetBtn {
    background-color: $window_bg;
    color: $text;
    font-family: $font_ui;
    font-size: 12px;
    border: 1px solid $border;
    border-radius: 5px;
    padding: 6px 16px;
}
QToolButton#resetBtn:hover   { background-color: $hover_bg; }
QToolButton#resetBtn:pressed { background-color: $pressed_bg; }

#settingsScroll QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0px;
    border: none;
}
#settingsScroll QScrollBar::handle:vertical {
    background-color: $scrollbar;
    border-radius: 3px;
    min-height: 36px;
    margin: 2px;
}
#settingsScroll QScrollBar::handle:vertical:hover {
    background-color: $scrollbar_hover;
}
#settingsScroll QScrollBar::add-line:vertical,
#settingsScroll QScrollBar::sub-line:vertical {
    height: 0px;
    background: transparent;
    border: none;
}
#settingsScroll QScrollBar::add-page:vertical,
#settingsScroll QScrollBar::sub-page:vertical {
    background: transparent;
}

/* ---------- 狀態列 ---------- */
#statusBar {
    background-color: $surface;
    border: none;
    border-top: 1px solid $border;
}
#statusLabel {
    color: $text_muted;
    font-family: $font_ui;
    font-size: 11px;
    background: transparent;
    padding: 0px 4px;
}
/* 路徑是可以點的（在檔案總管中顯示），hover 時給出可點的暗示 */
#statusPathLabel {
    color: $text_muted;
    font-family: $font_ui;
    font-size: 11px;
    background: transparent;
    padding: 0px 4px;
}
#statusPathLabel:hover {
    color: $accent;
    text-decoration: underline;
}

/* ---------- 對話框與提示 ---------- */
QMessageBox {
    background-color: $window_bg;
    font-family: $font_ui;
}
QMessageBox QLabel {
    color: $text;
    font-family: $font_ui;
}
QMessageBox QPushButton {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 4px 16px;
    min-width: 68px;
}
QMessageBox QPushButton:hover {
    background-color: $hover_bg;
}
QToolTip {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    font-family: $font_ui;
    padding: 3px 6px;
}
"""
)


def build_qss(theme: str, language_code: str = config.DEFAULT_LANGUAGE) -> str:
    """產生整個視窗的 QSS（全專案唯一一次 setStyleSheet 就是套用這份字串）。"""
    return _QSS.substitute(_values(theme, language_code))


# 拖曳幽靈是獨立的頂層小視窗，吃不到主視窗的 QSS，要自己套一份。
# 徽章用 intent 動態屬性切換配色：合併＝強調色實心、拆分＝描邊、無動作＝灰。
_GHOST_QSS = Template("""
QLabel#dragGhostBadge {
    font-family: $font_ui;
    font-size: 11px;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 4px;
    border: 1px solid $border;
    color: $text_muted;
    background-color: $surface;
}
QLabel#dragGhostBadge[intent="merge"] {
    color: $icon_on_accent;
    background-color: $accent;
    border: 1px solid $accent;
}
QLabel#dragGhostBadge[intent="detach"] {
    color: $accent;
    background-color: $surface;
    border: 1px solid $accent;
}
""")


def build_ghost_qss(theme: str, language_code: str = config.DEFAULT_LANGUAGE) -> str:
    """拖曳幽靈（分頁縮影＋下一步徽章）的 QSS。"""
    return _GHOST_QSS.substitute(_values(theme, language_code))


# --- Markdown 文件樣式（QTextDocument CSS） ---------------------------------
# 所有 font-size 一律用 em/%，才能隨 QTextBrowser 的字級設定等比縮放。
# 區塊元素的 border/padding 在 Qt 無效，因此引用區塊、程式碼區塊與分隔線
# 都由 qt_html.QtRichTextExtension 改寫成單格表格後，靠儲存格樣式呈現。
_DOC_CSS = Template(
    """
body {
    color: $text;
    background-color: $window_bg;
    font-family: $font_ui;
    font-size: 100%;
    line-height: $line_height%;
}
p { margin-top: 0.35em; margin-bottom: 0.85em; }
a { color: $accent; text-decoration: none; }

/* 只放一張圖片的段落。Qt 會把行高倍率套用到圖片所在的行框上，
   若沿用內文行高，圖片下方會多出一大塊空白，因此改回 100%。 */
p.imgblock { line-height: 100%; margin-top: 0.45em; margin-bottom: 1.00em; }

/* 【Qt 限制】h1~h5 會忽略 CSS 的 font-size（em/%/pt/px 一律無效），
   一律使用 Qt 內建比例 2.0 / 1.5 / 1.2 / 1.0 / 0.8 倍。這個比例本身接近
   GitHub 的階層，因此直接沿用；但 h6 的內建值是 1.0 倍，會比 h5 還大，
   階層反轉。h6 是唯一吃 pt 的標題，所以在這裡明確指定字級修正。 */
h1, h2, h3, h4, h5, h6 { color: $text; font-weight: bold; line-height: 128%; }
h3 { margin-top: 1.20em; margin-bottom: 0.30em; }
h4 { margin-top: 1.20em; margin-bottom: 0.30em; }
h5 { margin-top: 1.20em; margin-bottom: 0.30em; }
h6 { margin-top: 1.20em; margin-bottom: 0.30em; color: $text_muted; font-size: $h6_size; }

/* H1 / H2 的底線：擴充把標題包進 table.headrule > td.headcell，
   底線來自儲存格的 border-bottom。之所以不用「標題後另插一條橫線」，
   是因為 Qt 會讓獨立橫線區塊帶上整行行高，和標題之間出現大片空白。 */
table.headrule { margin-top: 1.15em; margin-bottom: 0.55em; }
td.headcell { border-bottom: 1px solid $border; padding-bottom: 6px; }
td.headcell h1, td.headcell h2 { margin-top: 0px; margin-bottom: 0px; }

ul, ol { margin-top: 0.20em; margin-bottom: 0.85em; }
li { margin-top: 0.12em; margin-bottom: 0.12em; }

strong { font-weight: bold; }
em { font-style: italic; }
del { text-decoration: line-through; color: $text_muted; }

code {
    font-family: $font_code;
    font-size: 0.90em;
    background-color: $code_inline_bg;
    color: $text;
}
kbd {
    font-family: $font_code;
    font-size: 0.85em;
    background-color: $code_inline_bg;
    color: $text;
}

/* 程式碼區塊：擴充改寫成 table.code > td.codecell */
table.code { margin-top: 0.30em; margin-bottom: 0.95em; }
td.codecell {
    background-color: $code_bg;
    padding: 10px 16px;
}
/* 這裡千萬不能寫 background-color: transparent。實測 Qt 會讓 <pre> 區塊
   把儲存格底色整片蓋成白色，必須填入與儲存格相同的顏色才會正常。 */
td.codecell pre, pre {
    font-family: $font_code;
    font-size: 0.88em;
    white-space: pre-wrap;
    margin: 0px;
    background-color: $code_bg;
    line-height: 142%;
}
td.codecell div { background-color: $code_bg; }
td.codecell code { background-color: $code_bg; font-size: 1.0em; }

/* 引用區塊：擴充改寫成 table.bq > td.quotecell。
   blockquote 元素本身仍留在儲存格內（巢狀引用才能正確層層包裝），
   必須把 Qt 預設的縮排歸零，否則會出現雙重縮排。 */
blockquote {
    margin-left: 0px;
    margin-right: 0px;
    margin-top: 0px;
    margin-bottom: 0px;
}
td.codecell div { margin-top: 0px; margin-bottom: 0px; }
table.bq { margin-top: 0.30em; margin-bottom: 0.95em; }
table.notice { margin-top: 0.40em; margin-bottom: 1.00em; }
td.quotecell {
    border-left: 4px solid $quote_bar;
    padding: 1px 18px;
    background-color: transparent;
    color: $quote_text;
}
td.quotecell p { color: $quote_text; margin-top: 0.35em; margin-bottom: 0.35em; }
td.quotecell li { color: $quote_text; }

/* 分隔線與 H1/H2 底線：擴充改寫成 table.rule > td.rulecell */
table.rule { margin-top: 0.20em; margin-bottom: 0.55em; }
table.hrule { margin-top: 1.30em; margin-bottom: 1.30em; }
td.rulecell {
    border-bottom: 1px solid $border;
    padding: 0px;
    font-size: 0.10em;
}

/* 資料表格 */
table.data { border-collapse: collapse; margin-top: 0.35em; margin-bottom: 0.95em; }
table.data th {
    border: 1px solid $border;
    background-color: $table_header_bg;
    padding: 7px 14px;
    font-weight: bold;
    color: $text;
}
table.data td {
    border: 1px solid $border;
    padding: 7px 14px;
    color: $text;
}

/* 註腳 */
div.footnote { font-size: 0.90em; color: $text_muted; }
div.footnote li { color: $text_muted; }

/* 提示／錯誤頁專用 */
td.noticecell {
    border-left: 4px solid $accent;
    padding: 10px 18px;
    background-color: $code_bg;
}
td.warncell {
    border-left: 4px solid #bf8700;
    padding: 8px 18px;
    background-color: $code_bg;
}
.muted { color: $text_muted; }
.mono { font-family: $font_code; font-size: 0.90em; }
"""
)


def build_doc_css(
    theme: str,
    base_point_size: float = 11.0,
    line_height: int = 160,
    language_code: str = config.DEFAULT_LANGUAGE,
) -> str:
    """產生 QTextDocument 用的 Markdown 樣式表。

    base_point_size 是閱讀區目前的基準字級（pt）。之所以需要它，是因為 h6 的
    字級只能用絕對單位指定（見樣式表中的說明），必須隨縮放一起重算。
    line_height 是內文行高百分比，由設定列控制。
    """
    values = _values(theme, language_code)
    values["h6_size"] = f"{max(1.0, base_point_size * 0.72):.1f}pt"
    values["line_height"] = str(int(line_height))
    return _DOC_CSS.substitute(values)
