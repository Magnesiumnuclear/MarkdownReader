"""分頁列。

放在標題列下方而不是做進標題列：標題列已經有八個按鈕約 250px，而視窗最小寬度
只有 420px，分頁再擠進去就沒有空間了。

作用中的分頁底色與內容區相同、頂端一條強調色，視覺上和下方文件連成一片；
未選取的分頁融入分頁列底色。只有一個分頁時整條列會隱藏，外觀與沒有分頁功能時一致。

外觀全部來自 styles.py 的集中 QSS，本模組不呼叫 setStyleSheet。
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from . import config
from .title_bar import IconButton


class TabButton(QFrame):
    """單一分頁：檔名 + 關閉鈕。"""

    clicked = pyqtSignal()
    closeClicked = pyqtSignal()

    def __init__(
        self,
        name: str,
        tooltip: str,
        active: bool,
        theme: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._active = active
        self.setObjectName("tabActive" if active else "tab")
        self.setFixedHeight(config.TAB_HEIGHT)
        self.setMinimumWidth(config.TAB_MIN_WIDTH)
        self.setMaximumWidth(config.TAB_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip(tooltip)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 5, 0)
        row.setSpacing(6)

        self._label = QLabel(self)
        self._label.setObjectName("tabLabelActive" if active else "tabLabel")
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        metrics = QFontMetrics(self._label.font())
        self._label.setText(
            metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, config.TAB_LABEL_WIDTH)
        )
        row.addWidget(self._label, 1)

        self._close = IconButton("close", "關閉分頁", self, size=(18, 18))
        self._close.setObjectName("tabClose")
        self._close.setIconSize(QSize(10, 10))
        self._close.apply_theme(theme)
        self._close.clicked.connect(self.closeClicked)
        # 未選取的分頁平常不顯示關閉鈕，滑鼠移上去才出現，避免整列都是叉叉
        self._close.setVisible(active)
        row.addWidget(self._close)

    def apply_theme(self, theme: str) -> None:
        self._close.apply_theme(theme)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._close.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._close.setVisible(self._active)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            # 中鍵關閉，和瀏覽器一致
            self.closeClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class TabBar(QFrame):
    """整條分頁列。"""

    activated = pyqtSignal(int)
    closeRequested = pyqtSignal(int)
    newTabRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tabBar")
        self.setFixedHeight(config.TAB_HEIGHT + 1)
        self._theme = config.DEFAULT_THEME
        self._buttons: list[TabButton] = []

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("tabScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(self._scroll, 1)

        self._strip = QWidget(self._scroll)
        self._strip.setObjectName("tabStrip")
        self._row = QHBoxLayout(self._strip)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(0)
        self._row.addStretch(1)
        self._scroll.setWidget(self._strip)

        self.new_button = IconButton(
            "plus", "開新分頁 (Ctrl+T)", self, size=(30, config.TAB_HEIGHT)
        )
        self.new_button.setObjectName("tabNew")
        self.new_button.clicked.connect(self.newTabRequested)
        outer.addWidget(self.new_button)

        self.hide()

    # -- 內容 ----------------------------------------------------------------
    def set_tabs(self, entries: list[tuple[str, str]], active: int) -> None:
        """重建整列。entries 是 (顯示名稱, 完整路徑) 的清單。"""
        for button in self._buttons:
            button.setParent(None)
            button.deleteLater()
        self._buttons.clear()

        for index, (name, tooltip) in enumerate(entries):
            button = TabButton(name, tooltip, index == active, self._theme, self._strip)
            button.clicked.connect(lambda i=index: self.activated.emit(i))
            button.closeClicked.connect(lambda i=index: self.closeRequested.emit(i))
            self._row.insertWidget(index, button)
            self._buttons.append(button)

        # 只有一個分頁時整條列隱藏，維持原本的外觀
        self.setVisible(len(entries) > 1)
        if 0 <= active < len(self._buttons):
            self._scroll.ensureWidgetVisible(self._buttons[active], 40, 0)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        self.new_button.apply_theme(theme)
        for button in self._buttons:
            button.apply_theme(theme)
