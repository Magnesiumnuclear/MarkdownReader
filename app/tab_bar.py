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
- 兩種情況都用同一個 InsertMarker 標出「會插在哪兩個之間」。列內重排時只有
  一條規則：**游標落在被拖分頁的哪一半，就畫那一側的緣**——右半畫右緣、左半
  畫左緣，拖曳中一律顯示一側（見 _show_reorder_marker）。跨視窗那條由視窗層
  指定索引，不受影響。

【關閉鈕是覆蓋層，不進版面】
關閉鈕若排在版面裡，滑鼠移進移出就會讓分頁寬度跳一下（未選取的分頁平常不顯示
關閉鈕），整列跟著位移，很難瞄準。改成絕對定位疊在檔名右端：分頁寬度從此只由
檔名決定，滑鼠怎麼動都不變。代價是叉叉會蓋到長檔名的尾巴，所以底下鋪一條
與分頁同色的襯底（右段實色、左段漸層淡出），見 TabButton._layout_close。

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

from . import config, icons, styles
from .language import t
from .title_bar import IconButton, SplitIconButton

# 離開分頁列矩形四周多少邏輯像素才算「撕下來」。太小會誤觸，
# 太大則拖不出去；瀏覽器實測大約就是這個量級。
TEAR_OFF_MARGIN = 48

