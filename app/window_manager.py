"""多視窗管理：瀏覽器式的分頁拆分與合併。

單一「行程」、多個視窗。拆出去的分頁仍活在同一個行程裡，DocumentTab（含已
渲染好的 QTextBrowser）直接搬到另一個視窗，不需要序列化任何狀態。

職責：
  - 建立視窗（一般開啟，或收養一個從別的視窗拖出來的分頁）
  - 記錄視窗清單與「最後作用中」的視窗
  - 單一實例轉交進來的路徑，路由到最後作用中的視窗
  - 拖曳放開時，判斷游標落在哪個視窗的分頁列上（合併目標）

【生命週期】
視窗帶著 WA_DeleteOnClose，關閉即銷毀。這裡只留 Python 參照防止 GC，
並靠 destroyed 訊號把死掉的視窗從清單剔除——清單裡絕不能留已銷毀的
sip 包裝，任何人拿去用就是 RuntimeError（這專案在捲軸快照上吃過同樣的虧）。
最後一個視窗關閉時，Qt 的 quitOnLastWindowClosed 會結束 app.exec()。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QPoint, QRect

from . import config, win32
from .viewer import MarkdownViewer


def _native_point(global_pos: QPoint) -> tuple[int, int] | None:
    """邏輯座標 -> 原生像素座標。

    不能全域乘一個 devicePixelRatio：Qt 的高 DPI 換算是「每個螢幕各自縮放、
    原點守恆」（QHighDpiScaling 以螢幕自己的左上角為原點縮放它的矩形），
    多螢幕不同縮放時，只有把點所在那個螢幕的原點與縮放拿出來算才會對。
    """
    from PyQt6.QtGui import QGuiApplication

    screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    ratio = screen.devicePixelRatio()
    origin = screen.geometry().topLeft()
    return (
        round(origin.x() + (global_pos.x() - origin.x()) * ratio),
        round(origin.y() + (global_pos.y() - origin.y()) * ratio),
    )


def top_level_widget_at(global_pos: QPoint):
    """游標下最上層的本程式視窗；是別的程式的視窗、或問不出來時回 None。

    不直接用 QApplication.topLevelAt：拖曳幽靈維持「按下時抓的那一點在游標
    底下」（見 tab_bar.DragGhost），因此它必然蓋住游標，而 Qt 的版本會把它
    當成命中結果——見 win32.top_level_hwnd_at 的說明。

    最後那道 frameGeometry 檢查是座標換算的保險。換算若在某種多螢幕組合下
    失準，這裡會發現解出來的視窗根本不含這個點而回 None，呼叫端就退回既有的
    幾何掃描——寧可退化成修正前的行為，也不能拿一個錯的視窗當答案。

    整段包在 try 裡：這個函式在 mouseMoveEvent 的呼叫鏈上跑，PyQt6 對虛擬
    函式裡漏出去的例外是直接中止行程（0xC0000409），沒有第二次機會。
    """
    from PyQt6.QtWidgets import QApplication, QWidget

    try:
        if not win32.IS_WINDOWS:
            # 非 Windows 只剩 Qt 的版本可用（幽靈仍可能被當成命中結果）。
            # 這個程式的目標平台是 Windows，不為此再養一套 X11/Cocoa 的路徑。
            return QApplication.topLevelAt(global_pos)
        native = _native_point(global_pos)
        if native is None:
            return None
        handle = win32.top_level_hwnd_at(*native)
        if not handle:
            return None
        widget = QWidget.find(handle)
        if widget is None:
            return None
        window = widget.window()
        return window if window.frameGeometry().contains(global_pos) else None
    except Exception:
        return None


class WindowManager(QObject):
    """行程內所有閱讀器視窗的登記處。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._windows: list[MarkdownViewer] = []
        self._last_active: MarkdownViewer | None = None

    # -- 建立與登記 -----------------------------------------------------------
    def create_window(
        self,
        path: str | None = None,
        adopt=None,
        near: QPoint | None = None,
    ) -> MarkdownViewer:
        """開一個新視窗。

        adopt：從別的視窗拖出來的 DocumentTab，直接收進來（不重新讀檔）。
        near：拖曳放開的位置；新視窗會出現在那附近，符合「拖到哪就落在哪」。
        """
        from PyQt6.QtCore import Qt

        # 拆分／合併建立的視窗不還原上次 session（見 viewer 的說明），
        # 也不套用上次的最大化狀態——拖到哪就落在哪，最大化會蓋掉落點。
        viewer = MarkdownViewer(path, manager=self, restore_session=(adopt is None))
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._register(viewer)

        if adopt is not None:
            # 沒給路徑時建構子會先開一個歡迎分頁佔位；收養真正的分頁後把它關掉，
            # 否則每次拆分都會多出一個「新分頁」。
            placeholder = viewer.tab_count() == 1 and viewer._tabs[0].path is None
            viewer.adopt_tab(adopt)
            if placeholder:
                viewer.close_tab_at(0)

        if near is not None:
            from PyQt6.QtGui import QGuiApplication

            # 上次若是最大化關閉的，建構子會把狀態帶進來；拆出的視窗要跟著
            # 游標落點，不能一出來就整個鋪滿螢幕
            viewer.setWindowState(
                viewer.windowState() & ~Qt.WindowState.WindowMaximized
            )
            viewer.resize(*config.DEFAULT_WINDOW_SIZE)
            # 以「游標所在的那個螢幕」為界夾住位置。夾到 (0,0) 的話，
            # 主螢幕左邊或上面的螢幕會永遠放不了視窗。
            screen = QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            viewer.move(
                max(area.left(), min(near.x() - 120, area.right() - 240)),
                max(area.top(), min(near.y() - config.TAB_HEIGHT, area.bottom() - 120)),
            )
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()
        return viewer

    def _register(self, viewer: MarkdownViewer) -> None:
        self._windows.append(viewer)
        self._last_active = viewer
        viewer.destroyed.connect(lambda _=None, v=viewer: self._forget(v))

    def _forget(self, viewer: MarkdownViewer) -> None:
        # 這裡拿到的 viewer 其 C++ 端已經銷毀，只能做身分比對，不能呼叫方法
        self._windows = [w for w in self._windows if w is not viewer]
        if self._last_active is viewer:
            self._last_active = self._windows[-1] if self._windows else None

    def broadcast_language(self, mode: str, origin=None) -> None:
        """把語言變更推到所有視窗。

        每次都重新向 windows() 要一份清單、不快取視窗參照——沿用
        viewer._update_insert_markers 的安全範式：期間視窗可能被關閉，
        留著參照去用就是碰到已銷毀的 sip 包裝（本專案在捲軸快照上吃過這個虧）。

        傳的是「模式」而不是解析後的語言碼：這樣每個視窗記住的 _language_mode
        才會一致。只同步解析結果的話，其他視窗的模式會留在舊值，設定面板打開
        來會顯示錯的選取項。

        origin 是發起的那個視窗，它自己已經套過了，跳過以免多跑一次渲染。
        """
        for window in self.windows():
            if window is origin:
                continue
            window.adopt_language_mode(mode)

    def note_activated(self, viewer: MarkdownViewer) -> None:
        """視窗被啟用時呼叫（viewer 的 changeEvent 轉進來）。"""
        if viewer in self._windows:
            self._last_active = viewer

    def windows(self) -> list[MarkdownViewer]:
        return list(self._windows)

    # -- 單一實例路由 ---------------------------------------------------------
    def route_external_open(self, path: str) -> None:
        """雙擊 .md 轉交進來的路徑：交給最後作用中的視窗開新分頁。"""
        target = self._last_active if self._last_active in self._windows else None
        if target is None and self._windows:
            target = self._windows[-1]
        if target is None:
            # 所有視窗都關了但行程還沒退出（理論上只有極短暫的窗口）
            self.create_window(path or None)
            return
        target.handle_external_open(path)

    # -- 拖曳合併的命中測試 ---------------------------------------------------
    def drop_target_at(
        self, global_pos: QPoint, exclude: MarkdownViewer
    ) -> tuple[MarkdownViewer, int] | None:
        """游標下若有另一個視窗的分頁區，回傳 (視窗, 插入位置)。

        分頁列只在兩個以上分頁時可見；單分頁視窗以「標題列」當作放置區，
        否則永遠沒辦法把分頁合併進單分頁的視窗。
        由上而下依「最後作用優先」檢查，重疊時取使用者最近碰過的那個。
        """
        candidates = [w for w in self._windows if w is not exclude and w.isVisible()]

        # 【以真實堆疊順序解決重疊】
        # 依登記順序掃描的話，被完全蓋住的視窗可能先命中，分頁就合併進一個
        # 使用者根本看不到的視窗。先問視窗系統游標下最上層的是誰：是候選者
        # 就只考慮它；是被排除的來源視窗就當作沒有目標（放在自己視窗上＝拆分）。
        #
        # 【這裡不能用 QApplication.topLevelAt】拖曳幽靈永遠蓋在游標底下，Qt 會
        # 回傳幽靈；它既不是 exclude 也不在候選裡，上面兩條規則會整段失效而
        # 安靜退回下面的幾何掃描——「合併進看不見的視窗」這個 bug 就原封不動
        # 回來了，而且因為幾何掃描有時剛好猜對，症狀會是時好時壞。
        top = top_level_widget_at(global_pos)
        if top is not None:
            if top is exclude:
                return None
            top_hits = [w for w in candidates if w is top]
            if top_hits:
                candidates = top_hits
        # top 是 None（游標在視窗之外，但可能仍在放寬的容忍帶內）或不屬於本
        # 程式時，退回逐一掃描；此時以最後作用優先當作近似的堆疊順序。
        ordered = sorted(
            candidates, key=lambda w: 0 if w is self._last_active else 1
        )
        for viewer in ordered:
            if viewer.isMinimized():
                continue
            if viewer.tab_bar.isVisible():
                x, y, width, height = viewer.tab_bar.global_drop_rect()
                if QRect(x, y, width, height).contains(global_pos):
                    return viewer, viewer.tab_bar.insert_index_at(global_pos)
            else:
                bar = viewer.title_bar
                top_left = bar.mapToGlobal(bar.rect().topLeft())
                rect = QRect(top_left.x(), top_left.y(), bar.width(), bar.height())
                if rect.contains(global_pos):
                    # 附加在既有分頁之後
                    return viewer, viewer.tab_count()
        return None
