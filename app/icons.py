"""SVG 圖示供應器。

專案規範：UI 上所有圖示一律來自 assets/icons/*.svg 真實檔案，
不使用任何 Unicode 字元或 Emoji 充當圖示。

由於 Qt 的 SVG 渲染器不支援 CSS 的 currentColor，這裡採用的作法是：
每個 SVG 檔的 stroke/fill 都寫成 {color} 佔位符，載入時以主題色置換，
再交給 QSvgRenderer 繪製成 QPixmap，即可讓同一份向量圖隨主題變色。
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtSvg import QSvgRenderer

from . import config, resources

# 快取鍵為 (名稱, 顏色, 尺寸, 裝置像素比)，避免每次重繪都重新解析 SVG
_pixmap_cache: dict[tuple[str, str, int, float], QPixmap] = {}
_source_cache: dict[str, str] = {}


def _device_pixel_ratio() -> float:
    screen = QGuiApplication.primaryScreen()
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


def _read_source(name: str) -> str:
    """讀取並快取 SVG 原始碼；檔案不存在時回傳空字串而非拋出例外。"""
    if name not in _source_cache:
        try:
            with open(resources.icon_path(name), "r", encoding="utf-8") as fh:
                _source_cache[name] = fh.read()
        except OSError:
            _source_cache[name] = ""
    return _source_cache[name]


def pixmap(name: str, color: str, size: int = config.ICON_PIXEL_SIZE) -> QPixmap:
    """取得指定顏色與尺寸的圖示點陣圖（高 DPI 下自動以整數倍繪製後降採樣）。"""
    dpr = _device_pixel_ratio()
    key = (name, color, size, dpr)
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached

    source = _read_source(name)
    if not source:
        empty = QPixmap(size, size)
        empty.fill(Qt.GlobalColor.transparent)
        _pixmap_cache[key] = empty
        return empty

    # {color} 佔位符置換；app.svg 之類的固定配色圖示不含佔位符，format 後原樣不變
    svg = source.replace("{color}", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))

    physical = max(1, int(round(size * dpr)))
    image = QImage(physical, physical, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, physical, physical))
    painter.end()

    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(dpr)
    _pixmap_cache[key] = result
    return result


def icon(name: str, color: str, size: int = config.ICON_PIXEL_SIZE) -> QIcon:
    """取得指定顏色的 QIcon。"""
    return QIcon(pixmap(name, color, size))


def app_icon() -> QIcon:
    """應用程式圖示（固定配色，供視窗與工作列使用，含多種尺寸）。"""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(pixmap("app", "#FFFFFF", size))
    return result


def checkbox_pixmap(checked: bool, color: str, accent: str, size: int = 15) -> QPixmap:
    """繪製任務清單用的核取方塊。

    Qt 的 rich text 引擎會直接丟棄 <input type="checkbox">，而專案規範禁止用
    Unicode 符號替代，因此改由 QPainter 即時繪製成行內圖片。
    """
    dpr = _device_pixel_ratio()
    physical = max(1, int(round(size * dpr)))
    image = QImage(physical, physical, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(dpr, dpr)

    box = QRectF(1.0, 1.5, size - 2.5, size - 2.5)
    if checked:
        pen = painter.pen()
        pen.setColor(QColor(accent))
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.setBrush(QColor(accent))
        painter.drawRoundedRect(box, 3.0, 3.0)
        # 勾選符號
        pen.setColor(QColor("#FFFFFF"))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        left, top, width = box.left(), box.top(), box.width()
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(left + width * 0.24, top + width * 0.50),
                    QPointF(left + width * 0.43, top + width * 0.70),
                    QPointF(left + width * 0.76, top + width * 0.30),
                ]
            )
        )
    else:
        pen = painter.pen()
        pen.setColor(QColor(color))
        pen.setWidthF(1.3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 3.0, 3.0)
    painter.end()

    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(dpr)
    return result


def clear_cache() -> None:
    """清空圖示快取（例如螢幕 DPI 改變時）。"""
    _pixmap_cache.clear()
