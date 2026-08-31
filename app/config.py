"""全域常數設定。

集中管理應用程式識別、視窗尺寸、支援的副檔名與 QSettings 鍵名，
避免這些數值散落在各模組中。
"""

from __future__ import annotations

# --- 應用程式識別 -----------------------------------------------------------
APP_NAME = "MarkdownReader"          # QSettings 路徑用，不隨語言變
# 產品名稱刻意不放在這裡：它會隨語言變，來源一律是 language.t("app.displayName")。
# 留一個常數在這裡的話，遲早有人繼續引用它，然後在英文介面下冒出中文。
ORG_NAME = "SamHo"
APP_USER_MODEL_ID = "SamHo.MarkdownReader.Viewer.1"

# --- 支援的檔案類型 ---------------------------------------------------------
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdtxt")
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES + (".txt",)
# 開檔對話框的篩選字串在語言檔的 dialog.openFilter（型別名稱要翻譯）。
# 那條字串裡的副檔名 pattern 必須和上面的 MARKDOWN_SUFFIXES 保持一致。

# --- 視窗尺寸 ---------------------------------------------------------------
DEFAULT_WINDOW_SIZE = (980, 760)
MIN_WINDOW_SIZE = (420, 320)
TITLE_BAR_HEIGHT = 36
TITLE_BUTTON_SIZE = (44, 28)   # 最小化／最大化／關閉
TOOL_BUTTON_SIZE = (32, 28)    # 返回／開檔／搜尋／主題／釘選
ICON_PIXEL_SIZE = 16

# 無邊框視窗的邊緣縮放感應寬度（邏輯像素）。
# 不依 devicePixelRatio 放大：Qt 的座標本來就是邏輯像素，系統縮放 150% 時
# 6 邏輯像素已經等於 9 實體像素，物理寬度是固定的；再乘一次 dpr 等於放大兩倍，
# 反而會把右側捲軸整條蓋掉。
RESIZE_MARGIN = 6

# 游標落在捲軸這類「本來就要拖曳」的控制項上時，只保留最外側這幾像素給視窗縮放，
# 其餘讓給控制項。沒有這條規則，10px 的捲軸會有 9px 被縮放判定搶走。
RESIZE_MARGIN_OVER_CONTROL = 4

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

# 超過這個大小才顯示「正在載入」與等待游標。實測 1MB / 5272 行的檔案，
# Markdown 轉換 592ms + Qt 排版共約 731ms——那段時間介面完全凍結且毫無反應。
# 門檻取 256KB：更小的檔案渲染在百毫秒內，閃一下反而是雜訊。
BUSY_FEEDBACK_BYTES = 256 * 1024

# --- 分段渲染（先見首屏）---------------------------------------------------
# 大文件的 setHtml 會整份排完版才回來，那段時間介面完全凍結。改成先把首屏
# 交出去、其餘分批在事件迴圈的空檔補上，使用者幾毫秒內就看得到內容。
#
# 【為什麼要切成很多塊，而不是「首屏 + 剩下一整塊」】
# 實測 3000 個標題的文件（頂層元素 6000 個，一次到底 883ms）：
#     剩下切 1 塊 -> 總計 1915ms（2.17x），最久一塊 1915ms —— 比不切還糟
#     剩下切 16 塊 -> 總計  952ms（1.08x），最久一塊   83ms
#     剩下切 64 塊 -> 總計  985ms（1.12x），最久一塊   32ms
# 單次 insertHtml 塞一大段進已經有內容的文件特別貴，切開反而連總量都變好。
# （試過 beginEditBlock 與關閉 undo，都沒有幫助。）
#
# 首屏取前 40 個頂層元素：實測 4ms 內畫得出來，足夠鋪滿一個畫面。
PROGRESSIVE_FIRST_ELEMENTS = 40
# 之後每塊 400 個頂層元素。用「每塊固定元素數」而不是「固定切成 N 塊」，
# 每塊的凍結時間才有上界（實測約 83ms），不會隨文件變大而惡化。
PROGRESSIVE_CHUNK_ELEMENTS = 400
# 頂層元素少於這個數量就一次到底：小文件本來就快，分段只是多繞路。
PROGRESSIVE_MIN_ELEMENTS = 200

