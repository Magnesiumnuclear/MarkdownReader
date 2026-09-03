"""流程圖子集：原始碼 -> Flowchart、節點量測、Flowchart + FlowLayout -> Scene。

【解析策略】
不建完整文法，逐行、逐敘述用正則從左到右掃：吃一個節點（id + 可選形狀），
再吃一個邊運算子（可帶標籤），再吃節點……直到敘述結束。`&` 群組展開成
笛卡兒積；`subgraph … end` 用堆疊追蹤，節點歸屬「第一次在子圖區塊內被提到」
的那個子圖。官方文法允許節點 id 含各種標點，這裡只認 `[A-Za-z0-9_]`
與夾在中間的 `-`，換來可預測的錯誤位置。

【容易踩的陷阱】
- `-- 文字 -->` 和 `--- B` 都以兩個 `-` 開頭：得先試「帶標籤」的形式，並限定
  `--` 之後不能緊接 `-` `>` `x` `o` `=` `.`，否則 `A --- B` 會被讀成空標籤的邊。
- 形狀開頭符號互為前綴（`[` / `[[` / `[(` / `([`），比對順序是長的先。
- `%%` 註解與 `;` 分隔只在引號、括號、`|…|` 之外生效，不然 `A[x &amp; y]`
  會在實體的分號處被切成兩個敘述。
- 邊標籤文字裡的 `--`、`==`、`.-` 會被當成邊的結尾；要用就以引號包住。
- `x--x` / `o--o` 的尾端記號要和來源節點隔空白（`A x--x B`），貼著寫會被
  吃進 id；官方也是這樣。

【已知限制】
- 不支援 v11 的 `A@{shape: …}`、markdown 字串（反引號原樣保留）、圖示。
- 把邊連到子圖 id 會變成一個同名的矩形節點（版面不會把它框進去）。
- 同一節點被兩個子圖提到時只歸前者，之後的提及不會搬動它。
"""

from __future__ import annotations

import html
import re
from dataclasses import replace

from .model import (
    ERR_SYNTAX,
    ERR_TOO_DEEP,
    ERR_TOO_LARGE,
    ERR_UNCLOSED,
    ERR_UNEXPECTED_END,
    Box,
    Direction,
    EdgeEnd,
    EdgeStyle,
    Ellipse,
    FlowEdge,
    FlowLayout,
    FlowNode,
    Flowchart,
    LineStyle,
    MermaidError,
    NodeShape,
    Path,
    Point,
    Polygon,
    Primitive,
    Rect,
    Scene,
    Size,
    Subgraph,
    Text,
    TextMeasurer,
)

# --- 尺寸常數 ---------------------------------------------------------------
_PAD_X, _PAD_Y = 14.0, 8.0            # 矩形類：文字四周的內距（每側）
_MIN_W, _MIN_H = 40.0, 28.0
_SLANT = 10.0                          # lean/trapezoid 斜邊的水平位移；量測時寬 +20 就是給它的
_HEX_INSET = 12.0                      # hexagon 左右尖角的深度；寬 +24
_CYLINDER_CAP = 14.0                   # 假 3D 圓柱頂面橢圓的最大高度
_LABEL_PAD_X, _LABEL_PAD_Y = 6.0, 3.0  # 邊標籤底色框每側的留白

# --- 語法 -------------------------------------------------------------------
_DIRECTIONS: dict[str, Direction] = {"TB": "TB", "TD": "TB", "BT": "BT", "LR": "LR", "RL": "RL"}
_HEADER_RE = re.compile(r"(?:graph|flowchart)(?:\s+(TD|TB|BT|LR|RL))?")
_ID_RE = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")
_QUOTED_RE = re.compile(r'\s*"([^"]*)"\s*')
_CLASS_RE = re.compile(r"\s*:::\s*[A-Za-z0-9_\-]+")
_SUBGRAPH_RE = re.compile(r"subgraph\b\s*(?P<rest>.*)")
_SUBGRAPH_ID_RE = re.compile(r"(?P<id>[A-Za-z0-9_\-]+)\s*\[\s*(?P<title>.*?)\s*\]")
# classDef 要排在 class 前面，否則 `class\b` 先比不到、整段又從頭再試一次沒意義。
_IGNORED_RE = re.compile(r"(?:style|classDef|class|linkStyle|click|direction|accTitle|accDescr)\b")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HASH_ENTITY_RE = re.compile(r"#(\w+);")

