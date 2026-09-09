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
from .language import t
from .qt_html import QtRichTextExtension

try:  # Pygments 只影響程式碼高亮，缺少時自動降級為純文字區塊
    import pygments  # noqa: F401

    PYGMENTS_AVAILABLE = True
except ImportError:  # pragma: no cover - 視安裝環境而定
    PYGMENTS_AVAILABLE = False


class DocumentError(Exception):
    """讀取文件失敗，附帶可顯示給使用者的說明。

    【存的是翻譯鍵，不是翻好的字串】
    錯誤物件會被分頁一直留著（DocumentTab.error），語言切換後 render_error
    會拿它重畫一次。存翻好的字串的話，那一頁會永遠停在出錯當下的語言。
    存鍵 + 參數，翻譯延到真的要顯示時才做。

    exception message（給 log 用）仍在建構當下組出來——那是寫進 error.log 的
    開發者訊息，不需要跟著介面語言跑。
    """

    def __init__(self, key: str, **args) -> None:
        self.key = key
        self.args_map = args
        super().__init__(f"{key}: {args.get('path', '')}")

    @property
    def title(self) -> str:
        return t(f"{self.key}.title", **self.args_map)

    @property
    def detail(self) -> str:
        return t(f"{self.key}.detail", **self.args_map)

    @property
    def hint(self) -> str:
        return t(f"{self.key}.hint", **self.args_map)


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
    return raw.decode("latin-1", errors="replace"), t("encoding.latin1Uncertain")


def read_text_file(path: str) -> tuple[str, DocumentMeta]:
    """讀取文字檔並回傳內容與附帶資訊；失敗時拋出 DocumentError。"""
    if os.path.isdir(path):
        raise DocumentError("error.isDir", path=path)

    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        raise DocumentError("error.notFound", path=path) from None
    except PermissionError:
        raise DocumentError("error.denied", path=path) from None
    except OSError as error:
        # 例外訊息本身是 OS 給的，語系由作業系統決定，我們管不到
        raise DocumentError(
            "error.readFailed",
            path=path,
            error=f"{error.__class__.__name__}: {error}",
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
        # 非空白字元數。以前是逐字元的 Python 迴圈，20 MB 的檔要 0.8 秒，而且這段
        # 在忙碌回饋之外，使用者看到的是「凍住一秒才出現等待游標」。str.split()
        # 走 C 迴圈，而且用的正是 str.isspace 同一套判定，結果完全等價、快九倍。
        char_count=sum(map(len, text.split())),
        line_count=text.count("\n") + 1,
    )
    return text, meta


# --- Markdown 轉換 ----------------------------------------------------------
def meta_for_pasted(text: str) -> DocumentMeta:
    """替「貼上的內容」造一份 DocumentMeta。

    分頁靠 meta is None 來決定要畫歡迎頁還是文件（見 DocumentTab.build_html），
    所以貼上的內容也得有 meta，否則永遠停在歡迎頁。

    path 留空字串而不是 None：DocumentMeta.path 宣告是 str，而狀態列直接把它
    丟給 status_path_label；空字串會顯示成「沒有路徑」，正是貼上內容的實情。
    編碼欄填 UTF-8——剪貼簿給的已經是解好的 str，這裡標的是「存成檔案時會是
    什麼」，而不是解碼過程。
    """
    return DocumentMeta(
        path="",
        encoding="UTF-8",
        size_bytes=len(text.encode("utf-8")),
        modified=datetime.now(),
        char_count=len(text),
        line_count=text.count("\n") + 1,
    )


def _build_converter(theme: str) -> markdown.Markdown:
    from markdown.extensions.toc import slugify_unicode

    extensions: list = ["extra", "sane_lists", "toc"]
    # 【標題 id 要留住中文】toc 預設的 slugify 把非 ASCII 全部丟掉，`## 表格` 的
    # id 變成 `_1`、`## 安裝與執行` 變成 `_3`，而使用者手寫的 `[跳](#表格)`
    # 產生的 href 仍是 `#表格`——兩邊對不上，點了完全沒反應，連 sample.md 自己
    # 的示範連結都是死的。slugify_unicode 保留 Unicode 字元、重複標題照樣
    # 加 `_1` 後綴，[TOC] 與註腳的 id 和 href 由同一個擴充產生，不受影響。
    configs: dict = {"toc": {"slugify": slugify_unicode}}
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

