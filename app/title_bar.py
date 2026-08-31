"""自訂標題列。

無邊框視窗沒有系統標題列，這裡自行實作拖曳、檔名顯示與各項控制按鈕。

所有外觀（底色、hover、圓角）都來自 styles.py 的集中 QSS，本模組不呼叫
setStyleSheet；唯一在程式碼中處理的視覺行為是「圖示隨 hover 換色」——
QSS 無法替 SVG 圖示重新著色，必須在 enter/leave 事件切換 QIcon。
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QMouseEvent
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from . import config, icons, styles
from .language import t


class IconButton(QToolButton):
    """以 SVG 檔為圖示的工具按鈕。

    圖示顏色會隨主題與 hover 狀態改變，勾選狀態可另外指定圖示與顏色。
    """

    def __init__(
        self,
        icon_name: str,
        tooltip_key: str,
        parent: QWidget | None = None,
        *,
        danger: bool = False,
        checked_icon: str | None = None,
        size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._checked_icon = checked_icon
        self._danger = danger
        self._hovered = False
        self._color_normal = "#000000"
        self._color_active = "#000000"
        self._color_checked = "#000000"
        self._color_on_danger = "#ffffff"
        # 存的是翻譯鍵不是字串：tooltip 原本只在這裡 setToolTip 一次、來源沒留下，
        # 語言一換十七顆按鈕的提示會全部停在舊語言。作法比照下面的 apply_theme
        # ——保留來源（那裡是主題色，這裡是翻譯鍵），要換的時候重算。
        self._tooltip_key = tooltip_key
        self._tooltip_args: dict = {}

        self.setObjectName("closeBtn" if danger else "navBtn")
        self.setToolTip(t(tooltip_key))
        self.setFixedSize(QSize(*(size or config.TITLE_BUTTON_SIZE)))
        self.setIconSize(QSize(config.ICON_PIXEL_SIZE, config.ICON_PIXEL_SIZE))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if checked_icon is not None:
            self.setCheckable(True)
            self.toggled.connect(lambda _checked: self.refresh_icon())

    def apply_theme(self, theme: str) -> None:
        colors = styles.palette(theme)
        self._color_normal = colors["icon"]
        self._color_active = colors["icon_active"]
        self._color_checked = colors["accent"]
        self._color_on_danger = colors["icon_on_accent"]
        self.refresh_icon()

    def set_tooltip_key(self, key: str, **kwargs) -> None:
        """換掉 tooltip 的來源鍵。狀態相依的按鈕（主題、最大化）用這個。"""
        self._tooltip_key = key
        self._tooltip_args = kwargs
        self.setToolTip(t(key, **kwargs))

    def apply_language(self) -> None:
        """就地重設 tooltip（語言切換）。"""
        self.setToolTip(t(self._tooltip_key, **self._tooltip_args))

    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        self.refresh_icon()

    def refresh_icon(self) -> None:
        checked = self.isCheckable() and self.isChecked()
        name = self._checked_icon if (checked and self._checked_icon) else self._icon_name
        if self._danger and self._hovered:
            color = self._color_on_danger
        elif checked:
            color = self._color_checked
        elif self._hovered:
            color = self._color_active
        else:
            color = self._color_normal
        self.setIcon(icons.icon(name, color))

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt 事件命名)
        self._hovered = True
        self.refresh_icon()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.refresh_icon()
        super().leaveEvent(event)


class CustomTitleBar(QWidget):
    """視窗頂端的自訂標題列。"""

    openRequested = pyqtSignal()
    backRequested = pyqtSignal()
    findRequested = pyqtSignal()
    settingsRequested = pyqtSignal()
    themeToggleRequested = pyqtSignal()
    pinToggled = pyqtSignal(bool)
    minimizeRequested = pyqtSignal()
    maximizeToggleRequested = pyqtSignal()
    closeRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(config.TITLE_BAR_HEIGHT)

        self._theme = config.DEFAULT_THEME
        self._maximized = False
        self._full_title = t("app.displayName")
        self._drag_offset = None

        tool_size = config.TOOL_BUTTON_SIZE

        self._icon_label = QLabel(self)
        self._icon_label.setObjectName("titleIcon")
        self._icon_label.setPixmap(icons.pixmap("app", "#ffffff", 16))
        # 讓標題文字與圖示區域也能用來拖曳視窗
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._title_label = QLabel(self._full_title, self)
        self._title_label.setObjectName("titleLabel")
        self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.back_button = IconButton("back", "titleBar.back", self, size=tool_size)
        self.open_button = IconButton("open", "titleBar.open", self, size=tool_size)
        self.find_button = IconButton("search", "titleBar.find", self, size=tool_size)
        self.settings_button = IconButton("settings", "titleBar.settings", self, size=tool_size)
        self.theme_button = IconButton("theme_dark", "titleBar.theme", self, size=tool_size)
        self.pin_button = IconButton(
            "pin_off", "titleBar.pin", self, checked_icon="pin_on", size=tool_size
        )
        self.minimize_button = IconButton("minimize", "titleBar.minimize", self)
        self.maximize_button = IconButton("maximize", "titleBar.maximize", self)
        self.close_button = IconButton("close", "titleBar.close", self, danger=True)

        self.back_button.setEnabled(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label, 1)
        layout.addWidget(self.back_button)
        layout.addWidget(self.open_button)
        layout.addWidget(self.find_button)
        layout.addWidget(self.settings_button)
        layout.addWidget(self.theme_button)
        layout.addWidget(self.pin_button)
        layout.addSpacing(6)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

        self.back_button.clicked.connect(self.backRequested)
        self.open_button.clicked.connect(self.openRequested)
        self.find_button.clicked.connect(self.findRequested)
        self.settings_button.clicked.connect(self.settingsRequested)
        self.theme_button.clicked.connect(self.themeToggleRequested)
        self.pin_button.toggled.connect(self.pinToggled)
        self.minimize_button.clicked.connect(self.minimizeRequested)
        self.maximize_button.clicked.connect(self.maximizeToggleRequested)
        self.close_button.clicked.connect(self.closeRequested)

    # -- 外觀 ----------------------------------------------------------------
    @property
    def _buttons(self) -> tuple[IconButton, ...]:
        return (
            self.back_button,
            self.open_button,
            self.find_button,
            self.settings_button,
            self.theme_button,
            self.pin_button,
            self.minimize_button,
            self.maximize_button,
            self.close_button,
        )

    def apply_theme(self, theme: str) -> None:
        """套用主題色到所有圖示，並讓主題按鈕顯示「將切換到的」樣貌。"""
        # 記下來給 apply_language 用：那時要重算主題按鈕的 tooltip，而它依主題而定
        self._theme = theme
        # 目前是淺色 -> 顯示月亮（點下去會變深色），反之亦然
        self.theme_button.set_icon_name(
            "theme_dark" if theme == "light" else "theme_light"
        )
        self._apply_theme_button_tooltip()
        for button in self._buttons:
            button.apply_theme(theme)

    def _apply_theme_button_tooltip(self) -> None:
        """「切換到深色主題」——整句一個鍵、把「深色」當參數代入。

        不在程式碼裡拼接：英文是 "Switch to dark theme"，語序和中文不同，
        拼接出來的順序永遠會是錯的。
        """
        target_key = (
            "titleBar.themeTarget.dark" if self._theme == "light"
            else "titleBar.themeTarget.light"
        )
        self.theme_button.set_tooltip_key(
            "titleBar.themeToggle", target=t(target_key)
        )

    def apply_language(self) -> None:
        """語言切換：所有按鈕的 tooltip 與標題重設一次。"""
        for button in self._buttons:
            button.apply_language()
        # 這兩顆依狀態而定，而且主題那顆是「翻譯字串裡再套一個翻譯過的詞」，
        # 參數不能凍在按鈕裡，得重跑產生它的邏輯
        self._apply_theme_button_tooltip()
        self.set_maximized(self._maximized)
        self._update_elided_title()

    def set_maximized(self, maximized: bool) -> None:
        self._maximized = maximized
        self.maximize_button.set_icon_name("restore" if maximized else "maximize")
        self.maximize_button.set_tooltip_key(
            "titleBar.restore" if maximized else "titleBar.maximize"
        )

    def set_pinned(self, pinned: bool) -> None:
        if self.pin_button.isChecked() != pinned:
            self.pin_button.blockSignals(True)
            self.pin_button.setChecked(pinned)
            self.pin_button.blockSignals(False)
            self.pin_button.refresh_icon()

    def set_back_enabled(self, enabled: bool) -> None:
        self.back_button.setEnabled(enabled)

    def set_title(self, title: str) -> None:
        self._full_title = title
        self._update_elided_title()

    def _update_elided_title(self) -> None:
        metrics = QFontMetrics(self._title_label.font())
        available = max(40, self._title_label.width() - 8)
        self._title_label.setText(
            metrics.elidedText(self._full_title, Qt.TextElideMode.ElideMiddle, available)
        )
        self._title_label.setToolTip(self._full_title)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elided_title()

    # -- 拖曳與雙擊 ----------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        window = self.window()
        handle = window.windowHandle()
        # Qt 6 的原生移動，可保留 Windows 的貼齊（Aero Snap）行為
        if handle is not None and handle.startSystemMove():
            self._drag_offset = None
            event.accept()
            return
        # 原生移動不可用時的後備方案：手動位移
        self._drag_offset = (
            event.globalPosition().toPoint() - window.frameGeometry().topLeft()
        )
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is None:
            super().mouseMoveEvent(event)
            return
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        window.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximizeToggleRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
