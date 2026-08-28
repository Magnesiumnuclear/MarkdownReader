"""檔案讀取與 Markdown 轉換。

負責三件事：
1. 安全讀檔——BOM 偵測、多重編碼回退、檔案鎖定重試，並把各種失敗情況
   轉成語意明確的 DocumentError。
2. 把 Markdown 轉成 Qt rich text 能正確呈現的 HTML。
3. 產生歡迎頁與錯誤頁（沿用同一套樣式，避免用彈窗轟炸使用者）。
"""

from __future__ import annotations

import codecs
import html
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime

import markdown

from . import config, styles
from .qt_html import QtRichTextExtension

try:  # Pygments 只影響程式碼高亮，缺少時自動降級為純文字區塊
    import pygments  # noqa: F401

    PYGMENTS_AVAILABLE = True
except ImportError:  # pragma: no cover - 視安裝環境而定
    PYGMENTS_AVAILABLE = False


class DocumentError(Exception):
    """讀取文件失敗，附帶可直接顯示給使用者的說明。"""

    def __init__(self, title: str, detail: str, hint: str = "") -> None:
        super().__init__(f"{title}: {detail}")
        self.title = title
        self.detail = detail
        self.hint = hint


@dataclass(frozen=True)
class DocumentMeta:
    """文件的附帶資訊，供狀態列顯示。"""

    path: str
    encoding: str
    size_bytes: int
    modified: datetime
    char_count: int
    line_count: int

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path) or self.path

    @property
    def size_text(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{self.size_bytes} B"


# --- 讀檔 -------------------------------------------------------------------
# UTF-32 的 BOM 前兩個位元組和 UTF-16 相同，必須先比對 UTF-32。
_BOM_TABLE = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# 無 BOM 時的嘗試順序：先 UTF-8，再繁體中文 Windows 常見編碼，最後保底。
_FALLBACK_ENCODINGS = ("utf-8", "cp950", "big5", "gbk", "utf-16")


def _read_bytes(path: str) -> bytes:
    """讀取原始位元組，並對「檔案暫時無法存取」的情況重試。

    需要重試的原因有二：
    * VS Code 等編輯器在存檔瞬間可能仍持有檔案鎖 -> PermissionError
    * 許多編輯器採「寫暫存檔再改名覆蓋」的原子存檔，該空窗期原路徑會短暫消失
      -> FileNotFoundError
    重試上限為 3 次 / 每次 50ms，總延遲 150ms，不會造成有感卡頓。
    """
    last_error: OSError | None = None
    for attempt in range(config.READ_RETRY_COUNT):
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except (PermissionError, FileNotFoundError) as error:
            last_error = error
            if attempt < config.READ_RETRY_COUNT - 1:
                time.sleep(config.READ_RETRY_DELAY_S)
    assert last_error is not None
    raise last_error


def _decode(raw: bytes) -> tuple[str, str]:
    """把位元組解碼成文字，回傳 (內容, 實際使用的編碼)。"""
    for bom, encoding in _BOM_TABLE:
        if raw.startswith(bom):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                break

    for encoding in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue

    # 保底：latin-1 不會失敗，但可能出現亂碼，因此在編碼名稱標註出來
    return raw.decode("latin-1", errors="replace"), "latin-1（可能不正確）"


def read_text_file(path: str) -> tuple[str, DocumentMeta]:
    """讀取文字檔並回傳內容與附帶資訊；失敗時拋出 DocumentError。"""
    if os.path.isdir(path):
        raise DocumentError(
            "無法開啟資料夾",
            f"指定的路徑是一個資料夾，而不是檔案：\n{path}",
            "請改為指定一個 Markdown 檔案，或按 Ctrl+O 選擇檔案。",
        )

    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        raise DocumentError(
            "找不到檔案",
            f"這個路徑不存在，或檔案已被移動、刪除、更名：\n{path}",
            "請確認路徑是否正確，或按 Ctrl+O 重新選擇檔案。",
        ) from None
    except PermissionError:
        raise DocumentError(
            "沒有讀取權限",
            f"系統拒絕存取這個檔案：\n{path}",
            "檔案可能正被其他程式獨占，或需要較高權限才能讀取。",
        ) from None
    except OSError as error:
        raise DocumentError(
            "讀取檔案時發生錯誤",
            f"{path}\n\n{error.__class__.__name__}: {error}",
            "請確認磁碟或網路位置是否正常。",
        ) from None

    text, encoding = _decode(raw)
    try:
        modified = datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        modified = datetime.now()

    meta = DocumentMeta(
        path=path,
        encoding=encoding,
        size_bytes=len(raw),
        modified=modified,
        char_count=sum(1 for char in text if not char.isspace()),
        line_count=text.count("\n") + 1,
    )
    return text, meta


# --- Markdown 轉換 ----------------------------------------------------------
def _build_converter(theme: str) -> markdown.Markdown:
    extensions: list = ["extra", "sane_lists", "toc"]
    configs: dict = {}
    if PYGMENTS_AVAILABLE:
        extensions.append("codehilite")
        configs["codehilite"] = {
            # noclasses 會把配色直接寫成 inline style，Qt 對這種寫法支援最完整
            "noclasses": True,
            "pygments_style": styles.PYGMENTS_STYLE.get(theme, "default"),
            "guess_lang": False,
        }
    extensions.append(QtRichTextExtension())
    return markdown.Markdown(extensions=extensions, extension_configs=configs)


# 每個主題各留一個轉換器實例重複使用。建立轉換器要載入並串接八個擴充
# （extra、toc、codehilite、本專案的 QtRichTextExtension…），每次渲染都重建
# 是純粹的浪費——主題只有淺色與深色兩種，字典最多兩筆。
#
# 【一定要 reset()】
# Markdown 實例會累積狀態：註腳、參考連結、toc、htmlStash（本專案用來取出
# fenced code 再包成表格的那個）。不重設就重用，第二份文件會帶著第一份的
# 註腳與參考連結，而且 htmlStash 的編號會對不上，程式碼區塊會整段錯位。
_CONVERTERS: dict[str, markdown.Markdown] = {}


def markdown_to_html(text: str, theme: str) -> str:
    """把 Markdown 原始碼轉成 Qt 相容的 HTML 片段。"""
    converter = _CONVERTERS.get(theme)
    if converter is None:
        converter = _build_converter(theme)
        _CONVERTERS[theme] = converter
    else:
        converter.reset()
    return converter.convert(text)


def _page(body: str) -> str:
    """包成完整 HTML 文件（樣式另由 setDefaultStyleSheet 提供）。"""
    return f'<html><head><meta charset="utf-8"></head><body>{body}</body></html>'


def _notice(cell_class: str, body: str) -> str:
    return (
        f'<table class="notice" width="100%" border="0" cellspacing="0" cellpadding="0">'
        f'<tr><td class="{cell_class}">{body}</td></tr></table>'
    )


def render_document(text: str, meta: DocumentMeta, theme: str) -> str:
    """把文件內容轉成可直接餵給 QTextBrowser 的完整 HTML。"""
    body = markdown_to_html(text, theme)
    if meta.size_bytes > config.LARGE_FILE_BYTES:
        body = (
            _notice(
                "warncell",
                f"<b>大型文件（{html.escape(meta.size_text)}）</b>"
                "<br>內容較多，排版與捲動可能較慢。",
            )
            + body
        )
    return _page(body)


# --- 分段渲染 ---------------------------------------------------------------
# 頂層元素掃描用。self-closing 與這些 void 元素不會產生巢狀深度。
_VOID_TAGS = frozenset(
    ("img", "br", "hr", "meta", "input", "link", "col", "area", "base", "wbr")
)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>")


def _top_level_ends(body: str) -> list[int]:
    """回傳 body 裡每個頂層元素結束後的位移（巢狀深度回到 0 的位置）。

    只認標籤的巢狀深度，不解析屬性內容——產生這份 HTML 的是 python-markdown
    的序列化器，屬性值裡的 `<` `>` 都已經轉義，程式碼區塊的內容也是。
    只要掃到深度為負或收尾不為 0（代表這份 HTML 不如預期）就回空清單，
    呼叫端會退回「一次到底」，寧可不分段也不要切出壞掉的片段。
    """
    depth = 0
    ends: list[int] = []
    for match in _TAG_RE.finditer(body):
        closing, name, _attrs, self_closing = match.groups()
        if self_closing or name.lower() in _VOID_TAGS:
            continue
        depth += -1 if closing else 1
        if depth == 0:
            ends.append(match.end())
        elif depth < 0:
            return []
    return [] if depth else ends


def split_for_progressive_render(page: str) -> tuple[str, list[str]]:
    """把整頁 HTML 拆成「首屏」與之後要逐塊附加的片段。

    回傳 (首屏整頁 HTML, [片段, ...])。片段是裸的 body 片段，交給
    QTextCursor.insertHtml 附加。不適合分段時回傳 (原樣, []),
    呼叫端照舊一次 setHtml 到底。

    不分段的情況：找不到 body、掃描結果不平衡、頂層元素太少。
    """
    start = page.find("<body>")
    end = page.rfind("</body>")
    if start < 0 or end < 0 or end <= start:
        return page, []
    start += len("<body>")

    body = page[start:end]
    ends = _top_level_ends(body)
    if len(ends) < config.PROGRESSIVE_MIN_ELEMENTS:
        return page, []

    first = config.PROGRESSIVE_FIRST_ELEMENTS
    step = config.PROGRESSIVE_CHUNK_ELEMENTS
    cuts = [ends[min(first, len(ends)) - 1]]
    index = first
    while index < len(ends):
        index = min(index + step, len(ends))
        cuts.append(ends[index - 1])

    head = page[:start] + body[: cuts[0]] + page[end:]
    chunks = [body[a:b] for a, b in zip(cuts, cuts[1:]) if b > a]
    return head, chunks


def render_error(error: DocumentError, path: str = "") -> str:
    """產生錯誤頁面（顯示在閱讀區內，而不是彈出對話框）。"""
    detail = html.escape(error.detail).replace("\n", "<br>")
    hint = html.escape(error.hint).replace("\n", "<br>")
    body = [f"<h1>{html.escape(error.title)}</h1>"]
    body.append(_notice("noticecell", f'<span class="mono">{detail}</span>'))
    if hint:
        body.append(f'<p class="muted">{hint}</p>')
    body.append(
        '<p class="muted">可用按鍵：<b>Ctrl+O</b> 開啟其他檔案、'
        "<b>F5</b> 重新載入、<b>Ctrl+W</b> 關閉視窗。</p>"
    )
    return _page("".join(body))


def render_welcome() -> str:
    """未指定檔案時顯示的歡迎頁。"""
    rows = [
        ("Ctrl + O", "開啟 Markdown 檔案（開在新分頁）"),
        ("Ctrl + T", "開一個空白分頁"),
        ("Ctrl + W", "關閉目前分頁；只剩一個時關閉視窗"),
        ("Ctrl + Tab / Ctrl + Shift + Tab", "切換到下一個／上一個分頁"),
        ("F5 / Ctrl + R", "重新載入目前檔案"),
        ("Ctrl + F", "在文件中搜尋"),
        ("Ctrl + ,", "開啟設定列"),
        ("Ctrl + D", "切換深色／淺色主題（會鎖定，不再跟隨系統）"),
        ("Ctrl + P", "切換視窗釘選最上層"),
        ("Ctrl + + / - / 0", "放大／縮小／重設字級"),
        ("Alt + 左方向鍵", "回到上一篇文件"),
        ("F11", "切換最大化"),
        ("Esc", "關閉視窗"),
    ]
    table = [
        '<table class="data"><thead><tr><th>快速鍵</th><th>功能</th></tr></thead><tbody>'
    ]
    for key, description in rows:
        table.append(
            f'<tr><td><span class="mono">{html.escape(key)}</span></td>'
            f"<td>{html.escape(description)}</td></tr>"
        )
    table.append("</tbody></table>")

    body = [
        "<h1>Markdown 閱讀器</h1>",
        _notice(
            "noticecell",
            "把 <b>.md</b> 檔案拖曳到這個視窗，或按 <b>Ctrl+O</b> 選擇檔案即可開始閱讀。"
            "<br>設定成 <b>.md</b> 的預設開啟程式後，直接雙擊檔案就會用本程式開啟。",
        ),
        "<h2>快速鍵</h2>",
        "".join(table),
    ]
    if not PYGMENTS_AVAILABLE:
        body.append(
            _notice(
                "warncell",
                "目前環境沒有安裝 <span class='mono'>Pygments</span>，"
                "程式碼區塊會以純文字顯示。",
            )
        )
    return _page("".join(body))
