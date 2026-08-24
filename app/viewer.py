"""無邊框主視窗。

包含三個部分：
* FramelessResizer —— 無邊框視窗失去系統縮放，這裡用事件過濾器補回四邊四角
  的縮放，並依 devicePixelRatio 動態調整感應寬度（高 DPI 螢幕才點得到）。
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
    QSettings,
    QTimer,
    QUrl,
    Qt,
)
from PyQt6.QtGui import (
    QDesktopServices,
    QGuiApplication,
    QImage,
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
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import config, document, icons, qt_html, styles, win32
from .find_bar import FindBar
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


class FramelessResizer(QObject):
    """替無邊框視窗補回邊緣縮放。

    子元件（尤其 QTextBrowser 的 viewport）會吃掉滑鼠事件，因此事件過濾器要
    裝在視窗與所有子元件上，並全部開啟 mouse tracking，游標移到邊緣才收得到
    MouseMove。實際縮放交給 Qt 6 的 startSystemResize，行為與原生視窗一致。
    """

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._margin = config.RESIZE_MARGIN_BASE
        self._corner = config.RESIZE_MARGIN_BASE * 2
        self._override_active = False
        window.installEventFilter(self)
        self.refresh_targets()
        self.update_margin()

    # -- 安裝與 DPI ----------------------------------------------------------
    def refresh_targets(self) -> None:
        """對視窗與所有子元件開啟 mouse tracking 並安裝過濾器。"""
        self._window.setMouseTracking(True)
        for child in self._window.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def update_margin(self) -> None:
        """依 devicePixelRatio 調整感應寬度。

        Qt 的滑鼠座標是邏輯像素，固定 6px 在高 DPI 螢幕上物理寬度極窄、
        非常難點中，因此依縮放比例放大，並限制在合理範圍內。
        """
        ratio = float(self._window.devicePixelRatioF() or 1.0)
        margin = int(round(config.RESIZE_MARGIN_BASE * ratio))
        self._margin = max(config.RESIZE_MARGIN_MIN, min(config.RESIZE_MARGIN_MAX, margin))
        self._corner = self._margin * 2

    # -- 邊緣判定 ------------------------------------------------------------
    def _edges_at(self, global_x: int, global_y: int) -> int:
        window = self._window
        if window.isMaximized() or window.isFullScreen():
            return 0

        rect = window.frameGeometry()
        margin, corner = self._margin, self._corner
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


class MarkdownBrowser(QTextBrowser):
    """負責自訂資源載入的閱讀區。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("contentView")
        self._theme = config.DEFAULT_THEME
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(styles.DOCUMENT_MARGIN)

    def set_theme(self, theme: str) -> None:
        self._theme = theme

    def loadResource(self, resource_type: int, url: QUrl):  # noqa: N802
        # 任務清單的核取方塊：Qt 不渲染 <input>，改以 QPainter 即時繪製
        if url.scheme() == qt_html.CHECKBOX_SCHEME:
            colors = styles.palette(self._theme)
            checked = url.toString().endswith("checkbox-on")
            return icons.checkbox_pixmap(checked, colors["text_muted"], colors["accent"])

        resource = super().loadResource(resource_type, url)

        # 過寬的圖片等比縮到可視寬度，避免撐爆版面（Qt 不會自動縮圖）
        if resource_type == QTextDocument.ResourceType.ImageResource.value:
            image = self._as_image(resource)
            if image is not None and not image.isNull():
                # setHtml 有可能在版面定案前就被呼叫，此時 viewport 寬度還不可靠
                available = self.viewport().width()
                if available < 100:
                    available = self.width()
                limit = available - 2 * styles.DOCUMENT_MARGIN - 8
                if limit > 80 and image.width() > limit:
                    return image.scaledToWidth(
                        limit, Qt.TransformationMode.SmoothTransformation
                    )
                return image
        return resource

    @staticmethod
    def _as_image(resource) -> QImage | None:
        if isinstance(resource, QImage):
            return resource
        if isinstance(resource, QPixmap):
            return resource.toImage()
        if isinstance(resource, (QByteArray, bytes, bytearray)):
            image = QImage()
            if image.loadFromData(QByteArray(bytes(resource))):
                return image
        return None


