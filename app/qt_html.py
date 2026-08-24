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
        # 先蒐集再改寫，避免一邊迭代一邊變動樹狀結構
        targets: list[tuple[ET.Element, str, str]] = []
        for element in root.iter():
            if element.tag == "blockquote":
                targets.append((element, "bq", "quotecell"))
            elif element.tag == "pre":
                targets.append((element, "code", "codecell"))

        for element, table_class, cell_class in targets:
            parent = parents.get(element)
            if parent is None:
                continue
            # codehilite 會在 <pre> 外再包一層 <div class="codehilite">，
            # 且把背景色直接寫在 inline style 上，會和儲存格底色打架，故移除。
            if element.tag == "pre":
                element.attrib.pop("style", None)
                if parent.tag == "div" and "codehilite" in (parent.get("class") or ""):
                    grandparent = parents.get(parent)
                    if grandparent is not None:
                        parent.attrib.pop("style", None)
                        parent.attrib.pop("class", None)
                        element, parent = parent, grandparent

            index = list(parent).index(element)
            table, cell = _new_table(table_class, cell_class)
            parent.remove(element)
            cell.append(element)
            parent.insert(index, table)
            parents[table] = parent

        # <hr> 換成一列細橫線表格
        for element in list(root.iter("hr")):
            parent = parents.get(element)
            if parent is None:
                continue
            index = list(parent).index(element)
            table, _cell = _new_table("rule hrule", "rulecell")
            parent.remove(element)
            parent.insert(index, table)
            parents[table] = parent

        # h1 / h2 包進單格表格，靠儲存格的 border-bottom 做出 GitHub 風格底線。
        # 之所以不是「在標題後面另插一條橫線」，是因為 Qt 會讓獨立的橫線區塊
        # 帶上整行行高，和標題之間出現一大段空白；包進儲存格則能用 padding
        # 精準控制底線與文字的距離。
        for element in list(root.iter()):
            if element.tag not in ("h1", "h2"):
                continue
            parent = parents.get(element)
            if parent is None:
                continue
            index = list(parent).index(element)
            table, cell = _new_table("headrule", "headcell")
            parent.remove(element)
            cell.append(element)
            parent.insert(index, table)
            parents[table] = parent

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
