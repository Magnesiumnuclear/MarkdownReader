"""設定面板（Ctrl+, 或標題列的齒輪鈕）。

覆蓋標題列以下的整個視窗區域，是一頁完整的設定畫面而不是一條窄列，
因此每個項目都能配上名稱與說明文字。標題列保持可見，除了必須留著拖曳與
關閉視窗的入口之外，齒輪鈕本身也是收起面板的方式之一。

版面：頁首（標題 + 關閉鈕）＋ 可垂直捲動的內容區，內容分成「外觀」與
「行為」兩組，最後是恢復預設值。所有外觀來自 styles.py 的集中 QSS，
本模組不呼叫 setStyleSheet。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import config
from .title_bar import IconButton

# 左側名稱欄的固定寬度，讓所有控制項對齊在同一直線上
LABEL_WIDTH = 96


class _Chip(QToolButton):
    """分段選擇／開關用的按鈕。"""

    def __init__(self, text: str, tooltip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segBtn")
        self.setText(text)
        if tooltip:
            self.setToolTip(tooltip)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class SettingsPanel(QWidget):
    """覆蓋式設定面板。"""

    themeModeChanged = pyqtSignal(str)      # "light" / "dark" / "system"
    lineHeightChanged = pyqtSignal(str)     # LINE_HEIGHT_OPTIONS 的 key
    contentWidthChanged = pyqtSignal(int)   # 像素，0 代表不限
    autoReloadChanged = pyqtSignal(bool)
    alwaysOnTopChanged = pyqtSignal(bool)
    statusBarChanged = pyqtSignal(bool)
    confirmLinksChanged = pyqtSignal(bool)
    restoreTabsChanged = pyqtSignal(bool)
    resetRequested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        # 面板是不透明的覆蓋層，必須自行填滿背景才不會透出底下的內文。
        # 純 QWidget 預設不會套用 QSS 的 background-color，一定要開
        # WA_StyledBackground 才會由樣式表繪製背景。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._icon_buttons: list[IconButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("settingsScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self._scroll, 1)

        content = QWidget(self._scroll)
        content.setObjectName("settingsContent")
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(30, 22, 30, 26)
        self._body.setSpacing(0)
        self._scroll.setWidget(content)

        self._add_section("外觀")
        self._build_theme_row()
        self._build_font_row()
        self._build_line_height_row()
        self._build_width_row()

        self._add_section("行為")
        self._build_behavior_rows()

        self._add_section("重設")
        self._build_reset_row()

        self._body.addStretch(1)
        self.hide()

    # -- 版面元件 ------------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QFrame(self)
        header.setObjectName("settingsHeader")
        header.setFixedHeight(44)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(30, 0, 10, 0)
        layout.setSpacing(0)

        title = QLabel("設定", header)
        title.setObjectName("settingsTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        self.close_button = IconButton("close", "關閉設定 (Esc)", header, size=(30, 26))
        self.close_button.setObjectName("stepBtn")
        self._icon_buttons.append(self.close_button)
        self.close_button.clicked.connect(self.deactivate)
        layout.addWidget(self.close_button)
        return header

    def _add_section(self, title: str) -> None:
        wrapper = QWidget(self)
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 22, 0, 10)
        layout.setSpacing(10)

        label = QLabel(title.upper() if title.isascii() else title, wrapper)
        label.setObjectName("settingsSection")
        layout.addWidget(label)

        line = QFrame(wrapper)
        line.setObjectName("settingsSectionLine")
        line.setFixedHeight(1)
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(line, 1)
        self._body.addWidget(wrapper)

    def _add_row(self, label_text: str, control: QWidget, hint: str = "") -> None:
        """一列設定：左側名稱、右側控制項，控制項下方可加說明文字。"""
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(14)

        label = QLabel(label_text, row)
        label.setObjectName("settingsRowLabel")
        label.setFixedWidth(LABEL_WIDTH)
        label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        label.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(label)

        right = QWidget(row)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.addWidget(control)
        if hint:
            note = QLabel(hint, right)
            note.setObjectName("settingsHint")
            note.setWordWrap(True)
            right_layout.addWidget(note)
        layout.addWidget(right, 1)

        self._body.addWidget(row)

    @staticmethod
    def _chip_row(parent: QWidget, chips: list[QWidget]) -> QWidget:
        holder = QWidget(parent)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for chip in chips:
            layout.addWidget(chip)
        layout.addStretch(1)
        return holder

    # -- 外觀區 --------------------------------------------------------------
    def _build_theme_row(self) -> None:
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_chips: dict[str, _Chip] = {}
        chips: list[QWidget] = []
        for mode, label in config.THEME_MODES:
            chip = _Chip(label, f"主題模式：{label}", self)
            self._theme_chips[mode] = chip
            self._theme_group.addButton(chip)
            chip.clicked.connect(lambda _c, m=mode: self.themeModeChanged.emit(m))
            chips.append(chip)
        self._add_row(
            "主題",
            self._chip_row(self, chips),
            "「跟隨系統」會採用 Windows 目前的深淺色設定，"
            "並在你切換系統配色時即時跟著變；選擇淺色或深色則會鎖定。",
        )

    def _build_font_row(self) -> None:
        holder = QWidget(self)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.font_minus = IconButton("minus", "縮小字級 (Ctrl+-)", holder, size=(30, 28))
        self.font_minus.setObjectName("stepBtn")
        self.font_plus = IconButton("plus", "放大字級 (Ctrl++)", holder, size=(30, 28))
        self.font_plus.setObjectName("stepBtn")
        self._icon_buttons += [self.font_minus, self.font_plus]

        self._font_value = QLabel("11 pt", holder)
        self._font_value.setObjectName("settingsValue")
        self._font_value.setFixedWidth(52)
        self._font_value.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.font_minus)
        layout.addWidget(self._font_value)
        layout.addWidget(self.font_plus)
        layout.addStretch(1)
        self._add_row(
            "字級",
            holder,
            f"{config.MIN_FONT_POINT_SIZE}～{config.MAX_FONT_POINT_SIZE} pt，"
            "與 Ctrl 加號／減號／0 連動。標題與程式碼會等比縮放。",
        )

    def _build_line_height_row(self) -> None:
        self._line_group = QButtonGroup(self)
        self._line_group.setExclusive(True)
        self._line_chips: dict[str, _Chip] = {}
        chips: list[QWidget] = []
        for key, label, percent in config.LINE_HEIGHT_OPTIONS:
            chip = _Chip(f"{label} {percent}%", f"內文行高 {percent}%", self)
            self._line_chips[key] = chip
            self._line_group.addButton(chip)
            chip.clicked.connect(lambda _c, k=key: self.lineHeightChanged.emit(k))
            chips.append(chip)
        self._add_row("行高", self._chip_row(self, chips), "調整內文與清單的行距。")

    def _build_width_row(self) -> None:
        self._width_group = QButtonGroup(self)
        self._width_group.setExclusive(True)
        self._width_chips: dict[int, _Chip] = {}
        chips: list[QWidget] = []
        for width, label in config.CONTENT_WIDTH_OPTIONS:
            text = label if width == 0 else f"{label} {width}px"
            tip = "不限制行寬，文字填滿視窗" if width == 0 else f"文字欄固定 {width}px 並置中"
            chip = _Chip(text, tip, self)
            self._width_chips[width] = chip
            self._width_group.addButton(chip)
            chip.clicked.connect(lambda _c, w=width: self.contentWidthChanged.emit(w))
            chips.append(chip)
        self._add_row(
            "內文寬度",
            self._chip_row(self, chips),
            "限制文字欄寬度並置中。視窗拉寬時文字不會變成超長的一行，長文閱讀舒適很多。",
        )

    # -- 行為區 --------------------------------------------------------------
    def _build_behavior_rows(self) -> None:
        self.auto_reload_chip = _Chip("自動重載", parent=self)
        self.always_on_top_chip = _Chip("釘選最上層", parent=self)
        self.status_bar_chip = _Chip("顯示狀態列", parent=self)
        self.confirm_links_chip = _Chip("開啟前詢問", parent=self)
        self.restore_tabs_chip = _Chip("還原上次分頁", parent=self)

        rows = (
            (
                "檔案監看",
                self.auto_reload_chip,
                self.autoReloadChanged,
                "外部編輯器存檔後自動重新渲染，並保留目前的閱讀位置。",
            ),
            (
                "視窗",
                self.always_on_top_chip,
                self.alwaysOnTopChanged,
                "把視窗固定在其他程式之上，等同 Ctrl+P。",
            ),
            (
                "狀態列",
                self.status_bar_chip,
                self.statusBarChanged,
                "底部顯示檔案路徑、編碼、大小、行數與修改時間，等同 Ctrl+/。",
            ),
            (
                "外部連結",
                self.confirm_links_chip,
                self.confirmLinksChanged,
                "點擊外部連結時先跳出確認，再交給系統預設瀏覽器開啟。",
            ),
            (
                "分頁",
                self.restore_tabs_chip,
                self.restoreTabsChanged,
                "關閉程式時記住開著哪些檔案，下次啟動自動還原。"
                "還原的分頁會等你切過去才載入內容，不影響啟動速度。",
            ),
        )
        for label, chip, signal, hint in rows:
            chip.toggled.connect(signal)
            self._add_row(label, self._chip_row(self, [chip]), hint)

    def _build_reset_row(self) -> None:
        self.reset_button = QToolButton(self)
        self.reset_button.setObjectName("resetBtn")
        self.reset_button.setText("恢復預設值")
        self.reset_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.reset_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reset_button.clicked.connect(self.resetRequested)
        self._add_row(
            "",
            self._chip_row(self, [self.reset_button]),
            "把上面所有設定恢復成預設值，主題也會回到「跟隨系統」。",
        )

    # -- 對外介面 ------------------------------------------------------------
    def apply_theme(self, theme: str) -> None:
        for button in self._icon_buttons:
            button.apply_theme(theme)

    def sync(
        self,
        *,
        theme_mode: str,
        font_point_size: int,
        line_height: str,
        content_width: int,
        auto_reload: bool,
        always_on_top: bool,
        status_bar: bool,
        confirm_links: bool,
        restore_tabs: bool,
    ) -> None:
        """依目前設定更新所有控制項外觀（不會反向送出訊號）。"""
        self._check(self._theme_chips.get(theme_mode))
        self._check(self._line_chips.get(line_height))
        self._check(self._width_chips.get(content_width))
        self._font_value.setText(f"{font_point_size} pt")
        self.font_minus.setEnabled(font_point_size > config.MIN_FONT_POINT_SIZE)
        self.font_plus.setEnabled(font_point_size < config.MAX_FONT_POINT_SIZE)

        for chip, value in (
            (self.auto_reload_chip, auto_reload),
            (self.always_on_top_chip, always_on_top),
            (self.status_bar_chip, status_bar),
            (self.confirm_links_chip, confirm_links),
            (self.restore_tabs_chip, restore_tabs),
        ):
            chip.blockSignals(True)
            chip.setChecked(value)
            chip.blockSignals(False)

    @staticmethod
    def _check(chip: _Chip | None) -> None:
        if chip is None:
            return
        chip.blockSignals(True)
        chip.setChecked(True)
        chip.blockSignals(False)

    def activate(self) -> None:
        self.show()
        self.raise_()

    def deactivate(self) -> None:
        self.hide()
        self.closed.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.deactivate()
        else:
            self.activate()
