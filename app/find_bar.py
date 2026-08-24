"""文件內搜尋列（Ctrl+F）。

Enter 找下一個、Shift+Enter 找上一個、Esc 關閉，並以背景色高亮所有符合項，
目前所在的那一項使用另一種顏色以便辨識。外觀同樣由集中 QSS 控制。
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QTextBrowser, QTextEdit, QWidget

from . import config, styles
from .title_bar import IconButton


class FindBar(QWidget):
    """底部搜尋列。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("findBar")
        self._browser: QTextBrowser | None = None
        self._match_bg = QColor("#fff3c4")
        self._current_bg = QColor("#ffd33d")
        self._text_color = QColor("#1f2328")
        self._matches: list[int] = []
        self._current_index = -1

        tool_size = config.TOOL_BUTTON_SIZE

        self.input = QLineEdit(self)
        self.input.setObjectName("findInput")
        self.input.setPlaceholderText("搜尋文件內容…")
        self.input.setClearButtonEnabled(False)
        self.input.installEventFilter(self)

        self.status = QLabel("", self)
        self.status.setObjectName("findStatus")

        self.prev_button = IconButton("arrow_up", "上一個 (Shift+Enter)", self, size=tool_size)
        self.next_button = IconButton("arrow_down", "下一個 (Enter)", self, size=tool_size)
        self.close_button = IconButton("close", "關閉搜尋 (Esc)", self, size=tool_size)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 8, 5)
        layout.setSpacing(4)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.prev_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.close_button)

        self.input.textChanged.connect(self._on_text_changed)
        self.prev_button.clicked.connect(lambda: self.search(forward=False))
        self.next_button.clicked.connect(lambda: self.search(forward=True))
        self.close_button.clicked.connect(self.deactivate)

        self.hide()

    # -- 對外介面 ------------------------------------------------------------
    def attach(self, browser: QTextBrowser) -> None:
        self._browser = browser

    def apply_theme(self, theme: str) -> None:
        colors = styles.palette(theme)
        self._match_bg = QColor(colors["find_match_bg"])
        self._current_bg = QColor(colors["find_current_bg"])
        self._text_color = QColor(colors["text"])
        for button in (self.prev_button, self.next_button, self.close_button):
            button.apply_theme(theme)
        if self.isVisible():
            self._refresh_highlights()

    def activate(self) -> None:
        """顯示搜尋列並聚焦；若閱讀區有選取文字則帶入為搜尋詞。"""
        if self._browser is not None:
            selected = self._browser.textCursor().selectedText().strip()
            if selected and " " not in selected:
                self.input.setText(selected)
        self.show()
        self.input.setFocus()
        self.input.selectAll()
        self._recompute()

    def deactivate(self) -> None:
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
    def _on_text_changed(self, _text: str) -> None:
        self._recompute()

    def _recompute(self) -> None:
        """重新計算所有符合位置，並跳到游標之後的第一個。"""
        self._matches = []
        self._current_index = -1
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
            self.status.setText("無相符")
            return

        anchor = self._browser.textCursor().selectionStart()
        self._current_index = next(
            (i for i, pos in enumerate(self._matches) if pos >= anchor), 0
        )
        self._goto_current()

    def search(self, forward: bool = True) -> None:
        """跳到下一個／上一個符合項（循環）。"""
        if not self._matches:
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
        self.status.setText(f"{self._current_index + 1} / {len(self._matches)}")
        self._refresh_highlights()

    # -- 高亮 ----------------------------------------------------------------
    def _refresh_highlights(self) -> None:
        if self._browser is None:
            return
        needle_length = len(self.input.text())
        selections: list[QTextEdit.ExtraSelection] = []
        for index, start in enumerate(self._matches):
            selection = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self._browser.document())
            cursor.setPosition(start)
            cursor.setPosition(start + needle_length, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            selection.format.setBackground(
                self._current_bg if index == self._current_index else self._match_bg
            )
            selection.format.setForeground(self._text_color)
            selections.append(selection)
        self._browser.setExtraSelections(selections)

    def _clear_highlights(self) -> None:
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
