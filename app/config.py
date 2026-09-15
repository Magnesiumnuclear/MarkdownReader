"""全域常數設定。

集中管理應用程式識別、視窗尺寸、支援的副檔名與 QSettings 鍵名，
避免這些數值散落在各模組中。
"""

from __future__ import annotations

import os

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
# 慢速輪詢：watcher 掛失敗或整個資料夾消失（磁碟拔掉）時的後備，只在自動重載
# 開啟時跑。十個分頁一輪 os.stat 不到 1ms，3 秒一次感覺不到。
WATCH_POLL_MS = 3000
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
KEY_FIND_CASE_SENSITIVE = "find/caseSensitive"
KEY_FIND_WHOLE_WORDS = "find/wholeWords"

DEFAULT_THEME = "light"          # 系統偵測失敗時的保底值
DEFAULT_AUTO_RELOAD = True
DEFAULT_CONFIRM_LINKS = False

# --- 文件內搜尋的比對選項 ---------------------------------------------------
# 兩個都預設關閉：不區分大小寫、不限全字，是「隨手找一下」最不會落空的組合。
# 選項會留存到下次啟動（搜尋列上的按鈕會亮著，看得出目前是開的）。
DEFAULT_FIND_CASE_SENSITIVE = False
DEFAULT_FIND_WHOLE_WORDS = False

# --- 搜尋列的位置（浮動面板）------------------------------------------------
# 搜尋列不釘在版面裡，是一塊浮在內文上的面板，抓住空白處可以拖到任何位置。
# 位置記成「最近的角落 + 對該角落的位移」而不是絕對座標：視窗改變大小時，
# 靠右的面板要跟著右邊走（瀏覽器的搜尋框都是這樣貼著右上），存絕對座標做不到。
FIND_BAR_CORNERS = ("TL", "TR", "BL", "BR")
DEFAULT_FIND_BAR_CORNER = "TR"
DEFAULT_FIND_BAR_OFFSET = (12, 10)
KEY_FIND_BAR_CORNER = "find/barCorner"
KEY_FIND_BAR_OFFSET_X = "find/barOffsetX"
KEY_FIND_BAR_OFFSET_Y = "find/barOffsetY"

# --- Mermaid 圖表 -----------------------------------------------------------
# ```mermaid 區塊由 app/mermaid 自己解析、排版、用 QPainter 畫成圖片嵌入文件
# （QTextDocument 跑不了 JavaScript，內嵌瀏覽器要多 150MB，都不採用）。
# 支援 flowchart 與 sequence diagram 的子集；其餘類型維持顯示原始碼。
#
# 上限是為了把「一張圖」的解析與版面成本壓在百毫秒內：版面演算法是純 Python 的
# 分層排版，節點數上去是超線性的。超過就退回顯示原始碼並標示，不硬畫。
MERMAID_MAX_NODES = 300
MERMAID_MAX_EDGES = 600
MERMAID_MAX_MESSAGES = 400
MERMAID_MAX_SOURCE_BYTES = 64 * 1024
# 巢狀深度上限（subgraph 與序列圖的 loop/alt…）。版面與組場景是「一層一層」
# 遞迴的，兩萬字節的來源就能疊出上千層、打穿直譯器的遞迴上限——那會變成
# 當機對話框而不是「解析失敗」標示。60 層遠超任何真實圖表。
MERMAID_MAX_DEPTH = 60
# 已解析圖表的登錄表與畫好的圖片各留多少筆（LRU）。登錄表的鍵是原始碼雜湊，
# 同一張圖在不同分頁、不同主題下共用同一筆。
#
# 登錄表放大方：被擠掉的鍵會讓「HTML 已快取、圖表卻查不到」的分頁永遠顯示
# 破圖佔位（重新整理 F5 才會重新解析登錄）。一筆就是幾個 dataclass，4096 筆
# 也只是幾 MB；要在一個行程裡塞爆它得看過四千多張「內容都不同」的圖。
MERMAID_DIAGRAM_CACHE = 4096
MERMAID_IMAGE_CACHE = 128
# 已排好版的場景（Scene）。場景不含顏色也不含縮放，換主題、換欄寬、加搜尋
# 高亮都能重用同一份——這正是「帶著搜尋詞重畫」只付繪製那 3.2ms、不必再付
# 一次量測與版面的關鍵。搜尋列也靠它拿圖表裡的文字來比對。
MERMAID_SCENE_CACHE = 256

