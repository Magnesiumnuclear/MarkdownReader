"""Mermaid 子集渲染器：flowchart 與 sequence diagram，用 QPainter 畫成圖片嵌入文件。

對外介面全在這個檔案；各階段的實作分散在：
    model.py      共用資料契約（圖表模型、幾何、Scene 原語）
    flowchart.py  流程圖：解析 + 組場景
    layout.py     流程圖的分層版面（LayoutEngine 的實作）
    sequence.py   序列圖：解析 + 版面 + 組場景
    paint.py      Scene -> QImage（主題色、字型、DPR 都在這裡處理）

流程：qt_html 的前處理器在轉換 Markdown 時呼叫 parse()，成功就 register() 換到
一把鍵，文件裡放的是 <img src="mermaid:鍵">；等 Qt 排版要圖時，browser.loadResource
以那把鍵呼叫 render()。解析在轉換期、畫在載入期——因為畫要知道主題、字級、欄寬，
那些轉換期都還不知道；而解析只跟原始碼有關，做一次就夠。

【為什麼登錄表是全域的、鍵是原始碼雜湊】
HTML 是按分頁、按主題快取的；同一份文件換主題、換字級都不會重新轉換 Markdown，
所以「鍵 -> 已解析圖表」的對照必須活得比一次轉換久。用內容雜湊當鍵，同一張圖
在不同分頁、不同主題下共用同一筆，重新載入內容沒變也直接命中。登錄表有上限
（LRU），被擠掉的鍵在 loadResource 會查不到，那時退回破圖佔位（alt 有標示）——
要真的碰到得同時開著幾百張不同的圖。

各階段模組刻意延到函式內才 import：qt_html 在程式一啟動就會 import 這個套件，
但只有真的遇到 mermaid 區塊才需要載入解析與繪圖的程式碼。
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from .. import config
from .model import (
    ERR_SYNTAX,
    ERR_TOO_LARGE,
    Diagram,
    Flowchart,
    Highlight,
    Hit,
    MermaidError,
    Scene,
    SequenceDiagram,
    UnsupportedDiagram,
)

if TYPE_CHECKING:  # pragma: no cover
    from PyQt6.QtGui import QFont, QImage

__all__ = [
    "SCHEME", "MermaidError", "UnsupportedDiagram", "Highlight",
    "parse", "register", "diagram", "render", "scene_for", "diagram_hits",
    "clear_caches",
]

# 文件裡 <img src="mermaid:鍵"> 用的協定，browser.loadResource 靠它辨識
SCHEME = "mermaid"

_FLOW_HEAD = re.compile(r"^(graph|flowchart)\b", re.IGNORECASE)
_SEQ_HEAD = re.compile(r"^sequenceDiagram\b")
_DIRECTIVE = re.compile(r"^%%\{.*\}%%\s*$")

# 鍵 -> 已解析的圖表；鍵 -> 畫好的圖片（見模組說明）
_diagrams: "OrderedDict[str, Diagram]" = OrderedDict()
_images: "OrderedDict[tuple, QImage]" = OrderedDict()
# (鍵, 字級) -> 排好版的場景。場景不含顏色也不含縮放，所以換主題、換欄寬、
# 加搜尋高亮都能重用——帶著搜尋詞重畫時只要再付繪製，不必重算版面。
# 搜尋列也靠它拿圖裡的文字來比對（見 search.scene_text）。
_scenes: "OrderedDict[tuple[str, int], Scene]" = OrderedDict()


# --- 解析 -------------------------------------------------------------------
def _strip_preamble(source: str) -> tuple[str, int]:
    """去掉開頭的 frontmatter（--- … ---）、指令（%%{…}%%）、註解與空白行。

    回傳 (剩下的原始碼, 去掉了幾行)。行數要記住：解析器回報的行號是相對於它
    拿到的文字，顯示給使用者時要加回去，否則指到的會是錯的行。
    """
    lines = source.split("\n")
    index = 0
    if lines and lines[0].strip() == "---":
        closing = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
        )
        if closing is not None:
            index = closing + 1
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("%%"):
            break
        index += 1
    return "\n".join(lines[index:]), index


def parse(source: str) -> Diagram:
    """把 mermaid 區塊的原始碼解析成圖表模型。

    失敗會拋 UnsupportedDiagram（類型不支援）或 MermaidError（語法錯、太大），
    呼叫端（qt_html 的前處理器）據此決定要嵌圖還是顯示原始碼加標示。
    """
    if len(source.encode("utf-8")) > config.MERMAID_MAX_SOURCE_BYTES:
        raise MermaidError(1, ERR_TOO_LARGE, limit=config.MERMAID_MAX_SOURCE_BYTES)
    body, offset = _strip_preamble(source)
    head = body.split("\n", 1)[0].strip()
    if not head:
        # 整個區塊只有空白與註解：指向最後一行（沒有內容可指），而不是區塊外的下一行
        raise MermaidError(max(1, offset), ERR_SYNTAX, token="")
    try:
        if _FLOW_HEAD.match(head):
            from . import flowchart

            return flowchart.parse_flowchart(
                body,
                max_nodes=config.MERMAID_MAX_NODES,
                max_edges=config.MERMAID_MAX_EDGES,
                max_depth=config.MERMAID_MAX_DEPTH,
            )
        if _SEQ_HEAD.match(head):
            from . import sequence

            return sequence.parse_sequence(
                body,
                max_messages=config.MERMAID_MAX_MESSAGES,
                max_depth=config.MERMAID_MAX_DEPTH,
            )
    except MermaidError as error:
        # 解析器的行號是相對於去掉前言後的文字，這裡加回去
        raise MermaidError(error.line + offset, error.key, **error.params) from None
    raise UnsupportedDiagram(head.split()[0].rstrip(":"))


# --- 登錄表 -----------------------------------------------------------------
def _key_for(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def register(parsed: Diagram, source: str) -> str:
    """把已解析的圖表登錄起來，回傳文件裡要用的鍵。"""
    key = _key_for(source)
    _diagrams[key] = parsed
    _diagrams.move_to_end(key)
    while len(_diagrams) > config.MERMAID_DIAGRAM_CACHE:
        _diagrams.popitem(last=False)
    return key


def diagram(key: str) -> Diagram | None:
    found = _diagrams.get(key)
    if found is not None:
        _diagrams.move_to_end(key)
    return found


def clear_caches() -> None:
    """測試用：清空登錄表與各層快取。"""
    _diagrams.clear()
    _images.clear()
    _scenes.clear()


# --- 畫 ---------------------------------------------------------------------
def scene_for(key: str, font: "QFont") -> "Scene | None":
    """取（或建立）這張圖排好版的場景；查無此鍵回傳 None。

    場景只取決於圖表本身與字級——顏色是畫的時候才依角色查色票，縮放是乘進
    畫筆變換的，都不在場景裡。所以換主題、換欄寬、加搜尋高亮都能重用同一份，
    重畫時只要再付繪製那一段。搜尋列也用它拿圖裡的文字來比對。
    """
    parsed = diagram(key)
    if parsed is None:
        return None
    from .paint import QtMeasurer, pixel_font

    base_font = pixel_font(font)
    cache_key = (key, base_font.pixelSize())
    cached = _scenes.get(cache_key)
    if cached is not None:
        _scenes.move_to_end(cache_key)
        return cached
    scene = _scene_for(parsed, QtMeasurer(base_font))
    _scenes[cache_key] = scene
    while len(_scenes) > config.MERMAID_SCENE_CACHE:
        _scenes.popitem(last=False)
    return scene


def _scale_for(scene: "Scene", column_width: float) -> float:
    """圖太寬時等比縮小（只縮不放）。render 與 diagram_hits 共用同一份算法，
    不然標出來的位置會和畫出來的差一個比例。"""
    if column_width > 0 and scene.width > column_width:
        return column_width / scene.width
    return 1.0


def diagram_hits(
    key: str, font: "QFont", column_width: float, needle: str,
    case_sensitive: bool = False, whole_words: bool = False,
) -> list[Hit]:
    """這張圖裡所有命中，依畫面順序編號，座標已換成縮放後的邏輯像素。

    搜尋列用它算「這張圖有幾筆」與「第 k 筆畫在哪，要捲到哪」；畫家在
    render_scene 裡用同一個函式算上色的位置。單一來源，順序不會分家。
    """
    scene = scene_for(key, font)
    if scene is None:
        return []
    from .paint import pixel_font, scene_hits

    return scene_hits(
        scene, pixel_font(font), needle, case_sensitive, whole_words,
        _scale_for(scene, column_width),
    )


def render(
    key: str, theme: str, column_width: float, font: "QFont", dpr: float,
    highlight: Highlight | None = None,
) -> "QImage | None":
    """把鍵對應的圖表畫成 QImage；查無此鍵回傳 None。

    自然寬度超過內文欄寬時等比縮小（只縮不放），縮放直接乘進畫家的座標變換，
    不是畫大再縮圖——這樣線條與文字在縮小後仍是向量品質。
    快取鍵含主題、欄寬、字級與 DPR：任何一個變了都是另一張圖。

    highlight 是搜尋列要標出來的詞（None 代表乾淨的圖）。它也進快取鍵——
    但 highlight 為 None 時整個鍵與行為和從前一字不差，原本那些「同樣參數
    第二次直接命中同一個物件」的呼叫端不受影響。
    """
    parsed = diagram(key)
    if parsed is None:
        return None
    from .paint import pixel_font

    base_font = pixel_font(font)
    cache_key = (key, theme, int(round(column_width)), base_font.pixelSize(), float(dpr))
    if highlight is not None:
        cache_key += tuple(highlight)
    cached = _images.get(cache_key)
    if cached is not None:
        _images.move_to_end(cache_key)
        return cached

    scene = scene_for(key, base_font)
    scale = _scale_for(scene, column_width)

    from .paint import render_scene

    image = render_scene(scene, theme, base_font, dpr, scale, highlight)
    _images[cache_key] = image
    while len(_images) > config.MERMAID_IMAGE_CACHE:
        _images.popitem(last=False)
    return image


def _scene_for(parsed: Diagram, measurer):
    if isinstance(parsed, Flowchart):
        from . import flowchart
        from .layout import LayeredLayout
        from .model import LayoutSpacing

        node_sizes = flowchart.measure_nodes(parsed, measurer)
        label_sizes = flowchart.measure_edge_labels(parsed, measurer)
        placed = LayeredLayout().layout(
            parsed, node_sizes, label_sizes, LayoutSpacing(),
            flowchart.measure_subgraph_titles(parsed, measurer),
        )
        return flowchart.to_scene(parsed, placed, measurer)
    if isinstance(parsed, SequenceDiagram):
        from . import sequence

        return sequence.to_scene(parsed, measurer)
    raise TypeError(f"不認識的圖表模型：{type(parsed).__name__}")