# 帶標籤的邊 `-- 文字 -->`：開頭兩個符號後不能緊接運算子的其他字元，否則和
# `--->`、`--x`、`-.-`、`===` 撞在一起。文字可用引號包住（裡面才能放 `==`）。
_LABEL_EDGE_RE = re.compile(
    r"(?P<tail>[<xo])?(?P<open>-\.|--|==)(?![-=.>xo])\s*"
    r'(?P<text>"[^"]*"|.*?)\s*'
    r"(?P<body>-{2,}|={2,}|\.+-+)(?P<head>[>xo])?"
)
# 不帶標籤（或 `|文字|` 形式）的邊；多幾個 `-`/`=`/`.` 都收。
_EDGE_RE = re.compile(
    r"(?P<tail>[<xo])?(?P<body>-\.+-|-{2,}|={2,})(?P<head>[>xo])?"
    r"(?:\s*\|(?P<text>[^|]*)\|)?"
)
_HEADS: dict[str, EdgeEnd] = {">": "arrow", "x": "cross", "o": "circle"}
_TAILS: dict[str, EdgeEnd] = {"<": "arrow", "x": "cross", "o": "circle"}

# 形狀開頭 -> ((結尾, 形狀), …)。開頭互為前綴，這個順序就是比對順序。
# `[/` 與 `[\` 各有兩種結尾（斜邊同向是平行四邊形、反向是梯形），取先出現的那個。
_SHAPE_OPENERS: tuple[tuple[str, tuple[tuple[str, NodeShape], ...]], ...] = (
    ("([", (("])", "stadium"),)),
    ("[[", (("]]", "subroutine"),)),
    ("[(", ((")]", "cylinder"),)),
    ("[/", (("/]", "lean_right"), ("\\]", "trapezoid"))),
    ("[\\", (("\\]", "lean_left"), ("/]", "trapezoid_alt"))),
    ("[", (("]", "rect"),)),
    ("((", (("))", "circle"),)),
    ("(", ((")", "round"),)),
    ("{{", (("}}", "hexagon"),)),
    ("{", (("}", "diamond"),)),
    (">", (("]", "asym"),)),
)


def _hash_entity(m: re.Match[str]) -> str:
    """`#quot;` `#35;` 換成標準實體，交給最後那一次 html.unescape 統一還原。

    只換真的是實體的（`#include;` 這種要原樣留著），而且這裡不直接解碼，
    否則 `#amp;lt;` 會被解兩次。
    """
    name = m.group(1)
    candidate = f"&#{name};" if name.isdigit() else f"&{name};"
    return candidate if html.unescape(candidate) != candidate else m.group(0)


def _decode_label(raw: str) -> str:
    """標籤文字：去引號、<br> 換行、實體還原、每行修邊。"""
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    # <br> 要在實體還原之前換掉，寫成 &lt;br&gt; 的才會留成字面文字
    text = _HASH_ENTITY_RE.sub(_hash_entity, _BR_RE.sub("\n", text))
    return "\n".join(line.strip() for line in html.unescape(text).split("\n"))


def _split_statements(raw: str) -> list[str]:
    """一行拆成多個敘述：`%%` 之後是註解、`;` 分隔。

    兩者都只在引號、括號、`|…|` 之外才算數：實體 `&amp;` 自帶分號，
    `A[x &amp; y]` 這種最常見的寫法不能被切開。
    """
    parts: list[str] = []
    buf: list[str] = []
    quoted = piped = False
    depth = 0
    for i, ch in enumerate(raw):
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch in "[({":
                depth += 1
            elif ch in "])}":
                depth = max(0, depth - 1)
            elif ch == "|":
                piped = not piped
            elif depth == 0 and not piped:
                if raw.startswith("%%", i):
                    break
                if ch == ";":
                    parts.append("".join(buf))
                    buf = []
                    continue
        buf.append(ch)
    parts.append("".join(buf))
    return [stmt for stmt in (part.strip() for part in parts) if stmt]


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