# 分頁的框線寬度，和 styles.py 的 #tab / #tabActive 規則對應。關閉鈕的覆蓋層
# 要讓開它們，否則會蓋掉作用中分頁頂端那條強調色，以及分頁之間的分隔線。
_TAB_BORDER_TOP = 2
_TAB_BORDER_RIGHT = 1


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

    # 下一步預告的翻譯鍵。key 與 viewer 的 _drag_intent_at 回傳值對應。
    INTENT_KEYS = {
        "merge": "tab.drag.merge",
        "detach": "tab.drag.detach",
        "none": "tab.drag.none",
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
        self._paint_intent()
        # 【合併時幽靈要讓路】相對跟隨後幽靈蓋在游標正下方，而合併瞄準的唯一
        # 落點回饋——目標分頁列上的插入指示線——就在游標底下。實測抓分頁中央
        # 拖去合併時，0.9 的不透明度會把指示線 100% 蓋掉，使用者根本瞄不了。
        # 合併時降到 0.45：指示線與目標分頁列透出來、徽章仍看得清。
        self.setWindowOpacity(0.45 if intent == "merge" else 0.9)

    def _paint_intent(self) -> None:
        """把目前的 intent 畫成徽章。

        從 set_intent 抽出來，是因為 set_intent 對相同 intent 會早退——
        語言切換時 intent 沒變、但文字要換，早退會把它擋掉。
        """
        self._badge.setText(t(self.INTENT_KEYS.get(self._intent, "tab.drag.none")))
        self._badge.setProperty("intent", self._intent)
        # 動態屬性變了要重跑 QSS 選擇器
        self._badge.style().unpolish(self._badge)
        self._badge.style().polish(self._badge)
        self.adjustSize()

    def apply_theme(self, theme: str) -> None:
        """拖曳途中主題被切換（例如 Windows 排程的自動深色模式）時重套樣式，
        否則徽章配色會停在舊主題直到放開。"""
        self.setStyleSheet(styles.build_ghost_qss(theme))

    def apply_language(self) -> None:
        """拖曳途中換語言：徽章文字跟著換。"""
        self._paint_intent()

    def follow(self, global_pos: QPoint) -> None:
        """讓按下時抓的那一點一直待在游標底下（瀏覽器的行為）。

        不需要補償縮影在幽靈內的位置：縮影靠左貼齊、是版面第一個項目、外層
        margins 為 0，它的左上角恆等於幽靈的左上角（見類別說明）。
        """
        self.move(global_pos - self._grab_offset)


class InsertMarker(QWidget):
    """「分頁會插進這條縫」的指示：一條強調色的線，上下各扣一個三角帽。

    為什麼要三角帽：分頁之間本來就有一條 1px 的分隔線，純粹加粗那條線在深色
    主題下很難分辨是「新東西」還是分頁自己的邊框；上下兩端各張開一個比線寬
    四倍的三角，形狀就不可能和任何既有的邊框混淆。

    做成三個子元件而不是自己 paintEvent：專案規範是「圖示一律來自
    assets/icons/*.svg」「外觀來自 styles.py 的集中 QSS」。線走 QSS，
    三角帽走 icons.pixmap（同一份 SVG 換色即可跟著主題走）。

    整塊是分頁條上的覆蓋層，不進版面——進版面的話每次移動都會把分頁擠開重排。
    背景保持透明，所以容器本身刻意沒有樣式（只有線和帽子畫得出東西）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tabInsertMarker")
        # 覆蓋層絕不能吃到滑鼠事件：它就蓋在分頁的縫上，攔下來會讓那一條
        # 細縫按不動（拖曳中滑鼠被按鈕抓著，但放開後這塊還在）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        cap = config.TAB_INSERT_CAP_SIZE
        self.setFixedSize(cap, config.TAB_HEIGHT)

        line_width = config.TAB_INSERT_MARKER_WIDTH
        self._line = QFrame(self)
        self._line.setObjectName("tabInsertLine")
        self._line.setGeometry(
            (cap - line_width) // 2, 0, line_width, config.TAB_HEIGHT
        )

        self._top = QLabel(self)
        self._top.setObjectName("tabInsertCap")
        self._top.setGeometry(0, 0, cap, cap)
        self._bottom = QLabel(self)
        self._bottom.setObjectName("tabInsertCap")
        self._bottom.setGeometry(0, config.TAB_HEIGHT - cap, cap, cap)
        self.apply_theme(config.DEFAULT_THEME)

    def line_x(self) -> int:
        """線在本元件內的水平中心；定位時要對齊的是線，不是容器邊緣。"""
        return self._line.x() + self._line.width() // 2

    def apply_theme(self, theme: str) -> None:
        """三角帽是點陣圖，換主題要重新上色（線走 QSS，會自己跟著換）。"""
        colour = styles.palette(theme)["accent"]
        cap = config.TAB_INSERT_CAP_SIZE
        self._top.setPixmap(icons.pixmap("insert_cap_down", colour, cap))
        self._bottom.setPixmap(icons.pixmap("insert_cap_up", colour, cap))


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
        path: str | None,
        active: bool,
        theme: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
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
        self.setToolTip(path or t("tab.noFile"))

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

        # 襯底在下、關閉鈕在上，兩個都不進版面（見模組開頭）。襯底吃不到滑鼠，
        # 否則它會擋住關閉鈕左邊那一段的點擊，也會蓋掉分頁本身的拖曳手勢。
        self._backdrop = QFrame(self)
        self._backdrop.setObjectName("tabCloseBackdrop")
        self._backdrop.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._close = IconButton(
            "close", "tab.close", self,
            size=(config.TAB_CLOSE_SIZE, config.TAB_CLOSE_SIZE),
        )
        self._close.setObjectName("tabClose")
        self._close.setIconSize(QSize(config.TAB_CLOSE_ICON, config.TAB_CLOSE_ICON))
        self._close.apply_theme(theme)
        self._close.clicked.connect(self.closeClicked)
        # 未選取的分頁平常不顯示關閉鈕，滑鼠移上去才出現，避免整列都是叉叉
        self._show_close(active)

    # -- 關閉鈕的覆蓋層 -------------------------------------------------------
    def _show_close(self, visible: bool) -> None:
        """關閉鈕與它的襯底一起顯示或收起——只顯示其中一個都是破圖。"""
        self._backdrop.setVisible(visible)
        self._close.setVisible(visible)
        if visible:
            self._layout_close()

    def _layout_close(self) -> None:
        """把關閉鈕與襯底放到右端。

        襯底不能鋪滿整顆分頁：上緣 2px 是 border-top（作用中的分頁那條是強調
        色），右緣 1px 是 border-right，子元件畫在框線之上，鋪過去會把兩條線
        蓋掉。所以上下各讓開框線，右緣停在 width-1。
        """
        margin = config.TAB_CLOSE_MARGIN
        size = config.TAB_CLOSE_SIZE
        top = _TAB_BORDER_TOP
        height = max(0, self.height() - top)
        right = max(0, self.width() - _TAB_BORDER_RIGHT)
        close_x = right - margin - size
        self._close.setGeometry(
            close_x, top + max(0, (height - size) // 2), size, size
        )
        backdrop_x = close_x - config.TAB_CLOSE_FADE
        self._backdrop.setGeometry(
            backdrop_x, top, max(0, right - backdrop_x), height
        )
        # 疊放順序：襯底要在**檔名之上**（它的工作就是把檔名尾巴蓋掉），
        # 關閉鈕再疊在襯底之上。先 raise 襯底、再 raise 關閉鈕，順序不能反。
        # 寫成 lower() 會把襯底壓到檔名底下，看起來就是「叉叉和字糊在一起」。
        self._backdrop.raise_()
        self._close.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """分頁寬度變了就重排覆蓋層。

        這裡**不能**用 isVisible() 當守衛。isVisible() 問的是「現在畫得到嗎」，
        視窗還沒 show 之前一律是 False——而分頁拿到最終寬度的那一次 resize
        正好發生在那個時候。守衛擋掉之後，覆蓋層就停在建構當下那個
        sizeHint 寬度（實測 100，實際分頁 133），叉叉會落在檔名中間。
        隱藏的元件照樣定位，沒有成本。
        """
        super().resizeEvent(event)
        self._layout_close()

    # -- 樣式 ----------------------------------------------------------------
    def matches(self, name: str, path: str | None) -> bool:
        """結構是否相同。

        比對的是**路徑**而不是 tooltip：沒有路徑的空白分頁，tooltip 是翻譯過的
        「尚未開啟檔案」，拿它來比會讓語言一換就判定成結構改變而重建按鈕——
        而「拖曳期間重建按鈕」正是本模組開頭警告的那個崩潰類別。
        路徑是穩定身分，翻譯字串不是。
        """
        return self._name == name and self._path == path

    def set_active(self, active: bool) -> None:
        """就地切換選取樣式，不重建元件（拖曳中換 objectName 也安全）。"""
        if active == self._active:
            return
        self._active = active
        self.setObjectName("tabActive" if active else "tab")
        self._label.setObjectName("tabLabelActive" if active else "tabLabel")
        # objectName 變了要重跑 QSS 選擇器，否則外觀停在舊樣式。
        # 襯底一定要一起重跑：它的顏色是用 `#tabActive QFrame#tabCloseBackdrop`
        # 這種後代選擇器挑的，父層的 objectName 換了而它沒重跑，作用中的分頁
        # 會頂著「非作用中」那個底色，接縫處看得出一塊色差。
        for widget in (self, self._label, self._backdrop, self._close):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._show_close(active or self.underMouse())

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        self._close.apply_theme(theme)

    def apply_language(self) -> None:
        self._close.apply_language()
        self.setToolTip(self._path or t("tab.noFile"))

    # -- 滑鼠 ----------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._show_close(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._show_close(self._active)
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
    # 「＋」右側展開選單裡的「開啟檔案…」
    openFileRequested = pyqtSignal()
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
        # 按下的那一刻它排在第幾格。即時重排會一直改變它「現在」的位置，
        # 但指示線要畫哪一側是拿現在和**原位**比出來的，所以原位要單獨記著。
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
        # 標示插入位置（列內重排與跨視窗合併共用）。放在分頁條上當覆蓋層
        # （不進版面），否則插入標記會把分頁擠開、每次移動都重新排版。
        self._insert_marker = InsertMarker(self._strip)
        self._insert_marker.hide()

        # 一顆按鈕兩個功能：主區域開新分頁（可直接貼上 Markdown 原始碼），
        # 右側箭頭展開選單選「開啟檔案…」。寬度要含得下箭頭那一條。
        self.new_button = SplitIconButton(
            "plus", "tab.new", self,
            size=(30 + SplitIconButton.EXPANDER_WIDTH, config.TAB_HEIGHT),
        )
        self.new_button.setObjectName("tabNew")
        self.new_button.clicked.connect(self.newTabRequested)
        self._open_action = self.new_button.menu_widget().addAction(
            t("tab.openFile")
        )
        self._open_action.triggered.connect(self.openFileRequested)
        outer.addWidget(self.new_button)

        self.hide()

    # -- 內容 ----------------------------------------------------------------
    def set_tabs(self, entries: list[tuple[str, str]], active: int) -> None:
        """更新整列。entries 是 (顯示名稱, 完整路徑) 的清單。

        結構相同（同數量、同名稱與路徑、同順序）時只就地更新選取樣式。
        這不只是效能：按下分頁會觸發 activate -> set_tabs，若在這裡重建，
        被按住的按鈕會在拖曳手勢中被銷毀（見模組開頭）。
        """
        same_structure = len(entries) == len(self._buttons) and all(
            button.matches(name, path)
            for button, (name, path) in zip(self._buttons, entries)
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
            for index, (name, path) in enumerate(entries):
                button = TabButton(name, path, index == active, self._theme, self._strip)
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
        self._insert_marker.apply_theme(theme)
        for button in self._buttons:
            button.apply_theme(theme)
        # 拖曳進行中主題可能被系統切換（自動深色模式），幽靈也要跟上
        if self._ghost is not None:
            self._ghost.apply_theme(theme)

    def apply_language(self) -> None:
        self.new_button.apply_language()
        # 選單項的文字是一次性設進 QAction 的，語言換了要自己重設
        self._open_action.setText(t("tab.openFile"))
        for button in self._buttons:
            button.apply_language()
        # 比照上面的 apply_theme：拖曳途中換語言，幽靈的徽章也要跟上
        if self._ghost is not None:
            self._ghost.apply_language()

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
            # 離開本列就把自己的線收掉：接下來要標的是「目標視窗」那條，
            # 由 drop_intent_probe 決定畫在誰身上（可能就是本列，也可能不是）。
            self.hide_insert_marker()
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
        # 【只在游標真的落在本列上時才畫】容忍帶（列外 48px 內）也會走到這裡
        # 繼續重排，但那時游標可能正停在**別的視窗**的分頁列上、上面那段
        # 已經請 drop_intent_probe 在那邊畫了一條。不擋的話兩條線同時亮，
        # 使用者無從知道放開會插到哪一個。
        if on_bar:
            self._show_reorder_marker(button, global_pos)
        else:
            self.hide_insert_marker()

    def _show_reorder_marker(self, button: TabButton, global_pos: QPoint) -> None:
        """列內重排：指示線畫在「游標落在被拖分頁哪一半」的那一側。

            游標在被拖分頁的右半（含正中心）-> 畫這格的右緣
            游標在被拖分頁的左半           -> 畫這格的左緣

        拖曳進行中一律顯示一側，不設「正中心不畫」的中性帶——那需要像素精準的
        相等判斷（分頁寬是偶數，中心落在兩像素之間），實際上永遠不會剛好命中，
        徒增一個測不準的分支。正中心歸右半，簡單且不會 flicker。

        判斷的是**游標此刻相對被拖分頁的位置**，不是相對按下點。差別只在「往右
        拖到底之後再往回晃」：按下點版會一直停在右緣（只顯示「你從起點往右
        移」），中心版一往回越過中心就翻成左緣，貼著游標當下在哪。使用者要的是
        後者——「以滑鼠相對位置是左還是右」。

        因為列內即時重排會把被拖分頁一路挪到游標下，指示線本來就貼在它的邊上；
        這條規則只決定貼哪一邊。用被拖分頁自己的中心當界，抓取點落在哪裡都一樣
        （抓左半或右半不影響），純看游標現在偏哪邊。

        右緣＝下一格的左緣，所以傳 now + 1；now 是最後一格時 show_insert_marker
        會退回「最後一個分頁的右緣」，語意一樣。
        """
        try:
            now = self._buttons.index(button)
        except ValueError:
            self.hide_insert_marker()
            return
        strip_x = self._strip.mapFromGlobal(global_pos).x()
        centre = button.x() + button.width() // 2
        self.show_insert_marker(now + 1 if strip_x >= centre else now)

    def _move_button(self, frm: int, to: int) -> None:
        button = self._buttons.pop(frm)
        self._buttons.insert(to, button)
        self._row.removeWidget(button)
        self._row.insertWidget(to, button)
        # 立刻套用新版面。insertWidget 只是排程一次 LayoutRequest，在它跑完之前
        # 每顆按鈕的 x() 都還是舊槽位的值——而插入指示線正是用 buttons[i].x()
        # 定位的，晚一拍就會畫在隔壁那條縫上，而且要等下一次滑鼠移動才更正。
        # 使用者跨過一個鄰居就停手時，線會一直停在錯的地方。
        self._row.activate()

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
        """在第 index 個分頁「之前」的縫隙畫指示線（index == 分頁數代表最後）。

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
        # 對齊的是**線**，不是容器左緣：容器比線寬（要放得下三角帽），
        # 拿容器去對齊會讓線整個偏掉半個帽子的寬度。
        # 最左邊那條縫的座標是 0，容器因此有一半落在分頁條外被裁掉——這是
        # 刻意的：指示線的職責是說「插在哪」，寧可帽子少一半，也不要為了
        # 完整顯示而把線挪進第一個分頁裡、指到錯的縫。
        self._insert_marker.move(x - self._insert_marker.line_x(), 0)
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
