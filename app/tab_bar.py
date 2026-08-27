"""分頁列。

放在標題列下方而不是做進標題列：標題列已經有八個按鈕約 250px，而視窗最小寬度
只有 420px，分頁再擠進去就沒有空間了。

作用中的分頁底色與內容區相同、頂端一條強調色，視覺上和下方文件連成一片；
未選取的分頁融入分頁列底色。分頁列永遠顯示（含單一分頁）——單分頁若隱藏，
就沒有分頁可以抓，永遠拖不去別的視窗合併。

【拖曳（比照 Chrome）】
- 在列內左右拖 -> 即時重排，每跨過一個鄰居就交換位置並發出 tabMoved。
- 拖出列外（任一方向超過 TEAR_OFF_MARGIN）再放開 -> 發出 detachRequested，
  由視窗層決定是「拆成新視窗」還是「合併進游標下的另一個視窗」。
  分頁列自己不認識其他視窗，這個決定不屬於它。

【拖曳期間絕對不能重建按鈕】
按下分頁會觸發 activate_tab -> set_tabs。舊版 set_tabs 一律砍掉重建，
被按住的那顆按鈕會在手勢進行中被銷毀，滑鼠抓取跟著消失，拖曳就死了。
因此 set_tabs 在「結構沒變」時改為就地更新樣式；真的需要重建時，
會先取消進行中的拖曳手勢。

外觀全部來自 styles.py 的集中 QSS，本模組不呼叫 setStyleSheet。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from . import config, styles
from .title_bar import IconButton

# 離開分頁列矩形四周多少邏輯像素才算「撕下來」。太小會誤觸，
# 太大則拖不出去；Chrome 實測大約就是這個量級。
TEAR_OFF_MARGIN = 48


class DragGhost(QWidget):
    """撕下分頁時跟著游標的半透明縮影——沒有它，使用者拖到一半完全不知道
    放開會發生什麼事（實際回饋就是這樣來的）。

    要點：
    - WindowTransparentForInput + 游標偏移 (12,12)：幽靈本身是一個頂層視窗，
      不偏移的話游標點會落在幽靈裡，合併的命中測試（topLevelAt）會抓到
      幽靈而不是底下的目標視窗。
    - ShowWithoutActivating：不能搶焦點，搶了拖曳手勢就斷了。
    """

    OFFSET = QPoint(12, 12)

    # 下一步預告的文案。key 與 viewer 的 _drag_intent_at 回傳值對應。
    INTENT_TEXT = {
        "merge": "合併",
        "detach": "拆分為新視窗",
        "none": "無動作",
    }

    def __init__(self, pixmap, theme: str) -> None:
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(0.9)
        # 幽靈是獨立頂層視窗，吃不到主視窗的 QSS，樣式集中在 styles.py、
        # 在這裡一次套用（和主視窗 apply_theme 的模式一致）
        self.setStyleSheet(styles.build_ghost_qss(theme))

        from PyQt6.QtWidgets import QVBoxLayout

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        snapshot = QLabel(self)
        snapshot.setPixmap(pixmap)
        column.addWidget(snapshot, 0, Qt.AlignmentFlag.AlignLeft)
        self._badge = QLabel(self)
        self._badge.setObjectName("dragGhostBadge")
        self._intent = ""
        column.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignLeft)
        self.set_intent("none")

    def set_intent(self, intent: str) -> None:
        """更新「放開會發生什麼」的徽章：merge / detach / none。"""
        if intent == self._intent:
            return
        self._intent = intent
        self._badge.setText(self.INTENT_TEXT.get(intent, ""))
        self._badge.setProperty("intent", intent)
        # 動態屬性變了要重跑 QSS 選擇器
        self._badge.style().unpolish(self._badge)
        self._badge.style().polish(self._badge)
        self.adjustSize()

    def follow(self, global_pos: QPoint) -> None:
        self.move(global_pos + self.OFFSET)


class TabButton(QFrame):
    """單一分頁：檔名 + 關閉鈕，帶拖曳手勢。"""

    clicked = pyqtSignal()
    closeClicked = pyqtSignal()
    dragStarted = pyqtSignal()
    dragMoved = pyqtSignal(QPoint)      # 全域座標
    dragReleased = pyqtSignal(QPoint)   # 全域座標

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
        self._theme = theme
        self._press_pos: QPoint | None = None
        self._dragging = False
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
        self._name = name
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

    # -- 樣式 ----------------------------------------------------------------
    def matches(self, name: str, tooltip: str) -> bool:
        return self._name == name and self.toolTip() == tooltip

    def set_active(self, active: bool) -> None:
        """就地切換選取樣式，不重建元件（拖曳中換 objectName 也安全）。"""
        if active == self._active:
            return
        self._active = active
        self.setObjectName("tabActive" if active else "tab")
        self._label.setObjectName("tabLabelActive" if active else "tabLabel")
        # objectName 變了要重跑 QSS 選擇器，否則外觀停在舊樣式
        for widget in (self, self._label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._close.setVisible(active or self.underMouse())

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        self._close.apply_theme(theme)

    # -- 滑鼠 ----------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._close.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._close.setVisible(self._active)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # 和 Chrome 一致：按下立刻切換到這個分頁，之後才可能進入拖曳
            self._press_pos = event.globalPosition().toPoint()
            self._dragging = False
            self.clicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            # 中鍵關閉，和瀏覽器一致
            self.closeClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_pos is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        global_pos = event.globalPosition().toPoint()
        if not self._dragging:
            if ((global_pos - self._press_pos).manhattanLength()
                    < QApplication.startDragDistance()):
                return
            self._dragging = True
            self.dragStarted.emit()
        self.dragMoved.emit(global_pos)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            was_dragging = self._dragging
            self._press_pos = None
            self._dragging = False
            if was_dragging:
                self.dragReleased.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def cancel_drag(self) -> None:
        self._press_pos = None
        self._dragging = False


class TabBar(QFrame):
    """整條分頁列。"""

    activated = pyqtSignal(int)
    closeRequested = pyqtSignal(int)
    newTabRequested = pyqtSignal()
    # 拖曳重排：每次交換發一次（from, to），視窗層要同步 _tabs 的順序
    tabMoved = pyqtSignal(int, int)
    # 拖出列外放開：index 是「目前」的位置（重排後的），global_pos 是放開點。
    # allow_new_window：放開點是否遠到可以拆成新視窗（超出容忍帶）。
    # False 表示只離開了分頁列本身、還在容忍帶內——這種情況只允許「合併進
    # 游標下的其他視窗」，不允許拆分（手滑超出幾個像素不該噴出一個新視窗）。
    detachRequested = pyqtSignal(int, QPoint, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tabBar")
        self.setFixedHeight(config.TAB_HEIGHT + 1)
        self._theme = config.DEFAULT_THEME
        self._buttons: list[TabButton] = []
        self._drag_button: TabButton | None = None
        self._torn_off = False
        self._ghost: DragGhost | None = None
        # 由視窗層注入：callable(global_pos, outside_band) -> "merge"|"detach"|"none"。
        # 分頁列不認識其他視窗，「放開會發生什麼」只有視窗層答得出來。
        self.drop_intent_probe = None

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
        # 跨視窗合併時標示插入位置。放在分頁條上當覆蓋層（不進版面），
        # 否則插入標記會把分頁擠開、每次移動都重新排版。
        self._insert_marker = QFrame(self._strip)
        self._insert_marker.setObjectName("tabInsertMarker")
        self._insert_marker.setFixedWidth(config.TAB_INSERT_MARKER_WIDTH)
        self._insert_marker.hide()

        self.new_button = IconButton(
            "plus", "開新分頁 (Ctrl+T)", self, size=(30, config.TAB_HEIGHT)
        )
        self.new_button.setObjectName("tabNew")
        self.new_button.clicked.connect(self.newTabRequested)
        outer.addWidget(self.new_button)

        self.hide()

    # -- 內容 ----------------------------------------------------------------
    def set_tabs(self, entries: list[tuple[str, str]], active: int) -> None:
        """更新整列。entries 是 (顯示名稱, 完整路徑) 的清單。

        結構相同（同數量、同名稱與 tooltip、同順序）時只就地更新選取樣式。
        這不只是效能：按下分頁會觸發 activate -> set_tabs，若在這裡重建，
        被按住的按鈕會在拖曳手勢中被銷毀（見模組開頭）。
        """
        same_structure = len(entries) == len(self._buttons) and all(
            button.matches(name, tooltip)
            for button, (name, tooltip) in zip(self._buttons, entries)
        )
        if same_structure:
            for index, button in enumerate(self._buttons):
                button.set_active(index == active)
        else:
            self._cancel_drag()
            for button in self._buttons:
                button.setParent(None)
                button.deleteLater()
            self._buttons.clear()
            for index, (name, tooltip) in enumerate(entries):
                button = TabButton(name, tooltip, index == active, self._theme, self._strip)
                self._connect_button(button)
                self._row.insertWidget(index, button)
                self._buttons.append(button)

        # 永遠顯示（比照 Chrome）。原本單分頁時隱藏，但那樣「單開一個 .md」
        # 就沒有分頁可以抓，永遠拖不去別的視窗合併。
        self.setVisible(len(entries) > 0)
        if 0 <= active < len(self._buttons):
            self._scroll.ensureWidgetVisible(self._buttons[active], 40, 0)

    def _connect_button(self, button: TabButton) -> None:
        # 不用 lambda 捕捉索引：拖曳重排後索引會變，改成每次以「目前位置」回報
        button.clicked.connect(lambda b=button: self._emit_for(b, self.activated))
        button.closeClicked.connect(lambda b=button: self._emit_for(b, self.closeRequested))
        button.dragStarted.connect(lambda b=button: self._on_drag_started(b))
        button.dragMoved.connect(self._on_drag_moved)
        button.dragReleased.connect(self._on_drag_released)

    def _emit_for(self, button: TabButton, signal) -> None:
        try:
            index = self._buttons.index(button)
        except ValueError:
            return
        signal.emit(index)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        self.new_button.apply_theme(theme)
        for button in self._buttons:
            button.apply_theme(theme)

    # -- 拖曳 ----------------------------------------------------------------
    def _on_drag_started(self, button: TabButton) -> None:
        self._drag_button = button
        self._torn_off = False

    def _on_drag_moved(self, global_pos: QPoint) -> None:
        button = self._drag_button
        if button is None:
            return

        # 撕下判定：離開分頁列的矩形（四周各放寬 TEAR_OFF_MARGIN）就算撕下。
        # 【必須含水平方向】只看垂直的話，兩個視窗並排、分頁列同高時，
        # 橫向拖到隔壁視窗的分頁列上 y 從未離開本列的容忍帶，torn 永遠是
        # False，放開就什麼都不做——「拖過去合併不了」的回報就是這樣來的。
        bar_rect = self.rect()
        top_left = self.mapToGlobal(bar_rect.topLeft())
        band = QRect(
            top_left.x() - TEAR_OFF_MARGIN,
            top_left.y() - TEAR_OFF_MARGIN,
            bar_rect.width() + TEAR_OFF_MARGIN * 2,
            bar_rect.height() + TEAR_OFF_MARGIN * 2,
        )
        on_bar = QRect(
            top_left.x(), top_left.y(), bar_rect.width(), bar_rect.height()
        ).contains(global_pos)
        outside_band = not band.contains(global_pos)

        # 幽靈與徽章：離開本列（含容忍帶內）就顯示，回到列上才收——
        # 容忍帶內雖不能拆分，仍可能合併進相鄰視窗，也需要預告。
        if not on_bar:
            if self._ghost is None:
                self._ghost = DragGhost(button.grab(), self._theme)
            intent = "none"
            if self.drop_intent_probe is not None:
                intent = self.drop_intent_probe(global_pos, outside_band)
            self._ghost.set_intent(intent)
            self._ghost.show()
            self._ghost.follow(global_pos)
        elif self._ghost is not None:
            self._ghost.hide()

        if outside_band:
            if not self._torn_off:
                self._torn_off = True
                QApplication.setOverrideCursor(Qt.CursorShape.DragMoveCursor)
            return
        if self._torn_off:
            self._torn_off = False
            QApplication.restoreOverrideCursor()

        # 列內重排。目標位置＝「其他分頁中，中心點在游標左邊的有幾個」：
        # 想成先把拖曳中的分頁抽出來，再依游標位置插回去，數學上剛好等於
        # pop(frm) 之後 insert(to) 的 to。不要把拖曳中的分頁算進去，
        # 否則從右往左拖時會多數一個，落點永遠偏右一格。
        try:
            current = self._buttons.index(button)
        except ValueError:
            return
        strip_x = self._strip.mapFromGlobal(global_pos).x()
        target = sum(
            1
            for other in self._buttons
            if other is not button and other.x() + other.width() // 2 < strip_x
        )
        if target != current:
            self._move_button(current, target)
            self.tabMoved.emit(current, target)

    def _move_button(self, frm: int, to: int) -> None:
        button = self._buttons.pop(frm)
        self._buttons.insert(to, button)
        self._row.removeWidget(button)
        self._row.insertWidget(to, button)

    def _on_drag_released(self, global_pos: QPoint) -> None:
        button = self._drag_button
        self._drag_button = None
        self._dispose_ghost()
        if self._torn_off:
            self._torn_off = False
            QApplication.restoreOverrideCursor()
        if button is None:
            return

        # 【用放開點重新判定，不能只信拖曳中的 torn 狀態】
        # torn 是移動事件設的；兩個視窗相鄰（間距小於容忍帶）時，游標已經在
        # 對方的分頁列上、卻還沒離開本列的容忍帶——靠 torn 判斷會直接放棄，
        # 「拖過去合併不了」就是這樣來的。規則改成：
        #   放開在本列矩形內      -> 純重排，結束
        #   放開在容忍帶內、列外  -> 只允許合併（有目標才動作）
        #   放開在容忍帶外        -> 合併或拆分都可以
        bar_rect = self.rect()
        top_left = self.mapToGlobal(bar_rect.topLeft())
        on_bar = QRect(
            top_left.x(), top_left.y(), bar_rect.width(), bar_rect.height()
        ).contains(global_pos)
        if on_bar:
            return
        outside_band = not QRect(
            top_left.x() - TEAR_OFF_MARGIN,
            top_left.y() - TEAR_OFF_MARGIN,
            bar_rect.width() + TEAR_OFF_MARGIN * 2,
            bar_rect.height() + TEAR_OFF_MARGIN * 2,
        ).contains(global_pos)
        try:
            index = self._buttons.index(button)
        except ValueError:
            return
        self.detachRequested.emit(index, global_pos, outside_band)

    def _cancel_drag(self) -> None:
        if self._drag_button is not None:
            self._drag_button.cancel_drag()
            self._drag_button = None
        self._dispose_ghost()
        if self._torn_off:
            self._torn_off = False
            QApplication.restoreOverrideCursor()

    def _dispose_ghost(self) -> None:
        self.hide_insert_marker()
        if self._ghost is not None:
            self._ghost.hide()
            self._ghost.deleteLater()
            self._ghost = None

    # -- 插入位置指示線 -------------------------------------------------------
    def show_insert_marker(self, index: int) -> None:
        """在第 index 個分頁「之前」的縫隙畫一條線（index == 分頁數代表最後）。

        位置用同一套 insert_index_at 算出來的索引，因此指示線與實際落點
        必然一致——預告不能說一套做一套。
        """
        if not self._buttons:
            x = 0
        elif index >= len(self._buttons):
            last = self._buttons[-1]
            x = last.x() + last.width()
        else:
            x = self._buttons[index].x()
        half = self._insert_marker.width() // 2
        self._insert_marker.setGeometry(
            max(0, x - half), 0, self._insert_marker.width(), config.TAB_HEIGHT
        )
        self._insert_marker.raise_()
        self._insert_marker.show()

    def hide_insert_marker(self) -> None:
        self._insert_marker.hide()

    # -- 給視窗管理器的查詢 ---------------------------------------------------
    def insert_index_at(self, global_pos: QPoint) -> int:
        """回傳把分頁放到 global_pos 時應插入的位置。"""
        strip_x = self._strip.mapFromGlobal(global_pos).x()
        for index, button in enumerate(self._buttons):
            if strip_x < button.x() + button.width() // 2:
                return index
        return len(self._buttons)

    def global_drop_rect(self):
        """整條分頁列的全域矩形（含撕下容忍帶），給跨視窗命中測試用。"""
        rect = self.rect()
        top_left = self.mapToGlobal(rect.topLeft())
        return (
            top_left.x(),
            top_left.y() - TEAR_OFF_MARGIN,
            rect.width(),
            rect.height() + TEAR_OFF_MARGIN * 2,
        )
