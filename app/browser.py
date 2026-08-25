"""Markdown 閱讀區。

從 viewer.py 抽出來獨立成模組：每個分頁都要有自己的一份，若留在 viewer 裡，
document_tab 匯入它就會和 viewer 形成循環匯入。
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QUrl, Qt
from PyQt6.QtGui import QImage, QPixmap, QTextDocument
from PyQt6.QtWidgets import QFrame, QTextBrowser, QWidget

from . import config, icons, qt_html, styles


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
