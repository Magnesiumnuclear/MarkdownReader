"""一個分頁所擁有的狀態。

每個分頁都有自己的閱讀區、捲動位置、返回歷史與檔案時間戳，彼此不互相影響。

【延後載入】
還原上次的分頁時，只先建立空的分頁物件並記住路徑，等使用者真的切過去才讀檔與
渲染。否則還原十個分頁就要付十次 Markdown 轉換的代價，啟動會明顯變慢。

【dirty 旗標】
主題、字級、行高改變時，只重繪目前看到的分頁，其餘標記為待重繪，
切過去時才處理。這樣切換主題不會因為開了很多分頁而卡住。
"""

from __future__ import annotations

import os

from . import document
from .browser import MarkdownBrowser


class DocumentTab:
    """單一分頁的內容與狀態。"""

    def __init__(self, path: str | None = None) -> None:
        self.browser = MarkdownBrowser()
        self.path: str | None = os.path.abspath(path) if path else None
        self.text: str = ""
        self.meta: document.DocumentMeta | None = None
        self.error: document.DocumentError | None = None

        self.history: list[str] = []          # 返回用的路徑堆疊
        self.file_stamp: tuple[float, int] | None = None
        self.pending_anchor: str = ""

        self.loaded: bool = False             # 是否已讀進檔案內容
        self.dirty: bool = True               # 是否需要重新渲染
        # 跨視窗搬移時暫存的捲動比例：take_tab 寫入、adopt_tab 讀走後清空
        self.transfer_scroll: float | None = None
        # 主題 -> (產生它的文字物件, HTML)。見 build_html 的說明。
        self._html_cache: dict[str, tuple[str, str]] = {}
        self.has_scalable_images: bool = False
        self.last_render_width: int = 0

    # -- 顯示用名稱 ----------------------------------------------------------
    @property
    def display_name(self) -> str:
        if self.path:
            return os.path.basename(self.path) or self.path
        return "新分頁"

    @property
    def tooltip(self) -> str:
        return self.path or "尚未開啟檔案"

    # -- 讀檔 ----------------------------------------------------------------
    def load(self) -> None:
        """把檔案讀進來；失敗時記錄錯誤，不拋出。"""
        self.loaded = True
        self.dirty = True
        if not self.path:
            self.text, self.meta, self.error, self.file_stamp = "", None, None, None
            return
        try:
            self.text, self.meta = document.read_text_file(self.path)
            self.error = None
            self.file_stamp = self.stamp()
        except document.DocumentError as error:
            self.text, self.meta = "", None
            self.error = error
            self.file_stamp = None

    def stamp(self) -> tuple[float, int] | None:
        """檔案的 (修改時間, 大小)，用來判斷內容是否真的變了。"""
        if not self.path:
            return None
        try:
            info = os.stat(self.path)
            return (info.st_mtime, info.st_size)
        except OSError:
            return None

    # -- 產生 HTML -----------------------------------------------------------
    def build_html(self, theme: str) -> str:
        """產生（或取用快取的）HTML。

        【為什麼要快取】
        字級縮放、內文寬度、視窗縮放都會重新渲染，但這些只影響 Qt 的排版，
        Markdown→HTML 的結果完全一樣。實測 1MB 的文件光轉換就要 518 ms，
        每按一次 Ctrl+加號都重付一次。

        鍵用「文字物件的識別 + 主題」：
        - 識別（is）而不是內容比對，避免每次都掃過整份文字。重新載入會產生
          新的字串物件，識別自然不同 -> 自動失效，不需要另外清快取。
        - 主題要入鍵：程式碼高亮的顏色是用 inline style 寫死在 HTML 裡的
          （codehilite noclasses），換主題必須重新轉換。
        兩個主題各留一份，切回上一個主題就是直接命中。
        """
        if self.error is not None:
            return document.render_error(self.error, self.path or "")
        if self.meta is None:
            return document.render_welcome()

        cached = self._html_cache.get(theme)
        if cached is not None and cached[0] is self.text:
            return cached[1]
        rendered = document.render_document(self.text, self.meta, theme)
        self._html_cache[theme] = (self.text, rendered)
        return rendered

    # -- 捲動位置 ------------------------------------------------------------
    def scroll_ratio(self) -> float:
        bar = self.browser.verticalScrollBar()
        return bar.value() / bar.maximum() if bar.maximum() else 0.0

    def apply_scroll_ratio(self, ratio: float) -> None:
        bar = self.browser.verticalScrollBar()
        bar.setValue(int(round(ratio * bar.maximum())))
