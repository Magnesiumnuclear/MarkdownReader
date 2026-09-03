"""序列圖子集：解析 sequenceDiagram 原始碼，並自己排版面、組成 Scene。

流程圖的版面是獨立模組（layout.py），序列圖沒有這麼拆：參與者橫排、訊息一列
一列往下，版面本身就是「走一遍項目、y 往下加」，拆出去只會多一層。所以這裡是
解析（parse_sequence）＋版面與組場景（to_scene）兩段。

解析採「一行一敘述」：每行去掉頭尾空白後依序試各種敘述的正則，沒有任何一種
吃得下就報 ERR_SYNTAX。區塊（loop/alt/…）與 box 用同一個堆疊配對 end；box 在堆疊裡
是空殼（block=None），純粹為了讓它的 end 有東西可配，成員照常落到外層。

版面（全部是邏輯像素，文字尺寸交給 TextMeasurer）：
- 欄位：先量每個參與者的標籤決定欄寬，再掃一遍所有訊息與便條，把相鄰欄的間距
  撐到文字放得下。跨多欄的訊息把不足的寬度平均攤到經過的每個間距；依跨距由短
  到長套用，短的先撐開、長的通常就夠了。
- 列：每則訊息／便條佔一列，區塊多一列標籤與底部留白；啟用框要到 deactivate 才
  知道高度，先收在另一個清單，最後排在訊息之前，免得蓋住箭頭端點。
- 最後對所有原語取包圍盒，整體平移到留 16px 邊界——便條放在最左參與者左邊、
  自訊息的文字掛在最右參與者右邊，都會凸出欄位範圍，用包圍盒處理最省事。

已知限制：
- `-)`／`--)` 的開放箭頭在 Scene 裡沒有對應的端點記號，畫成一般實心箭頭。
- 不支援 `;` 分隔同一行多個敘述、`create`/`destroy`、`<<->>` 雙向箭頭、`par over`，
  也不理 `autonumber` 的起始值與步進（有 autonumber 就從 1 開始編）。
- `box` 的框與標題不畫、`rect` 的顏色不用（跟閱讀器配色走）。
- 參與者順序是「首次提到」的順序（宣告或訊息都算），和 Mermaid 一致；先用後宣告
  的參與者留在首次出現的位置、只補上標籤與 actor 旗標。
- deactivate 沒有對應的 activate 時直接略過（Mermaid 會報錯）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

from .model import (
    ERR_SYNTAX,
    ERR_TOO_DEEP,
    ERR_TOO_LARGE,
    ERR_UNCLOSED,
    ERR_UNEXPECTED_END,
    ERR_UNKNOWN_REF,
    Activate,
    Align,
    Block,
    Box,
    Deactivate,
    EdgeEnd,
    Ellipse,
    LineKind,
    Message,
    MermaidError,
    Note,
    Participant,
    Path,
    Primitive,
    Rect,
    Scene,
    Section,
    SeqHead,
    SeqItem,
    SequenceDiagram,
    Text,
    TextMeasurer,
    VAlign,
)

# --- 解析 -------------------------------------------------------------------
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HEADER = re.compile(r"^sequenceDiagram$")
_PARTICIPANT = re.compile(r"^(participant|actor)\s+(.+?)(?:\s+as\s+(.+))?$")
# 箭頭的候選要長的排前面：`-->>` 若排在 `->>` 之後，正則會先咬到短的那個
_MESSAGE = re.compile(
    r"^([^<>\-:,;]+?)\s*(-->>|->>|-->|->|--x|-x|--\)|-\))\s*([+-]?)\s*([^<>\-:,;+]+?)\s*:\s?(.*)$"
)
_NOTE = re.compile(r"^note\s+(left of|right of|over)\s+([^:]+?)\s*:\s?(.*)$", re.IGNORECASE)
_ACTIVATE = re.compile(r"^(activate|deactivate)\s+(.+)$")
_BLOCK = re.compile(r"^(loop|alt|opt|par|critical|break|rect)(?:\s+(.*))?$")
_SECTION = re.compile(r"^(else|and|option)(?:\s+(.*))?$")
_BOX = re.compile(r"^box(?:\s|$)")
_IGNORED = re.compile(r"^(?:links|link|properties)\s")
_TITLE = re.compile(r"^title(?::|\s|$)\s*(.*)$")
_AUTONUMBER = re.compile(r"^autonumber(?:\s+(.*))?$")

_ARROWS: dict[str, tuple[LineKind, SeqHead]] = {
    "->": ("solid", "none"), "-->": ("dotted", "none"),
    "->>": ("solid", "arrow"), "-->>": ("dotted", "arrow"),
    "-x": ("solid", "cross"), "--x": ("dotted", "cross"),
    "-)": ("solid", "open"), "--)": ("dotted", "open"),
}
_SECTION_OF = {"else": "alt", "and": "par", "option": "critical"}


def _unbr(text: str) -> str:
    """<br> 系列換成換行；每行各自去頭尾空白，免得 `a <br> b` 留下多餘空格。"""
    return "\n".join(part.strip() for part in _BR.split(text))


@dataclass
class _Frame:
    line: int                 # 開始行，少了 end 時報這一行
    block: Block | None       # None 代表 box：只為了配對 end，成員落到外層


class _Parser:
    def __init__(self, max_messages: int, max_depth: int) -> None:
        self.limit = max_messages
        self.max_depth = max_depth
        self.diagram = SequenceDiagram()
        self.participants: dict[str, Participant] = {}   # 首次提到的順序
        self.stack: list[_Frame] = []
        self.count = 0

    def _items(self) -> list[SeqItem]:
        """目前敘述該放進哪個清單：最近一個真正的區塊的最後一節，沒有就是頂層。"""
        for frame in reversed(self.stack):
            if frame.block is not None:
                return frame.block.sections[-1].items
        return self.diagram.items

    def _ref(self, no: int, pid: str) -> str:
        pid = pid.strip()
        if not pid:
            raise MermaidError(no, ERR_SYNTAX, token=pid)
        self.participants.setdefault(pid, Participant(pid, pid))
        return pid

    def feed(self, no: int, line: str) -> None:
        # 訊息排在區塊關鍵字之前：`opt`/`par` 之類的字也可能是參與者 id
        if m := _PARTICIPANT.match(line):
            kind, pid, label = m.groups()
            participant = self.participants.setdefault(pid, Participant(pid, pid))
            if label:
                participant.label = _unbr(label)
            participant.actor = kind == "actor"
        elif m := _MESSAGE.match(line):
            self._message(no, m)
        elif m := _NOTE.match(line):
            position, ids, text = m.groups()
            refs = [self._ref(no, pid) for pid in ids.split(",")]
            self._items().append(Note(position.lower().split()[0], refs, _unbr(text)))
        elif m := _ACTIVATE.match(line):
            pid = self._ref(no, m.group(2))
            self._items().append(Activate(pid) if m.group(1) == "activate" else Deactivate(pid))
        elif m := _BLOCK.match(line):
            kind, label = m.groups()
            # 深度要在這裡擋：版面是逐層遞迴的，太深會打穿直譯器（同 flowchart）
            if len(self.stack) >= self.max_depth:
                raise MermaidError(no, ERR_TOO_DEEP, limit=self.max_depth)
            block = Block(kind, [Section("" if kind == "rect" else _unbr(label or ""))])
            self._items().append(block)
            self.stack.append(_Frame(no, block))
        elif m := _SECTION.match(line):
            word, label = m.groups()
            top = self.stack[-1].block if self.stack else None
            if top is None or top.kind != _SECTION_OF[word]:
                raise MermaidError(no, ERR_SYNTAX, token=line[:40])
            top.sections.append(Section(_unbr(label or "")))
        elif line == "end":
            if not self.stack:
                raise MermaidError(no, ERR_UNEXPECTED_END)
            self.stack.pop()
        elif _BOX.match(line):
            self.stack.append(_Frame(no, None))
        elif m := _TITLE.match(line):
            self.diagram.title = _unbr(m.group(1))
        elif m := _AUTONUMBER.match(line):
            self.diagram.autonumber = (m.group(1) or "").strip() != "off"
        elif not _IGNORED.match(line):
            raise MermaidError(no, ERR_SYNTAX, token=line[:40])

    def _message(self, no: int, m: re.Match[str]) -> None:
        source, arrow, suffix, target, text = m.groups()
        self.count += 1
        if self.count > self.limit:
            raise MermaidError(no, ERR_TOO_LARGE, limit=self.limit)
        line, head = _ARROWS[arrow]
        self._items().append(Message(
            self._ref(no, source), self._ref(no, target), _unbr(text), line, head,
            activate=suffix == "+", deactivate=suffix == "-",
        ))

    def finish(self) -> SequenceDiagram:
        if self.stack:
            raise MermaidError(self.stack[-1].line, ERR_UNCLOSED)
        self.diagram.participants = list(self.participants.values())
        return self.diagram


def parse_sequence(source: str, *, max_messages: int = 400,
                   max_depth: int = 60) -> SequenceDiagram:
    """把 sequenceDiagram 原始碼解析成模型。行號 1-based，相對於 source 的第一行。"""
    parser = _Parser(max_messages, max_depth)
    seen_header = False
    for no, raw in enumerate(source.split("\n"), 1):
        line = raw.strip()
        if line.endswith(";"):
            line = line[:-1].rstrip()
        if not line or line.startswith("%%"):
            continue
        if seen_header:
            parser.feed(no, line)
        elif _HEADER.match(line):
            seen_header = True
        else:
            raise MermaidError(no, ERR_SYNTAX, token=line[:40])
    if not seen_header:
        raise MermaidError(1, ERR_SYNTAX, token="")
    return parser.finish()


# --- 版面與組場景 -----------------------------------------------------------
_MARGIN = 16.0
_COL_MIN = 100.0
_GAP_MIN = 40.0
_HOOK = 30.0          # 自訊息往右勾出去的距離
_ACT_W = 10.0         # 啟用框寬；巢狀每層再往右挪 _ACT_STEP
_ACT_STEP = 6.0
_BLOCK_HEAD = 28.0    # 區塊標籤列／分節列的高度
_BLOCK_PAD = 12.0     # 區塊底部留白
_HEAD: dict[SeqHead, EdgeEnd] = {"none": "none", "arrow": "arrow", "open": "arrow", "cross": "cross"}
_SEP_WORD = {"alt": "else", "par": "and", "critical": "option"}


def _leaves(items: list[SeqItem]) -> Iterator[SeqItem]:
    """攤平巢狀區塊，依文件順序吐出訊息／便條／啟用敘述。"""
    for item in items:
        if isinstance(item, Block):
            for section in item.sections:
                yield from _leaves(section.items)
        else:
            yield item


def _refs(item: SeqItem) -> list[str]:
    if isinstance(item, Message):
        return [item.source, item.target]
    if isinstance(item, Note):
        return list(item.participants)
    if isinstance(item, (Activate, Deactivate)):
        return [item.participant]
    return []


def _shift(prim: Primitive, dx: float, dy: float) -> None:
    if isinstance(prim, (Rect, Ellipse)):
        prim.box.x += dx
        prim.box.y += dy
    elif isinstance(prim, Path):
        prim.points = [(x + dx, y + dy) for x, y in prim.points]
    else:
        prim.x += dx
        prim.y += dy


class _Layout:
    def __init__(self, diagram: SequenceDiagram, measurer: TextMeasurer) -> None:
        self.d = diagram
        self.m = measurer
        self.col = {p.id: i for i, p in enumerate(diagram.participants)}
        self.w: list[float] = []          # 欄寬
        self.x: list[float] = []          # 欄中心（生命線的 x）
        self.header_h = 0.0
        # 五個清單最後照這個順序接起來，就是繪圖的疊放次序
        self.chrome: list[Primitive] = []   # 標題、頂底兩排參與者
        self.lines: list[Primitive] = []    # 生命線
        self.acts: list[Primitive] = []     # 啟用框
        self.frames: list[Primitive] = []   # 區塊框、標籤、分節線
        self.body: list[Primitive] = []     # 訊息、便條
        self.active: dict[str, list[tuple[float, int]]] = {}   # 參與者 -> [(起點 y, 巢狀深度)]
        self.number = 0
        self.y = 0.0
        self.last_y = 0.0                 # 最近一條訊息線／便條底邊的 y：activate/deactivate 敘述貼在這裡

    def _index(self, pid: str) -> int:
        try:
            return self.col[pid]
        except KeyError:
            raise MermaidError(0, ERR_UNKNOWN_REF, token=pid) from None

    def _prefix(self, number: int) -> str:
        return f"{number}. " if self.d.autonumber else ""

    def _text_width(self, text: str, number: int) -> float:
        prefix = self._prefix(number)
        width = self.m.measure(text).w
        return width + (self.m.measure(prefix, scale=0.85).w if prefix else 0.0)

    # --- 欄位 ---
    def _columns(self) -> None:
        """決定欄寬、欄距與每欄的 x。

        needs 收集「第 i 欄到第 j 欄的中心距至少要多少」：相鄰訊息的文字要放得下、
        自訊息的文字別壓到右邊那條生命線、便條別壓到隔壁。邊緣參與者外側的
        便條（i < 0 或 j 超出）不撐間距，留給最後的包圍盒往外擴。
        """
        heights: list[float] = []
        for p in self.d.participants:
            size = self.m.measure(p.label)
            self.w.append(max(size.w + 24, _COL_MIN))
            heights.append(size.h + (38 if p.actor else 16))
        self.header_h = max(heights, default=self.m.line_height + 16)
        count = len(self.w)
        gaps = [_GAP_MIN] * max(count - 1, 0)
        needs: list[tuple[int, int, float]] = []
        number = 0
        for item in _leaves(self.d.items):
            if isinstance(item, Message):
                number += 1
                width = self._text_width(item.text, number)
                i, j = self._index(item.source), self._index(item.target)
                if i == j:
                    needs.append((i, i + 1, width + _HOOK + 16))
                else:
                    needs.append((min(i, j), max(i, j), width + 24))
            elif isinstance(item, Note):
                width = self.m.measure(item.text).w + 24
                cols = sorted(self._index(pid) for pid in item.participants)
                if item.position == "left":
                    needs.append((cols[0] - 1, cols[0], width + 20))
                elif item.position == "right":
                    needs.append((cols[0], cols[0] + 1, width + 20))
                elif cols[0] != cols[-1]:
                    needs.append((cols[0], cols[-1], width))
                else:
                    needs += [(cols[0] - 1, cols[0], width / 2 + 8), (cols[0], cols[0] + 1, width / 2 + 8)]
        for i, j, distance in sorted(needs, key=lambda need: need[1] - need[0]):
            if i < 0 or j >= count:
                continue
            current = self.w[i] / 2 + self.w[j] / 2 + sum(gaps[i:j]) + sum(self.w[i + 1:j])
            if distance > current:
                for k in range(i, j):
                    gaps[k] += (distance - current) / (j - i)
        for i in range(count):
            previous = self.x[-1] + self.w[i - 1] / 2 + gaps[i - 1] if i else 0.0
            self.x.append(previous + self.w[i] / 2)

    # --- 參與者列 ---
    def _participant_row(self, top: float, at_top: bool) -> None:
        """頂／底各一排。頂排貼齊列底（方框和小人的腳同高），底排貼齊列頂。"""
        for p, x, w in zip(self.d.participants, self.x, self.w):
            label_h = self.m.measure(p.label).h
            h = label_h + (38 if p.actor else 16)
            y0 = top + self.header_h - h if at_top else top
            if p.actor:
                self.chrome += [
                    Ellipse(Box(x - 6, y0, 12, 12)),
                    Path([(x, y0 + 12), (x, y0 + 24)], stroke="node_stroke"),
                    Path([(x - 8, y0 + 16), (x + 8, y0 + 16)], stroke="node_stroke"),
                    Path([(x - 7, y0 + 34), (x, y0 + 24), (x + 7, y0 + 34)], stroke="node_stroke"),
                    Text(x, y0 + 38, p.label, valign="top"),
                ]
            else:
                self.chrome += [
                    Rect(Box(x - w / 2, y0, w, h), fill="header_fill", radius=4),
                    Text(x, y0 + h / 2, p.label),
                ]

    # --- 逐項排列 ---
    def _lay_items(self, items: list[SeqItem], depth: int) -> None:
        for item in items:
            if isinstance(item, Message):
                self._message(item)
            elif isinstance(item, Note):
                self._note(item)
            elif isinstance(item, Activate):
                self._activate(item.participant, self.last_y)
            elif isinstance(item, Deactivate):
                self._deactivate(item.participant, self.last_y)
            else:
                self._block(item, depth)

    def _label(self, x: float, y: float, text: str, align: Align, valign: VAlign) -> None:
        """訊息文字（含 autonumber 的序號小字）＋蓋住線的底色。x/y 是整塊文字的錨點。"""
        prefix = self._prefix(self.number)
        prefix_w = self.m.measure(prefix, scale=0.85).w if prefix else 0.0
        size = self.m.measure(text)
        total = prefix_w + size.w
        left = x - total / 2 if align == "center" else x
        top = y - size.h if valign == "bottom" else y - size.h / 2
        self.body.append(Rect(Box(left - 4, top - 2, total + 8, size.h + 4), fill="label_bg", stroke="none"))
        if prefix:
            self.body.append(Text(left, y, prefix, role="text_muted", align="left", valign=valign, scale=0.85))
        self.body.append(Text(left + prefix_w, y, text, align="left", valign=valign))

    def _message(self, msg: Message) -> None:
        self.number += 1
        xs, xt = self.x[self._index(msg.source)], self.x[self._index(msg.target)]
        text_h = self.m.measure(msg.text).h
        style = "dotted" if msg.line == "dotted" else "solid"
        head = _HEAD[msg.head]
        if xs == xt:
            # 自訊息：往右勾出去、下一列勾回來，文字掛在勾的右邊
            y0, y1 = self.y + 8, self.y + text_h + 16
            self.body.append(Path([(xs, y0), (xs + _HOOK, y0), (xs + _HOOK, y1), (xs, y1)], line=style, head=head))
            self._label(xs + _HOOK + 8, (y0 + y1) / 2, msg.text, "left", "middle")
            self.y = y1 + 8
        else:
            y0 = y1 = self.y + text_h + 12
            self._label((xs + xt) / 2, y0 - 4, msg.text, "center", "bottom")
            self.body.append(Path([(xs, y0), (xt, y0)], line=style, head=head))
            self.y = y0 + 4
        self.last_y = y1
        if msg.activate:
            self._activate(msg.target, y0)
        if msg.deactivate:
            self._deactivate(msg.source, y1)

    def _note(self, note: Note) -> None:
        size = self.m.measure(note.text)
        w, h = size.w + 24, size.h + 12
        cols = [self._index(pid) for pid in note.participants]
        if note.position == "over":
            x0, x1 = min(self.x[c] for c in cols), max(self.x[c] for c in cols)
            w = max(x1 - x0, w)
            left = (x0 + x1) / 2 - w / 2
        elif note.position == "left":
            left = self.x[cols[0]] - 12 - w
        else:
            left = self.x[cols[0]] + 12
        top = self.y + 4
        self.body += [
            Rect(Box(left, top, w, h), fill="note_fill", stroke="note_stroke", radius=2),
            Text(left + w / 2, top + h / 2, note.text),
        ]
        # 便條也推進錨點：activate 接在訊息後、deactivate 接在便條後，框才有高度
        self.last_y = top + h
        self.y = self.last_y + 4

    def _activate(self, pid: str, y: float) -> None:
        stack = self.active.setdefault(pid, [])
        stack.append((y, len(stack)))

    def _deactivate(self, pid: str, y: float) -> None:
        stack = self.active.get(pid)
        if not stack:
            return
        start, depth = stack.pop()
        x = self.x[self._index(pid)] - _ACT_W / 2 + _ACT_STEP * depth
        self.acts.append(Rect(Box(x, start, _ACT_W, y - start)))

    def _span(self, block: Block, depth: int) -> tuple[float, float]:
        """區塊框的左右邊：涉及參與者的欄外緣；巢狀每層往內縮 6px，邊線才不會疊在一起。"""
        cols = sorted({
            self._index(pid)
            for section in block.sections
            for item in _leaves(section.items)
            for pid in _refs(item)
        }) or list(range(len(self.x)))
        if not cols:
            return 0.0, 120.0   # 連參與者都沒有的圖：只剩標籤，給個固定寬
        inset = 6.0 * depth
        return (
            self.x[cols[0]] - self.w[cols[0]] / 2 + inset,
            self.x[cols[-1]] + self.w[cols[-1]] / 2 - inset,
        )

    def _block(self, block: Block, depth: int) -> None:
        left, right = self._span(block, depth)
        top = self.y
        first = block.sections[0]
        tag = f"{block.kind} {first.label}".strip()
        size = self.m.measure(tag)
        self.frames += [
            Rect(Box(left, top, size.w + 12, size.h + 6), fill="header_fill", stroke="frame"),
            Text(left + 6, top + 3 + size.h / 2, tag, align="left"),
        ]
        self.y += _BLOCK_HEAD
        self._lay_items(first.items, depth + 1)
        word = _SEP_WORD.get(block.kind, "")
        for section in block.sections[1:]:
            self.frames += [
                Path([(left, self.y), (right, self.y)], stroke="frame", line="dashed"),
                Text(left + 8, self.y + 6, f"{word} {section.label}".strip(),
                     role="text_muted", align="left", valign="top"),
            ]
            self.y += _BLOCK_HEAD
            self._lay_items(section.items, depth + 1)
        self.y += _BLOCK_PAD
        self.frames.append(Rect(Box(left, top, right - left, self.y - top), fill="none", stroke="frame", radius=2))

    # --- 收尾 ---
    def _bbox(self, prim: Primitive) -> tuple[float, float, float, float]:
        if isinstance(prim, (Rect, Ellipse)):
            return prim.box.x, prim.box.y, prim.box.right, prim.box.bottom
        if isinstance(prim, Path):
            xs, ys = [p[0] for p in prim.points], [p[1] for p in prim.points]
            return min(xs), min(ys), max(xs), max(ys)
        size = self.m.measure(prim.text, prim.mono, prim.scale)
        x0 = prim.x - {"left": 0.0, "center": size.w / 2, "right": size.w}[prim.align]
        y0 = prim.y - {"top": 0.0, "middle": size.h / 2, "bottom": size.h}[prim.valign]
        return x0, y0, x0 + size.w, y0 + size.h

    def build(self) -> Scene:
        self._columns()
        if self.d.title:
            center = (self.x[0] - self.w[0] / 2 + self.x[-1] + self.w[-1] / 2) / 2 if self.x else 0.0
            title_h = self.m.measure(self.d.title, scale=1.1).h
            self.chrome.append(Text(center, self.y + title_h / 2, self.d.title, scale=1.1))
            self.y += title_h + 8
        self._participant_row(self.y, at_top=True)
        self.y += self.header_h
        lifeline_top = self.last_y = self.y
        self.y += 12
        self._lay_items(self.d.items, 0)
        self.y += 8
        for pid in list(self.active):   # 到最後還開著的啟用框，收在生命線底
            while self.active[pid]:
                self._deactivate(pid, self.y)
        self._participant_row(self.y, at_top=False)
        self.lines += [Path([(x, lifeline_top), (x, self.y)], line="dashed") for x in self.x]
        items = self.chrome + self.lines + self.acts + self.frames + self.body
        boxes = [self._bbox(prim) for prim in items]
        x0 = min((b[0] for b in boxes), default=0.0)
        y0 = min((b[1] for b in boxes), default=0.0)
        x1 = max((b[2] for b in boxes), default=0.0)
        y1 = max((b[3] for b in boxes), default=0.0)
        for prim in items:
            _shift(prim, _MARGIN - x0, _MARGIN - y0)
        return Scene(x1 - x0 + 2 * _MARGIN, y1 - y0 + 2 * _MARGIN, items)


def to_scene(diagram: SequenceDiagram, measurer: TextMeasurer) -> Scene:
    """排版面並組成 Scene。結果只跟 diagram 與量尺有關，同樣輸入永遠得到同樣的場景。"""
    return _Layout(diagram, measurer).build()
