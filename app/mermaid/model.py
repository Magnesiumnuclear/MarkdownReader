"""Mermaid 子集渲染器的共用資料模型（所有模組的契約）。

整條管線分四段，段與段之間只靠這裡的型別溝通：

    解析        flowchart.py / sequence.py       原始碼 -> Flowchart / SequenceDiagram
    版面        layout.py（流程圖）              圖 + 節點尺寸 -> FlowLayout
                sequence.py（序列圖自己排）
    組場景      各自的 to_scene()                 幾何 -> Scene（繪圖原語清單）
    畫          paint.py                          Scene -> QImage

Scene 是刻意多加的一層：畫家只認得矩形、線、文字這幾種原語，不必知道
流程圖或序列圖的語意；測試也可以直接對原語做斷言，不必比對像素。

【為什麼錯誤帶的是翻譯鍵，不是句子】
解析失敗的原因最後會顯示在文件裡（「Mermaid 解析失敗（第 N 行）…」），
介面語言可以切換，所以 MermaidError 只記 language 的鍵與參數，
句子在顯示的那一刻才組。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, NamedTuple, Protocol, Union


# --- 錯誤 -------------------------------------------------------------------
class MermaidError(Exception):
    """解析失敗。line 是 1-based 行號（相對於 mermaid 區塊的第一行）。

    key 是 language 檔裡的鍵（mermaid.err.*），params 是它的佔位符參數。
    """

    def __init__(self, line: int, key: str, **params: object) -> None:
        super().__init__(f"{key} @ line {line} {params or ''}")
        self.line = line
        self.key = key
        self.params = params


class UnsupportedDiagram(Exception):
    """圖表類型不在支援範圍（classDiagram、gantt…）。kind 是第一個關鍵字。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# 解析器共用的錯誤鍵（language 檔兩邊都要有）
ERR_SYNTAX = "mermaid.err.syntax"            # {line} 行看不懂：{token}
ERR_TOO_LARGE = "mermaid.err.tooLarge"       # 節點/連線/訊息超過上限 {limit}
ERR_UNCLOSED = "mermaid.err.unclosedBlock"   # subgraph / loop / alt… 少了 end
ERR_TOO_DEEP = "mermaid.err.tooDeep"         # 巢狀超過 {limit} 層
ERR_UNEXPECTED_END = "mermaid.err.unexpectedEnd"  # 多出來的 end
ERR_UNKNOWN_REF = "mermaid.err.unknownParticipant"  # 序列圖引用了沒宣告的參與者（僅在嚴格處）


# --- 幾何 -------------------------------------------------------------------
@dataclass
class Size:
    w: float
    h: float


@dataclass
class Box:
    """左上角座標 + 寬高，邏輯像素。"""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


Point = tuple[float, float]


# --- 流程圖 -----------------------------------------------------------------
Direction = Literal["TB", "BT", "LR", "RL"]          # TD 正規化成 TB
NodeShape = Literal[
    "rect",           # [text]
    "round",          # (text)
    "stadium",        # ([text])
    "subroutine",     # [[text]]
    "cylinder",       # [(text)]
    "circle",         # ((text))
    "diamond",        # {text}
    "hexagon",        # {{text}}
    "asym",           # >text]
    "lean_right",     # [/text/]
    "lean_left",      # [\text\]
    "trapezoid",      # [/text\]
    "trapezoid_alt",  # [\text/]
]
EdgeStyle = Literal["solid", "dotted", "thick"]
EdgeEnd = Literal["none", "arrow", "circle", "cross"]


@dataclass
class FlowNode:
    id: str
    label: str                 # 多行以 "\n" 分隔（來源的 <br>）
    shape: NodeShape = "rect"


@dataclass
class FlowEdge:
    source: str
    target: str
    label: str = ""
    style: EdgeStyle = "solid"
    head: EdgeEnd = "arrow"    # target 那一端
    tail: EdgeEnd = "none"     # source 那一端（<--> 時是 arrow）


@dataclass
class Subgraph:
    id: str
    title: str
    nodes: list[str] = field(default_factory=list)          # 直接成員（節點 id）
    children: list["Subgraph"] = field(default_factory=list)


@dataclass
class Flowchart:
    direction: Direction = "TB"
    nodes: dict[str, FlowNode] = field(default_factory=dict)  # 保留首次出現的順序
    edges: list[FlowEdge] = field(default_factory=list)
    subgraphs: list[Subgraph] = field(default_factory=list)   # 只放頂層；巢狀在 children


@dataclass
class LayoutSpacing:
    node_sep: float = 40.0          # 同一層相鄰節點的間距
    rank_sep: float = 56.0          # 層與層之間的間距
    margin: float = 16.0            # 整張圖四周留白
    subgraph_pad: float = 14.0      # 子圖外框與成員之間
    subgraph_title_h: float = 22.0  # 子圖標題列高度


@dataclass
class EdgeRoute:
    index: int                        # 對應 Flowchart.edges 的索引
    points: list[Point]               # 從 source 邊界到 target 邊界，含中繼點
    label_center: Point | None = None


@dataclass
class FlowLayout:
    width: float
    height: float
    nodes: dict[str, Box]             # 節點 id -> 外框
    edges: list[EdgeRoute]
    subgraphs: dict[str, Box]         # 子圖 id -> 外框（含標題列）


class LayoutEngine(Protocol):
    """流程圖版面演算法的介面。要換成別的引擎只要實作這個。"""

    def layout(
        self,
        chart: Flowchart,
        node_sizes: dict[str, Size],       # 每個節點畫出來的尺寸（含形狀的內距）
        label_sizes: dict[int, Size],      # 邊索引 -> 邊上文字的尺寸（沒文字就不在裡面）
        spacing: LayoutSpacing,
        title_sizes: dict[str, Size] | None = None,  # 子圖 id -> 標題文字的尺寸
    ) -> FlowLayout: ...


