"""Markdown 閱讀區。

從 viewer.py 抽出來獨立成模組：每個分頁都要有自己的一份，若留在 viewer 裡，
document_tab 匯入它就會和 viewer 形成循環匯入。
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QUrl, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTextDocument,
)
from PyQt6.QtWidgets import QFrame, QTextBrowser, QWidget

from . import config, icons, mermaid, qt_html, styles
from .language import t


class MarkdownBrowser(QTextBrowser):
    """負責自訂資源載入的閱讀區。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("contentView")
        self._theme = config.DEFAULT_THEME
        # 解析後的圖片 url -> (alt 文字, 原始 src)。破圖佔位要用（見 set_image_alts）
        self._image_alts: dict[str, tuple[str, str]] = {}
        # 內文欄的實際寬度（像素），由 viewer 在算完內文寬度後餵入。Mermaid 圖表
        # 要縮到欄寬內，而欄寬取決於「內文寬度」設定，不是 viewport 寬——
        # 閱讀區自己算不出來（見 viewer._apply_content_width）。0 代表還不知道。
        self._column_width = 0
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(styles.DOCUMENT_MARGIN)

    def set_theme(self, theme: str) -> None:
        self._theme = theme

    def set_content_column_width(self, width: float) -> None:
        """記下內文欄寬。一定要在 setHtml 之前設好——圖片資源是排版時載入的。"""
        self._column_width = max(0, int(width))

    def _column_width_or_fallback(self) -> int:
        """內文欄寬；viewer 還沒餵入時退回 viewport 減去邊距（和圖片縮放同一個算法）。"""
        if self._column_width > 0:
            return self._column_width
        available = self.viewport().width()
        if available < 100:
            available = self.width()
        return max(0, available - 2 * styles.DOCUMENT_MARGIN - 8)

    def set_image_alts(self, alts: dict[str, str]) -> None:
        """記下這份文件每張圖的 alt 文字，供破圖佔位顯示。

        鍵要先用文件的 baseUrl 解析過：loadResource 收到的是 Qt 解析後的絕對
        url，不是 HTML 裡的原始 src（實測 docs/a.png 會變成
        file:///.../docs/a.png），拿原始 src 當鍵一定查不到。

        一定要在 setHtml 之前呼叫，而且 baseUrl 要先設好。
        """
        base = self.document().baseUrl()
        resolved: dict[str, tuple[str, str]] = {}
        for src, alt in alts.items():
            key = base.resolved(QUrl(src)).toString() if not base.isEmpty() else src
            resolved[key] = (alt, src)
        self._image_alts = resolved

    def loadResource(self, resource_type: int, url: QUrl):  # noqa: N802
        # 任務清單的核取方塊：Qt 不渲染 <input>，改以 QPainter 即時繪製
        if url.scheme() == qt_html.CHECKBOX_SCHEME:
            colors = styles.palette(self._theme)
            checked = url.toString().endswith("checkbox-on")
            return icons.checkbox_pixmap(checked, colors["text_muted"], colors["accent"])

        # Mermaid 圖表：文件裡是 <img src="mermaid:鍵">，這裡才真的畫。
        # 畫要知道主題、字級、欄寬與 DPR，都是這一刻才確定的東西。
        if url.scheme() == mermaid.SCHEME:
            try:
                image = mermaid.render(
                    url.path(),
                    self._theme,
                    self._column_width_or_fallback(),
                    self.document().defaultFont(),
                    self.devicePixelRatioF() or 1.0,
                )
            except Exception:
                # 這裡在 Qt 排版的呼叫鏈上，例外漏出去就是整個程式的當機對話框。
                # 解析期的上限應該擋掉一切病態輸入，這是最後一道保險：
                # 畫不出來就退回破圖佔位，一張圖壞不該拖垮整份文件。
                image = None
            # 登錄表被擠掉才會查不到；退回破圖佔位，alt 會寫著 Mermaid
            return image if image is not None else self._broken_image(url)

        resource = super().loadResource(resource_type, url)

        # 過寬的圖片等比縮到可視寬度，避免撐爆版面（Qt 不會自動縮圖）
        if resource_type == QTextDocument.ResourceType.ImageResource.value:
            image = self._as_image(resource)
            if image is None or image.isNull():
                # 載不到就自己畫一張。回傳 null 的話 Qt 會用它內建的
                # :/qt-project.org/styles/commonstyle/images/file-16.png
                # ——一個 16x16 的灰色小檔案圖，不顯示 alt、也不說是哪個路徑壞了。
                return self._broken_image(url)
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

    def _broken_image(self, url: QUrl) -> QImage:
        """圖片載不到時的替代圖：圖示 + alt 文字 + 作者寫的那個路徑。

        路徑顯示原始 src 而不是解析後的絕對 url——作者要修的是自己寫的那一行。
        取不到（例如文件裡沒有對應的 alt 記錄）才退回 url。
        """
        colors = styles.palette(self._theme)
        muted = QColor(colors["text_muted"])
        alt, src = self._image_alts.get(url.toString(), ("", ""))
        shown = src or url.toLocalFile() or url.toString()

        icon_px, pad, gap = 24, 14, 10
        # 【用像素字級而不是點數】點數要靠「畫布的邏輯 DPI」換算成像素，而
        # QFontMetrics 量的是螢幕的 DPI、QPainter 畫在 QImage 上用的是圖片的
        # DPI——兩者不一致時，量出來的尺寸和實際畫出來的大小會對不上，框就會
        # 把文字裁掉。像素字級沒有這個換算，量測與繪製必定一致。
        title_font = QFont()
        title_font.setPixelSize(14)
        path_font = QFont("Consolas")
        path_font.setPixelSize(11)
        fm_title, fm_path = QFontMetrics(title_font), QFontMetrics(path_font)

        available = self.viewport().width()
        if available < 100:
            available = self.width()
        limit = max(200, available - 2 * styles.DOCUMENT_MARGIN - 8)
        text_limit = limit - pad * 2 - icon_px - gap
        title = fm_title.elidedText(
            alt or t("image.notFound"), Qt.TextElideMode.ElideRight, text_limit
        )
        # 路徑從中間省略：開頭的資料夾與結尾的檔名都比中間有用
        path_line = fm_path.elidedText(
            shown, Qt.TextElideMode.ElideMiddle, text_limit
        )

        width = min(limit, pad * 2 + icon_px + gap + max(
            fm_title.horizontalAdvance(title), fm_path.horizontalAdvance(path_line)
        ))
        height = max(pad * 2 + icon_px,
                     pad * 2 + fm_title.height() + 4 + fm_path.height())

        # 【一定要照裝置像素比繪製】不然在高 DPI 螢幕上，這張圖會以 1x 畫好再
        # 被放大到 1.5x／2x，框線與文字全都糊掉——旁邊的內文是 Qt 直接以螢幕
        # 解析度畫的，一比就看得出來。作法與 icons.pixmap 相同：以實體像素開圖、
        # 把畫筆座標系縮放回邏輯單位（下面的繪製程式碼因此不必改）、最後標上
        # devicePixelRatio 讓 Qt 知道這張圖每單位有幾個像素。
        dpr = self.devicePixelRatioF() or 1.0
        image = QImage(
            max(1, int(round(width * dpr))),
            max(1, int(round(height * dpr))),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(QColor(0, 0, 0, 0))
        # devicePixelRatio 一定要等畫完才標（icons.pixmap 也是這個順序）：
        # 先標的話 QPainter 會自己套一次 DPR 變換，加上下面這行就等於縮放兩次，
        # 內容會被畫成 1.5 倍大而溢出框外。
        painter = QPainter(image)
        painter.scale(dpr, dpr)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        pen = QPen(muted)
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRoundedRect(1, 1, int(width) - 3, int(height) - 3, 6, 6)
        painter.drawPixmap(
            pad, (int(height) - icon_px) // 2,
            icons.pixmap("image_broken", colors["text_muted"], icon_px),
        )
        text_x = pad + icon_px + gap
        painter.setPen(QPen(QColor(colors["text"])))
        painter.setFont(title_font)
        painter.drawText(text_x, pad + fm_title.ascent(), title)
        painter.setPen(QPen(muted))
        painter.setFont(path_font)
        painter.drawText(
            text_x, pad + fm_title.height() + 4 + fm_path.ascent(), path_line
        )
        painter.end()
        image.setDevicePixelRatio(dpr)
        return image

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
