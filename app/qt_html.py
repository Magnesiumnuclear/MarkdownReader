"""把標準 Markdown 輸出改寫成 Qt rich text 能正確呈現的 HTML。

Qt 的 QTextDocument 只支援 CSS 2.1 子集，實測結果：
  * blockquote / pre / div / h1 / hr 這類「區塊元素」的 border 與 padding 完全無效
  * 只有「表格儲存格」的 border / padding / background 可用
  * <input type="checkbox"> 會被直接丟棄

因此本模組提供一個 markdown 擴充，在 ElementTree 階段把結構安全地改寫成
Qt 認得的等效結構（而不是用正規表達式改字串，以免誤傷程式碼區塊的內容）：

  <blockquote>  -> <table class="bq">   <tr><td class="quotecell">...</td></tr></table>
  <pre>         -> <table class="code">  <tr><td class="codecell">...</td></tr></table>
  <hr>          -> <table class="rule"> <tr><td class="rulecell"></td></tr></table>
  h1 / h2 後方   -> 追加一列 table.hrule 當作 GitHub 風格的標題底線
  <table>       -> 加上 class="data" 以便和上述包裝用表格區分樣式
  - [ ] / - [x] -> <img src="mdres:checkbox-off|on">（由 viewer 以 QPainter 繪製）
  標題 id       -> 於標題開頭插入 <a name="id"> 讓 #錨點 連結可以跳轉
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from markdown.extensions import Extension
from markdown.inlinepatterns import SimpleTagInlineProcessor
from markdown.treeprocessors import Treeprocessor

# 任務清單語法：位於清單項目開頭的 [ ] 或 [x]
_TASK_RE = re.compile(r"^\[([ xX])\]\s+")

# codehilite 產生的外層 div 會把底色寫死在 inline style 上，深色主題下會突兀，
# 這兩個樣式只出現在最外層的開頭標籤，內容本身已 HTML 轉義，不會被誤傷。
_CODEHILITE_DIV_RE = re.compile(r'^<div class="codehilite"[^>]*>')
_PRE_OPEN_RE = re.compile(r"<pre[^>]*>")

# 供 viewer.loadResource() 辨識的自訂資源協定
CHECKBOX_SCHEME = "mdres"
CHECKBOX_ON = f"{CHECKBOX_SCHEME}:checkbox-on"
CHECKBOX_OFF = f"{CHECKBOX_SCHEME}:checkbox-off"


def _new_table(table_class: str, cell_class: str) -> tuple[ET.Element, ET.Element]:
    """建立單格表格，回傳 (table, td)。

    cellspacing/cellpadding/border 用 HTML 屬性明確歸零，避免 Qt 套用預設值，
    實際外觀一律交給 styles.py 的 CSS 控制。
    """
    table = ET.Element(
        "table",
        {
            "class": table_class,
            "width": "100%",
            "border": "0",
            "cellspacing": "0",
            "cellpadding": "0",
        },
    )
    row = ET.SubElement(table, "tr")
    cell = ET.SubElement(row, "td", {"class": cell_class})
    return table, cell


class QtRichTextTreeprocessor(Treeprocessor):
    """把文件樹改寫成 Qt 相容結構。"""

    def run(self, root: ET.Element) -> ET.Element:
        # ElementTree 沒有 parent 指標，先建對照表再改寫（標準作法）
        parents = {child: parent for parent in root.iter() for child in parent}

        self._convert_task_items(root)
        self._tag_image_paragraphs(root)
        self._convert_blocks(root, parents)
        self._mark_data_tables(root)
        self._add_heading_anchors(root)
        self._wrap_stashed_code_blocks()
        return root

    # -- 程式碼區塊（暫存在 htmlStash 的原始 HTML） --------------------------
    def _wrap_stashed_code_blocks(self) -> None:
        """包裝 fenced_code / codehilite 產生的程式碼區塊。

        這兩個擴充是在前處理階段就把高亮後的 HTML 丟進 md.htmlStash，
        文件樹裡只留下佔位符，因此 <pre> 不會出現在 ElementTree 中，
        必須直接對暫存區的字串做包裝。這裡只在「整個區塊的最外層」加上
        表格與移除 inline 底色，不解析區塊內部，因此不會動到程式碼內容。
        """
        stash = getattr(self.md, "htmlStash", None)
        if stash is None:
            return

        for index, block in enumerate(stash.rawHtmlBlocks):
            if not isinstance(block, str):
                continue
            stripped = block.strip()
            if not (
                stripped.startswith('<div class="codehilite"')
                or stripped.startswith("<pre")
            ):
                continue

            # 拆掉 codehilite 的外層 div（含寫死的底色），儲存格本身就是容器
            cleaned = stripped
            if _CODEHILITE_DIV_RE.match(cleaned):
                cleaned = _CODEHILITE_DIV_RE.sub("", cleaned, count=1)
                if cleaned.endswith("</div>"):
                    cleaned = cleaned[: -len("</div>")]
            cleaned = _PRE_OPEN_RE.sub("<pre>", cleaned, count=1)
            # 去掉結尾多餘的換行，否則程式碼區塊底部會多出一整行空白
            cleaned = cleaned.replace("\n</code></pre>", "</code></pre>")
            cleaned = cleaned.replace("\n</pre>", "</pre>")
            stash.rawHtmlBlocks[index] = (
                '<table class="code" width="100%" border="0" '
                'cellspacing="0" cellpadding="0">'
                '<tr><td class="codecell">' + cleaned + "</td></tr></table>"
            )

    # -- 任務清單 ------------------------------------------------------------
    def _convert_task_items(self, root: ET.Element) -> None:
        for item in list(root.iter("li")):
            text = item.text or ""
            match = _TASK_RE.match(text)
            if not match:
                continue
            checked = match.group(1).lower() == "x"
            remainder = text[match.end() :]

            box = ET.Element(
                "img",
                {
                    "src": CHECKBOX_ON if checked else CHECKBOX_OFF,
                    "class": "taskbox",
                },
            )
            # li.text 會排在第一個子元素之前，因此把剩餘文字移到 img 的 tail
            item.text = ""
            box.tail = remainder
            item.insert(0, box)

    # -- 圖片段落 ------------------------------------------------------------
    def _tag_image_paragraphs(self, root: ET.Element) -> None:
        """替「只有一張圖片」的段落加上 class，讓樣式表把行高調回 100%。

        Qt 會把段落的行高倍率也套用到圖片所在的行框上，例如行高 160%、
        圖高 214px 時，該行會佔掉 342px，圖片下方就多出一大片空白。
        """
        for paragraph in root.iter("p"):
            children = list(paragraph)
            if len(children) != 1 or children[0].tag != "img":
                continue
            if (paragraph.text or "").strip() or (children[0].tail or "").strip():
                continue
            classes = (paragraph.get("class") or "").split()
            classes.append("imgblock")
            paragraph.set("class", " ".join(classes).strip())

    # -- 區塊改寫 ------------------------------------------------------------
    def _convert_blocks(self, root: ET.Element, parents: dict) -> None:
        """把 blockquote / pre / hr / h1 / h2 換成 Qt 認得的單格表格。

        【為什麼是「先排計畫、再依父節點一次換完」】
        直覺寫法是走到一個就 `list(parent).index(e)` 找位置、`remove` 再
        `insert`。這三個操作每一個都要掃過父節點的整份子清單，而 Markdown
        文件的區塊多半都是 root 的直接子節點——一份 N 個區塊、其中 M 個要換
        的文件就是 O(N x M)。標題很多的文件（每個 h1/h2 都要換）M 逼近 N，
        直接退化成 O(n^2)：實測 3000 個標題的文件，光這一段就要好幾秒。

        改法是把「哪個節點要換成哪個表格」先蒐集起來，再對每個父節點走一次
        子清單、用 `parent[index] = table` 就地替換（O(1)，不必 remove/insert
        搬動後面的元素）。總成本降到 O(N)。
        """
        # 蒐集階段完全不動樹：先走一次就把四種目標都收齊，也省下原本的四趟遍歷
        specs: list[tuple[ET.Element, str, str, bool]] = []
        for element in root.iter():
            tag = element.tag
            if tag == "blockquote":
                specs.append((element, "bq", "quotecell", True))
            elif tag == "pre":
                specs.append((element, "code", "codecell", True))
            elif tag == "hr":
                # hr 本身丟棄，只留下那條細橫線表格
                specs.append((element, "rule hrule", "rulecell", False))
            elif tag in ("h1", "h2"):
                # 包進單格表格，靠儲存格的 border-bottom 做出 GitHub 風格底線。
                # 不是「在標題後面另插一條橫線」，因為 Qt 會讓獨立的橫線區塊帶上
                # 整行行高，和標題之間出現一大段空白；包進儲存格才能用 padding
                # 精準控制底線與文字的距離。
                specs.append((element, "headrule", "headcell", True))

        # parent -> {要被換掉的子節點: 換上去的表格}
        replacements: dict[ET.Element, dict[ET.Element, ET.Element]] = {}

        for element, table_class, cell_class, wrap in specs:
            parent = parents.get(element)
            if parent is None:
                continue
            node = element
            # codehilite 會在 <pre> 外再包一層 <div class="codehilite">，
            # 且把背景色直接寫在 inline style 上，會和儲存格底色打架，故移除；
            # 這種情況要換掉的是那層 div，不是 pre 本身。
            if element.tag == "pre":
                element.attrib.pop("style", None)
                if parent.tag == "div" and "codehilite" in (parent.get("class") or ""):
                    grandparent = parents.get(parent)
                    if grandparent is not None:
                        parent.attrib.pop("style", None)
                        parent.attrib.pop("class", None)
                        node, parent = parent, grandparent

            table, cell = _new_table(table_class, cell_class)
            if wrap:
                # 此刻 node 同時掛在 cell 與 parent 底下；下面的就地替換會把它
                # 從 parent 拿掉。ElementTree 沒有 parent 指標，這樣是安全的。
                cell.append(node)
            replacements.setdefault(parent, {})[node] = table
            parents[table] = parent

        # 套用階段：每個父節點只走一次子清單，就地替換不搬動其他元素。
        # 只做替換、不增刪，所以 index 在整趟迴圈中都保持有效。
        for parent, mapping in replacements.items():
            for index, child in enumerate(list(parent)):
                table = mapping.get(child)
                if table is not None:
                    parent[index] = table

    # -- 資料表格 ------------------------------------------------------------
    def _mark_data_tables(self, root: ET.Element) -> None:
        wrappers = {"bq", "code", "rule", "hrule", "headrule"}
        for table in root.iter("table"):
            classes = (table.get("class") or "").split()
            if not wrappers & set(classes):
                classes.append("data")
                table.set("class", " ".join(classes).strip())

    # -- 標題錨點 ------------------------------------------------------------
    def _add_heading_anchors(self, root: ET.Element) -> None:
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            for heading in root.iter(tag):
                anchor_id = heading.get("id")
                if not anchor_id:
                    continue
                anchor = ET.Element("a", {"name": anchor_id})
                anchor.tail = heading.text or ""
                heading.text = ""
                heading.insert(0, anchor)


class QtRichTextExtension(Extension):
    """註冊 QtRichTextTreeprocessor。

    priority 設為 1，確保在 codehilite(30)、inline(20)、prettify(10)、toc(5)
    等內建處理器之後才執行，改寫的對象才會是最終結構。
    """

    def extendMarkdown(self, md) -> None:  # noqa: N802 (markdown 套件的命名慣例)
        # python-markdown 的 extra 不含 GitHub 的 ~~刪除線~~，這裡補上
        md.inlinePatterns.register(
            SimpleTagInlineProcessor(r"()~~(.*?)~~", "del"), "strikethrough", 100
        )
        md.treeprocessors.register(
            QtRichTextTreeprocessor(md), "qt_rich_text", 1
        )
        md.registerExtension(self)


def makeExtension(**kwargs):  # noqa: N802 (markdown 套件的工廠函式慣例)
    return QtRichTextExtension(**kwargs)
