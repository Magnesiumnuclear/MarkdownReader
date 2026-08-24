"""由 assets/icons/app.svg 產生 assets/app.ico（執行一次即可）。

PyInstaller 的 --icon 需要 .ico 檔，而檔案總管顯示 .md 檔案時也會用到它。
這裡以 QSvgRenderer 把向量圖繪製成多種尺寸的 PNG，再用 struct 手動組出 ICO
容器——ICO 自 Windows Vista 起支援直接內嵌 PNG，因此不需要 Pillow 或任何
額外套件。

用法：
    py -3.13 tools/make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PyQt6.QtSvg import QSvgRenderer  # noqa: E402

from app import resources  # noqa: E402

# Windows 檔案總管會依顯示模式挑選最接近的尺寸
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png(svg_path: str, size: int) -> bytes:
    """把 SVG 繪製成指定尺寸的 PNG 位元組。"""
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        raise SystemExit(f"無法讀取 SVG：{svg_path}")

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()

    buffer_data = QByteArray()
    buffer = QBuffer(buffer_data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(buffer_data)


def build_ico(pngs: dict[int, bytes]) -> bytes:
    """把多個 PNG 打包成 ICO 容器。"""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=icon, count
    directory = b""
    payload = b""
    offset = 6 + 16 * count

    for size, data in sorted(pngs.items()):
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 寬（0 代表 256）
            size if size < 256 else 0,  # 高
            0,  # 調色盤色數（PNG 不使用）
            0,  # 保留
            1,  # 色彩平面
            32,  # 每像素位元數
            len(data),
            offset,
        )
        payload += data
        offset += len(data)

    return header + directory + payload


def main() -> int:
    app = QGuiApplication(sys.argv)  # QImage/QPainter 需要
    _ = app

    svg_path = resources.icon_path("app")
    output = resources.resource_path("assets", "app.ico")

    pngs = {size: render_png(svg_path, size) for size in SIZES}
    with open(output, "wb") as handle:
        handle.write(build_ico(pngs))

    total = os.path.getsize(output)
    print(f"已產生 {output}")
    print(f"  尺寸：{', '.join(f'{s}x{s}' for s in SIZES)}")
    print(f"  大小：{total:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