# --- 分頁 -------------------------------------------------------------------
TAB_HEIGHT = 34
TAB_INSERT_MARKER_WIDTH = 3   # 插入位置指示線的線寬
TAB_INSERT_CAP_SIZE = 13      # 線上下兩端的三角帽邊長；也決定指示線的整體寬度
TAB_MAX_WIDTH = 190
TAB_MIN_WIDTH = 92
TAB_LABEL_WIDTH = 130            # 檔名可用寬度，超過就中間省略
# 關閉鈕是「疊在檔名上」的覆蓋層，不進版面——滑鼠移進移出不會改變分頁寬度。
# 底下鋪一條與分頁同色的襯底（右段實色、左段漸層淡出），字才不會和叉叉糊在一起。
TAB_CLOSE_SIZE = 18              # 關閉鈕邊長
TAB_CLOSE_ICON = 10              # 叉叉圖示邊長
TAB_CLOSE_MARGIN = 5             # 關閉鈕距分頁右緣
TAB_CLOSE_FADE = 16              # 襯底左側漸層淡出的寬度

KEY_RESTORE_TABS = "behavior/restoreTabs"
KEY_OPEN_TABS = "session/openTabs"
KEY_ACTIVE_TAB = "session/activeTab"
DEFAULT_RESTORE_TABS = False
# Ctrl+Shift+T 能找回的「最近關閉的分頁」筆數，每個視窗各自一份，視窗關閉即消失。
MAX_CLOSED_TABS = 10

# --- 單一實例 ---------------------------------------------------------------
def _login_session_id() -> int:
    """目前行程所在的登入工作階段編號（Windows）；查不到就回 0。"""
    try:
        import ctypes

        session = ctypes.c_ulong(0)
        kernel32 = ctypes.windll.kernel32
        if kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                         ctypes.byref(session)):
            return int(session.value)
    except (AttributeError, OSError):
        pass
    return 0


# 具名管道是整台機器共用的（它沒有互斥鎖那種 Local\ 前綴可用），兩個使用者同時
# 登入時，後登入的人雙擊 .md 會把路徑送進前一個人的視窗——在他看不到的桌面上
# 開了分頁，自己這邊什麼都沒發生。所以名稱帶上登入工作階段編號。
# 用工作階段編號而不是使用者名稱：同一個人從主控台與遠端桌面同時登入也是兩個
# 桌面，各自一個實例才對；而且編號是純數字，不必煩惱使用者名稱裡的中文與空白
# 在 Python 與 C++ 兩邊編碼是否一致。轉交器（src_cpp/md_open/main.cpp）用同樣的
# 方式組名稱：IPC_SERVER_BASE 對應它的 kPipeBase，改了要兩邊一起改，測試會比對。
# 這個註解以前寫「要含使用者名稱」但名稱其實什麼都沒含，排查多使用者互搶時會被
# 引到錯的方向——現在說的就是做的。
IPC_SERVER_BASE = "MarkdownReader.SingleInstance"
# 環境變數 MDREADER_PIPE_NAME 可整個換掉管道名稱（轉交器讀同一個變數）。給回歸
# 測試用：測試自己開的本體與轉交器走專屬管道，和你正在用的閱讀器互不相干；
# 搭配 MDREADER_PORTABLE_ROOT 把設定也隔開，測試就不必再要求「先關掉程式」。
IPC_SERVER_NAME = os.environ.get("MDREADER_PIPE_NAME") or f"{IPC_SERVER_BASE}.{_login_session_id()}"
IPC_CONNECT_TIMEOUT_MS = 150     # 連不到就當作沒有既有實例，不要卡住啟動
IPC_WRITE_TIMEOUT_MS = 1000
# 送出後等對方關閉連線的確認。只是保險，等不到也不影響正確性
# （資料已經在管道裡），所以放短一點，別讓使用者對著沒反應的畫面枯等。
IPC_DISCONNECT_TIMEOUT_MS = 300
# 對方已經斷線、但資料還沒被拉進 Qt 讀取緩衝區時，補讀一次的等待上限。
IPC_READ_TIMEOUT_MS = 200
