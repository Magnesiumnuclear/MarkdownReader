# Markdown 閱讀器語法測試

這份文件涵蓋閱讀器支援的所有語法，用來驗證排版是否正確。內文混排 **粗體**、*斜體*、~~刪除線~~、`行內程式碼`，以及 [外部連結](https://github.com)（點擊會用系統預設瀏覽器開啟）。

## 二級標題

三級以下的標題不加底線。

### 三級標題

#### 四級標題

##### 五級標題

###### 六級標題

## 段落與清單

一般段落。中文與 English 混排時，標點與字距應該自然，數字 1234567890 也要對齊。

- 第一層項目
- 第二個項目
    - 巢狀子項目
    - 另一個子項目
        - 第三層
- 第三個項目

1. 有序清單第一項
2. 第二項
3. 第三項

### 任務清單

- [x] 已完成的項目（核取方塊由 QPainter 繪製）
- [ ] 尚未完成的項目
- [ ] 另一個待辦事項

## 引用區塊

> 這是一段引用文字，左側應該有一條灰色直條。
>
> 引用可以包含多個段落。
>
> > 巢狀引用會再往內縮一層，並多一條直條。

## 程式碼

行內程式碼像這樣：`pip install markdown`。

```python
def render(path: str, theme: str = "light") -> str:
    """把 Markdown 檔轉成 HTML。"""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return markdown_to_html(text, theme)   # 回傳 HTML 片段
```

```json
{
  "name": "MarkdownReader",
  "version": "1.0.0",
  "frameless": true,
  "themes": ["light", "dark"]
}
```

```bash
py -3.13 main.py "D:\docs\note.md"
```

沒有指定語言的程式碼區塊：

```
這是純文字區塊
    保留縮排與   空白
```

## 表格

| 功能 | 快速鍵 | 說明 |
|------|--------|------|
| 開啟檔案 | `Ctrl+O` | 選擇要閱讀的檔案 |
| 重新載入 | `F5` | 重新讀取目前檔案 |
| 搜尋 | `Ctrl+F` | 在文件中搜尋文字 |
| 切換主題 | `Ctrl+D` | 深色／淺色切換 |
| 釘選 | `Ctrl+P` | 視窗固定在最上層 |

## 分隔線

上方內容。

---

下方內容。

## 圖片

相對路徑的圖片（過寬時會自動縮到視窗寬度）：

![範例圖片](assets/sample.png)

## 連結測試

- 外部連結：[Python 官網](https://www.python.org) — 應以系統預設瀏覽器開啟
- 錨點連結：[跳到「表格」章節](#表格) — 應在本文件內捲動
- 本機連結：[開啟 README](README.md) — 應在同一視窗開啟，可按 Alt+左方向鍵 返回

## 註腳

這句話有一個註腳[^1]，另一句也有[^note]。

[^1]: 這是第一個註腳的內容。
[^note]: 這是具名註腳，會出現在文件最下方。

## 定義清單

Markdown
:   一種輕量級標記語言。

PyQt6
:   Python 的 Qt 6 綁定，用來建立桌面圖形介面。

## 長文字換行測試

ThisIsAnExtremelyLongTokenWithoutAnySpacesToVerifyThatWrappingBehavesCorrectlyInsideCodeAndParagraphBlocks 這段用來確認超長字串不會撐破版面。

```
ThisIsAnExtremelyLongTokenInsideACodeBlockWithoutAnySpacesToVerifyThatPreWrapWorksAsExpectedAndDoesNotOverflowHorizontally
```

## 結尾

如果以上全部正常顯示，代表渲染管線運作正確。
