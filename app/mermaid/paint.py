"""Scene -> QImage 的畫家：主題色、字型、DPR 都在這裡收口。

上游（flowchart / sequence 的 to_scene）只產出「角色」與邏輯座標，這裡負責三件事：

  1. 角色 -> 色票：依 styles.palette(theme) 對應。場景本身不含任何顏色值，
     切主題只要重畫、不必重排版面。
  2. 文字：QtMeasurer 與畫家共用同一組 QFont / QFontMetricsF（像素字級）。
     版面量到的寬高就是畫出來的寬高——量與畫用不同的字型解析度，
     是「文字撐出框外」最常見的來源。
  3. DPR：以實體像素開圖、把畫筆縮放回邏輯單位、畫完才標 devicePixelRatio。
     順序反了 QPainter 會自己再套一次 DPR，內容放大兩倍溢出圖外
     （browser._broken_image 踩過的坑）。

已知限制：
  * 平滑曲線只是以折點為控制點、經過各段中點的二次曲線，不做避障，可能穿過別的節點。
  * 端點記號尺寸固定，不隨線寬放大；thick 線配箭頭會顯得箭頭偏小。
  * Text.scale 最後落到整數像素字級，小字級時 0.85 倍會被四捨五入吃掉一部分。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from .. import styles
from . import model, search
from .model import (
    Highlight,
    Hit,
    Ellipse,
    Path,
    Point,
    Polygon,
    Primitive,
    Rect,
    Role,
    Scene,
    Size,
    Text,
)

DEFAULT_SPACING = model.LayoutSpacing()

# 角色 -> 色票鍵；None 代表透明（不填／不描）
_ROLE_KEY: dict[str, str | None] = {
    "node_fill": "surface",
    "node_stroke": "border",
    "text": "text",
    "text_muted": "text_muted",
    "edge": "text_muted",
    "label_bg": "window_bg",
    "frame": "border",
    "frame_fill": None,
    "note_fill": "code_inline_bg",
    "note_stroke": "border",
    "header_fill": "code_bg",
    "find_match": "find_match_bg",
    "find_current": "find_current_bg",
    "accent": "accent",
    "none": None,
}
_PEN_STYLE = {
    "solid": Qt.PenStyle.SolidLine,
    "thick": Qt.PenStyle.SolidLine,
    "dotted": Qt.PenStyle.DotLine,
    "dashed": Qt.PenStyle.DashLine,
}
_PEN_WIDTH = 1.3
_ARROW_LEN = 9.0
_ARROW_HALF_W = 3.5
_CIRCLE_R = 4.0
_CROSS_HALF = 3.0
# 線要讓給記號的長度：箭頭讓到三角形底邊、圓與叉讓到記號的近端
_MARKER_LEN = {
    "none": 0.0,
    "arrow": _ARROW_LEN,
    "circle": _CIRCLE_R * 2,
    "cross": _CROSS_HALF * 2,
}
_ALIGN_FACTOR = {"left": 0.0, "center": 0.5, "right": 1.0}
_VALIGN_FACTOR = {"top": 0.0, "middle": 0.5, "bottom": 1.0}


def pixel_font(font: QFont) -> QFont:
    """回傳改用像素字級的複本。

    量字與畫字都必須走像素字級：點數字級會依裝置 DPI 換算，QFontMetricsF
    （看螢幕 DPI）和畫到 QImage 上（96 dpi）算出來的尺寸可能差一截。
    文件字型多半是點數設定的，這裡統一以 96 dpi 換算（px = pt * 96 / 72），
    和 QTextDocument 內文的基準一致。
    """
    result = QFont(font)
    if font.pixelSize() <= 0:
        result.setPixelSize(max(1, round(font.pointSizeF() * 96 / 72)))
    return result


def _first_family(css_families: str) -> str:
    """從 CSS 字型清單（'"A", "B", monospace'）取第一個字型名稱。"""
    return css_families.split(",", 1)[0].strip().strip("\"'")


class QtMeasurer:
    """以 Qt 字型量字（實作 model.TextMeasurer）；畫家也向它要字型與量尺。

    字型依 (mono, 像素字級) 快取：版面階段每個節點、每條邊都要量，每次重建
    QFontMetricsF 太浪費；更重要的是量與畫因此拿到同一個字型物件，
    不會因為建構順序不同而落到不同的字級四捨五入。
    """

    def __init__(self, font: QFont) -> None:
        self._font = pixel_font(font)
        self._px = self._font.pixelSize()
        self._mono_family = _first_family(styles.FONT_CODE)
        self._cache: dict[tuple[bool, int], tuple[QFont, QFontMetricsF]] = {}
        self.line_height: float = self.metrics_for(False, 1.0)[1].height()

    def metrics_for(self, mono: bool, scale: float) -> tuple[QFont, QFontMetricsF]:
        """(字型, 量尺)。scale 落到整數像素，量與畫必須經過同一個四捨五入。"""
        px = max(1, round(self._px * scale))
        key = (mono, px)
        found = self._cache.get(key)
        if found is None:
            font = QFont(self._font)
            if mono:
                font.setFamily(self._mono_family)
                # 沒裝那個字型時退到系統等寬字，程式碼片段才不會變成比例字
                font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPixelSize(px)
            found = self._cache[key] = (font, QFontMetricsF(font))
        return found

    def measure(self, text: str, mono: bool = False, scale: float = 1.0) -> Size:
        _, fm = self.metrics_for(mono, scale)
        lines = text.split("\n") if text else [""]
        width = max(fm.horizontalAdvance(line) for line in lines)
        return Size(width, fm.height() * len(lines))


# --- 折線幾何 ---------------------------------------------------------------
def _mid(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _direction(points: list[Point], at_head: bool) -> tuple[Point, Point] | None:
    """折線某一端的 (端點, 朝外的單位向量)。

    跳過長度為零的段（版面偶爾會給重複的點），整條都是零就回 None、不畫記號。
    """
    seq = points if at_head else points[::-1]
    if not seq:
        return None
    tx, ty = seq[-1]
    for px, py in reversed(seq[:-1]):
        dx, dy = tx - px, ty - py
        length = math.hypot(dx, dy)
        if length > 1e-6:
            return (tx, ty), (dx / length, dy / length)
    return None


def _trim_end(points: list[Point], length: float) -> list[Point]:
    """把折線末端縮短 length，給端點記號讓位。

    最後一段比要縮的還短時整段吃掉、繼續往前縮：不然端點會被推到前一個
    折點後面，線在記號底下反折出一截小尾巴。
    """
    pts = list(points)
    while len(pts) >= 2 and length > 0:
        (px, py), (qx, qy) = pts[-2], pts[-1]
        seg = math.hypot(qx - px, qy - py)
        if seg > length:
            t = (seg - length) / seg
            pts[-1] = (px + (qx - px) * t, py + (qy - py) * t)
            return pts
        length -= seg
        pts.pop()
    return pts


def _smooth_path(points: list[Point]) -> QPainterPath:
    """以折點為控制點、經過各段中點的二次曲線。

    二次曲線不會跑出控制點的凸包，轉折處不會「甩出去」壓到旁邊的節點；
    代價是曲線從中繼點旁邊繞過而不真的通過它（版面給的中繼點是假節點中心，
    本來就只是引導）。首尾兩段各留一半直線，端點切線就是首尾段的方向，
    記號沿它擺才不會歪。
    """
    path = QPainterPath(QPointF(*points[0]))
    path.lineTo(QPointF(*_mid(points[0], points[1])))
    for i in range(1, len(points) - 1):
        path.quadTo(QPointF(*points[i]), QPointF(*_mid(points[i], points[i + 1])))
    path.lineTo(QPointF(*points[-1]))
    return path


# --- 原語 -> QPainter ---------------------------------------------------------
class _ScenePainter:
    """把原語畫到已設好縮放的 QPainter 上；角色到顏色、畫筆的對應集中在這裡。"""

    def __init__(
        self, painter: QPainter, colors: dict[str, str], measurer: QtMeasurer,
        highlight: "Highlight | None" = None,
        hits: "dict[int, list[Hit]] | None" = None,
    ) -> None:
        self._p = painter
        self._colors = colors
        self._measurer = measurer
        self._highlight = highlight
        # 原語索引 -> 那個原語裡的命中。由 scene_hits 算好交進來，畫家不自己比對
        # ——計數、上色、捲動共用同一份清單，序號才不會各算各的。
        self._hits = hits or {}

    def _color(self, role: Role) -> QColor:
        key = _ROLE_KEY[role]
        return QColor(self._colors[key]) if key else QColor(0, 0, 0, 0)

    def _brush(self, role: Role) -> QBrush:
        key = _ROLE_KEY[role]
        return QBrush(QColor(self._colors[key])) if key else QBrush(Qt.BrushStyle.NoBrush)

    def _pen(self, role: Role, line: str = "solid") -> QPen:
        key = _ROLE_KEY[role]
        if key is None:
            return QPen(Qt.PenStyle.NoPen)
        return QPen(
            QBrush(QColor(self._colors[key])),
            _PEN_WIDTH * (2 if line == "thick" else 1),
            _PEN_STYLE[line],
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )

    def draw(self, index: int, item: Primitive) -> None:
        if isinstance(item, Rect):
            self._rect(item)
        elif isinstance(item, Ellipse):
            self._ellipse(item)
        elif isinstance(item, Polygon):
            self._polygon(item)
        elif isinstance(item, Path):
            self._path(item)
        elif isinstance(item, Text):
            self._text(index, item)

    def _rect(self, item: Rect) -> None:
        self._p.setPen(self._pen(item.stroke, item.line))
        self._p.setBrush(self._brush(item.fill))
        rect = QRectF(item.box.x, item.box.y, item.box.w, item.box.h)
        if item.radius > 0:
            path = QPainterPath()
            path.addRoundedRect(rect, item.radius, item.radius)
            self._p.drawPath(path)
        else:
            self._p.drawRect(rect)

    def _ellipse(self, item: Ellipse) -> None:
        self._p.setPen(self._pen(item.stroke))
        self._p.setBrush(self._brush(item.fill))
        self._p.drawEllipse(QRectF(item.box.x, item.box.y, item.box.w, item.box.h))

    def _polygon(self, item: Polygon) -> None:
        self._p.setPen(self._pen(item.stroke, item.line))
        self._p.setBrush(self._brush(item.fill))
        self._p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in item.points]))

    def _path(self, item: Path) -> None:
        # 記號的位置與方向取自原始端點；線本身再依記號長度縮短
        pts = list(item.points)
        head = _direction(pts, True) if item.head != "none" else None
        tail = _direction(pts, False) if item.tail != "none" else None
        if head:
            pts = _trim_end(pts, _MARKER_LEN[item.head])
        if tail:
            pts = _trim_end(pts[::-1], _MARKER_LEN[item.tail])[::-1]
        if len(pts) >= 2:
            self._p.setPen(self._pen(item.stroke, item.line))
            self._p.setBrush(Qt.BrushStyle.NoBrush)
            if item.smooth and len(pts) > 2:
                self._p.drawPath(_smooth_path(pts))
            else:
                self._p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))
        if head:
            self._marker(item.head, head, item.stroke, item.line)
        if tail:
            self._marker(item.tail, tail, item.stroke, item.line)

    def _marker(
        self, kind: str, end: tuple[Point, Point], role: Role, line: str
    ) -> None:
        """在端點畫記號；d 是朝外的單位向量，n 是它的法向量。記號一律實線。"""
        (tx, ty), (dx, dy) = end
        nx, ny = -dy, dx
        pen = self._pen(role, "thick" if line == "thick" else "solid")
        if kind == "arrow":
            bx, by = tx - dx * _ARROW_LEN, ty - dy * _ARROW_LEN
            self._p.setPen(Qt.PenStyle.NoPen)
            self._p.setBrush(self._brush(role))
            self._p.drawPolygon(QPolygonF([
                QPointF(tx, ty),
                QPointF(bx + nx * _ARROW_HALF_W, by + ny * _ARROW_HALF_W),
                QPointF(bx - nx * _ARROW_HALF_W, by - ny * _ARROW_HALF_W),
            ]))
        elif kind == "circle":
            # 圓內填底色蓋住線尾，看起來才是「空心圓」而不是線穿過圓
            self._p.setPen(pen)
            self._p.setBrush(self._brush("label_bg"))
            self._p.drawEllipse(
                QPointF(tx - dx * _CIRCLE_R, ty - dy * _CIRCLE_R), _CIRCLE_R, _CIRCLE_R
            )
        elif kind == "cross":
            cx, cy = tx - dx * _CROSS_HALF, ty - dy * _CROSS_HALF
            ax, ay = dx * _CROSS_HALF, dy * _CROSS_HALF
            px, py = nx * _CROSS_HALF, ny * _CROSS_HALF
            self._p.setPen(pen)
            self._p.drawLine(QPointF(cx - ax - px, cy - ay - py), QPointF(cx + ax + px, cy + ay + py))
            self._p.drawLine(QPointF(cx - ax + px, cy - ay + py), QPointF(cx + ax - px, cy + ay - py))

    def _text(self, index: int, item: Text) -> None:
        font, fm = self._measurer.metrics_for(item.mono, item.scale)
        lines = item.text.split("\n")
        line_h = fm.height()
        top = item.y - _VALIGN_FACTOR[item.valign] * line_h * len(lines)
        # 命中的底色先鋪，字再畫上去。矩形是 scene_hits 算好的（和捲動用的
        # 是同一份），fillRect 不動畫筆，不必 save/restore。
        current = self._highlight.current_ordinal if self._highlight else -1
        for hit in self._hits.get(index, ()):
            role = "find_current" if hit.ordinal == current else "find_match"
            self._p.fillRect(
                QRectF(hit.x, hit.y, hit.w, hit.h), self._color(role)
            )
        self._p.setFont(font)
        self._p.setPen(QPen(self._color(item.role)))
        for i, line in enumerate(lines):
            x = item.x - _ALIGN_FACTOR[item.align] * fm.horizontalAdvance(line)
            # 以 ascent 定基線：畫出來的框才和 measure() 回報的 height() 疊合
            self._p.drawText(QPointF(x, top + i * line_h + fm.ascent()), line)


def scene_hits(
    scene: Scene, font: QFont, needle: str,
    case_sensitive: bool = False, whole_words: bool = False, scale: float = 1.0,
) -> list[Hit]:
    """列出這個場景裡所有命中，依**畫面順序**（由上而下、由左而右）編號。

    這是計數、上色與捲動的**單一來源**：三邊都吃這份清單，序號就不可能分家。
    幾何算法和 _text 畫字時完全一樣（同一組 metrics_for 的 QFontMetricsF），
    所以標出來的框必然對齊實際畫出來的字。

    scale 讓呼叫端直接拿到「縮放後的邏輯像素」，省得自己再乘一次。
    """
    if not needle:
        return []
    measurer = QtMeasurer(font)
    found: list[tuple[float, float, int, int, int, int, float, float, float]] = []
    for index, item in enumerate(scene.items):
        if not isinstance(item, Text):
            continue
        spans_by_line = [
            (i, search.matches_in(line, needle, case_sensitive, whole_words))
            for i, line in enumerate(item.text.split("\n"))
        ]
        if not any(spans for _i, spans in spans_by_line):
            continue
        _font, fm = measurer.metrics_for(item.mono, item.scale)
        lines = item.text.split("\n")
        line_h = fm.height()
        top = item.y - _VALIGN_FACTOR[item.valign] * line_h * len(lines)
        for i, spans in spans_by_line:
            line = lines[i]
            x = item.x - _ALIGN_FACTOR[item.align] * fm.horizontalAdvance(line)
            line_top = top + i * line_h
            for start, end in spans:
                left = x + fm.horizontalAdvance(line[:start])
                width = fm.horizontalAdvance(line[start:end])
                found.append((
                    line_top, left, index, i, start, end, left, line_top, width,
                ))
    # 畫面順序：先由上而下，再由左而右。四捨五入到 0.1 避免浮點抖動換順序；
    # 完全同位置時用場景索引與行內位置壓住，結果必定可重現。
    found.sort(key=lambda r: (round(r[0], 1), round(r[1], 1), r[2], r[3], r[4]))
    line_heights: dict[int, float] = {}
    hits: list[Hit] = []
    for ordinal, row in enumerate(found):
        line_top, left, index, i, start, end, hx, hy, width = row
        item = scene.items[index]
        if index not in line_heights:
            line_heights[index] = measurer.metrics_for(item.mono, item.scale)[1].height()
        hits.append(Hit(
            ordinal, index, i, start, end,
            hx * scale, hy * scale, width * scale, line_heights[index] * scale,
        ))
    return hits


def hits_by_item(hits: list[Hit]) -> dict[int, list[Hit]]:
    """把 scene_hits 的結果依原語索引分組，給畫家查表用。"""
    grouped: dict[int, list[Hit]] = {}
    for hit in hits:
        grouped.setdefault(hit.item, []).append(hit)
    return grouped


def render_scene(
    scene: Scene, theme: str, font: QFont, dpr: float, scale: float = 1.0,
    highlight: "Highlight | None" = None,
) -> QImage:
    """把 Scene 畫成 QImage。

    scale 是整張圖的縮放（欄寬不夠時只縮不放），dpr 是螢幕的像素比；兩者一起
    乘進畫筆變換，原語照邏輯座標畫就好。回傳的 QImage 已標好 devicePixelRatio，
    QTextDocument 會以邏輯尺寸排版、以實體像素顯示。
    """
    factor = scale * dpr
    image = QImage(
        max(1, round(scene.width * factor)),
        max(1, round(scene.height * factor)),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.scale(factor, factor)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    grouped: dict[int, list[Hit]] = {}
    if highlight is not None:
        # 命中的幾何在場景座標算（畫筆已經套了縮放），所以這裡 scale 用 1.0
        grouped = hits_by_item(scene_hits(
            scene, font, highlight.needle,
            highlight.case_sensitive, highlight.whole_words,
        ))
    drawer = _ScenePainter(
        painter, styles.palette(theme), QtMeasurer(font), highlight, grouped
    )
    for index, item in enumerate(scene.items):
        drawer.draw(index, item)
    painter.end()
    # 一定要畫完才標 DPR：先標的話 QPainter 會自己再套一次縮放，內容變兩倍大溢出圖外
    image.setDevicePixelRatio(dpr)
    return image