class _Parser:
    """逐敘述累積 Flowchart。line/text 是目前處理中的行，錯誤訊息用。"""

    def __init__(self, max_nodes: int, max_edges: int, max_depth: int) -> None:
        self.chart = Flowchart()
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self.max_depth = max_depth
        self.line = 0
        self.text = ""
        self.stack: list[tuple[Subgraph, int]] = []   # 開著的子圖與各自的起始行
        self.placed: set[str] = set()                 # 已歸入某個子圖的節點
        self.subgraph_ids: set[str] = set()

    def fail(self) -> MermaidError:
        return MermaidError(self.line, ERR_SYNTAX, token=self.text[:40])

    # --- 敘述層 ---
    def statement(self, stmt: str) -> None:
        if stmt.lower() == "end":
            if not self.stack:
                raise MermaidError(self.line, ERR_UNEXPECTED_END)
            self.stack.pop()
            return
        m = _SUBGRAPH_RE.match(stmt)
        if m:
            self.open_subgraph(m.group("rest"))
            return
        if _IGNORED_RE.match(stmt):
            return
        self.chain(stmt)

    def open_subgraph(self, rest: str) -> None:
        # 版面與組場景對子圖是一層一層遞迴的，深度必須在解析期就擋下來，
        # 不然畫的時候會打穿直譯器的遞迴上限（那就不是標示而是當機了）
        if len(self.stack) >= self.max_depth:
            raise MermaidError(self.line, ERR_TOO_DEEP, limit=self.max_depth)
        m = _SUBGRAPH_ID_RE.fullmatch(rest.strip())
        if m:
            sg_id, title = m.group("id"), _decode_label(m.group("title"))
        else:
            title = _decode_label(rest)
            sg_id = title or f"subgraph{len(self.subgraph_ids) + 1}"
        # 版面以 id 當鍵，兩個同名子圖會互相蓋掉外框，補序號讓它們分開
        base, n = sg_id, 1
        while sg_id in self.subgraph_ids:
            n += 1
            sg_id = f"{base}#{n}"
        self.subgraph_ids.add(sg_id)
        sg = Subgraph(sg_id, title)
        (self.stack[-1][0].children if self.stack else self.chart.subgraphs).append(sg)
        self.stack.append((sg, self.line))

    def chain(self, stmt: str) -> None:
        """`A & B --> C --> D`：每一段左右群組做笛卡兒積，右群組再當下一段的左邊。"""
        left, pos = self.group(stmt, 0)
        while True:
            pos = _skip_ws(stmt, pos)
            if pos >= len(stmt):
                return
            template, pos = self.edge(stmt, pos)
            right, pos = self.group(stmt, pos)
            for src in left:
                for dst in right:
                    self.add_edge(replace(template, source=src, target=dst))
            left = right

    # --- 節點 ---
    def group(self, stmt: str, pos: int) -> tuple[list[str], int]:
        ids: list[str] = []
        while True:
            nid, pos = self.node(stmt, _skip_ws(stmt, pos))
            ids.append(nid)
            after = _skip_ws(stmt, pos)
            if not stmt.startswith("&", after):
                return ids, pos
            pos = after + 1

    def node(self, stmt: str, pos: int) -> tuple[str, int]:
        m = _ID_RE.match(stmt, pos)
        if not m:
            raise self.fail()
        nid, pos = m.group(), m.end()
        shape = self.shape(stmt, _skip_ws(stmt, pos))
        if shape:
            label, kind, pos = shape
            self.register(nid, label, kind)
        else:
            self.register(nid, nid, None)
        cls = _CLASS_RE.match(stmt, pos)
        return nid, cls.end() if cls else pos

    def shape(self, stmt: str, pos: int) -> tuple[str, NodeShape, int] | None:
        """id 後面若接形狀符號就回 (標籤, 形狀, 結尾位置)，否則 None。"""
        for opener, closers in _SHAPE_OPENERS:
            if not stmt.startswith(opener, pos):
                continue
            start = pos + len(opener)
            quoted = _QUOTED_RE.match(stmt, start)
            if quoted:
                for closer, kind in closers:
                    if stmt.startswith(closer, quoted.end()):
                        return _decode_label(quoted.group(1)), kind, quoted.end() + len(closer)
                raise self.fail()
            hits = [(stmt.find(closer, start), closer, kind) for closer, kind in closers]
            found = [hit for hit in hits if hit[0] >= 0]
            if not found:
                raise self.fail()
            end, closer, kind = min(found)
            return _decode_label(stmt[start:end]), kind, end + len(closer)
        return None

    def register(self, nid: str, label: str, kind: NodeShape | None) -> None:
        node = self.chart.nodes.get(nid)
        if node is None:
            if len(self.chart.nodes) >= self.max_nodes:
                raise MermaidError(self.line, ERR_TOO_LARGE, limit=self.max_nodes)
            node = self.chart.nodes[nid] = FlowNode(nid, nid)
        if kind is not None:   # 只在邊裡出現的節點維持預設；再次宣告形狀就覆蓋
            node.label, node.shape = label, kind
        if self.stack and nid not in self.placed:
            self.stack[-1][0].nodes.append(nid)
            self.placed.add(nid)

    # --- 邊 ---
    def edge(self, stmt: str, pos: int) -> tuple[FlowEdge, int]:
        m = _LABEL_EDGE_RE.match(stmt, pos) or _EDGE_RE.match(stmt, pos)
        if not m:
            raise self.fail()
        body = m.group("body")
        style: EdgeStyle = "dotted" if "." in body else "thick" if "=" in body else "solid"
        template = FlowEdge(
            "", "", _decode_label(m.group("text") or ""), style,
            head=_HEADS.get(m.group("head") or "", "none"),
            tail=_TAILS.get(m.group("tail") or "", "none"),
        )
        return template, m.end()

    def add_edge(self, edge: FlowEdge) -> None:
        if len(self.chart.edges) >= self.max_edges:
            raise MermaidError(self.line, ERR_TOO_LARGE, limit=self.max_edges)
        self.chart.edges.append(edge)


