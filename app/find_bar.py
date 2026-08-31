"""文件內搜尋列（Ctrl+F）。

Enter 找下一個、Shift+Enter 找上一個、Esc 關閉，並以背景色高亮所有符合項，
目前所在的那一項使用另一種顏色以便辨識。外觀同樣由集中 QSS 控制。

【大文件上的兩個成本，以及對應的處置】
1. 比對要掃過整份文件。每按一個鍵就掃一次，長文件會打到卡字，因此輸入改走
   防抖：停手 DEBOUNCE_MS 之後才真的掃。Enter 會先把還在等的那次兌現，
   不會拿上一個鍵打完時的舊結果來跳。
2. 高亮是把 N 個 ExtraSelection 交給 Qt。按「下一個」時其實只有兩筆換色
   （原本那筆變回一般色、新的那筆變成強調色），所以整份 selection 留著重用、
   只換那兩筆；另外以 MAX_HIGHLIGHTS 限制同時交出去的筆數。
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QTextBrowser, QTextEdit, QWidget

from . import config, styles
from .language import t
from .title_bar import IconButton


class FindBar(QWidget):
    """底部搜尋列。"""

    closed = pyqtSignal()

    # 停止輸入多久（毫秒）之後才真的去掃文件
    DEBOUNCE_MS = 120
    # 一次最多交給 Qt 幾筆高亮。比對數量本身不受此限（狀態列的 N / M 仍是實數），
    # 有上限的只是「畫出來」這件事；超過時取以目前這筆為中心的一段窗格，
    # 使用者所在的位置附近一定畫得到。
    MAX_HIGHLIGHTS = 2000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("findBar")
        self._browser: QTextBrowser | None = None
        self._match_bg = QColor("#fff3c4")
        self._current_bg = QColor("#ffd33d")
        self._text_color = QColor("#1f2328")
        self._matches: list[int] = []
        self._current_index = -1
        # 高亮快取：_selections 對應 _matches[_window[0]:_window[1]]，
        # _painted_current 是目前畫成強調色的那一筆在 _matches 裡的索引
        self._selections: list[QTextEdit.ExtraSelection] = []
        self._window: tuple[int, int] = (0, 0)
        self._painted_current = -1

        tool_size = config.TOOL_BUTTON_SIZE

        self.input = QLineEdit(self)
        self.input.setObjectName("findInput")
        self.input.setPlaceholderText(t("find.placeholder"))
        self.input.setClearButtonEnabled(False)
        self.input.installEventFilter(self)

        self.status = QLabel("", self)
        self.status.setObjectName("findStatus")

        self.prev_button = IconButton("arrow_up", "find.prev", self, size=tool_size)
        self.next_button = IconButton("arrow_down", "find.next", self, size=tool_size)
        self.close_button = IconButton("close", "find.close", self, size=tool_size)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 8, 5)
        layout.setSpacing(4)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.prev_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.close_button)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._recompute)

        self.input.textChanged.connect(self._on_text_changed)
        self.prev_button.clicked.connect(lambda: self.search(forward=False))
        self.next_button.clicked.connect(lambda: self.search(forward=True))
        self.close_button.clicked.connect(self.deactivate)

        self.hide()

    # -- 對外介面 ------------------------------------------------------------
    def attach(self, browser: QTextBrowser) -> None:
        self._browser = browser
        # 換了文件，快取裡那些 QTextCursor 全都綁在舊文件上，不能再用
        self._invalidate_highlights()

    def apply_theme(self, theme: str) -> None:
        colors = styles.palette(theme)
        self._match_bg = QColor(colors["find_match_bg"])
        self._current_bg = QColor(colors["find_current_bg"])
        self._text_color = QColor(colors["text"])
        for button in (self.prev_button, self.next_button, self.close_button):
            button.apply_theme(theme)
        # 配色換了，快取裡每一筆 selection 的 format 都是舊色，整份重做
        self._invalidate_highlights()
        if self.isVisible():
            self._sync_highlights()

    def apply_language(self) -> None:
        """語言切換：靜態文字重設，狀態文字交給既有的重算路徑。"""
        self.input.setPlaceholderText(t("find.placeholder"))
        for button in (self.prev_button, self.next_button, self.close_button):
            button.apply_language()
        if self.isVisible():
            # 「N / M」與「無相符」是狀態衍生的，refresh_for_new_document 本來
            # 就是為了「狀態衍生文字要重算」而存在，直接重用
            self.refresh_for_new_document()

    def refresh_for_new_document(self) -> None:
        """文件被重建（重新渲染、換主題、換字級）之後重新比對。

        setHtml 是把同一個 QTextDocument 清空再填回，先前建好的 QTextCursor
        會整批被折到位置 0——快取裡的高亮會整片塌在文件開頭。比對位置本身也
        可能因為內容變了而失效，所以整個重算，不是只重畫。
        """
        if not self.isVisible():
            return
        self._debounce.stop()
        self._recompute()

    def activate(self) -> None:
        """顯示搜尋列並聚焦；若閱讀區有選取文字則帶入為搜尋詞。"""
        if self._browser is not None:
            selected = self._browser.textCursor().selectedText().strip()
            if selected and " " not in selected:
                self.input.setText(selected)
        self.show()
        self.input.setFocus()
        self.input.selectAll()
        # 上面的 setText 可能已經排了一次防抖；這裡是明確的「立刻算」
        self._debounce.stop()
        self._recompute()

    def deactivate(self) -> None:
        self._debounce.stop()
        self.hide()
        self._clear_highlights()
        self._matches = []
        self._current_index = -1
        if self._browser is not None:
            cursor = self._browser.textCursor()
            cursor.clearSelection()
            self._browser.setTextCursor(cursor)
            self._browser.setFocus()
        self.closed.emit()

    # -- 搜尋 ----------------------------------------------------------------
    def _on_text_changed(self, text: str) -> None:
        # 清空只是把高亮拿掉、沒有掃描成本，立刻做比較跟手；
        # 有內容才防抖，避免每按一個鍵就掃一次整份文件
        if not text:
            self._debounce.stop()
            self._recompute()
            return
        self._debounce.start()

    def _flush_pending(self) -> bool:
        """把還在等的防抖立刻兌現，回傳是否真的兌現了。"""
        if not self._debounce.isActive():
            return False
        self._debounce.stop()
        self._recompute()
        return True

    def _recompute(self) -> None:
        """重新計算所有符合位置，並跳到游標之後的第一個。"""
        self._debounce.stop()
        self._matches = []
        self._current_index = -1
        self._invalidate_highlights()
        needle = self.input.text()
        if self._browser is None or not needle:
            self._clear_highlights()
            self.status.setText("")
            return

        document = self._browser.document()
        cursor = QTextCursor(document)
        while True:
            cursor = document.find(needle, cursor)
            if cursor.isNull():
                break
            self._matches.append(cursor.selectionStart())

        if not self._matches:
            self._clear_highlights()
            self.status.setText(t("find.noMatch"))
            return

        anchor = self._browser.textCursor().selectionStart()
        self._current_index = next(
            (i for i, pos in enumerate(self._matches) if pos >= anchor), 0
        )
        self._goto_current()

    def search(self, forward: bool = True) -> None:
        """跳到下一個／上一個符合項（循環）。"""
        # 防抖還在等的話先兌現——Enter 不能拿上一個鍵打完時的舊結果來跳。
        # 兌現本身會跳到游標之後的第一筆，接著再前進一步，結果與沒有防抖時
        # 「打字即比對、Enter 再往下一筆」完全一致。
        flushed = self._flush_pending()
        if not self._matches:
            if not flushed:
                self._recompute()
            return
        step = 1 if forward else -1
        self._current_index = (self._current_index + step) % len(self._matches)
        self._goto_current()

    def _goto_current(self) -> None:
        if self._browser is None or not self._matches:
            return
        needle_length = len(self.input.text())
        start = self._matches[self._current_index]
        cursor = QTextCursor(self._browser.document())
        cursor.setPosition(start)
        cursor.setPosition(start + needle_length, QTextCursor.MoveMode.KeepAnchor)
        self._browser.setTextCursor(cursor)
        self._browser.ensureCursorVisible()
        total = len(self._matches)
        # 沒有全部畫出來就要講：捲到遠處看見沒上色的相符字會像是壞掉。
        # 整句一個鍵而不是拼接——英文的括號是半形，那是翻譯的一部分。
        key = "find.countCapped" if total > self.MAX_HIGHLIGHTS else "find.count"
        self.status.setText(t(
            key, current=self._current_index + 1, total=total, cap=self.MAX_HIGHLIGHTS
        ))
        self._sync_highlights()

    # -- 高亮 ----------------------------------------------------------------
    def _window_bounds(self) -> tuple[int, int]:
        """這次要畫 _matches 的哪一段。"""
        total = len(self._matches)
        if total <= self.MAX_HIGHLIGHTS:
            return 0, total
        # 超過上限：取以目前這筆為中心的一段窗格。但只有在目前這筆跑出窗格外
        # 時才重新置中——每次都置中的話窗格會跟著滑動一格，下面那條快路徑就
        # 永遠用不到，等於白做。
        low, high = self._window
        if high - low == self.MAX_HIGHLIGHTS and low <= self._current_index < high:
            return low, high
        low = max(
            0,
            min(self._current_index - self.MAX_HIGHLIGHTS // 2,
                total - self.MAX_HIGHLIGHTS),
        )
        return low, low + self.MAX_HIGHLIGHTS

    def _make_selection(self, start: int, current: bool) -> QTextEdit.ExtraSelection:
        selection = QTextEdit.ExtraSelection()
        cursor = QTextCursor(self._browser.document())
        cursor.setPosition(start)
        cursor.setPosition(
            start + len(self.input.text()), QTextCursor.MoveMode.KeepAnchor
        )
        selection.cursor = cursor
        selection.format.setBackground(self._current_bg if current else self._match_bg)
        selection.format.setForeground(self._text_color)
        return selection

    def _sync_highlights(self) -> None:
        """把高亮同步到目前狀態；能只換兩筆就不要重建整份。"""
        if self._browser is None:
            return
        low, high = self._window_bounds()
        if self._selections and (low, high) == self._window:
            # 【快路徑】窗格沒動，換的只有「目前是哪一筆」：把原本那筆改回一般
            # 色、新的那筆改成強調色，其餘 N-2 筆的 selection 原封不動地重用。
            # 按住「下一個」連跳時走的就是這裡。
            for index in {self._painted_current, self._current_index}:
                if low <= index < high:
                    self._selections[index - low] = self._make_selection(
                        self._matches[index], index == self._current_index
                    )
        else:
            self._selections = [
                self._make_selection(self._matches[i], i == self._current_index)
                for i in range(low, high)
            ]
            self._window = (low, high)
        self._painted_current = self._current_index
        self._browser.setExtraSelections(self._selections)

    def _invalidate_highlights(self) -> None:
        """丟掉高亮快取。文件、配色或比對結果換過之後一定要呼叫。"""
        self._selections = []
        self._window = (0, 0)
        self._painted_current = -1

    def _clear_highlights(self) -> None:
        self._invalidate_highlights()
        if self._browser is not None:
            self._browser.setExtraSelections([])

    # -- 鍵盤 ----------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.deactivate()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                backward = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.search(forward=not backward)
                return True
        return super().eventFilter(watched, event)