# --- 閱讀版面選項 -----------------------------------------------------------
# 行高（百分比）。Qt 的 line-height 用 % 才會隨字級等比縮放。
# 中間那欄是翻譯鍵而不是可直接顯示的字串，顯示前一定要過 language.t()。
LINE_HEIGHT_OPTIONS = (
    ("compact", "settings.lineHeight.compact", 142),
    ("normal", "settings.lineHeight.standard", 160),
    ("relaxed", "settings.lineHeight.relaxed", 188),
)
DEFAULT_LINE_HEIGHT = "normal"

# 內文最大寬度（像素），0 代表不限制。
# QTextDocument 不支援 max-width，實作方式是調整 root frame 的左右邊距，
# 讓文字欄固定寬度並置中（詳見 viewer._apply_content_width）。
CONTENT_WIDTH_OPTIONS = (
    (720, "settings.width.narrow"),
    (900, "settings.width.medium"),
    (0, "settings.width.unlimited"),
)
DEFAULT_CONTENT_WIDTH = 900

# 主題模式：system 會跟著 Windows 的深淺色設定即時變動
THEME_MODES = (
    ("light", "settings.theme.light"),
    ("dark", "settings.theme.dark"),
    ("system", "settings.theme.system"),
)
DEFAULT_THEME_MODE = "system"

# --- 介面語言 ---------------------------------------------------------------
# 第一欄是存進 QSettings 的值，最後一欄是「翻譯鍵」而不是可直接顯示的字串
# ——顯示前一定要過 language.t()，直接印會看到 settings.language.zhTW。
#
# settings.language.zhTW 在兩份語言檔裡都是「繁體中文」、settings.language.en
# 在兩份裡都是「English」：語言選擇器要顯示各語言的自稱，這是刻意的兩檔同值，
# 不是漏翻。只有「跟隨系統」那條真的需要翻。
LANGUAGE_MODES = (
    ("zh_TW", "settings.language.zhTW"),
    ("en", "settings.language.en"),
    ("system", "settings.language.system"),
)
DEFAULT_LANGUAGE_MODE = "system"
DEFAULT_LANGUAGE = "zh_TW"       # 系統偵測失敗時的保底值（比照 DEFAULT_THEME）
LANGUAGE_DIR = "languages"       # 相對於 resources.base_path()

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
KEY_LANGUAGE_MODE = "view/languageMode"

DEFAULT_THEME = "light"          # 系統偵測失敗時的保底值
DEFAULT_AUTO_RELOAD = True
DEFAULT_CONFIRM_LINKS = False

# --- 分頁 -------------------------------------------------------------------
TAB_HEIGHT = 34
TAB_INSERT_MARKER_WIDTH = 3   # 合併時的插入位置指示線
TAB_MAX_WIDTH = 190
TAB_MIN_WIDTH = 92
TAB_LABEL_WIDTH = 130            # 檔名可用寬度，超過就中間省略

KEY_RESTORE_TABS = "behavior/restoreTabs"
KEY_OPEN_TABS = "session/openTabs"
KEY_ACTIVE_TAB = "session/activeTab"
DEFAULT_RESTORE_TABS = False

# --- 單一實例 ---------------------------------------------------------------
# 具名管道的名稱要含使用者名稱，避免多使用者登入時互相搶。
IPC_SERVER_NAME = "MarkdownReader.SingleInstance"
IPC_CONNECT_TIMEOUT_MS = 150     # 連不到就當作沒有既有實例，不要卡住啟動
IPC_WRITE_TIMEOUT_MS = 1000
# 送出後等對方關閉連線的確認。只是保險，等不到也不影響正確性
# （資料已經在管道裡），所以放短一點，別讓使用者對著沒反應的畫面枯等。
IPC_DISCONNECT_TIMEOUT_MS = 300
# 對方已經斷線、但資料還沒被拉進 Qt 讀取緩衝區時，補讀一次的等待上限。
IPC_READ_TIMEOUT_MS = 200