def parse_flowchart(source: str, *, max_nodes: int = 300, max_edges: int = 600,
                    max_depth: int = 60) -> Flowchart:
    """把 mermaid 區塊的原始碼（含 `graph`/`flowchart` 首行）解析成 Flowchart。

    行號 1-based、相對於 source；首行前的空白行與註解行會略過。
    """
    parser = _Parser(max_nodes, max_edges, max_depth)
    seen_header = False
    for number, raw in enumerate(source.splitlines(), 1):
        parser.line, parser.text = number, raw.strip()
        for stmt in _split_statements(raw):
            if seen_header:
                parser.statement(stmt)
                continue
            header = _HEADER_RE.fullmatch(stmt)
            if not header:
                raise parser.fail()
            parser.chart.direction = _DIRECTIONS[header.group(1) or "TB"]
            seen_header = True
    if not seen_header:
        raise MermaidError(1, ERR_SYNTAX, token="")
    if parser.stack:
        raise MermaidError(parser.stack[-1][1], ERR_UNCLOSED)
    return parser.chart


# --- 量測 -------------------------------------------------------------------
def measure_nodes(chart: Flowchart, measurer: TextMeasurer) -> dict[str, Size]:
    """節點外框尺寸：先加矩形內距，再依形狀放大（菱形要斜邊也容得下文字）。"""
    sizes: dict[str, Size] = {}
    for node in chart.nodes.values():
        text = measurer.measure(node.label)
        w, h = text.w + 2 * _PAD_X, text.h + 2 * _PAD_Y
        if node.shape == "diamond":
            w, h = w * 1.6, h * 1.6
        elif node.shape == "hexagon":
            w += 2 * _HEX_INSET
        elif node.shape == "circle":
            w = h = max(w, h) + 16
        elif node.shape == "cylinder":
            h += 12
        elif node.shape in ("lean_right", "lean_left", "trapezoid", "trapezoid_alt"):
            w += 2 * _SLANT
        sizes[node.id] = Size(max(w, _MIN_W), max(h, _MIN_H))
    return sizes


def _label_box(text: Size) -> Size:
    return Size(text.w + 2 * _LABEL_PAD_X, text.h + 2 * _LABEL_PAD_Y)


def measure_edge_labels(chart: Flowchart, measurer: TextMeasurer) -> dict[int, Size]:
    """邊索引 -> 標籤底色框的尺寸（含留白，版面要留的就是這塊）。沒標籤的不在裡面。"""
    return {
        index: _label_box(measurer.measure(edge.label))
        for index, edge in enumerate(chart.edges)
        if edge.label
    }


# --- 組場景 -----------------------------------------------------------------
_EDGE_LINES: dict[EdgeStyle, LineStyle] = {"solid": "solid", "dotted": "dotted", "thick": "thick"}