# --- 序列圖 -----------------------------------------------------------------
LineKind = Literal["solid", "dotted"]
SeqHead = Literal["none", "arrow", "open", "cross"]
#   ->  solid/none   -->  dotted/none   ->> solid/arrow   -->> dotted/arrow
#   -x  solid/cross  --x  dotted/cross  -)  solid/open    --)  dotted/open


@dataclass
class Participant:
    id: str
    label: str
    actor: bool = False        # actor 畫小人，participant 畫方框


@dataclass
class Message:
    source: str
    target: str
    text: str
    line: LineKind = "solid"
    head: SeqHead = "arrow"
    activate: bool = False     # 箭頭後的 +：啟用 target
    deactivate: bool = False   # 箭頭後的 -：停用 source


@dataclass
class Note:
    position: Literal["left", "right", "over"]
    participants: list[str]
    text: str


@dataclass
class Activate:
    participant: str


@dataclass
class Deactivate:
    participant: str


@dataclass
class Section:
    label: str
    items: list["SeqItem"] = field(default_factory=list)


@dataclass
class Block:
    """loop / alt / opt / par / critical / break / rect。

    loop、opt、break、rect 只有一個 section；alt 的 else、par 的 and、
    critical 的 option 各多開一個 section。
    """

    kind: Literal["loop", "alt", "opt", "par", "critical", "break", "rect"]
    sections: list[Section] = field(default_factory=list)


SeqItem = Union[Message, Note, Activate, Deactivate, Block]


@dataclass
class SequenceDiagram:
    title: str = ""
    participants: list[Participant] = field(default_factory=list)  # 宣告順序，再依首次出現補上
    items: list[SeqItem] = field(default_factory=list)
    autonumber: bool = False


Diagram = Union[Flowchart, SequenceDiagram]


# --- Scene：繪圖原語 --------------------------------------------------------
# 顏色一律用「角色」，由 paint.py 依主題色票對應；場景本身不含任何顏色值。
Role = Literal[
    "node_fill", "node_stroke",   # 一般節點
    "text", "text_muted",
    "edge",                       # 連線與箭頭
    "label_bg",                   # 邊上文字的底色（蓋住線）
    "frame", "frame_fill",        # 子圖外框、序列圖的 loop/alt 框
    "note_fill", "note_stroke",   # 序列圖便條
    "header_fill",                # 序列圖參與者方框
    "find_match", "find_current", # 搜尋高亮：一般相符／目前所在那一筆
    "accent",
    "none",                       # 不填／不描
]
LineStyle = Literal["solid", "dotted", "dashed", "thick"]
Align = Literal["left", "center", "right"]
VAlign = Literal["top", "middle", "bottom"]


@dataclass
class Rect:
    box: Box
    fill: Role = "node_fill"
    stroke: Role = "node_stroke"
    radius: float = 0.0
    line: LineStyle = "solid"


@dataclass
class Ellipse:
    box: Box
    fill: Role = "node_fill"
    stroke: Role = "node_stroke"


@dataclass
class Polygon:
    points: list[Point]
    fill: Role = "node_fill"
    stroke: Role = "node_stroke"
    line: LineStyle = "solid"


@dataclass
class Path:
    """折線；smooth=True 時畫家用平滑曲線經過中繼點。head/tail 是兩端的端點記號。"""

    points: list[Point]
    stroke: Role = "edge"
    line: LineStyle = "solid"
    head: EdgeEnd = "none"
    tail: EdgeEnd = "none"
    smooth: bool = False


@dataclass
class Text:
    """x, y 是錨點；align/valign 決定文字相對錨點怎麼放。多行以 "\\n" 分隔。"""

    x: float
    y: float
    text: str
    role: Role = "text"
    align: Align = "center"
    valign: VAlign = "middle"
    mono: bool = False
    scale: float = 1.0          # 相對於基準字級（例如 autonumber 的小字 0.85）


Primitive = Union[Rect, Ellipse, Polygon, Path, Text]


@dataclass
class Scene:
    width: float
    height: float
    items: list[Primitive] = field(default_factory=list)


class Highlight(NamedTuple):
    """要在圖裡標出來的搜尋詞。

    放在契約層而不是 facade：畫家與外部介面都要用到它，畫家再反過來匯入
    facade 就成了循環匯入。current 代表「這張圖就是使用者目前所在的那一筆」，
    用強調色畫，其餘用一般的相符色——和內文的高亮同一套語彙。
    """

    needle: str
    case_sensitive: bool = False
    whole_words: bool = False
    current: bool = False


class TextMeasurer(Protocol):
    """量文字的尺寸。paint.py 用 Qt 字型量；純邏輯測試用固定寬字元的假量尺。"""

    line_height: float

    def measure(self, text: str, mono: bool = False, scale: float = 1.0) -> Size:
        """多行文字（以 "\\n" 分隔）的整塊寬高。"""
        ...


class FixedMeasurer:
    """假量尺：每個字元固定寬、每行固定高。給不開 Qt 的解析／版面測試用。"""

    def __init__(self, char_w: float = 7.0, line_h: float = 16.0) -> None:
        self.char_w = char_w
        self.line_height = line_h

    def measure(self, text: str, mono: bool = False, scale: float = 1.0) -> Size:
        lines = text.split("\n") if text else [""]
        width = max(len(line) for line in lines) * self.char_w * scale
        return Size(width, len(lines) * self.line_height * scale)