# 會被當成獨立快取鍵的主題全集。以 Pygments 配色表的鍵為準：主題只有被
# 這張表辨認，轉換結果才可能不同——不在表上的值全部退回 "default" 配色。
KNOWN_THEMES: tuple[str, ...] = tuple(styles.PYGMENTS_STYLE)


def theme_sensitive(html: str) -> bool:
    """這份轉換結果會不會隨主題不同而不同。

    主題唯一流進轉換的地方是程式碼高亮（_build_converter 把 pygments_style
    依主題挑色，顏色以 inline style 寫死在輸出裡）。而所有程式碼區塊都會被
    QtRichTextExtension 改寫成 td.codecell——輸出裡連一個 codecell 都沒有，
    就代表沒有任何高亮輸出，兩個主題產生的 HTML 逐位元組相同。

    判定方向刻意保守：內文碰巧含「codecell」字樣會被誤判成敏感，代價只是
    多付一次轉換；反過來誤判不敏感會讓程式碼顏色套錯主題，所以標記只認
    「不在就一定安全」的那一邊。沒裝 Pygments 時高亮整個不存在，一律不敏感。
    """
    if not PYGMENTS_AVAILABLE:
        return False
    return "codecell" in html


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
            _notice("warncell", t("doc.largeFile", size=html.escape(meta.size_text)))
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


# 從產生的 HTML 抽出圖片的 alt 文字。
# Qt 解析 HTML 時會把 alt 屬性整個丟掉（實測 charFormat 與 toolTip 裡都沒有），
# 而 browser.loadResource 只拿得到 url——圖片壞掉時想顯示「這裡本來是什麼」，
# 只能在交給 Qt 之前先自己記下來。
# python-markdown 產生的順序固定是 alt 在前、src 在後。
_IMG_ALT_RE = re.compile(r'<img\b[^>]*?alt="([^"]*)"[^>]*?src="([^"]*)"', re.IGNORECASE)


def image_alts(html: str) -> dict[str, str]:
    """回傳 {src: alt}。同一個 src 出現多次時保留第一個 alt。"""
    found: dict[str, str] = {}
    for alt, src in _IMG_ALT_RE.findall(html):
        found.setdefault(src, alt)
    return found


def render_error(error: DocumentError, path: str = "") -> str:
    """產生錯誤頁面（顯示在閱讀區內，而不是彈出對話框）。"""
    detail = html.escape(error.detail).replace("\n", "<br>")
    hint = html.escape(error.hint).replace("\n", "<br>")
    body = [f"<h1>{html.escape(error.title)}</h1>"]
    body.append(_notice("noticecell", f'<span class="mono">{detail}</span>'))
    if hint:
        body.append(f'<p class="muted">{hint}</p>')
    body.append('<p class="muted">' + t("error.keys") + "</p>")
    return _page("".join(body))


# 歡迎頁的快速鍵列。兩欄都進語言檔：按鍵名稱多半兩種語言相同，
# 但「Alt + 左方向鍵」在英文是 "Alt + Left"，不能只翻說明那一欄。
_WELCOME_SHORTCUTS = (
    "open", "newTab", "paste", "closeTab", "switchTab", "reload", "find",
    "settings", "theme", "pin", "zoom", "back", "maximize", "escape",
)


def render_welcome() -> str:
    """未指定檔案時顯示的歡迎頁。"""
    rows = [
        (t(f"welcome.sc.{name}"), t(f"welcome.scDesc.{name}"))
        for name in _WELCOME_SHORTCUTS
    ]
    table = [
        '<table class="data"><thead><tr><th>'
        + html.escape(t("welcome.table.key"))
        + "</th><th>"
        + html.escape(t("welcome.table.action"))
        + "</th></tr></thead><tbody>"
    ]
    for key, description in rows:
        table.append(
            f'<tr><td><span class="mono">{html.escape(key)}</span></td>'
            f"<td>{html.escape(description)}</td></tr>"
        )
    table.append("</tbody></table>")

    body = [
        "<h1>" + html.escape(t("app.displayName")) + "</h1>",
        _notice("noticecell", t("welcome.dropHint")),
        _notice("noticecell", t("welcome.pasteHint")),
        "<h2>" + html.escape(t("welcome.shortcuts")) + "</h2>",
        "".join(table),
    ]
    if not PYGMENTS_AVAILABLE:
        body.append(_notice("warncell", t("doc.noPygments")))
    return _page("".join(body))