def _outline(shape: NodeShape, b: Box) -> list[Primitive]:
    """節點外形（不含文字）。座標全部從 Box 推，畫家不必知道形狀語意。"""
    x, y, r, btm, cx, cy = b.x, b.y, b.right, b.bottom, b.cx, b.cy
    if shape == "rect":
        return [Rect(b)]
    if shape == "round":
        return [Rect(b, radius=8.0)]
    if shape == "stadium":
        return [Rect(b, radius=b.h / 2)]
    if shape == "circle":
        return [Ellipse(b)]
    if shape == "subroutine":
        return [
            Rect(b),
            Path([(x + 6, y), (x + 6, btm)], stroke="node_stroke"),
            Path([(r - 6, y), (r - 6, btm)], stroke="node_stroke"),
        ]
    if shape == "cylinder":
        # 假 3D：底面橢圓先畫、身體蓋住它的上半、兩側直線、最後頂面橢圓。
        # 身體不描邊，否則橫線會切過底面橢圓的中間。
        eh = min(b.w * 0.25, _CYLINDER_CAP)
        top, bot = y + eh / 2, btm - eh / 2
        return [
            Ellipse(Box(x, btm - eh, b.w, eh)),
            Polygon([(x, top), (r, top), (r, bot), (x, bot)], stroke="none"),
            Path([(x, top), (x, bot)], stroke="node_stroke"),
            Path([(r, top), (r, bot)], stroke="node_stroke"),
            Ellipse(Box(x, y, b.w, eh)),
        ]
    s = min(_SLANT, b.w / 4)
    i = min(_HEX_INSET, b.w / 4)
    n = min(b.h / 2, b.w / 4)
    polygons: dict[str, list[Point]] = {
        "diamond": [(cx, y), (r, cy), (cx, btm), (x, cy)],
        "hexagon": [(x + i, y), (r - i, y), (r, cy), (r - i, btm), (x + i, btm), (x, cy)],
        "asym": [(x, y), (r, y), (r, btm), (x, btm), (x + n, cy)],
        "lean_right": [(x + s, y), (r, y), (r - s, btm), (x, btm)],
        "lean_left": [(x, y), (r - s, y), (r, btm), (x + s, btm)],
        "trapezoid": [(x + s, y), (r - s, y), (r, btm), (x, btm)],
        "trapezoid_alt": [(x, y), (r, y), (r - s, btm), (x + s, btm)],
    }
    return [Polygon(polygons[shape])]


def measure_subgraph_titles(chart: Flowchart, measurer: TextMeasurer) -> dict[str, Size]:
    """量每個子圖標題的尺寸，給版面把外框撐到裝得下標題。

    少了這一步，標題比成員寬的子圖會把字畫到框外、甚至畫出圖片邊界被裁掉
    ——包圍盒只算了節點、邊與標籤，標題文字不在版面的世界裡。
    """
    sizes: dict[str, Size] = {}
    stack = list(chart.subgraphs)
    while stack:
        sg = stack.pop()
        if sg.title:
            sizes[sg.id] = measurer.measure(sg.title)
        stack.extend(sg.children)
    return sizes


def _emit_subgraphs(subgraphs: list[Subgraph], boxes: dict[str, Box], items: list[Primitive]) -> None:
    """父框先畫、子框後畫，巢狀時內層才會壓在外層上面。沒有外框的子圖（空的）略過。"""
    for sg in subgraphs:
        box = boxes.get(sg.id)
        if box is not None:
            items.append(Rect(box, fill="frame_fill", stroke="frame", radius=6.0, line="dashed"))
            if sg.title:
                items.append(Text(box.x + 8, box.y + 4, sg.title, "text_muted", "left", "top"))
        _emit_subgraphs(sg.children, boxes, items)


def to_scene(chart: Flowchart, layout: FlowLayout, measurer: TextMeasurer) -> Scene:
    """子圖 -> 邊 -> 節點的順序，節點最後畫才會蓋住線的端點。"""
    items: list[Primitive] = []
    _emit_subgraphs(chart.subgraphs, layout.subgraphs, items)
    for route in layout.edges:
        edge = chart.edges[route.index]
        items.append(Path(
            list(route.points), stroke="edge", line=_EDGE_LINES[edge.style],
            head=edge.head, tail=edge.tail, smooth=True,
        ))
        if edge.label and route.label_center is not None:
            cx, cy = route.label_center
            size = _label_box(measurer.measure(edge.label))
            items.append(Rect(Box(cx - size.w / 2, cy - size.h / 2, size.w, size.h),
                              fill="label_bg", stroke="none"))
            items.append(Text(cx, cy, edge.label))
    for node in chart.nodes.values():
        box = layout.nodes[node.id]
        items.extend(_outline(node.shape, box))
        items.append(Text(box.cx, box.cy, node.label))
    return Scene(layout.width, layout.height, items)
