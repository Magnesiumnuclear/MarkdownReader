"""全域常數設定。

集中管理應用程式識別、視窗尺寸、支援的副檔名與 QSettings 鍵名，
避免這些數值散落在各模組中。
"""

from __future__ import annotations

# --- 應用程式識別 -----------------------------------------------------------
APP_NAME = "MarkdownReader"
APP_DISPLAY_NAME = "Markdown 閱讀器"
ORG_NAME = "SamHo"
APP_USER_MODEL_ID = "SamHo.MarkdownReader.Viewer.1"

# --- 支援的檔案類型 ---------------------------------------------------------
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdtxt")
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES + (".txt",)
OPEN_DIALOG_FILTER = (
    "Markdown 文件 (*.md *.markdown *.mdown *.mkd *.mkdn *.mdtxt);;"
    "純文字檔 (*.txt);;所有檔案 (*.*)"
)

# --- 視窗尺寸 ---------------------------------------------------------------
DEFAULT_WINDOW_SIZE = (980, 760)
MIN_WINDOW_SIZE = (420, 320)
TITLE_BAR_HEIGHT = 36
TITLE_BUTTON_SIZE = (44, 28)   # 最小化／最大化／關閉
TOOL_BUTTON_SIZE = (32, 28)    # 返回／開檔／搜尋／主題／釘選
ICON_PIXEL_SIZE = 16

# 無邊框視窗的邊緣縮放感應寬度（邏輯像素，實際會依 devicePixelRatio 放大）
RESIZE_MARGIN_BASE = 6
RESIZE_MARGIN_MIN = 6
RESIZE_MARGIN_MAX = 14

# --- 字級縮放 ---------------------------------------------------------------
BASE_FONT_POINT_SIZE = 11
MIN_FONT_POINT_SIZE = 7
MAX_FONT_POINT_SIZE = 30

# --- 檔案監看 ---------------------------------------------------------------
WATCH_DEBOUNCE_MS = 150      # 存檔事件去抖動時間
READ_RETRY_COUNT = 3         # 檔案被鎖定時的重試次數
READ_RETRY_DELAY_S = 0.05    # 每次重試間隔（秒）

# 超過此大小的檔案會在頁首提示排版可能較慢
LARGE_FILE_BYTES = 5 * 1024 * 1024

# --- 閱讀版面選項 -----------------------------------------------------------
# 行高（百分比）。Qt 的 line-height 用 % 才會隨字級等比縮放。
LINE_HEIGHT_OPTIONS = (
    ("compact", "緊湊", 142),
    ("normal", "標準", 160),
    ("relaxed", "寬鬆", 188),
)
DEFAULT_LINE_HEIGHT = "normal"

# 內文最大寬度（像素），0 代表不限制。
# QTextDocument 不支援 max-width，實作方式是調整 root frame 的左右邊距，
# 讓文字欄固定寬度並置中（詳見 viewer._apply_content_width）。
CONTENT_WIDTH_OPTIONS = (
    (720, "窄"),
    (900, "中"),
    (0, "不限"),
)
DEFAULT_CONTENT_WIDTH = 900

# 主題模式：system 會跟著 Windows 的深淺色設定即時變動
THEME_MODES = (
    ("light", "淺色"),
    ("dark", "深色"),
    ("system", "跟隨系統"),
)
DEFAULT_THEME_MODE = "system"

# --- QSettings 鍵名 ---------------------------------------------------------
KEY_GEOMETRY = "window/geometry"
KEY_MAXIMIZED = "window/maximized"
KEY_THEME_MODE = "view/themeMode"
KEY_FONT_SIZE = "view/fontPointSize"
KEY_LINE_HEIGHT = "view/lineHeight"
KEY_CONTENT_WIDTH = "view/contentWidth"
KEY_ALWAYS_ON_TOP = "window/alwaysOnTop"
KEY_STATUS_VISIBLE = "view/statusBarVisible"
KEY_AUTO_RELOAD = "behavior/autoReload"
KEY_CONFIRM_LINKS = "behavior/confirmExternalLinks"

DEFAULT_THEME = "light"          # 系統偵測失敗時的保底值
DEFAULT_AUTO_RELOAD = True
DEFAULT_CONFIRM_LINKS = False