class MarkdownViewer(QWidget):
    """Markdown 閱讀器主視窗。"""

    def __init__(self, path: str | None = None) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setWindowIcon(icons.app_icon())
        self.setMinimumSize(*config.MIN_WINDOW_SIZE)
        self.setAcceptDrops(True)

        self._settings = QSettings(config.ORG_NAME, config.APP_NAME)
        self._theme = str(self._settings.value(config.KEY_THEME, config.DEFAULT_THEME))
        if self._theme not in styles.PALETTES:
            self._theme = config.DEFAULT_THEME
        self._font_point_size = int(
            self._settings.value(config.KEY_FONT_SIZE, config.BASE_FONT_POINT_SIZE)
        )
        self._always_on_top = self._settings.value(
            config.KEY_ALWAYS_ON_TOP, False, type=bool
        )
        self._status_visible = self._settings.value(
            config.KEY_STATUS_VISIBLE, True, type=bool
        )

        self._path: str | None = None
        self._text = ""
        self._meta: document.DocumentMeta | None = None
        self._error: document.DocumentError | None = None
        self._history: list[str] = []
        self._file_stamp: tuple[float, int] | None = None
        self._pending_anchor = ""
        self._has_scalable_images = False
        self._last_render_width = 0

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

        self._resizer = FramelessResizer(self)

        self.apply_theme(self._theme)
        self._restore_window_state()
        self.title_bar.set_pinned(self._always_on_top)
        self.status_bar.setVisible(self._status_visible)

        if path:
            self.open_path(path, push_history=False)
        else:
            self._show_welcome()

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
        self.browser = MarkdownBrowser(self.root_frame)
        self.find_bar = FindBar(self.root_frame)
        self.find_bar.attach(self.browser)

        self.status_bar = QFrame(self.root_frame)
        self.status_bar.setObjectName("statusBar")
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.setContentsMargins(12, 4, 12, 4)
        status_layout.setSpacing(0)
        self.status_label = QLabel("", self.status_bar)
        self.status_label.setObjectName("statusLabel")
        status_layout.addWidget(self.status_label)

        inner.addWidget(self.title_bar)
        inner.addWidget(self.browser, 1)
        inner.addWidget(self.find_bar)
        inner.addWidget(self.status_bar)

        self.title_bar.openRequested.connect(self.open_dialog)
        self.title_bar.backRequested.connect(self.go_back)
        self.title_bar.findRequested.connect(self.show_find)
        self.title_bar.themeToggleRequested.connect(self.toggle_theme)
        self.title_bar.pinToggled.connect(self.set_always_on_top)
        self.title_bar.minimizeRequested.connect(self.showMinimized)
        self.title_bar.maximizeToggleRequested.connect(self.toggle_maximized)
        self.title_bar.closeRequested.connect(self.close)
        self.browser.anchorClicked.connect(self._on_anchor_clicked)

    def _create_shortcuts(self) -> None:
        bindings = (
            ("Ctrl+O", self.open_dialog),
            ("Ctrl+R", self.reload),
            ("F5", self.reload),
            ("Ctrl+F", self.show_find),
            ("Ctrl+D", self.toggle_theme),
            ("Ctrl+P", self.toggle_always_on_top),
            ("Ctrl+=", self.zoom_in),
            ("Ctrl++", self.zoom_in),
            ("Ctrl+-", self.zoom_out),
            ("Ctrl+0", self.zoom_reset),
            ("Ctrl+/", self.toggle_status_bar),
            ("Alt+Left", self.go_back),
            ("F11", self.toggle_maximized),
            ("Ctrl+W", self.close),
            ("Esc", self._on_escape),
        )
        for sequence, slot in bindings:
            QShortcut(QKeySequence(sequence), self, activated=slot)

    # -- 主題 ----------------------------------------------------------------
    def apply_theme(self, theme: str) -> None:
        """套用主題：視窗 QSS、文件 CSS、圖示著色與程式碼高亮一次同步。

        這是全專案唯一的 setStyleSheet 呼叫。
        """
        self._theme = theme
        self.setStyleSheet(styles.build_qss(theme))
        self.browser.set_theme(theme)
        self.title_bar.apply_theme(theme)
        self.find_bar.apply_theme(theme)
        self._settings.setValue(config.KEY_THEME, theme)
        self._render()

    def toggle_theme(self) -> None:
        self.apply_theme(styles.other_theme(self._theme))

    # -- 開檔與渲染 ----------------------------------------------------------
    def open_dialog(self) -> None:
        start_dir = os.path.dirname(self._path) if self._path else ""
        path, _selected = QFileDialog.getOpenFileName(
            self, "開啟 Markdown 檔案", start_dir, config.OPEN_DIALOG_FILTER
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str, push_history: bool = True, anchor: str = "") -> None:
        """載入指定檔案；讀取失敗時在閱讀區顯示錯誤頁。"""
        path = os.path.abspath(path)
        if push_history and self._path and os.path.abspath(self._path) != path:
            self._history.append(self._path)
            self.title_bar.set_back_enabled(True)

        self._pending_anchor = anchor
        self._path = path
        self._set_search_context(path)

        try:
            self._text, self._meta = document.read_text_file(path)
        except document.DocumentError as error:
            self._text = ""
            self._meta = None
            self._error = error
            self._file_stamp = None
            self._render(preserve_scroll=False)
            self._update_titles(os.path.basename(path) or path)
            self.status_label.setText(f"無法讀取：{path}")
            self._watch_file(path)
            return

        self._error = None
        self._file_stamp = self._stamp_of(path)
        self._render()
        self._update_titles(self._meta.display_name)
        self._update_status()
        self._watch_file(path)
        if anchor:
            QTimer.singleShot(0, lambda: self.browser.scrollToAnchor(anchor))

    def _show_welcome(self) -> None:
        self._path = None
        self._text = ""
        self._meta = None
        self._error = None
        self._render(preserve_scroll=False)
        self._update_titles(config.APP_DISPLAY_NAME)
        self.status_label.setText("尚未開啟檔案 — 按 Ctrl+O 或直接拖放 .md 檔到視窗")

    def _render(self, preserve_scroll: bool = True) -> None:
        """重新產生 HTML 並套用，預設保留閱讀位置。"""
        ratio = self._scroll_ratio() if preserve_scroll else 0.0
        font = self.browser.font()
        font.setPointSize(self._font_point_size)
        self.browser.document().setDefaultFont(font)
        # 樣式表必須在 setHtml 之前套用，否則不會生效；字級改變時也要重算，
        # 因為 h6 的字級只能用絕對單位指定（詳見 styles.py 的說明）。
        self.browser.document().setDefaultStyleSheet(
            styles.build_doc_css(self._theme, self._font_point_size)
        )

        if self._error is not None:
            html = document.render_error(self._error, self._path or "")
        elif self._meta is not None:
            html = document.render_document(self._text, self._meta, self._theme)
        else:
            html = document.render_welcome()

        # 任務清單的核取方塊也是 <img>，但尺寸固定，不需要隨視窗重繪
        self._has_scalable_images = bool(
            re.search(r'<img[^>]+src="(?!mdres:)', html)
        )
        self._last_render_width = self.browser.viewport().width()

        self.browser.setHtml(html)
        if preserve_scroll and ratio > 0:
            QTimer.singleShot(0, lambda: self._apply_scroll_ratio(ratio))

    def reload(self) -> None:
        if not self._path:
            return
        self.open_path(self._path, push_history=False)

    def go_back(self) -> None:
        if not self._history:
            return
        previous = self._history.pop()
        self.title_bar.set_back_enabled(bool(self._history))
        self.open_path(previous, push_history=False)

    # -- 捲動位置 ------------------------------------------------------------
    def _scroll_ratio(self) -> float:
        bar = self.browser.verticalScrollBar()
        return bar.value() / bar.maximum() if bar.maximum() else 0.0

    def _apply_scroll_ratio(self, ratio: float) -> None:
        bar = self.browser.verticalScrollBar()
        bar.setValue(int(round(ratio * bar.maximum())))

    # -- 標題與狀態列 --------------------------------------------------------
    def _update_titles(self, name: str) -> None:
        self.title_bar.set_title(name)
        # 歡迎頁的名稱就是程式名稱，避免標題出現重複的「閱讀器 — 閱讀器」
        if name == config.APP_DISPLAY_NAME:
            self.setWindowTitle(config.APP_DISPLAY_NAME)
        else:
            self.setWindowTitle(f"{name} — {config.APP_DISPLAY_NAME}")

    def _update_status(self) -> None:
        if self._meta is None:
            return
        meta = self._meta
        parts = [
            meta.path,
            meta.encoding,
            meta.size_text,
            f"{meta.line_count} 行",
            f"{meta.char_count} 字",
            meta.modified.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        self.status_label.setText("　·　".join(parts))

    def toggle_status_bar(self) -> None:
        self._status_visible = not self._status_visible
        self.status_bar.setVisible(self._status_visible)
        self._settings.setValue(config.KEY_STATUS_VISIBLE, self._status_visible)

    # -- 檔案監看 ------------------------------------------------------------
    @staticmethod
    def _stamp_of(path: str) -> tuple[float, int] | None:
        try:
            info = os.stat(path)
            return (info.st_mtime, info.st_size)
        except OSError:
            return None

    def _watch_file(self, path: str) -> None:
        """同時監看檔案與其所在目錄。

        許多編輯器採「寫暫存檔再改名覆蓋」的原子存檔，原檔會短暫消失導致
        watcher 掉路徑，因此目錄事件是必要的補償來源。
        """
        for watched in self._watcher.files():
            self._watcher.removePath(watched)
        for watched in self._watcher.directories():
            self._watcher.removePath(watched)
        if os.path.isfile(path):
            self._watcher.addPath(path)
        folder = os.path.dirname(path)
        if os.path.isdir(folder):
            self._watcher.addPath(folder)

    def _on_watch_event(self, _changed: str) -> None:
        self._reload_timer.start(config.WATCH_DEBOUNCE_MS)

    def _on_reload_timeout(self) -> None:
        if not self._path:
            return
        # 原子存檔會讓 watcher 失去這個路徑，重新掛回去
        if os.path.isfile(self._path) and self._path not in self._watcher.files():
            self._watcher.addPath(self._path)

        stamp = self._stamp_of(self._path)
        if stamp is None:
            return
        if stamp == self._file_stamp:
            return  # 目錄裡其他檔案變動，與本文件無關

        ratio = self._scroll_ratio()
        try:
            self._text, self._meta = document.read_text_file(self._path)
        except document.DocumentError:
            # 檔案暫時無法讀取（仍在寫入中），稍後再試一次
            self._reload_timer.start(config.WATCH_DEBOUNCE_MS * 2)
            return

        self._error = None
        self._file_stamp = stamp
        self._render(preserve_scroll=False)
        self._update_status()
        QTimer.singleShot(0, lambda: self._apply_scroll_ratio(ratio))

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
            QDesktopServices.openUrl(url)
            return

        # 純 #錨點：在本文件內捲動
        if not url.path() and url.fragment():
            self.browser.scrollToAnchor(url.fragment())
            return

        if url.isRelative() and self._path:
            base = QUrl.fromLocalFile(os.path.dirname(self._path) + os.sep)
            url = base.resolved(url)

        if url.isLocalFile():
            local_path = url.toLocalFile()
            fragment = url.fragment()
            suffix = os.path.splitext(local_path)[1].lower()
            if suffix in config.SUPPORTED_SUFFIXES and os.path.isfile(local_path):
                self.open_path(local_path, anchor=fragment)
                return
            if os.path.exists(local_path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))
                return
            self.status_label.setText(f"找不到連結目標：{local_path}")
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
        win32.set_topmost(int(self.winId()), self._always_on_top)

    def toggle_always_on_top(self) -> None:
        self.set_always_on_top(not self._always_on_top)

    def show_find(self) -> None:
        self.find_bar.activate()

    def _on_escape(self) -> None:
        if self.find_bar.isVisible():
            self.find_bar.deactivate()
        else:
            self.close()

    # -- 字級 ----------------------------------------------------------------
    def _set_font_point_size(self, size: int) -> None:
        size = max(config.MIN_FONT_POINT_SIZE, min(config.MAX_FONT_POINT_SIZE, size))
        if size == self._font_point_size:
            return
        self._font_point_size = size
        self._settings.setValue(config.KEY_FONT_SIZE, size)
        self._render()

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
            self.open_path(path)

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
            self.showMaximized()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # winId 必須在原生視窗建立後才有效
        if self._always_on_top:
            win32.set_topmost(int(self.winId()), True)
        self._resizer.refresh_targets()
        self._resizer.update_margin()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Qt 會保留圖片的原始高度、卻把圖畫成縮小後的尺寸，導致下方留下空白，
        # 因此圖片是在載入時就等比縮到可視寬度。視窗寬度改變後必須重繪，
        # 讓縮放依新寬度重新計算（setHtml 會清掉舊的資源快取）。
        if self._has_scalable_images:
            self._resize_timer.start(180)

    def _on_resize_settled(self) -> None:
        width = self.browser.viewport().width()
        if abs(width - self._last_render_width) < 24:
            return
        self._render()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        # 拖到不同 DPI 的螢幕時重新計算邊緣感應寬度
        self._resizer.update_margin()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._settings.setValue(config.KEY_MAXIMIZED, self.isMaximized())
        if not self.isMaximized():
            self._settings.setValue(config.KEY_GEOMETRY, self.saveGeometry())
        self._settings.setValue(config.KEY_THEME, self._theme)
        self._settings.setValue(config.KEY_FONT_SIZE, self._font_point_size)
        self._settings.setValue(config.KEY_ALWAYS_ON_TOP, self._always_on_top)
        self._settings.sync()
        super().closeEvent(event)
