"""在圖表的文字裡找搜尋詞。

【為什麼不自己寫比對】
搜尋列的「區分大小寫」與「全字」是直接映射到 QTextDocument 的 FindFlag 的
（見 find_bar._find_flags）。全字的詞邊界規則有不少反直覺的地方——實測底線算
分隔符（`my_var` 用全字找 `var` 會中）、沒有空白的中文整串算一個詞——自己實作
一套遲早會和內文的結果對不起來，那是最難查的那種 bug：同一個字，內文找得到、
圖表找不到，或反過來。

所以這裡留一份暫存的 QTextDocument，把要比對的字串灌進去、用同一個
document.find() 去掃。引擎完全相同，語意就不可能分家。實測一張圖約 30 µs。

【位置換算】
QTextDocument 的位置是 UTF-16 單元，Python 的字串索引是字碼點。BMP 範圍內
兩者相等（段落分隔符和 "\\n" 一樣各佔一格），只有碰到 emoji 這類非 BMP 字元才會
分岔——畫高亮時要拿索引去切字串量寬度，錯一格就會標到旁邊，所以還是換算一次。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtGui import QTextCursor, QTextDocument

from .model import Text

if TYPE_CHECKING:  # pragma: no cover
    from .model import Scene

__all__ = ["scene_text", "matches_in", "has_match"]

# 比對用的暫存文件。留一份重複使用：每次比對都新建一個 QTextDocument 的話，
# 光是配置就比比對本身還貴。只在同一個執行緒（UI 執行緒）上用。
_scratch: QTextDocument | None = None


def _document() -> QTextDocument:
    global _scratch
    if _scratch is None:
        _scratch = QTextDocument()
    return _scratch


def _flags(case_sensitive: bool, whole_words: bool) -> QTextDocument.FindFlag:
    """和 find_bar._find_flags 同一組旗標，兩邊必須一致。"""
    flags = QTextDocument.FindFlag(0)
    if case_sensitive:
        flags |= QTextDocument.FindFlag.FindCaseSensitively
    if whole_words:
        flags |= QTextDocument.FindFlag.FindWholeWords
    return flags


def _index_of(text: str, utf16_offset: int, astral: bool) -> int:
    """把 UTF-16 位置換成 Python 字串索引。沒有非 BMP 字元時兩者相同。"""
    if not astral:
        return utf16_offset
    units = 0
    for index, char in enumerate(text):
        if units >= utf16_offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def scene_text(scene: "Scene") -> str:
    """場景裡所有文字原語的內容，以換行串起來。

    比對的對象刻意是「畫出來的文字」而不是原始碼：序列圖的區塊標籤
    （`loop 每次`）與 autonumber 的序號都是組出來的字串，只有場景裡才有；
    以場景為準，搜尋結果才會和眼睛看到的一致。
    """
    return "\n".join(item.text for item in scene.items if isinstance(item, Text))


def matches_in(
    text: str, needle: str, case_sensitive: bool = False, whole_words: bool = False
) -> list[tuple[int, int]]:
    """回傳每一筆相符在 text 裡的 (起, 迄) Python 字串索引。"""
    if not text or not needle:
        return []
    document = _document()
    document.setPlainText(text)
    flags = _flags(case_sensitive, whole_words)
    astral = any(ord(char) > 0xFFFF for char in text)
    cursor = QTextCursor(document)
    found: list[tuple[int, int]] = []
    while True:
        cursor = document.find(needle, cursor, flags)
        if cursor.isNull():
            break
        found.append((
            _index_of(text, cursor.selectionStart(), astral),
            _index_of(text, cursor.selectionEnd(), astral),
        ))
    return found


def has_match(
    text: str, needle: str, case_sensitive: bool = False, whole_words: bool = False
) -> bool:
    """只問「有沒有」，不必把每一筆都算出來。"""
    if not text or not needle:
        return False
    document = _document()
    document.setPlainText(text)
    cursor = document.find(
        needle, QTextCursor(document), _flags(case_sensitive, whole_words)
    )
    return not cursor.isNull()
