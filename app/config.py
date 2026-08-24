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

# --- QSettings 鍵名 ---------------------------------------------------------
KEY_GEOMETRY = "window/geometry"
KEY_MAXIMIZED = "window/maximized"
KEY_THEME = "view/theme"
KEY_FONT_SIZE = "view/fontPointSize"
KEY_ALWAYS_ON_TOP = "window/alwaysOnTop"
KEY_STATUS_VISIBLE = "view/statusBarVisible"

DEFAULT_THEME = "light"
