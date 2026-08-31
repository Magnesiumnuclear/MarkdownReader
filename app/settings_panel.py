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

from . import config, language
from .language import t
from .title_bar import IconButton

# 左側名稱欄的固定寬度，讓所有控制項對齊在同一直線上。
# 隨語言變：這是 setFixedWidth，QLabel 放不下時是直接裁掉而不是加省略號，
# 英文的 "Content width" 在 96px 會被切一半。
LABEL_WIDTH_BY_LANGUAGE = {"zh_TW": 96, "en": 124}


def _label_width() -> int:
    return LABEL_WIDTH_BY_LANGUAGE.get(
        language.current(), LABEL_WIDTH_BY_LANGUAGE[config.DEFAULT_LANGUAGE]
    )


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

    languageModeChanged = pyqtSignal(str)   # "zh_TW" / "en" / "system"
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
        # 【翻譯登記表】_add_section / _add_row 建出來的 QLabel 原本是區域變數，
        # 建完就丟——面板上三分之二的字（分區標題、每列名稱、每條說明）因此
        # 根本改不了。改成建的時候登記進來，語言一換走訪這幾份清單重設文字，
        # 不必重建整個面板（重建會讓捲動位置歸零、chip 的勾選狀態要重同步，
        # 而且 viewer 存著 font_minus / font_plus / reset_button 的直接參照）。
        #
        # 用 list of tuple 而不是 dict[widget]：順序與重複都無所謂，也不必去
        # 依賴 sip 包裝的 hash 語意。只准登記與面板同生共死的子元件——
        # 會獨立消失的元件（例如分頁按鈕）登記進來就會留下已銷毀的參照。
        self._row_labels: list[tuple[QLabel, str]] = []
        self._texts: list[tuple[QWidget, str, dict]] = []
        self._tooltips: list[tuple[QWidget, str, dict]] = []

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

        self._add_section("settings.section.appearance")
        # 語言放第一列：看不懂目前介面的人，會從第一區的第一列開始找
        self._build_language_row()
        self._build_theme_row()
        self._build_font_row()
        self._build_line_height_row()
        self._build_width_row()

        self._add_section("settings.section.behavior")
        self._build_behavior_rows()

        self._add_section("settings.section.reset")
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

        title = QLabel(t("settings.title"), header)
        title.setObjectName("settingsTitle")
        self._register_text(title, "settings.title")
        layout.addWidget(title)
        layout.addStretch(1)

        self.close_button = IconButton("close", "settings.close", header, size=(30, 26))
        self.close_button.setObjectName("stepBtn")
        self._icon_buttons.append(self.close_button)
        self.close_button.clicked.connect(self.deactivate)
        layout.addWidget(self.close_button)
        return header

    def _register_text(self, widget: QWidget, key: str, **kwargs) -> None:
        """記住「這個元件的文字來自哪個翻譯鍵」，語言切換時就地重設。"""
        self._texts.append((widget, key, kwargs))

    def _register_tooltip(self, widget: QWidget, key: str, **kwargs) -> None:
        self._tooltips.append((widget, key, kwargs))

    def _add_section(self, title_key: str) -> None:
        wrapper = QWidget(self)
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 22, 0, 10)
        layout.setSpacing(10)

        # 不做 title.upper()：英文的 "APPEARANCE" 直接大寫寫進語言檔，
        # 大小寫是翻譯的一部分，程式碼不猜
        label = QLabel(t(title_key), wrapper)
        label.setObjectName("settingsSection")
        self._register_text(label, title_key)
        layout.addWidget(label)

        line = QFrame(wrapper)
        line.setObjectName("settingsSectionLine")
        line.setFixedHeight(1)
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(line, 1)
        self._body.addWidget(wrapper)

    def _add_row(
        self,
        label_key: str,
        control: QWidget,
        hint_key: str = "",
        **hint_args,
    ) -> None:
        """一列設定：左側名稱、右側控制項，控制項下方可加說明文字。

        收的是翻譯鍵不是字串——建出來的 QLabel 會登記進 _row_labels /
        _texts，語言一換就地重設（見 __init__ 的說明）。
        """
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(14)

        label = QLabel(t(label_key) if label_key else "", row)
        label.setObjectName("settingsRowLabel")
        label.setFixedWidth(_label_width())
        if label_key:
            # 空字串是「重設」那列的佔位用途，沒有文字就不必登記
            self._row_labels.append((label, label_key))
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
        if hint_key:
            note = QLabel(t(hint_key, **hint_args), right)
            note.setObjectName("settingsHint")
            note.setWordWrap(True)
            self._register_text(note, hint_key, **hint_args)
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
    def _build_language_row(self) -> None:
        self._language_group = QButtonGroup(self)
        self._language_group.setExclusive(True)
        self._language_chips: dict[str, _Chip] = {}
        chips: list[QWidget] = []
        for mode, label_key in config.LANGUAGE_MODES:
            chip = _Chip(t(label_key), parent=self)
            self._register_text(chip, label_key)
            self._language_chips[mode] = chip
            self._language_group.addButton(chip)
            chip.clicked.connect(lambda _c, m=mode: self.languageModeChanged.emit(m))
            chips.append(chip)
        self._add_row(
            "settings.language",
            self._chip_row(self, chips),
            "settings.language.hint",
        )

    def _build_theme_row(self) -> None:
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_chips: dict[str, _Chip] = {}
        chips: list[QWidget] = []
        for mode, label_key in config.THEME_MODES:
            chip = _Chip(t(label_key), t("settings.theme.tip", label=t(label_key)), self)
            self._theme_chips[mode] = chip
            self._theme_group.addButton(chip)
            chip.clicked.connect(lambda _c, m=mode: self.themeModeChanged.emit(m))
            chips.append(chip)
        self._add_row(
            "settings.theme",
            self._chip_row(self, chips),
            "settings.theme.hint",
        )

    def _build_font_row(self) -> None:
        holder = QWidget(self)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.font_minus = IconButton("minus", "settings.font.minus", holder, size=(30, 28))
        self.font_minus.setObjectName("stepBtn")
        self.font_plus = IconButton("plus", "settings.font.plus", holder, size=(30, 28))
        self.font_plus.setObjectName("stepBtn")
        self._icon_buttons += [self.font_minus, self.font_plus]

        self._font_value = QLabel(
            t("settings.font.value", size=config.BASE_FONT_POINT_SIZE), holder
        )
        self._font_value.setObjectName("settingsValue")
        self._font_value.setFixedWidth(52)
        self._font_value.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.font_minus)
        layout.addWidget(self._font_value)
        layout.addWidget(self.font_plus)
        layout.addStretch(1)
        self._add_row(
            "settings.font",
            holder,
            "settings.font.hint",
            min=config.MIN_FONT_POINT_SIZE,
            max=config.MAX_FONT_POINT_SIZE,
        )

    def _build_line_height_row(self) -> None:
        self._line_group = QButtonGroup(self)
        self._line_group.setExclusive(True)
        self._line_chips: dict[str, _Chip] = {}
        chips: list[QWidget] = []
        for key, label_key, percent in config.LINE_HEIGHT_OPTIONS:
            chip = _Chip(
                t("settings.lineHeight.chip", label=t(label_key), percent=percent),
                t("settings.lineHeight.tip", percent=percent),
                self,
            )
            self._line_chips[key] = chip
            self._line_group.addButton(chip)
            chip.clicked.connect(lambda _c, k=key: self.lineHeightChanged.emit(k))
            chips.append(chip)
        self._add_row(
            "settings.lineHeight",
            self._chip_row(self, chips),
            "settings.lineHeight.hint",
        )

    def _build_width_row(self) -> None:
        self._width_group = QButtonGroup(self)
        self._width_group.setExclusive(True)
        self._width_chips: dict[int, _Chip] = {}
        chips: list[QWidget] = []
        for width, label_key in config.CONTENT_WIDTH_OPTIONS:
            text, tip = self._width_chip_text(width, label_key)
            chip = _Chip(text, tip, self)
            self._width_chips[width] = chip
            self._width_group.addButton(chip)
            chip.clicked.connect(lambda _c, w=width: self.contentWidthChanged.emit(w))
            chips.append(chip)
        self._add_row(
            "settings.width",
            self._chip_row(self, chips),
            "settings.width.hint",
        )

    @staticmethod
    def _width_chip_text(width: int, label_key: str) -> tuple[str, str]:
        """寬度 chip 的文字與提示。抽出來讓 _retranslate_chips 能重跑同一份邏輯。"""
        if width == 0:
            return t(label_key), t("settings.width.unlimitedTip")
        return (
            t("settings.width.chip", label=t(label_key), width=width),
            t("settings.width.tip", width=width),
        )

    # -- 行為區 --------------------------------------------------------------
    def _build_behavior_rows(self) -> None:
        self.auto_reload_chip = _Chip(t("settings.watch.chip"), parent=self)
        self.always_on_top_chip = _Chip(t("settings.window.chip"), parent=self)
        self.status_bar_chip = _Chip(t("settings.statusBar.chip"), parent=self)
        self.confirm_links_chip = _Chip(t("settings.links.chip"), parent=self)
        self.restore_tabs_chip = _Chip(t("settings.tabs.chip"), parent=self)

        # 三個鍵共用同一個前綴：<前綴> 是列名、<前綴>.chip 是開關文字、
        # <前綴>.hint 是說明。少一組資料表，也少一個對不齊的機會。
        rows = (
            ("settings.watch", self.auto_reload_chip, self.autoReloadChanged),
            ("settings.window", self.always_on_top_chip, self.alwaysOnTopChanged),
            ("settings.statusBar", self.status_bar_chip, self.statusBarChanged),
            ("settings.links", self.confirm_links_chip, self.confirmLinksChanged),
            ("settings.tabs", self.restore_tabs_chip, self.restoreTabsChanged),
        )
        for label_key, chip, signal in rows:
            chip.toggled.connect(signal)
            self._register_text(chip, label_key + ".chip")
            self._add_row(label_key, self._chip_row(self, [chip]), label_key + ".hint")

    def _build_reset_row(self) -> None:
        self.reset_button = QToolButton(self)
        self.reset_button.setObjectName("resetBtn")
        self.reset_button.setText(t("settings.reset.button"))
        self._register_text(self.reset_button, "settings.reset.button")
        self.reset_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.reset_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reset_button.clicked.connect(self.resetRequested)
        self._add_row(
            "",
            self._chip_row(self, [self.reset_button]),
            "settings.reset.hint",
        )

    # -- 對外介面 ------------------------------------------------------------
    def apply_theme(self, theme: str) -> None:
        for button in self._icon_buttons:
            button.apply_theme(theme)

    def apply_language(self) -> None:
        """語言切換：走訪登記表就地重設文字，不重建面板。"""
        width = _label_width()
        for label, key in self._row_labels:
            label.setText(t(key))
            # 英文的列名比中文長，欄寬要跟著放寬，否則會被 setFixedWidth 裁掉
            label.setFixedWidth(width)
        for widget, key, args in self._texts:
            widget.setText(t(key, **args))
        for widget, key, args in self._tooltips:
            widget.setToolTip(t(key, **args))
        for button in self._icon_buttons:
            button.apply_language()
        self._retranslate_chips()
        # _font_value 是狀態衍生的（「11 pt」），由 viewer 緊接著的
        # _sync_settings_panel() 補上——它才是知道目前字級的那一個

    def _retranslate_chips(self) -> None:
        """重算那些「翻譯過的詞再套進另一條翻譯字串」的 chip。

        行高的「標準 160%」、寬度的「窄 720px」、主題的 tooltip「主題模式：淺色」
        都是巢狀的：內層 label 本身要翻，再代進外層的樣板。這種參數不能在建構時
        就凍進登記表——凍進去的是舊語言的詞，語言換了也不會跟著變。這裡照
        _build_*_row 的同一份邏輯重算一次。

        刻意寫死這三組、不做自動化：面板就只有這三組分段選擇，而且它們的組合
        方式各不相同（一個加百分比、一個加 px、一個完全不加）。
        """
        for mode, label_key in config.THEME_MODES:
            chip = self._theme_chips.get(mode)
            if chip is not None:
                chip.setText(t(label_key))
                chip.setToolTip(t("settings.theme.tip", label=t(label_key)))
        for key, label_key, percent in config.LINE_HEIGHT_OPTIONS:
            chip = self._line_chips.get(key)
            if chip is not None:
                chip.setText(
                    t("settings.lineHeight.chip", label=t(label_key), percent=percent)
                )
                chip.setToolTip(t("settings.lineHeight.tip", percent=percent))
        for width, label_key in config.CONTENT_WIDTH_OPTIONS:
            chip = self._width_chips.get(width)
            if chip is not None:
                text, tip = self._width_chip_text(width, label_key)
                chip.setText(text)
                chip.setToolTip(tip)

    def sync(
        self,
        *,
        language_mode: str,
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
        self._check(self._language_chips.get(language_mode))
        self._check(self._theme_chips.get(theme_mode))
        self._check(self._line_chips.get(line_height))
        self._check(self._width_chips.get(content_width))
        self._font_value.setText(t("settings.font.value", size=font_point_size))
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
