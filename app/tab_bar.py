"""分頁列。

放在標題列下方而不是做進標題列：標題列已經有八個按鈕約 250px，而視窗最小寬度
只有 420px，分頁再擠進去就沒有空間了。

作用中的分頁底色與內容區相同、頂端一條強調色，視覺上和下方文件連成一片；
未選取的分頁融入分頁列底色。分頁列永遠顯示（含單一分頁）——單分頁若隱藏，
就沒有分頁可以抓，永遠拖不去別的視窗合併。

【拖曳（比照瀏覽器）】
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
# 太大則拖不出去；瀏覽器實測大約就是這個量級。
TEAR_OFF_MARGIN = 48


class DragGhost(QWidget):
    """撕下分頁時跟著游標的半透明縮影——沒有它，使用者拖到一半完全不知道
    放開會發生什麼事（實際回饋就是這樣來的）。

    要點：
    - 縮影維持「按下時抓的那一點在游標底下」（比照瀏覽器），因此幽靈**必然**
      蓋住游標。合併的命中測試不能再用 QApplication.topLevelAt——它會回傳幽靈，
      而幽靈既不是來源視窗也不是候選視窗，堆疊判定會整段失效、安靜退回幾何
      掃描，分頁就併進一個被蓋住、使用者看不見的視窗。改由
      window_manager.top_level_widget_at 用原生 WindowFromPoint 穿過幽靈。
    - WindowTransparentForInput 從此是**功能相依而不是視覺選擇**：它就是上面那個
      WS_EX_TRANSPARENT 的唯一來源（只留 WA_TransparentForMouseEvents 產生不出
      這個 exstyle），拿掉之後連 WindowFromPoint 也會抓到幽靈。
    - 縮影必須是版面的第一個項目、AlignLeft、外層 margins 為 0：抓取點是相對
      縮影左上角量的，徽章變寬時縮影一旦跟著位移，跟隨就整個歪掉。
    - ShowWithoutActivating：不能搶焦點，搶了拖曳手勢就斷了。
    """

    # 下一步預告的文案。key 與 viewer 的 _drag_intent_at 回傳值對應。
    INTENT_TEXT = {
        "merge": "合併",
        "detach": "拆分為新視窗",
        "none": "無動作",
    }

    def __init__(self, pixmap, theme: str, grab_offset: QPoint) -> None:
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        # 抓取點夾在縮影矩形內。offset 是按下當下量的，縮影卻是第一次拖出列外
        # 才 grab 的；中間分頁寬度若變過（關閉鈕出現／消失就差一截），沒夾住的話
        # 幽靈會整個偏到游標外面去。
        size = pixmap.deviceIndependentSize()
        self._grab_offset = QPoint(
            max(0, min(grab_offset.x(), int(size.width()) - 1)),
            max(0, min(grab_offset.y(), int(size.height()) - 1)),
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
        # 留成屬性讓回歸測試量得到它的位置：相對跟隨的前提是「縮影左上角＝幽靈
        # 左上角」，這件事只靠註解鎖不住（改成 AlignHCenter 就悄悄歪掉）。
        self._snapshot = QLabel(self)
        self._snapshot.setPixmap(pixmap)
        column.addWidget(self._snapshot, 0, Qt.AlignmentFlag.AlignLeft)
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
        # 【合併時幽靈要讓路】相對跟隨後幽靈蓋在游標正下方，而合併瞄準的唯一
        # 落點回饋——目標分頁列上的插入指示線——就在游標底下。實測抓分頁中央
        # 拖去合併時，0.9 的不透明度會把指示線 100% 蓋掉，使用者根本瞄不了。
        # 合併時降到 0.45：指示線與目標分頁列透出來、徽章仍看得清。
        self.setWindowOpacity(0.45 if intent == "merge" else 0.9)

    def apply_theme(self, theme: str) -> None:
        """拖曳途中主題被切換（例如 Windows 排程的自動深色模式）時重套樣式，
        否則徽章配色會停在舊主題直到放開。"""
        self.setStyleSheet(styles.build_ghost_qss(theme))

    def follow(self, global_pos: QPoint) -> None:
        """讓按下時抓的那一點一直待在游標底下（瀏覽器的行為）。

        不需要補償縮影在幽靈內的位置：縮影靠左貼齊、是版面第一個項目、外層
        margins 為 0，它的左上角恆等於幽靈的左上角（見類別說明）。
        """
        self.move(global_pos - self._grab_offset)


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
        self._grab_offset: QPoint | None = None
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
            # 和瀏覽器一致：按下立刻切換到這個分頁，之後才可能進入拖曳
            self._press_pos = event.globalPosition().toPoint()
            # 抓取點在按下這一刻就定案，不能等要建幽靈時再 mapFromGlobal 現算：
            # 幽靈是「第一次拖出列外」才建立的，在那之前使用者可能已經在列內
            # 重排過，_move_button 會把按鈕挪到別的槽位、x 瞬間跳走而游標不跳，
            # 那時候算出來的相對點已經不是使用者抓的那一點。
            # （_label 帶 WA_TransparentForMouseEvents，事件直接投遞到本體，
            #   event.position() 已經是這顆分頁的區域座標，不需要再 map。）
            self._grab_offset = event.position().toPoint()
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
            self._grab_offset = None
            self._dragging = False
            if was_dragging:
                self.dragReleased.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def grab_offset(self) -> QPoint:
        """按下時游標落在這顆分頁內的位置；拖曳幽靈靠它維持相對跟隨。

        回傳副本而不是本體：QPoint 可變，幽靈那邊還會夾值。取不到時回
        QPoint() 而不是 None——None 進到 follow() 的減法會在 mouseMoveEvent
        的呼叫鏈上拋 TypeError，那在 PyQt6 是直接中止行程。
        """
        return QPoint(self._grab_offset) if self._grab_offset is not None else QPoint()

    def cancel_drag(self) -> None:
        # 抓取點和 _press_pos 同生共死：留下一個屬於舊按鈕的抓取點，下一次
        # 幽靈重建就會拿錯世代的座標去定位
        self._press_pos = None
        self._grab_offset = None
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
        # 由視窗層注入：callable()，拖曳結束（放開或取消）時通知。分頁列只藏得了
        # 自己那條插入指示線，別的視窗上的要靠視窗層去清——少了這個通知，
        # 「拖去 B 畫了線、拖回自己列上放開」會讓 B 的指示線永久殘留。
        self.drag_ended = None

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

        # 永遠顯示（比照瀏覽器）。原本單分頁時隱藏，但那樣「單開一個 .md」
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
        # 拖曳進行中主題可能被系統切換（自動深色模式），幽靈也要跟上
        if self._ghost is not None:
            self._ghost.apply_theme(theme)

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
                self._ghost = DragGhost(
                    button.grab(), self._theme, button.grab_offset()
                )
            intent = "none"
            if self.drop_intent_probe is not None:
                intent = self.drop_intent_probe(global_pos, outside_band)
            self._ghost.set_intent(intent)
            # 先定位再顯示：反過來的話，新建的幽靈會先在預設位置（螢幕左上角）
            # 閃現一幀才跳到游標下
            self._ghost.follow(global_pos)
            self._ghost.show()
        elif self._ghost is not None:
            self._ghost.hide()
            # 回到本列也要探一次：剛才可能在別的視窗畫了插入指示線，游標在
            # 本列上時 drop_target_at 會回 None，探測順帶把所有指示線清掉。
            # 只在幽靈存在（曾離開過本列）時才付這個成本，純列內重排不受影響。
            if self.drop_intent_probe is not None:
                self.drop_intent_probe(global_pos, False)

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

    def cancel_active_drag(self) -> None:
        """取消進行中的拖曳手勢（給視窗層在 closeEvent 呼叫）。

        拖曳中把視窗關掉（中鍵按在被拖的分頁上、Ctrl+W）不會經過放開事件，
        少了這一步會留下兩個殘留：全域的拖曳游標永遠不會被還原（其餘所有
        視窗從此都顯示拖曳游標），以及無父件、置頂的幽靈視窗——它和已關閉
        視窗互相參照成環，引用計數收不掉，會一直掛在畫面最上層等世代 GC。
        """
        self._cancel_drag()

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
            # 拖曳結束的單一收斂點：放開（不論落點）與取消都會經過這裡，
            # 通知視窗層把「其他視窗」的插入指示線一併清掉
            if self.drag_ended is not None:
                self.drag_ended()

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
