"""無邊框主視窗。

包含三個部分：
* FramelessResizer —— 無邊框視窗失去系統縮放，這裡用事件過濾器補回四邊四角
  的縮放，並讓開捲軸這類需要拖曳的控制項。
* MarkdownBrowser  —— QTextBrowser 子類，負責任務清單核取方塊的即時繪製，
  以及過寬圖片的自動縮放（Qt 不會自動縮圖）。
* MarkdownViewer   —— 主視窗本體：標題列、閱讀區、搜尋列、狀態列、檔案監看、
  主題切換、快捷鍵與拖放。

樣式規範：全專案唯一的 setStyleSheet 呼叫在 MarkdownViewer.apply_theme。
"""

from __future__ import annotations

import os
import re

from PyQt6.QtCore import (
    QByteArray,
    QEvent,
    QFileSystemWatcher,
    QObject,
    QPoint,
    QRect,
    QSettings,
    QTimer,
    QUrl,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QDesktopServices,
    QGuiApplication,
    QImage,
    QTextCursor,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTextDocument,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollBar,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import (
    config,
    document,
    icons,
    language,
    qt_html,
    styles,
    theme as theme_utils,
    win32,
)
from .language import t
from .browser import MarkdownBrowser
from .document_tab import DocumentTab
from .find_bar import FindBar
from .settings_panel import SettingsPanel
from .tab_bar import TabBar
from .title_bar import CustomTitleBar

# 依邊緣組合決定游標形狀
_CURSOR_BY_EDGES = {
    Qt.Edge.LeftEdge.value: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.RightEdge.value: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.TopEdge.value: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeVerCursor,
    (Qt.Edge.LeftEdge | Qt.Edge.TopEdge).value: Qt.CursorShape.SizeFDiagCursor,
    (Qt.Edge.RightEdge | Qt.Edge.BottomEdge).value: Qt.CursorShape.SizeFDiagCursor,
    (Qt.Edge.RightEdge | Qt.Edge.TopEdge).value: Qt.CursorShape.SizeBDiagCursor,
    (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge).value: Qt.CursorShape.SizeBDiagCursor,
}


class StatusPathLabel(QLabel):
    """狀態列裡的檔案路徑——點一下在檔案總管中顯示該檔。

    做成按鈕語意（press 進、release 在範圍內才算數），
    但外觀維持文字，hover 的底線與變色在 QSS（#statusPathLabel）。
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__("", parent)
        self.setObjectName("statusPathLabel")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(t("status.revealTip"))

    def apply_language(self) -> None:
        self.setToolTip(t("status.revealTip"))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class FramelessResizer(QObject):
    """替無邊框視窗補回邊緣縮放。

    子元件（尤其 QTextBrowser 的 viewport）會吃掉滑鼠事件，因此事件過濾器要
    裝在視窗與所有子元件上，並全部開啟 mouse tracking，游標移到邊緣才收得到
    MouseMove。實際縮放交給 Qt 6 的 startSystemResize，行為與原生視窗一致。
    """

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._margin = config.RESIZE_MARGIN
        self._override_active = False
        # 這些控制項本來就要靠拖曳操作，縮放判定必須讓開（見 _edges_at）
        self._drag_controls: list[QWidget] = []
        window.installEventFilter(self)
        self.refresh_targets()

    # -- 安裝與 DPI ----------------------------------------------------------
    def refresh_targets(self) -> None:
        """對視窗與所有子元件開啟 mouse tracking 並安裝過濾器。"""
        self._window.setMouseTracking(True)
        for child in self._window.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def set_drag_controls(self, widgets: list[QWidget]) -> None:
        """登記需要讓開的控制項（捲軸等）。"""
        self._drag_controls = [w for w in widgets if w is not None]

    def _over_drag_control(self, global_x: int, global_y: int) -> bool:
        """游標是否落在需要讓開的控制項上。

        名單是快照，成員可能在分頁關閉後失效。這個函式是從 eventFilter 呼叫的，
        讓 RuntimeError 逸出等於直接中止行程，所以就地把失效的成員剔除。
        呼叫端已經會在分頁增減時重新登記，這裡只是最後一道防線。
        """
        alive: list[QWidget] = []
        hit = False
        for widget in self._drag_controls:
            try:
                visible = widget.isVisible()
            except RuntimeError:
                continue  # C++ 物件已被銷毀
            alive.append(widget)
            if not visible:
                continue
            rect = QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())
            if rect.contains(global_x, global_y):
                hit = True
        if len(alive) != len(self._drag_controls):
            self._drag_controls = alive
        return hit

    # -- 邊緣判定 ------------------------------------------------------------
    def _edges_at(self, global_x: int, global_y: int) -> int:
        window = self._window
        if window.isMaximized() or window.isFullScreen():
            return 0

        rect = window.frameGeometry()
        margin = self._margin
        # 游標在捲軸上時，縮放只保留最外側幾像素，其餘讓給捲軸拖曳
        if self._over_drag_control(global_x, global_y):
            margin = min(margin, config.RESIZE_MARGIN_OVER_CONTROL)
        corner = margin * 2
        edges = 0

        if global_x <= rect.left() + margin:
            edges |= Qt.Edge.LeftEdge.value
        elif global_x >= rect.right() - margin:
            edges |= Qt.Edge.RightEdge.value

        if global_y <= rect.top() + margin:
            edges |= Qt.Edge.TopEdge.value
        elif global_y >= rect.bottom() - margin:
            edges |= Qt.Edge.BottomEdge.value

        # 四個角落放寬到兩倍寬度，對角縮放才好抓
        horizontal = edges & (Qt.Edge.LeftEdge.value | Qt.Edge.RightEdge.value)
        vertical = edges & (Qt.Edge.TopEdge.value | Qt.Edge.BottomEdge.value)
        if horizontal and not vertical:
            if global_y <= rect.top() + corner:
                edges |= Qt.Edge.TopEdge.value
            elif global_y >= rect.bottom() - corner:
                edges |= Qt.Edge.BottomEdge.value
        elif vertical and not horizontal:
            if global_x <= rect.left() + corner:
                edges |= Qt.Edge.LeftEdge.value
            elif global_x >= rect.right() - corner:
                edges |= Qt.Edge.RightEdge.value
        return edges

    # -- 游標 ----------------------------------------------------------------
    def _apply_cursor(self, shape: Qt.CursorShape | None) -> None:
        if shape is None:
            if self._override_active:
                QApplication.restoreOverrideCursor()
                self._override_active = False
            return
        if self._override_active:
            QApplication.changeOverrideCursor(shape)
        else:
            QApplication.setOverrideCursor(shape)
            self._override_active = True

    # -- 事件 ----------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        event_type = event.type()

        if event_type == QEvent.Type.MouseMove:
            if event.buttons() == Qt.MouseButton.NoButton:
                position = event.globalPosition().toPoint()
                edges = self._edges_at(position.x(), position.y())
                self._apply_cursor(_CURSOR_BY_EDGES.get(edges))
            return False

        if event_type == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                position = event.globalPosition().toPoint()
                edges = self._edges_at(position.x(), position.y())
                if edges:
                    handle = self._window.windowHandle()
                    if handle is not None and handle.startSystemResize(Qt.Edge(edges)):
                        return True
            return False

        if event_type in (QEvent.Type.Leave, QEvent.Type.WindowDeactivate):
            if watched is self._window:
                self._apply_cursor(None)
            return False

        return False


class MarkdownViewer(QWidget):
    """Markdown 閱讀器主視窗。"""

    def __init__(
        self,
        path: str | None = None,
        manager=None,
        restore_session: bool = True,
    ) -> None:
        super().__init__()
        # 多視窗管理器（app/window_manager.py）。None 表示單視窗模式，
        # 分頁拆分／合併功能會安靜停用，其餘功能不受影響（測試大多走這條）。
        self._manager = manager
        # 「還原上次分頁」只屬於行程的第一個視窗。拆分／合併建立的視窗若也去
        # 讀 KEY_OPEN_TABS，會把上次 session 的殼分頁整批復活塞進來——拖一個
        # 分頁出去卻多出一堆舊分頁，關閉時又把污染後的清單存回去，越滾越大。
        self._restore_session_allowed = restore_session
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setWindowIcon(icons.app_icon())
        self.setMinimumSize(*config.MIN_WINDOW_SIZE)
        self.setAcceptDrops(True)

        self._settings = QSettings(config.ORG_NAME, config.APP_NAME)

        # 【一定要在 _build_ui 之前】標題列與設定面板是建構當下就把文字塞進
        # 元件的，目錄還沒載入的話整個介面會是一堆翻譯鍵。也因為如此，建構子
        # 不需要（也不該）再呼叫一次 apply_language——那和 apply_theme 的處境
        # 不同：QSS 沒辦法在建構時套，文字可以。
        self._language_mode = language.mode_from_settings(self._settings)
        self._language = language.resolve(self._language_mode)
        language.set_current(self._language)

        # 狀態列目前顯示的是不是「正在載入」。用旗標而不是比對文案：
        # 文案會隨語言變，拿它當程式邏輯用一翻譯就壞（見 _set_status）。
        self._status_is_busy = False

        # 主題模式預設為 system：首次啟動就會採用 Windows 目前的深淺色設定，
        # 之後系統切換時也會即時跟著變，直到使用者手動鎖定淺色或深色。
        self._theme_mode = str(
            self._settings.value(config.KEY_THEME_MODE, config.DEFAULT_THEME_MODE)
        )
        if self._theme_mode not in {mode for mode, _label in config.THEME_MODES}:
            self._theme_mode = config.DEFAULT_THEME_MODE
        self._theme = theme_utils.resolve(self._theme_mode)

        self._font_point_size = int(
            self._settings.value(config.KEY_FONT_SIZE, config.BASE_FONT_POINT_SIZE)
        )
        self._line_height = str(
            self._settings.value(config.KEY_LINE_HEIGHT, config.DEFAULT_LINE_HEIGHT)
        )
        if self._line_height not in {key for key, _l, _p in config.LINE_HEIGHT_OPTIONS}:
            self._line_height = config.DEFAULT_LINE_HEIGHT
        self._content_width = int(
            self._settings.value(config.KEY_CONTENT_WIDTH, config.DEFAULT_CONTENT_WIDTH)
        )
        if self._content_width not in {w for w, _label in config.CONTENT_WIDTH_OPTIONS}:
            self._content_width = config.DEFAULT_CONTENT_WIDTH

        self._always_on_top = self._settings.value(
            config.KEY_ALWAYS_ON_TOP, False, type=bool
        )
        self._status_visible = self._settings.value(
            config.KEY_STATUS_VISIBLE, True, type=bool
        )
        self._auto_reload = self._settings.value(
            config.KEY_AUTO_RELOAD, config.DEFAULT_AUTO_RELOAD, type=bool
        )
        self._confirm_links = self._settings.value(
            config.KEY_CONFIRM_LINKS, config.DEFAULT_CONFIRM_LINKS, type=bool
        )

        self._restore_tabs = self._settings.value(
            config.KEY_RESTORE_TABS, config.DEFAULT_RESTORE_TABS, type=bool
        )
        # 搜尋列的比對選項。和語言一樣要在 _build_ui 之前讀好：搜尋列一建出來
        # 就要把兩顆按鈕的勾選狀態擺對，不然開 Ctrl+F 的第一眼會是全部沒亮，
        # 按下去才「跳」成上次的設定。
        self._find_case_sensitive = self._settings.value(
            config.KEY_FIND_CASE_SENSITIVE,
            config.DEFAULT_FIND_CASE_SENSITIVE, type=bool
        )
        self._find_whole_words = self._settings.value(
            config.KEY_FIND_WHOLE_WORDS, config.DEFAULT_FIND_WHOLE_WORDS, type=bool
        )
        # 分頁清單。至少永遠有一個，_tab 屬性指向作用中的那個。
        self._tabs: list[DocumentTab] = []
        self._active = 0
        self._applying_topmost = False

        self._build_ui()
        self._create_shortcuts()

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watch_event)
        self._watcher.directoryChanged.connect(self._on_watch_event)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self._on_reload_timeout)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_settled)
        # 分段渲染：每塊之間回到事件迴圈，介面才有機會重繪與回應輸入
        self._chunk_timer = QTimer(self)
        self._chunk_timer.setSingleShot(True)
        self._chunk_timer.timeout.connect(self._append_next_chunk)

        self._resizer = FramelessResizer(self)

        # 這裡不重繪：下面的 open_path / _show_welcome 一定會渲染一次
        self.apply_theme(self._theme, render=False)
        self._restore_window_state()
        self.title_bar.set_pinned(self._always_on_top)
        self.status_bar.setVisible(self._status_visible)
        self._sync_settings_panel()

        # 「跟隨系統」需要在 Windows 切換深淺色時即時反應
        theme_utils.connect_system_changes(self._on_system_theme_changed)

        self._open_initial_tabs(path)

    # -- 分頁存取 ------------------------------------------------------------
    @property
    def _tab(self) -> DocumentTab:
        """目前作用中的分頁。其餘程式碼都透過它取用單一文件的狀態。"""
        return self._tabs[self._active]

    @property
    def browser(self) -> MarkdownBrowser:
        """作用中分頁的閱讀區。

        做成屬性而不是欄位，是為了讓既有那些「操作 self.browser」的程式碼
        在改成多分頁後完全不用改寫。
        """
        return self._tabs[self._active].browser

    # -- 分頁管理 ------------------------------------------------------------
    def _open_initial_tabs(self, path: str | None) -> None:
        """啟動時決定要開哪些分頁。

        還原上次的分頁時只建立空殼並記住路徑，等切過去才讀檔，
        否則還原十個分頁就要付十次 Markdown 轉換，啟動會明顯變慢。
        """
        restored: list[str] = []
        if self._restore_tabs and self._restore_session_allowed:
            saved = self._settings.value(config.KEY_OPEN_TABS, [], type=list)
            restored = [p for p in saved if isinstance(p, str) and os.path.isfile(p)]

        for saved_path in restored:
            if path and os.path.abspath(saved_path) == os.path.abspath(path):
                continue  # 命令列指定的檔案稍後才開，避免重複
            self._add_tab(DocumentTab(saved_path), activate=False)

        if path:
            self._add_tab(DocumentTab(), activate=True)
            self.open_path(path, push_history=False)
        elif self._tabs:
            index = int(self._settings.value(config.KEY_ACTIVE_TAB, 0))
            index = min(max(index, 0), len(self._tabs) - 1)
            # _active 一開始就是 0，直接呼叫 activate_tab(0) 會被「已經在這一頁」
            # 擋掉，於是還原出來的第一個分頁永遠不會被載入，畫面一片空白，
            # 搜尋列也沒有接到任何閱讀區。先指到不可能相等的值強制走完整流程。
            self._active = -1
            self.activate_tab(index)
        else:
            self._add_tab(DocumentTab(), activate=True)
            self._show_welcome()
        self._sync_tab_bar()

    def _add_tab(self, tab: DocumentTab, activate: bool) -> None:
        """把分頁加進堆疊。"""
        if activate and self._tabs:
            # 這條路徑會直接換掉 self._active，不經過 activate_tab，所以補完
            # 也要在這裡做一次。少了它，「分段載入到一半又開一個新檔」會讓
            # 舊分頁的片段永遠停在半途——那個分頁從此只剩首屏。
            self.flush_pending_chunks()
        tab.browser.setParent(self.stack)
        tab.browser.set_theme(self._theme)
        tab.browser.anchorClicked.connect(self._on_anchor_clicked)
        tab.browser.highlighted.connect(self._on_link_hovered)
        self.stack.addWidget(tab.browser)
        self._tabs.append(tab)
        if activate:
            self._active = len(self._tabs) - 1
            self.stack.setCurrentWidget(tab.browser)
            self.find_bar.attach(tab.browser)
        self._refresh_resizer_targets()

    def _refresh_resizer_targets(self) -> None:
        """分頁增減後重新登記邊緣縮放的目標。

        縮放靠的是裝在「視窗與所有子元件」上的事件過濾器，原本只在 showEvent
        裝一次。啟動之後才建立的分頁閱讀區不在那份名單裡，游標移到它上面時
        MouseMove 收不到，邊緣就拖不動了。

        更嚴重的是另一半：捲軸名單是同一時間拍下的快照，分頁一關，它的捲軸
        C++ 物件就沒了，留下的 sip 包裝再被碰到會丟 RuntimeError——而那是在
        eventFilter 裡，PyQt6 視為致命，滑鼠一動行程就中止。
        """
        if not self.isVisible():
            # 還沒顯示時 showEvent 之後會做一次，這裡跳過省得白做
            return
        self._resizer.refresh_targets()
        self._resizer.set_drag_controls(self.findChildren(QScrollBar))

    def tab_count(self) -> int:
        return len(self._tabs)

    def move_tab(self, frm: int, to: int) -> None:
        """重排分頁（拖曳排序）。分頁列已經自己把按鈕移好位置，
        這裡只同步資料順序，「不要」呼叫 _sync_tab_bar 重建。"""
        if frm == to or not (0 <= frm < len(self._tabs)) or not (0 <= to < len(self._tabs)):
            return
        tab = self._tabs.pop(frm)
        self._tabs.insert(to, tab)
        if self._active == frm:
            self._active = to
        elif frm < self._active <= to:
            self._active -= 1
        elif to <= self._active < frm:
            self._active += 1

    def take_tab(self, index: int) -> DocumentTab | None:
        """把分頁從這個視窗取出（不銷毀），準備搬去別的視窗。

        與 close_tab_at 的差別：browser 不 deleteLater、anchorClicked 要斷開
        （否則點連結會開在舊視窗）。取走最後一個分頁後這個視窗就空了，
        呼叫端負責把視窗關掉——這裡不能自己關，關了 take 的回傳值就沒人接。
        """
        if not (0 <= index < len(self._tabs)):
            return None
        if self.find_bar.isVisible():
            self.find_bar.deactivate()

        tab = self._tabs.pop(index)
        for signal, slot in (
            (tab.browser.anchorClicked, self._on_anchor_clicked),
            (tab.browser.highlighted, self._on_link_hovered),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass
        # 【拆掉來源視窗的縮放事件過濾器】
        # refresh_targets 把 resizer 裝在每個子元件上，而 Qt 的過濾器不會因為
        # reparent 而移除。少了這段，搬到新視窗的閱讀區仍會把滑鼠事件餵給
        # 「舊視窗」的縮放器：兩窗重疊時，游標在新視窗內卻讓舊視窗跳出縮放
        # 游標、甚至按下去開始縮放舊視窗。
        for widget in [tab.browser, *tab.browser.findChildren(QWidget)]:
            widget.removeEventFilter(self._resizer)
        # 帶走目前的閱讀位置，讓收養端渲染後還原（adopt_tab 會重新渲染，
        # setHtml 之後捲軸會回到頂端）
        tab.transfer_scroll = tab.scroll_ratio()
        self.stack.removeWidget(tab.browser)
        tab.browser.setParent(None)

        if not self._tabs:
            # 空視窗：不再碰 self._tab（會 IndexError），交給呼叫端收尾
            self._watch_files()
            return tab

        if index < self._active:
            self._active -= 1
        elif index == self._active:
            self._active = min(index, len(self._tabs) - 1)
        self._active = max(0, min(self._active, len(self._tabs) - 1))

        current = self._tabs[self._active]
        self.stack.setCurrentWidget(current.browser)
        self.find_bar.attach(current.browser)
        if not current.loaded:
            current.load()
            if current.path:
                self._set_search_context(current.path)
        if current.dirty:
            self._render(preserve_scroll=False)
        self.title_bar.set_back_enabled(bool(current.history))
        self._update_titles(current.display_name)
        self._update_status()
        self._sync_tab_bar()
        self._watch_files()
        self._refresh_resizer_targets()
        return tab

    def adopt_tab(self, tab: DocumentTab, index: int | None = None) -> None:
        """收養從別的視窗搬來的分頁，並切換到它（和瀏覽器一致）。

        主題可能和來源視窗不同：一律標記 dirty，activate_tab 會用本視窗的
        主題重新渲染，不然會出現「深色視窗裡有一頁是淺色」。
        """
        if index is None or not (0 <= index <= len(self._tabs)):
            index = len(self._tabs)
        if self.settings_panel.isVisible():
            # 設定面板是整片覆蓋層，不收起來的話剛合併進來的分頁會被蓋住，
            # 使用者只看到「拖進去之後什麼都沒發生」
            self.toggle_settings()
        tab.browser.setParent(self.stack)
        tab.browser.set_theme(self._theme)
        tab.browser.anchorClicked.connect(self._on_anchor_clicked)
        tab.browser.highlighted.connect(self._on_link_hovered)
        tab.dirty = True
        self.stack.insertWidget(index, tab.browser)
        self._tabs.insert(index, tab)
        if index <= self._active and len(self._tabs) > 1:
            self._active += 1
        self._sync_tab_bar()
        # activate_tab 需要「目前不在那個分頁」才會走完整流程
        if self._active == index:
            self._active = -1
        self.activate_tab(index)
        self._watch_files()
        self._refresh_resizer_targets()
        # 還原搬移前的閱讀位置。activate_tab 的重新渲染（setHtml）把捲軸打回
        # 頂端；文件高度要等版面算完才正確，所以下一個事件回合再套用。
        ratio = getattr(tab, "transfer_scroll", None)
        if ratio:
            QTimer.singleShot(0, lambda: tab.apply_scroll_ratio(ratio))
            tab.transfer_scroll = None

    def _drag_intent_at(self, global_pos, outside_band: bool) -> str:
        """拖曳中的即時預告：這個位置放開會發生什麼。

        與 _on_tab_detached 的決策必須一致，否則徽章寫「合併」放開卻拆分，
        比沒有預告更糟。順帶在目標視窗的分頁列畫出插入位置指示線——
        兩者共用同一個 drop_target_at 的結果，線的位置就是實際落點。
        """
        if self._manager is None:
            return "none"
        target = self._manager.drop_target_at(global_pos, exclude=self)
        self._update_insert_markers(target)
        if target is not None:
            return "merge"
        if outside_band and len(self._tabs) > 1:
            return "detach"
        return "none"

    def _update_insert_markers(self, target) -> None:
        """只在目標視窗顯示插入指示線，其餘視窗一律清掉。

        每次移動都全部掃一遍而不是記住上一個目標：拖曳期間視窗可能被關閉，
        留著參照去清會踩到已銷毀的物件（本專案在捲軸快照上吃過這個虧）。
        """
        if self._manager is None:
            return
        target_window = target[0] if target else None
        for window in self._manager.windows():
            if window is target_window:
                window.tab_bar.show_insert_marker(target[1])
            else:
                window.tab_bar.hide_insert_marker()

    def _clear_insert_markers(self) -> None:
        if self._manager is None:
            self.tab_bar.hide_insert_marker()
            return
        for window in self._manager.windows():
            window.tab_bar.hide_insert_marker()

    def _on_tab_detached(
        self, index: int, global_pos, allow_new_window: bool = True
    ) -> None:
        """分頁被拖出列外放開：合併進游標下的視窗，否則拆成新視窗。

        allow_new_window=False 表示放開點只離開了分頁列、還在容忍帶內
        （通常是相鄰視窗的分頁列上）：有合併目標就合併，沒有就不動作，
        不會因為手滑幾個像素噴出一個新視窗。
        """
        if self._manager is None:
            return
        self._clear_insert_markers()
        target = self._manager.drop_target_at(global_pos, exclude=self)
        if target is not None:
            other, insert_at = target
            tab = self.take_tab(index)
            if tab is None:
                return
            other.adopt_tab(tab, insert_at)
            other.raise_()
            other.activateWindow()
        else:
            if not allow_new_window:
                return
            if len(self._tabs) <= 1:
                # 單一分頁拖到空白處＝把整個視窗搬過去，沒有拆分的意義
                return
            tab = self.take_tab(index)
            if tab is None:
                return
            self._manager.create_window(adopt=tab, near=global_pos)
        if not self._tabs:
            self.close()

    def _index_of_path(self, path: str) -> int | None:
        target = os.path.abspath(path)
        for index, tab in enumerate(self._tabs):
            if tab.path and os.path.abspath(tab.path) == target:
                return index
        return None

    def activate_tab(self, index: int) -> None:
        """切到指定分頁；內容還沒讀或需要重繪時在這裡補上。"""
        if not (0 <= index < len(self._tabs)) or index == self._active:
            if 0 <= index < len(self._tabs):
                self._sync_tab_bar()
            return

        # 先把離開中的這個分頁補完：這樣「還沒補完的片段」永遠屬於作用中的
        # 分頁，計時器就不可能把 A 的片段塞進 B 的文件裡
        self.flush_pending_chunks()

        if self.find_bar.isVisible():
            self.find_bar.deactivate()

        self._active = index
        tab = self._tabs[index]
        self.stack.setCurrentWidget(tab.browser)
        self.find_bar.attach(tab.browser)

        if not tab.loaded:
            tab.load()
            if tab.path:
                self._set_search_context(tab.path)
        if tab.dirty:
            self._render(preserve_scroll=False)

        self._update_titles(tab.display_name)
        self._update_status()
        self.title_bar.set_back_enabled(bool(tab.history))
        self._sync_tab_bar()

    def new_tab(self) -> None:
        """開一個空白分頁並顯示歡迎頁。"""
        self._add_tab(DocumentTab(), activate=True)
        self._show_welcome()
        self._sync_tab_bar()

    def close_tab(self) -> None:
        """關閉作用中的分頁；只剩一個時關閉視窗。"""
        self.close_tab_at(self._active)

    def close_tab_at(self, index: int) -> None:
        if not (0 <= index < len(self._tabs)):
            return
        if len(self._tabs) <= 1:
            self.close()
            return

        # 比照 activate_tab：搜尋列裡的比對位置是針對「目前那份文件」算出來的
        # 字元位移，換一份文件就完全對不上了（實測 300 筆比對留在一份只有 4 個
        # 字元的文件上，計數器照跳、卻什麼都沒高亮）。
        if self.find_bar.isVisible():
            self.find_bar.deactivate()

        tab = self._tabs.pop(index)
        self.stack.removeWidget(tab.browser)
        tab.browser.setParent(None)
        tab.browser.deleteLater()

        # 關掉的若在作用分頁之前，索引要往前挪
        if index < self._active:
            self._active -= 1
        elif index == self._active:
            self._active = min(index, len(self._tabs) - 1)
        self._active = max(0, min(self._active, len(self._tabs) - 1))

        current = self._tabs[self._active]
        self.stack.setCurrentWidget(current.browser)
        self.find_bar.attach(current.browser)
        if not current.loaded:
            current.load()
            if current.path:
                self._set_search_context(current.path)
        if current.dirty:
            self._render(preserve_scroll=False)
        self.title_bar.set_back_enabled(bool(current.history))
        self._update_titles(current.display_name)
        self._update_status()
        self._sync_tab_bar()
        self._watch_files()
        self._refresh_resizer_targets()

    def next_tab(self) -> None:
        if len(self._tabs) > 1:
            self.activate_tab((self._active + 1) % len(self._tabs))

    def previous_tab(self) -> None:
        if len(self._tabs) > 1:
            self.activate_tab((self._active - 1) % len(self._tabs))

    def _mark_all_dirty(self) -> None:
        """字級或行高改變後，背景分頁切過去時才重繪。"""
        for tab in self._tabs:
            tab.dirty = True

    def _sync_tab_bar(self) -> None:
        # 傳路徑而不是 tooltip：tooltip 是翻譯過的字串，拿它當「結構有沒有變」
        # 的依據會讓語言一換就整列重建（見 TabButton.matches）
        self.tab_bar.set_tabs(
            [(tab.display_name, tab.path) for tab in self._tabs], self._active
        )

    # -- 外部開檔（單一實例） ------------------------------------------------
    def handle_external_open(self, path: str) -> None:
        """另一個行程把檔案交過來時呼叫（雙擊 .md 而本程式已在執行）。"""
        if path:
            self.open_path(path, new_tab=True)
        # 使用者是在檔案總管雙擊的，視窗要自己跳到前景
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        win32.force_foreground(int(self.winId()))

    # -- 分頁狀態保存 --------------------------------------------------------
    def set_restore_tabs(self, enabled: bool) -> None:
        self._restore_tabs = bool(enabled)
        self._settings.setValue(config.KEY_RESTORE_TABS, self._restore_tabs)
        if not self._restore_tabs:
            self._settings.remove(config.KEY_OPEN_TABS)
            self._settings.remove(config.KEY_ACTIVE_TAB)
        self._sync_settings_panel()

    def _save_session(self) -> None:
        if not self._restore_tabs:
            return
        if not self._tabs:
            # 分頁被合併到別的視窗後，空視窗的 close 會走到這裡。兩件事都不能做：
            # self._tab 會 IndexError（closeEvent 是 Qt 虛擬函式，例外＝行程中止），
            # 寫入空清單則會把還有分頁的那個視窗待存的 session 清掉。
            return
        paths = [tab.path for tab in self._tabs if tab.path]
        self._settings.setValue(config.KEY_OPEN_TABS, paths)
        # 索引要以「有路徑的分頁」為準，空白分頁不會被還原
        active_path = self._tab.path
        self._settings.setValue(
            config.KEY_ACTIVE_TAB,
            paths.index(active_path) if active_path in paths else 0,
        )

    # -- 介面組裝 ------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.root_frame = QFrame(self)
        self.root_frame.setObjectName("rootFrame")
        outer.addWidget(self.root_frame)

        inner = QVBoxLayout(self.root_frame)
        inner.setContentsMargins(1, 1, 1, 1)
        inner.setSpacing(0)

        self.title_bar = CustomTitleBar(self.root_frame)
        self.tab_bar = TabBar(self.root_frame)
        # 每個分頁一個閱讀區，用堆疊切換，各自保有捲動位置與文件物件
        self.stack = QStackedWidget(self.root_frame)
        self.find_bar = FindBar(self.root_frame)
        self.find_bar.set_options(self._find_case_sensitive, self._find_whole_words)
        # 設定面板是覆蓋層，不放進版面，改由 _position_settings_panel 手動定位，
        # 這樣它才能整片蓋住標題列以下的區域（含搜尋列與狀態列）
        self.settings_panel = SettingsPanel(self.root_frame)

        self.status_bar = QFrame(self.root_frame)
        self.status_bar.setObjectName("statusBar")
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.setContentsMargins(12, 4, 12, 4)
        status_layout.setSpacing(0)
        self.status_path_label = StatusPathLabel(self.status_bar)
        self.status_path_label.clicked.connect(self._on_status_path_clicked)
        status_layout.addWidget(self.status_path_label)
        self.status_label = QLabel("", self.status_bar)
        self.status_label.setObjectName("statusLabel")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch(1)

        inner.addWidget(self.title_bar)
        inner.addWidget(self.tab_bar)
        inner.addWidget(self.stack, 1)
        inner.addWidget(self.find_bar)
        inner.addWidget(self.status_bar)

        self.find_bar.optionsChanged.connect(self.set_find_options)
        self.title_bar.openRequested.connect(self.open_dialog)
        self.title_bar.backRequested.connect(self.go_back)
        self.title_bar.findRequested.connect(self.show_find)
        self.title_bar.settingsRequested.connect(self.toggle_settings)
        self.title_bar.themeToggleRequested.connect(self.toggle_theme)
        self.title_bar.pinToggled.connect(self.set_always_on_top)
        self.title_bar.minimizeRequested.connect(self.showMinimized)
        self.title_bar.maximizeToggleRequested.connect(self.toggle_maximized)
        # 標題列的 X 是「視窗」控制鈕，一律關整個視窗（開著的分頁由
        # closeEvent 的 _save_session 記住）。關「分頁」是 Ctrl+W 與
        # 分頁列上各自的 X 的事，兩者不要混——混了會變成「明明按了視窗的
        # 關閉鈕，視窗卻還在，只是分頁少一個」。
        self.title_bar.closeRequested.connect(self.close)

        self.tab_bar.activated.connect(self.activate_tab)
        self.tab_bar.closeRequested.connect(self.close_tab_at)
        self.tab_bar.tabMoved.connect(self.move_tab)
        self.tab_bar.detachRequested.connect(self._on_tab_detached)
        self.tab_bar.drop_intent_probe = self._drag_intent_at
        self.tab_bar.drag_ended = self._clear_insert_markers
        self.tab_bar.newTabRequested.connect(self.open_dialog)

        self.settings_panel.languageModeChanged.connect(self.set_language_mode)
        self.settings_panel.themeModeChanged.connect(self.set_theme_mode)
        self.settings_panel.lineHeightChanged.connect(self.set_line_height)
        self.settings_panel.contentWidthChanged.connect(self.set_content_width)
        self.settings_panel.autoReloadChanged.connect(self.set_auto_reload)
        self.settings_panel.alwaysOnTopChanged.connect(self.set_always_on_top)
        self.settings_panel.statusBarChanged.connect(self.set_status_bar_visible)
        self.settings_panel.confirmLinksChanged.connect(self.set_confirm_links)
        self.settings_panel.restoreTabsChanged.connect(self.set_restore_tabs)
        self.settings_panel.resetRequested.connect(self.reset_settings)
        self.settings_panel.font_minus.clicked.connect(self.zoom_out)
        self.settings_panel.font_plus.clicked.connect(self.zoom_in)

    def _create_shortcuts(self) -> None:
        bindings = (
            ("Ctrl+O", self.open_dialog),
            ("Ctrl+R", self.reload),
            ("F5", self.reload),
            ("Ctrl+F", self.show_find),
            ("Ctrl+,", self.toggle_settings),
            ("Ctrl+D", self.toggle_theme),
            ("Ctrl+P", self.toggle_always_on_top),
            ("Ctrl+=", self.zoom_in),
            ("Ctrl++", self.zoom_in),
            ("Ctrl+-", self.zoom_out),
            ("Ctrl+0", self.zoom_reset),
            ("Ctrl+/", self.toggle_status_bar),
            ("Alt+Left", self.go_back),
            ("F11", self.toggle_maximized),
            # Ctrl+T 對應分頁列的「＋」，兩個入口行為一致；要直接挑檔案用 Ctrl+O
            ("Ctrl+T", self.new_tab),
            ("Ctrl+W", self.close_tab),
            ("Ctrl+Tab", self.next_tab),
            ("Ctrl+Shift+Tab", self.previous_tab),
            ("Ctrl+PgDown", self.next_tab),
            ("Ctrl+PgUp", self.previous_tab),
            ("Esc", self._on_escape),
        )
        for sequence, slot in bindings:
            QShortcut(QKeySequence(sequence), self, activated=slot)

    # -- 主題 ----------------------------------------------------------------
    def apply_theme(self, theme: str, render: bool = True) -> None:
        """套用主題：視窗 QSS、文件 CSS、圖示著色與程式碼高亮一次同步。

        這是全專案唯一的 setStyleSheet 呼叫。

        render=False 用於程式啟動時：那時還沒載入檔案，重繪出來的是歡迎頁，
        緊接著就會被 open_path 蓋掉，等於白做一次 Markdown 轉換與版面計算。
        """
        self._theme = theme
        self.setStyleSheet(styles.build_qss(theme, self._language))
        self.title_bar.apply_theme(theme)
        self.tab_bar.apply_theme(theme)
        self.find_bar.apply_theme(theme)
        self.settings_panel.apply_theme(theme)
        # 只重繪看得到的那個分頁，其餘標記待重繪，切過去時才處理
        for tab in self._tabs:
            tab.browser.set_theme(theme)
            tab.dirty = True
        if render and self._tabs:
            self._render()

    def set_theme_mode(self, mode: str) -> None:
        """設定主題模式："light" / "dark" / "system"。"""
        if mode not in {name for name, _label in config.THEME_MODES}:
            return
        self._theme_mode = mode
        self._settings.setValue(config.KEY_THEME_MODE, mode)
        self.apply_theme(theme_utils.resolve(mode))
        self._sync_settings_panel()

    def toggle_theme(self) -> None:
        """標題列的切換鈕：直接切到另一個配色，並把模式鎖定成該配色。

        想回到自動跟隨，就到設定列選「跟隨系統」。
        """
        self.set_theme_mode(styles.other_theme(self._theme))

    def _on_system_theme_changed(self, _scheme=None) -> None:
        """Windows 切換深淺色時觸發；只有模式為 system 才跟著變。

        參數是 Qt 傳來的 ColorScheme，這裡用不到，但必須收下——直接連接
        bound method（而非 lambda）才能讓 Qt 在視窗銷毀時自動斷開連線。
        """
        if self._theme_mode != "system":
            return
        resolved = theme_utils.resolve("system")
        if resolved != self._theme:
            self.apply_theme(resolved)

    # -- 閱讀版面 ------------------------------------------------------------
    def set_line_height(self, key: str) -> None:
        if key not in {name for name, _l, _p in config.LINE_HEIGHT_OPTIONS}:
            return
        self._line_height = key
        self._settings.setValue(config.KEY_LINE_HEIGHT, key)
        self._mark_all_dirty()
        self._render()
        self._sync_settings_panel()

    def set_content_width(self, width: int) -> None:
        if width not in {value for value, _label in config.CONTENT_WIDTH_OPTIONS}:
            return
        self._content_width = int(width)
        self._settings.setValue(config.KEY_CONTENT_WIDTH, self._content_width)
        self._mark_all_dirty()
        self._apply_content_width()
        self._sync_settings_panel()

    # -- 語言 ----------------------------------------------------------------
    def apply_language(self, code: str, render: bool = True) -> None:
        """套用語言：介面文字、tooltip、視窗標題、狀態列與歡迎／錯誤頁一次同步。

        刻意比照 apply_theme 手寫呼叫四個頂層元件，而不是 findChildren 自動
        走訪：自動走訪碰不到「文字是算出來的」那些地方（狀態列、視窗標題、
        拖曳徽章、applicationDisplayName），而漏掉時完全沒有徵兆。名單寫在
        這裡，日後多一個元件會在 code review 上看得到。
        """
        language.set_current(code)          # 行程全域，含 Qt 內建翻譯的掛載
        self._language = code

        # 和 main.py 的 setApplicationDisplayName 必須同源，否則 Windows 會在
        # 標題後面自動補一段舊語言的後綴（見 _update_titles 的說明）
        app = QApplication.instance()
        if app is not None:
            app.setApplicationDisplayName(t("app.displayName"))

        self.title_bar.apply_language()
        self.tab_bar.apply_language()
        self.find_bar.apply_language()
        self.settings_panel.apply_language()
        self.status_path_label.apply_language()

        for tab in self._tabs:
            tab.on_language_changed()
        self._sync_tab_bar()
        self._sync_settings_panel()
        self._update_titles(
            os.path.basename(self._tab.path) if self._tab.path else None
        )
        self._refresh_status_text()

        # 介面字型隨語言換（styles.FONT_UI_BY_LANGUAGE），QSS 與文件 CSS 都要
        # 重算。借道 apply_theme 而不是自己 setStyleSheet：「全專案只有一次
        # setStyleSheet」的規範就在那個函式裡，在這裡再開一次就破功了。
        # 它順帶把所有分頁標記 dirty 並重繪看得到的那個——正是語言切換需要的。
        self.apply_theme(self._theme, render=render)

    def set_language_mode(self, mode: str) -> None:
        """設定語言模式："zh_TW" / "en" / "system"。"""
        if mode not in {code for code, _key in config.LANGUAGE_MODES}:
            return
        self._language_mode = mode
        self._settings.setValue(config.KEY_LANGUAGE_MODE, mode)
        self.apply_language(language.resolve(mode))
        if self._manager is not None:
            self._manager.broadcast_language(mode, origin=self)

    def adopt_language_mode(self, mode: str) -> None:
        """別的視窗改了語言，這裡跟著套用。

        刻意不重用 set_language_mode：那會再寫一次 QSettings（多餘）並再廣播
        一次（無窮迴圈）。名字說明了「這是外面傳進來的」，和 note_activated
        同一個路數。
        """
        if mode == self._language_mode:
            return
        self._language_mode = mode
        self.apply_language(language.resolve(mode))

    def _line_height_percent(self) -> int:
        for key, _label, percent in config.LINE_HEIGHT_OPTIONS:
            if key == self._line_height:
                return percent
        return 160

    def _apply_content_width(self) -> None:
        """限制文字欄寬度並置中。

        QTextDocument 不支援 max-width，而 setDocumentMargin 只能四邊同時設定
        （上下也會跟著變超大）。root frame 的 frameFormat 可以分別指定四邊，
        因此用左右邊距把文字欄夾成固定寬度，效果等同置中的閱讀欄。
        """
        margin = styles.DOCUMENT_MARGIN
        viewport = self.browser.viewport().width()

        if self._content_width and viewport > self._content_width + 2 * margin:
            side = (viewport - self._content_width) / 2
        else:
            side = margin

        # 【算出同一個邊距就不要碰 frameFormat】setFrameFormat 會讓整份文件
        # 重新排版，而 resizeEvent 每一幀都會走到這裡。限制內文寬度時，只要
        # 視窗還是比內文寬得多，side 一路都是同一個數字，重排純屬白工；
        # 不限制寬度（side 恆等於 margin）時更是從頭到尾都不會變。
        # 大文件上這是拖曳視窗邊緣最主要的卡頓來源。
        if self._tab.applied_side_margin == side:
            return

        frame = self.browser.document().rootFrame()
        fmt = frame.frameFormat()
        fmt.setLeftMargin(side)
        fmt.setRightMargin(side)
        fmt.setTopMargin(margin)
        fmt.setBottomMargin(margin)
        frame.setFrameFormat(fmt)
        self._tab.applied_side_margin = side

    # -- 行為開關 ------------------------------------------------------------
    def set_auto_reload(self, enabled: bool) -> None:
        self._auto_reload = bool(enabled)
        self._settings.setValue(config.KEY_AUTO_RELOAD, self._auto_reload)
        if self._auto_reload:
            self._watch_files()
        else:
            self._unwatch_all()
        self._sync_settings_panel()

    def set_status_bar_visible(self, visible: bool) -> None:
        self._status_visible = bool(visible)
        self.status_bar.setVisible(self._status_visible)
        self._settings.setValue(config.KEY_STATUS_VISIBLE, self._status_visible)
        self._sync_settings_panel()

    def set_confirm_links(self, enabled: bool) -> None:
        self._confirm_links = bool(enabled)
        self._settings.setValue(config.KEY_CONFIRM_LINKS, self._confirm_links)
        self._sync_settings_panel()

    def set_find_options(self, case_sensitive: bool, whole_words: bool) -> None:
        """搜尋列的比對選項改變時落盤。

        刻意不廣播到其他視窗：其餘視窗可能正開著自己的搜尋，被外部改掉比對
        規則會讓畫面上的高亮無故變動。存下來的值只影響「下一個開啟的視窗」。

        兩個鍵是**成對**寫的，不是各寫各的——多視窗時最後一個動過的視窗整組
        覆蓋（同 _save_session 對分頁清單的作法）。這樣存下來的一定是某個視窗
        真的呈現過的組合；改成逐鍵合併的話，會存出一個「A 的大小寫 + B 的全字」
        這種沒有任何視窗顯示過的狀態，下一個開的視窗跟誰都對不上。
        """
        self._find_case_sensitive = bool(case_sensitive)
        self._find_whole_words = bool(whole_words)
        self._settings.setValue(
            config.KEY_FIND_CASE_SENSITIVE, self._find_case_sensitive
        )
        self._settings.setValue(config.KEY_FIND_WHOLE_WORDS, self._find_whole_words)

    # -- 設定列 --------------------------------------------------------------
    def toggle_settings(self) -> None:
        if self.settings_panel.isVisible():
            self.settings_panel.deactivate()
            return
        if self.find_bar.isVisible():
            self.find_bar.deactivate()
        self._position_settings_panel()
        self.settings_panel.activate()

    def _position_settings_panel(self) -> None:
        """讓面板剛好覆蓋標題列以下的整個視窗內部。

        位置直接從標題列的實際矩形推算，不寫死邊框寬度——外框線與版面內距
        會疊加（實測起點是 2px 而不是 1px），寫死很容易差一列蓋到標題列。
        """
        bar = self.title_bar.geometry()
        inset = bar.left()
        top = bar.bottom() + 1
        frame = self.root_frame
        self.settings_panel.setGeometry(
            inset,
            top,
            max(0, bar.width()),
            max(0, frame.height() - top - inset),
        )

    def _sync_settings_panel(self) -> None:
        self.settings_panel.sync(
            language_mode=self._language_mode,
            theme_mode=self._theme_mode,
            font_point_size=self._font_point_size,
            line_height=self._line_height,
            content_width=self._content_width,
            auto_reload=self._auto_reload,
            always_on_top=self._always_on_top,
            status_bar=self._status_visible,
            confirm_links=self._confirm_links,
            restore_tabs=self._restore_tabs,
        )

    def reset_settings(self) -> None:
        """把所有可調設定恢復成預設值。"""
        self._font_point_size = config.BASE_FONT_POINT_SIZE
        self._line_height = config.DEFAULT_LINE_HEIGHT
        self._content_width = config.DEFAULT_CONTENT_WIDTH
        self._auto_reload = config.DEFAULT_AUTO_RELOAD
        self._confirm_links = config.DEFAULT_CONFIRM_LINKS
        self._restore_tabs = config.DEFAULT_RESTORE_TABS
        self._find_case_sensitive = config.DEFAULT_FIND_CASE_SENSITIVE
        self._find_whole_words = config.DEFAULT_FIND_WHOLE_WORDS
        # set_options 不會反向送出訊號，下面那圈迴圈才是真正寫進 QSettings 的地方
        self.find_bar.set_options(self._find_case_sensitive, self._find_whole_words)
        self._status_visible = True
        self.status_bar.setVisible(True)
        self.set_always_on_top(False)

        for key, value in (
            (config.KEY_FONT_SIZE, self._font_point_size),
            (config.KEY_LINE_HEIGHT, self._line_height),
            (config.KEY_CONTENT_WIDTH, self._content_width),
            (config.KEY_AUTO_RELOAD, self._auto_reload),
            (config.KEY_CONFIRM_LINKS, self._confirm_links),
            (config.KEY_RESTORE_TABS, self._restore_tabs),
            (config.KEY_STATUS_VISIBLE, self._status_visible),
            (config.KEY_FIND_CASE_SENSITIVE, self._find_case_sensitive),
            (config.KEY_FIND_WHOLE_WORDS, self._find_whole_words),
            (config.KEY_LANGUAGE_MODE, config.DEFAULT_LANGUAGE_MODE),
        ):
            self._settings.setValue(key, value)

        self._watch_files()
        # 語言要先切回預設，最後那則訊息才會是新語言的
        self.set_language_mode(config.DEFAULT_LANGUAGE_MODE)
        self.set_theme_mode(config.DEFAULT_THEME_MODE)
        self.status_path_label.setText("")
        self._set_status(t("status.resetDone"))

    # -- 開檔與渲染 ----------------------------------------------------------
    def open_dialog(self) -> None:
        start_dir = os.path.dirname(self._tab.path) if self._tab.path else ""
        path, _selected = QFileDialog.getOpenFileName(
            self, t("dialog.openTitle"), start_dir, t("dialog.openFilter")
        )
        if path:
            self.open_path(path, new_tab=True)

    def open_path(
        self,
        path: str,
        push_history: bool = True,
        anchor: str = "",
        new_tab: bool = False,
    ) -> None:
        """載入指定檔案；讀取失敗時在閱讀區顯示錯誤頁。

        new_tab=True 會開在新分頁；若該檔案已經開著，就直接切過去而不重複開。
        """
        path = os.path.abspath(path)
        if new_tab:
            existing = self._index_of_path(path)
            if existing is not None:
                self.activate_tab(existing)
                if anchor:
                    # 錨點可能落在還沒補上的片段裡，跳之前先補完
                    QTimer.singleShot(0, lambda: self._scroll_to_anchor(anchor))
                return
            self._add_tab(DocumentTab(), activate=True)

        if push_history and self._tab.path and os.path.abspath(self._tab.path) != path:
            self._tab.history.append(self._tab.path)
            self.title_bar.set_back_enabled(True)

        self._tab.pending_anchor = anchor
        self._tab.path = path
        self._set_search_context(path)

        try:
            self._tab.text, self._tab.meta = document.read_text_file(path)
        except document.DocumentError as error:
            self._tab.text = ""
            self._tab.meta = None
            self._tab.error = error
            self._tab.file_stamp = None
            self._tab.loaded = True
            self._render(preserve_scroll=False)
            self._update_titles(os.path.basename(path) or path)
            self.status_path_label.setText("")
            self._set_status(t("status.readFailed", path=path))
            self._sync_tab_bar()
            self._watch_files()
            return

        self._tab.error = None
        self._tab.loaded = True
        self._tab.file_stamp = self._stamp_of(path)
        self._render()
        self._update_titles(self._tab.meta.display_name)
        self._update_status()
        self._sync_tab_bar()
        self._watch_files()
        if anchor:
            QTimer.singleShot(0, lambda: self._scroll_to_anchor(anchor))

    def _show_welcome(self) -> None:
        self._tab.path = None
        self._tab.text = ""
        self._tab.meta = None
        self._tab.error = None
        self._tab.loaded = True
        self._render(preserve_scroll=False)
        self._update_titles(None)
        self.status_path_label.setText("")
        self._set_status(t("status.noFile"))

    def _render(self, preserve_scroll: bool = True) -> None:
        """重新產生 HTML 並套用，預設保留閱讀位置。

        這裡是所有渲染路徑的咽喉（開檔、延後載入、切主題、字級、收養分頁），
        大檔的忙碌回饋因此只需要掛在這一處。
        """
        with self._busy_feedback(self._tab.path):
            self._render_now(preserve_scroll)

    def _render_now(self, preserve_scroll: bool) -> None:
        # 上一輪的分段還沒補完就重新渲染（換主題、換字級）：停掉計時器。
        # 真正讓舊片段作廢的是下面那行重新指派 pending_chunks，這裡停計時器
        # 只是把意圖寫明白，並省下一次無謂的喚醒。
        # 這時算出來的捲動比例是對著只有首屏的文件算的，但正在分段載入
        # 代表使用者根本還沒來得及捲動，實務上就是 0。
        self._chunk_timer.stop()
        ratio = self._scroll_ratio() if preserve_scroll else 0.0
        font = self.browser.font()
        font.setPointSize(self._font_point_size)
        self.browser.document().setDefaultFont(font)
        # 樣式表必須在 setHtml 之前套用，否則不會生效；字級改變時也要重算，
        # 因為 h6 的字級只能用絕對單位指定（詳見 styles.py 的說明）。
        self.browser.document().setDefaultStyleSheet(
            styles.build_doc_css(
                self._theme,
                self._font_point_size,
                self._line_height_percent(),
                self._language,
            )
        )

        html = self._tab.build_html(self._theme)
        self._tab.dirty = False

        # 任務清單的核取方塊也是 <img>，但尺寸固定，不需要隨視窗重繪
        self._tab.has_scalable_images = bool(
            re.search(r'<img[^>]+src="(?!mdres:)', html)
        )
        self._tab.last_render_width = self.browser.viewport().width()

        # 圖片的 alt 文字要在 setHtml 之前交給閱讀區：Qt 解析 HTML 時會把 alt
        # 丟掉，而破圖佔位要靠它顯示「這裡本來是什麼」（見 browser.set_image_alts）
        self.browser.set_image_alts(document.image_alts(html))

        # 【分段渲染】大文件一次 setHtml 會整份排完版才回來，那段時間介面
        # 完全凍結。改成先把首屏交出去、其餘分批在事件迴圈的空檔補上。
        # 不適合分段時 chunks 是空的，行為與從前完全相同。
        head, chunks = document.split_for_progressive_render(html)
        self._tab.pending_chunks = chunks
        self._tab.pending_scroll_ratio = ratio if preserve_scroll else 0.0

        self.browser.setHtml(head)
        # setHtml 會重建文件，root frame 的邊距回到預設值——快取要跟著失效，
        # 否則下面這次會誤判「和上次算出來的一樣」而跳過，邊距就套不上去
        self._tab.applied_side_margin = None
        self._apply_content_width()
        if chunks:
            # 首屏已經在文件裡了；把控制權交回事件迴圈讓它畫出來，再補其餘的
            self._chunk_timer.start(0)
        else:
            self._finish_render()

    # -- 分段渲染 ------------------------------------------------------------
    def _append_next_chunk(self) -> None:
        """補上一塊，然後把控制權還給事件迴圈。

        每塊之間都回到事件迴圈，介面才有機會重繪與回應輸入。實測最壞情況
        （3000 個標題）每塊約 83ms，而一次補完是 1.9 秒。
        """
        tab = self._tab
        if not tab.pending_chunks:
            return
        self._insert_chunk(tab.pending_chunks.pop(0))
        if tab.pending_chunks:
            self._chunk_timer.start(0)
        else:
            self._finish_render()

    def _insert_chunk(self, chunk: str) -> None:
        # 用獨立的 QTextCursor 而不是 browser 的文字游標：後者是使用者的選取，
        # 動到它會把選取範圍與捲動位置一起弄掉
        cursor = QTextCursor(self.browser.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # 【接合處要不要先開一個新區塊，看片段的第一個元素是什麼】
        # insertHtml 會把片段的第一個區塊併進游標所在的那個區塊。片段以
        # <p>、<h3>、清單等開頭時就會和前一段黏成一段（實測少掉一個換行）；
        # 而表格在 Qt 裡自成一個 frame，不會併——這時多開一個區塊反而會留下
        # 一個空段落。實測九種文件輪廓（純段落／純 h3／清單／表格／引言／
        # 程式碼／混合／標題／隨機），只有這個條件式的版本每一種都與
        # 一次到底逐字元相同。
        if not chunk.lstrip()[:6].lower().startswith("<table"):
            cursor.insertBlock()
        cursor.insertHtml(chunk)

    def flush_pending_chunks(self) -> None:
        """把還沒補上的片段立刻補完。

        任何需要「完整文件」的操作——搜尋、捲動比例、切分頁、存工作階段——
        在動作之前都要先呼叫，否則會對著只有首屏的文件下判斷。
        """
        if not self._tabs:
            return
        tab = self._tab
        if not tab.pending_chunks:
            return
        self._chunk_timer.stop()
        while tab.pending_chunks:
            self._insert_chunk(tab.pending_chunks.pop(0))
        self._finish_render()

    def _scroll_to_anchor(self, anchor: str) -> None:
        """跳到錨點。分段渲染時錨點可能還在沒補上的片段裡，先補完再跳。"""
        self.flush_pending_chunks()
        self.browser.scrollToAnchor(anchor)

    def _finish_render(self) -> None:
        """整份文件都進去之後的收尾（一次到底與分段補完共用）。"""
        tab = self._tab
        # 內容長高了，限寬時的左右邊距要依最終的可視寬度再算一次
        self._apply_content_width()
        # 文件剛被清空再填回，搜尋列快取的比對位置與游標都已經失效
        self.find_bar.refresh_for_new_document()
        ratio = tab.pending_scroll_ratio
        tab.pending_scroll_ratio = 0.0
        if ratio > 0:
            QTimer.singleShot(0, lambda: self._apply_scroll_ratio(ratio))

    def reload(self) -> None:
        if not self._tab.path:
            return
        self.open_path(self._tab.path, push_history=False)

    def go_back(self) -> None:
        if not self._tab.history:
            return
        previous = self._tab.history.pop()
        self.title_bar.set_back_enabled(bool(self._tab.history))
        self.open_path(previous, push_history=False)

    # -- 捲動位置 ------------------------------------------------------------
    def _scroll_ratio(self) -> float:
        bar = self.browser.verticalScrollBar()
        return bar.value() / bar.maximum() if bar.maximum() else 0.0

    def _apply_scroll_ratio(self, ratio: float) -> None:
        bar = self.browser.verticalScrollBar()
        bar.setValue(int(round(ratio * bar.maximum())))

    # -- 標題與狀態列 --------------------------------------------------------
    def _update_titles(self, name: str | None) -> None:
        """更新標題列與視窗標題。name 為 None 代表歡迎頁（目前沒有開檔）。

        原本是拿 name 和產品名稱做字串相等來判斷歡迎頁。產品名稱一旦隨語言
        改變，那個判斷會在切語言的瞬間失效，標題會變成
        「Markdown Reader — Markdown Reader」。改由呼叫端明講。
        """
        self.title_bar.set_title(name if name is not None else t("app.displayName"))
        # 歡迎頁的名稱就是程式名稱，避免標題出現重複的「閱讀器 — 閱讀器」
        app_name = t("app.displayName")
        # 【windowTitle 一定要以 applicationDisplayName 結尾】
        # Qt 在 Windows 上若發現 windowTitle 沒有以 applicationDisplayName 收尾，
        # 會自動補一段 " - <displayName>"。兩邊來源不一致時實測會得到
        # 「note.md — Markdown Reader - Markdown 閱讀器」。因此這裡的後綴與
        # main.py 的 setApplicationDisplayName 必須是同一個 t("app.displayName")，
        # 語言切換時兩邊也要同時更新（apply_language 有一行專門做這件事）。
        self.setWindowTitle(
            app_name if name is None else t("window.title", name=name, app=app_name)
        )

    def _on_link_hovered(self, url: QUrl) -> None:
        """滑鼠移到連結上：在狀態列顯示目標，移開就恢復原本的檔案資訊。

        外部連結會交給系統瀏覽器開啟，點下去之前看得到要去哪很重要。
        路徑欄位保持不動（它是可點的「在檔案總管中顯示」，不該被連結蓋掉）。

        注意 QTextBrowser.highlighted 帶的是 QUrl 不是 str；接成 str 會在
        真的 hover 時因型別不符而完全不觸發（測試才抓到）。本機檔案顯示成
        原生路徑，看起來才像這個程式的其他地方。
        """
        # toLocalFile 回傳的是正斜線；狀態列路徑與其他地方都用 Windows
        # 原生的反斜線，這裡正規化以免同一列出現兩種風格
        text = (os.path.normpath(url.toLocalFile()) if url.isLocalFile()
                else url.toString())
        if not text:
            self._update_status()
            return
        self.status_label.setText("　·　" + text)

    def _busy_feedback(self, path: str | None):
        """大檔載入期間的等待游標與提示，用 with 包住會凍結的那段。

        回傳 context manager。檔案夠小就什麼都不做——小檔渲染在百毫秒內，
        閃一下游標反而是雜訊。
        """
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield

        @contextmanager
        def _busy():
            name = os.path.basename(path) if path else ""
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            previous = self.status_label.text()
            was_busy = self._status_is_busy
            self._set_status(t("status.separator") + t("status.loading", name=name),
                             busy=True)
            # 這一段會同步凍結，訊息必須在凍結前就畫出來；repaint() 直接重繪，
            # 不像 processEvents 會重入事件迴圈（重入會在載入途中處理別的
            # 訊號，本專案已經為此付過代價，見 single_instance 的註解）。
            self.status_label.repaint()
            try:
                yield
            finally:
                # 一定要用 finally：例外路徑若沒還原，等待游標會永遠卡住。
                # FramelessResizer 也在用覆蓋游標，這裡必須成對還原不能清空堆疊。
                QApplication.restoreOverrideCursor()
                # 只有「訊息還是我貼的那一則」才還原——中途被別人蓋掉就別動它。
                # 判斷用旗標而不是比對文案（見 _set_status）。
                if self._status_is_busy:
                    self._set_status(previous, busy=was_busy)

        try:
            large = path and os.path.getsize(path) >= config.BUSY_FEEDBACK_BYTES
        except OSError:
            large = False
        return _busy() if large else _noop()

    def _set_status(self, text: str, *, busy: bool = False) -> None:
        """狀態列訊息的唯一入口。

        busy 旗標取代原本的「text().startswith(\"　·　正在載入\")」——那是拿
        翻譯過的文案當程式邏輯用，切到英文之後永遠不成立，忙碌訊息會永久
        卡在狀態列。旗標和語言無關，怎麼翻都不會壞。
        """
        self._status_is_busy = busy
        self.status_label.setText(text)

    def _update_status(self) -> None:
        if self._tab.meta is None:
            return
        meta = self._tab.meta
        # 千分位交給 format：中英文都用逗號，5,272 行遠比 5272 行好讀。
        # 修改時間刻意維持 ISO：無歧義、可排序、寬度固定，不隨語言變。
        parts = [
            meta.encoding,
            meta.size_text,
            t("status.lines", count=f"{meta.line_count:,}"),
            t("status.chars", count=f"{meta.char_count:,}"),
            meta.modified.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        separator = t("status.separator")
        self.status_path_label.setText(meta.path)
        self._set_status(separator + separator.join(parts))

    def _refresh_status_text(self) -> None:
        """依目前分頁狀態重畫狀態列（開檔成功／失敗／歡迎頁三種）。

        語言切換之後要重跑一次：狀態列的內容是算出來的，不是掛在某個
        元件上的靜態文字，登記表那一套抓不到它。
        """
        tab = self._tab
        if tab.error is not None and tab.path:
            self.status_path_label.setText("")
            self._set_status(t("status.readFailed", path=tab.path))
        elif tab.meta is not None:
            self._update_status()
        else:
            self.status_path_label.setText("")
            self._set_status(t("status.noFile"))

    def _on_status_path_clicked(self) -> None:
        """點狀態列的路徑：在檔案總管中開啟所在資料夾並選取該檔。"""
        path = self._tab.path
        if not path:
            return
        if not win32.reveal_in_explorer(path):
            # 後備：Shell API 失敗（例如檔案剛被移走）就至少把資料夾打開
            folder = os.path.dirname(path)
            if os.path.isdir(folder):
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def toggle_status_bar(self) -> None:
        self.set_status_bar_visible(not self._status_visible)

    # -- 檔案監看 ------------------------------------------------------------
    @staticmethod
    def _stamp_of(path: str) -> tuple[float, int] | None:
        try:
            info = os.stat(path)
            return (info.st_mtime, info.st_size)
        except OSError:
            return None

    def _unwatch_all(self) -> None:
        for watched in self._watcher.files():
            self._watcher.removePath(watched)
        for watched in self._watcher.directories():
            self._watcher.removePath(watched)

    def _watch_files(self) -> None:
        """監看所有分頁的檔案與其所在目錄。

        背景分頁也要監看，否則切過去時看到的是舊內容。

        許多編輯器採「寫暫存檔再改名覆蓋」的原子存檔，原檔會短暫消失導致
        watcher 掉路徑，因此目錄事件是必要的補償來源。
        """
        self._unwatch_all()
        if not self._auto_reload:
            return
        for tab in self._tabs:
            if not tab.path:
                continue
            if os.path.isfile(tab.path):
                self._watcher.addPath(tab.path)
            folder = os.path.dirname(tab.path)
            if folder and os.path.isdir(folder) and folder not in self._watcher.directories():
                self._watcher.addPath(folder)

    def _on_watch_event(self, _changed: str) -> None:
        self._reload_timer.start(config.WATCH_DEBOUNCE_MS)

    def _on_reload_timeout(self) -> None:
        """檢查每個分頁的檔案有沒有真的變動。

        作用中的分頁立刻重繪；背景分頁只標記成待重讀，切過去時才處理，
        免得背景改了十個檔案就要當場轉換十次 Markdown。
        """
        for tab in self._tabs:
            if not tab.path:
                continue
            # 原子存檔會讓 watcher 失去這個路徑，重新掛回去
            if os.path.isfile(tab.path) and tab.path not in self._watcher.files():
                self._watcher.addPath(tab.path)

            stamp = tab.stamp()
            if stamp is None or stamp == tab.file_stamp:
                continue  # 目錄裡其他檔案變動，與這個分頁無關

            if tab is not self._tab:
                tab.loaded = False       # 切過去時再讀
                tab.file_stamp = stamp
                continue

            ratio = tab.scroll_ratio()
            try:
                tab.text, tab.meta = document.read_text_file(tab.path)
            except document.DocumentError:
                # 檔案暫時無法讀取（仍在寫入中），稍後再試一次
                self._reload_timer.start(config.WATCH_DEBOUNCE_MS * 2)
                return
            tab.error = None
            tab.file_stamp = stamp
            self._render(preserve_scroll=False)
            self._update_status()
            QTimer.singleShot(0, lambda r=ratio: self._apply_scroll_ratio(r))

    # -- 連結 ----------------------------------------------------------------
    def _set_search_context(self, path: str) -> None:
        """設定相對路徑資源（圖片等）的解析基準。"""
        folder = os.path.dirname(path)
        if folder:
            self.browser.setSearchPaths([folder])
            self.browser.document().setBaseUrl(
                QUrl.fromLocalFile(folder + os.sep)
            )

    def _on_anchor_clicked(self, url: QUrl) -> None:
        """所有外部連結一律交給系統預設瀏覽器，閱讀區內絕不載入網頁。"""
        scheme = url.scheme().lower()
        if scheme in ("http", "https", "ftp", "ftps", "mailto"):
            self._open_external(url)
            return

        # 純 #錨點：在本文件內捲動
        if not url.path() and url.fragment():
            self._scroll_to_anchor(url.fragment())
            return

        if url.isRelative() and self._tab.path:
            base = QUrl.fromLocalFile(os.path.dirname(self._tab.path) + os.sep)
            url = base.resolved(url)

        if url.isLocalFile():
            local_path = url.toLocalFile()
            fragment = url.fragment()
            suffix = os.path.splitext(local_path)[1].lower()
            if suffix in config.SUPPORTED_SUFFIXES and os.path.isfile(local_path):
                # 文件裡的本機連結一律開新分頁，原本那篇留在原處
                self.open_path(local_path, anchor=fragment, new_tab=True)
                return
            if os.path.exists(local_path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))
                return
            self._set_status(t("status.linkNotFound", path=local_path))
            return

        self._open_external(url)

    def _open_external(self, url: QUrl) -> None:
        """交給系統預設瀏覽器開啟；設定為「連結詢問」時先確認。"""
        if self._confirm_links:
            target = url.toString()
            display = target if len(target) <= 90 else target[:87] + "..."
            answer = QMessageBox.question(
                self,
                t("dialog.externalLink.title"),
                t("dialog.externalLink.body", target=display),
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Open,
            )
            if answer != QMessageBox.StandardButton.Open:
                self._set_status(t("status.linkCancelled", target=target))
                return
        QDesktopServices.openUrl(url)

    # -- 視窗控制 ------------------------------------------------------------
    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def set_always_on_top(self, enabled: bool) -> None:
        self._always_on_top = bool(enabled)
        self._settings.setValue(config.KEY_ALWAYS_ON_TOP, self._always_on_top)
        self.title_bar.set_pinned(self._always_on_top)
        self._apply_always_on_top()
        self._sync_settings_panel()

    def _apply_always_on_top(self) -> None:
        """套用置頂狀態；原生作法失敗時退回 Qt 旗標。

        優先用原生的 SetWindowPos——它不會重建視窗，沒有閃爍、也不會掉失最大化
        狀態。但萬一失敗（平台不符、權限問題等），就改用 Qt 的旗標並補回原本的
        顯示狀態，免得出現「按鈕有反應、視窗卻沒有置頂」這種只有 UI 說謊的情形。
        """
        if self._applying_topmost:
            return
        if win32.set_topmost(int(self.winId()), self._always_on_top):
            return

        self._applying_topmost = True
        try:
            was_maximized = self.isMaximized()
            geometry = self.saveGeometry()
            self.setWindowFlag(
                Qt.WindowType.WindowStaysOnTopHint, self._always_on_top
            )
            # 更動旗標會讓 Windows 重建原生視窗並把它隱藏，必須重新顯示
            if was_maximized:
                self.showMaximized()
            else:
                self.restoreGeometry(geometry)
                self.show()
        finally:
            self._applying_topmost = False

    def toggle_always_on_top(self) -> None:
        self.set_always_on_top(not self._always_on_top)

    def show_find(self) -> None:
        if self.settings_panel.isVisible():
            self.settings_panel.deactivate()
        # 搜尋要掃整份文件，只有首屏的話比對數會是錯的
        self.flush_pending_chunks()
        self.find_bar.activate()

    def _on_escape(self) -> None:
        if self.settings_panel.isVisible():
            self.settings_panel.deactivate()
        elif self.find_bar.isVisible():
            self.find_bar.deactivate()
        else:
            self.close_tab()

    # -- 字級 ----------------------------------------------------------------
    def _set_font_point_size(self, size: int) -> None:
        size = max(config.MIN_FONT_POINT_SIZE, min(config.MAX_FONT_POINT_SIZE, size))
        if size == self._font_point_size:
            return
        self._font_point_size = size
        self._settings.setValue(config.KEY_FONT_SIZE, size)
        self._mark_all_dirty()
        self._render()
        self._sync_settings_panel()

    def zoom_in(self) -> None:
        self._set_font_point_size(self._font_point_size + 1)

    def zoom_out(self) -> None:
        self._set_font_point_size(self._font_point_size - 1)

    def zoom_reset(self) -> None:
        self._set_font_point_size(config.BASE_FONT_POINT_SIZE)

    # -- 拖放 ----------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._first_supported_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._first_supported_path(event)
        if path is not None:
            event.acceptProposedAction()
            self.open_path(path, new_tab=True)

    @staticmethod
    def _first_supported_path(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if os.path.splitext(path)[1].lower() in config.SUPPORTED_SUFFIXES:
                return path
        return None

    # -- 視窗狀態 ------------------------------------------------------------
    def _restore_window_state(self) -> None:
        geometry = self._settings.value(config.KEY_GEOMETRY)
        restored = False
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            restored = self.restoreGeometry(geometry)
        if not restored:
            self.resize(*config.DEFAULT_WINDOW_SIZE)
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.move(
                    available.center().x() - self.width() // 2,
                    available.center().y() - self.height() // 2,
                )
        if self._settings.value(config.KEY_MAXIMIZED, False, type=bool):
            # 【不要用 showMaximized()】
            # 它會「立刻把視窗顯示出來」，而這個函式是在 __init__ 中段被呼叫的，
            # 那時第一個分頁還沒建立。顯示會同步送出一個真正的 QResizeEvent，
            # resizeEvent 便去存取 self.browser -> self._tabs[0] -> IndexError。
            # PyQt6 對虛擬函式裡的未攔截例外是致命的，行程直接以 0xC0000409 中止。
            # 而且 closeEvent 會把 isMaximized() 存回設定，所以只要使用者曾經在
            # 最大化狀態關閉程式，之後每次啟動都會中止——完全打不開。
            # setWindowState 只設定狀態不顯示視窗，等 main.py 呼叫 show() 才生效。
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # winId 必須在原生視窗建立後才有效
        if self._always_on_top and not win32.set_topmost(int(self.winId()), True):
            # 後備方案會呼叫 show()，不能在 showEvent 裡直接跑
            QTimer.singleShot(0, self._apply_always_on_top)
        self._resizer.refresh_targets()
        # 所有捲軸都要讓開，包含設定面板裡的
        self._resizer.set_drag_controls(self.findChildren(QScrollBar))
        self._position_settings_panel()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())
        elif event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow() and self._manager is not None:
                # 單一實例轉交進來的檔案，開在使用者最後碰過的視窗
                self._manager.note_activated(self)
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self._tabs:
            # 分頁還沒建立就收到 resize（還原視窗狀態時可能發生）。這時沒有東西
            # 需要排版，_open_initial_tabs 之後會自己渲染一次。
            return
        # Qt 會保留圖片的原始高度、卻把圖畫成縮小後的尺寸，導致下方留下空白，
        # 因此圖片是在載入時就等比縮到可視寬度。視窗寬度改變後必須重繪，
        # 讓縮放依新寬度重新計算（setHtml 會清掉舊的資源快取）。
        # 限制內文寬度時，左右邊距是依可視寬度算出來的，必須立即重算
        self._apply_content_width()
        self._position_settings_panel()
        if self._tab.has_scalable_images:
            self._resize_timer.start(180)

    def _on_resize_settled(self) -> None:
        width = self.browser.viewport().width()
        if abs(width - self._tab.last_render_width) < 24:
            return
        self._render()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 拖曳中關窗（中鍵按在被拖的分頁上、Ctrl+W）不會經過放開事件，
        # 要在這裡收掉手勢，否則全域拖曳游標與置頂的幽靈視窗會殘留
        # （詳見 tab_bar.cancel_active_drag 的說明）
        self.tab_bar.cancel_active_drag()
        # 【這裡刻意不回寫各項設定】
        # 每個設定在被改動的當下就由它自己的 setter 寫進 QSettings 了，關窗再
        # 寫一次純屬多餘——而且有害：多視窗時，先開的那個視窗握著的是它「開啟
        # 當下」的值，別的視窗之後改過的設定會在它關閉時被舊值蓋回去。實際症狀
        # 是「在 B 視窗調大字級，關掉 A 視窗，下次啟動又變回原樣」。
        # 只有幾何、最大化與工作階段是「關閉當下才知道」的，留在這裡。
        self._settings.setValue(config.KEY_MAXIMIZED, self.isMaximized())
        if not self.isMaximized():
            self._settings.setValue(config.KEY_GEOMETRY, self.saveGeometry())
        self._save_session()
        self._settings.sync()

        # 【關閉時一定要放掉分頁的 Python 參考】
        # 每個 DocumentTab 都握著一個 MarkdownBrowser。視窗一關，Qt 會連同這些
        # 子元件一起銷毀 C++ 物件，但 Python 這邊的 sip 包裝要等直譯器結束才回收
        # ——那時 QApplication 往往已經先消失，回收動作就會踩到已釋放的記憶體。
        # 實測分頁功能加進來之後，關閉時約有四成機率發生存取違規；先停掉還會回頭
        # 存取分頁的計時器與監看器，再清空清單，就不再重現。
        self._reload_timer.stop()
        self._chunk_timer.stop()
        self._resize_timer.stop()
        self._watcher.blockSignals(True)
        self._tabs.clear()

        super().closeEvent(event)
