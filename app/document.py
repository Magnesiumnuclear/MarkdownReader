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


def markdown_to_html(text: str, theme: str) -> str:
    """把 Markdown 原始碼轉成 Qt 相容的 HTML 片段。"""
    return _build_converter(theme).convert(text)


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
