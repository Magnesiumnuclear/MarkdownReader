# Markdown 閱讀器

專為 Windows 打造的輕量級 Markdown 閱讀器。雙擊 `.md` 檔案即可用 GitHub 風格排版閱讀，具備無邊框視窗、自訂標題列、深淺色主題與釘選最上層。

---

## 功能

| 分類 | 內容 |
|------|------|
| 開檔 | 接收 Windows 傳入的命令列路徑、`Ctrl+O` 選檔、拖放檔案到視窗 |
| 排版 | GitHub 風格 CSS、程式碼語法高亮、表格、任務清單、註腳、定義清單、刪除線 |
| 視窗 | 無邊框 + 自訂標題列、拖曳移動（保留 Aero Snap）、四邊四角縮放、雙擊最大化 |
| 閱讀 | 深淺色主題切換、字級縮放、`Ctrl+F` 搜尋並高亮全部符合項、狀態列資訊 |
| 設定 | 覆蓋式設定面板（`Ctrl+,`）：主題模式、字級、行高、內文寬度、行為開關，一鍵恢復預設 |
| 主題 | 預設跟隨 Windows 深淺色設定並即時同步，也可手動鎖定淺色或深色 |
| 便利 | 釘選最上層、存檔後自動重載並保留閱讀位置、本機 `.md` 連結可在同視窗開啟並返回 |
| 穩健 | 多重編碼偵測（UTF-8 / BOM / cp950 / Big5 / GBK）、檔案鎖定重試、友善錯誤頁 |

外部連結一律交給**系統預設瀏覽器**開啟，閱讀區內不會載入任何網頁。

---

## 快速鍵

| 按鍵 | 功能 |
|------|------|
| `Ctrl + O` | 開啟 Markdown 檔案 |
| `F5` / `Ctrl + R` | 重新載入目前檔案 |
| `Ctrl + F` | 文件內搜尋（`Enter` 下一個、`Shift+Enter` 上一個、`Esc` 關閉） |
| `Ctrl + ,` | 開啟／關閉設定面板 |
| `Ctrl + D` | 切換深色／淺色主題（會鎖定，不再跟隨系統） |
| `Ctrl + P` | 切換視窗釘選最上層 |
| `Ctrl + +` / `Ctrl + -` / `Ctrl + 0` | 放大／縮小／重設字級 |
| `Ctrl + /` | 顯示或隱藏底部狀態列 |
| `Alt + 左方向鍵` | 回到上一篇文件 |
| `F11` | 切換最大化 |
| `Ctrl + W` / `Esc` | 關閉視窗 |

視窗幾何、主題、字級與釘選狀態會自動記住，下次開啟時沿用。

---

## 設定面板

按 `Ctrl+,` 或標題列的齒輪鈕開啟。面板會**覆蓋標題列以下的整個視窗**，是一頁
完整的設定畫面，因此每個項目都配有名稱與說明文字。標題列保持可見——無邊框視窗
需要它才能拖曳與關閉，齒輪鈕本身也是收起面板的方式之一（`Esc` 或右上角的
關閉鈕也可以）。視窗不夠高時面板會垂直捲動。

| 區塊 | 項目 | 選項 |
|------|------|------|
| 外觀 | 主題 | 淺色 / 深色 / 跟隨系統（預設） |
| | 字級 | 7～30 pt，與 `Ctrl` `+` / `-` / `0` 連動 |
| | 行高 | 緊湊 142% / 標準 160% / 寬鬆 188% |
| | 內文寬度 | 窄 720px / 中 900px / 不限 |
| 行為 | 檔案監看 | 存檔後自動重新渲染並保留閱讀位置 |
| | 視窗 | 釘選最上層，等同 `Ctrl+P` |
| | 狀態列 | 顯示底部檔案資訊，等同 `Ctrl+/` |
| | 外部連結 | 開啟前先跳出確認 |
| 重設 | 恢復預設值 | 全部歸零，主題也回到「跟隨系統」 |

分段選擇與開關共用同一種「晶片」外觀：未選取是描邊、選取是強調色實心。

### 系統主題

主題模式預設是 **跟隨系統**：

- 首次啟動時讀取 Windows 的「選擇您的預設應用程式模式」，直接套用對應配色。
- 之後在 Windows 切換深淺色，視窗會**即時**跟著變，不需要重開程式。
- 在設定面板選了「淺色」或「深色」就會鎖定，不再跟隨；想恢復自動就選回「跟隨系統」。
- 標題列的主題切換鈕等同於「切到另一個配色並鎖定」。

偵測優先使用 Qt 6.5+ 的 `QStyleHints.colorScheme()`（即時同步就是靠它的
`colorSchemeChanged` 訊號）；若回報 `Unknown`，才退回讀登錄檔的
`HKCU\...\Themes\Personalize\AppsUseLightTheme`。

### 內文寬度是怎麼實作的

`QTextDocument` 不支援 `max-width`，而 `setDocumentMargin()` 只能四邊一起設定
（上下也會跟著變超大）。因此改用 root frame 的 `frameFormat`，它可以分別指定
四個邊距——把左右邊距設成 `(可視寬度 - 目標寬度) / 2`，就得到一欄固定寬度且
置中的閱讀區。視窗縮放時會即時重算。

---

## 安裝與執行

```bash
py -3.13 -m pip install -r requirements.txt
```

```bash
py -3.13 main.py sample.md
```

不帶參數執行會顯示歡迎頁。

> **注意**：如果系統 PATH 上有 MSYS2 或其他 Python，直接打 `python` 可能會抓到沒有安裝 PyQt6 的版本。請一律使用 `py -3.13`。

---

## 打包成單一 exe

**建議用附的 `build.spec`**，排除清單最完整，產出約 30 MB：

```bash
py -3.13 -m PyInstaller --noconfirm --clean build.spec
```

或用腳本（等同上面那行，另外會先確認相依套件與圖示）：

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` 也支援 `-OneDir`（資料夾版）與 `-Clean`（先清除舊產物）。

### 純 CLI 形式（不使用 spec）

```bash
py -3.13 -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --name MarkdownReader --icon assets/app.ico --add-data "assets;assets" --hidden-import PyQt6.QtSvg --exclude-module PIL --exclude-module numpy --exclude-module pyqtgraph --exclude-module PyQt6.QtWebEngineCore --exclude-module PyQt6.QtWebEngineWidgets --exclude-module PyQt6.QtQml --exclude-module PyQt6.QtQuick --exclude-module PyQt6.QtMultimedia --exclude-module PyQt6.QtSql --exclude-module PyQt6.QtTest --exclude-module tkinter main.py
```

> **兩個要注意的地方**
> 1. 這種形式會自動產生一份 `MarkdownReader.spec`，**會覆蓋同名檔案**。專案裡的設定檔因此取名 `build.spec`，才不會被蓋掉。
> 2. `--exclude-module PIL --exclude-module numpy` 一定要加。`pygments.formatters.img` 會 import PIL，PIL 的 hook 又把 numpy 拉進來，光這兩個就會讓 exe 從 30 MB 膨脹到 60 MB（含 20 MB 的 OpenBLAS 與 7.8 MB 的 AVIF 外掛）。本程式只用 `HtmlFormatter` 產生 HTML，不需要它們。
>
> CLI 形式無法過濾 DLL，所以仍會包進 `opengl32sw.dll`（20 MB）等用不到的二進位；`build.spec` 有做這層過濾，這也是兩者差距的來源。

### 各參數的用意

- `--windowed`（等同 `--noconsole`）：**隱藏終端機視窗**，這是最關鍵的一個。
- `--add-data "assets;assets"`：所有圖示都是外部 SVG 檔，一定要打包進去（Windows 用 `;` 當分隔符）。程式端一律透過 `app/resources.py` 的 `resource_path()` 讀取，會自動處理 PyInstaller 的 `sys._MEIPASS`。
- `--hidden-import PyQt6.QtSvg`：搭配 `main.py` 頂端的顯式 `import PyQt6.QtSvg`，確保 Qt 的 SVG 插件（`qsvg.dll` / `qsvgicon.dll`）一併被收錄。**漏掉的典型症狀是「原始碼跑起來圖示正常、打包後全部變空白」。**
- `--noupx`：UPX 壓縮 Qt 的 DLL 經常導致程式無法啟動。
- `--exclude-module ...`：排除用不到的 Qt 模組與 PIL / numpy。
- `build.spec` 另外過濾了幾個大型 DLL：`opengl32sw.dll`（20 MB，Mesa 軟體 OpenGL 後備，純 widget 程式走 raster 繪製用不到）、`Qt6Pdf.dll`、`libcrypto-3.dll` / `libssl-3.dll`（QtNetwork 的 TLS 後端，本程式不連網）。若日後在極舊的顯卡或虛擬機出現空白視窗，把 `build.spec` 裡 `EXCLUDE_BINARIES` 的 `opengl32sw.dll` 拿掉重新打包即可。

打包尺寸實測：

| 打包方式 | 大小 |
|---|---|
| 未排除 PIL / numpy 的 CLI 形式 | 60.4 MB |
| `build.spec`（含 DLL 過濾） | **29.8 MB** |

### onefile 還是 onedir？

| | 單一 exe（`--onefile`） | 資料夾版（`--onedir`） |
|---|---|---|
| 散布 | 一個檔案，最方便 | 一整個資料夾 |
| 冷啟動 | 約 1～2 秒（每次都要解壓到暫存目錄） | 低於 0.5 秒 |
| 建議 | 給別人、放隨身碟 | **設成 `.md` 預設開啟程式**（雙擊要馬上看到內容） |

---

## 設定成 `.md` 的開啟程式

```bash
py -3.13 tools/install_association.py --target "D:\software\Customize-Open-File\md\dist\MarkdownReader.exe"
```

不指定 `--target` 時會自動找 `dist\MarkdownReader.exe`；找不到就退回開發模式（用 `pythonw.exe` 執行 `main.py`）。

這個腳本**只寫入 `HKCU\Software\Classes`**，不需要系統管理員權限，而且可以完整還原：

```bash
py -3.13 tools/install_association.py --uninstall
```

### 關於「預設開啟程式」

Windows 10/11 以 **UserChoice** 雜湊保護預設程式設定，第三方程式無法單靠寫入登錄檔強制指定。腳本執行完後，本程式已經出現在「開啟檔案」清單中，接著手動指定一次即可：

> 右鍵點 `.md` 檔 → 開啟檔案 → 選擇其他應用程式 → 勾選「一律使用此應用程式」

之後雙擊任何 `.md` 檔就會用本程式開啟。

---

## 專案結構

```
main.py                     進入點：sys.argv、QApplication、全域例外攔截
app/
  config.py                 常數（識別名稱、副檔名、尺寸、QSettings 鍵名）
  resources.py              資源路徑解析（相容 PyInstaller）
  icons.py                  SVG 圖示供應器：讀檔、著色、快取
  styles.py                 ★ 樣式唯一來源：色票 + QSS + 文件 CSS
  theme.py                  系統深淺色偵測（QStyleHints，登錄檔備援）
  document.py               安全讀檔（編碼偵測、鎖定重試）+ Markdown 轉換
  qt_html.py                把 Markdown 輸出改寫成 Qt 能正確呈現的 HTML
  title_bar.py              自訂標題列（拖曳、控制按鈕）
  find_bar.py               Ctrl+F 搜尋列
  settings_panel.py         Ctrl+, 覆蓋式設定面板
  viewer.py                 無邊框主視窗、邊緣縮放、檔案監看、快捷鍵
  win32.py                  ctypes：置頂、AppUserModelID、通知檔案總管
tools/
  make_icon.py              由 app.svg 產生 assets/app.ico
  install_association.py    檔案關聯註冊／移除（僅 HKCU）
assets/icons/*.svg          所有 UI 圖示
```

### 兩條設計規範

1. **圖示一律來自 SVG 檔或 QPainter**
   `assets/icons/*.svg` 的 `stroke`/`fill` 寫成 `{color}` 佔位符，載入時置換成主題色後交給 `QSvgRenderer` 繪製，因此同一份向量圖能隨主題變色。程式中不使用任何 Unicode 字元或 Emoji 充當圖示。唯一的例外是任務清單的核取方塊——Qt 會直接丟棄 `<input type="checkbox">`，所以改由 `QPainter` 即時繪製成行內圖片。

2. **樣式集中在 `styles.py`**
   全專案只有 **一次** `setStyleSheet()` 呼叫（`viewer.MarkdownViewer.apply_theme`），套用 `styles.build_qss(theme)` 產生的單一字串，再靠 `objectName` 選擇器級聯到所有子元件；文件樣式則由 `setDefaultStyleSheet()` 套用 `styles.build_doc_css(theme, 字級)`。切換主題時，視窗外觀、內文排版、圖示顏色與程式碼高亮會一次同步。

---

## Qt rich text 的限制與對應作法

選用 `QTextBrowser` 而非 `QWebEngineView`，可讓 exe 少約 150MB 且啟動更快，代價是只支援 CSS 2.1 子集。以下是實測結果與本專案的處理方式：

| Qt 的實際行為 | 對應作法 |
|---|---|
| 區塊元素（`blockquote`/`pre`/`hr`/`h1`）的 `border`、`padding` **完全無效** | 由 `qt_html.py` 的 markdown 擴充改寫成單格表格，靠儲存格的 `border-left` / `padding` 呈現 |
| 只有表格儲存格的 `border`/`padding`/`background` 有效 | 引用區塊、程式碼區塊、標題底線全部走表格 |
| `<pre>` 若設 `background-color: transparent`，會把所在儲存格的底色整片蓋掉 | 明確填入與儲存格相同的顏色 |
| `h1`～`h5` 忽略所有 `font-size` 單位，固定使用內建比例 2.0 / 1.5 / 1.2 / 1.0 / 0.8 | 沿用內建比例；`h6` 是唯一吃 `pt` 的標題，明確指定字級避免階層反轉 |
| 段落行高倍率會套用到圖片所在的行框，圖片下方多出大片空白 | 只含一張圖片的段落改用 100% 行高 |
| `<input type="checkbox">` 會被丟棄 | 改用 `QPainter` 繪製的行內圖片 |
| 圖片不會自動縮到視窗寬度，且會保留原始高度 | 覆寫 `loadResource()` 在載入時等比縮小；視窗寬度改變後重繪 |
| 字級要能隨縮放等比變化 | 文件 CSS 一律用 `em` / `%`（`px` 不會跟著縮放） |

---

## 已知限制

- 不支援圓角、陰影、Mermaid 圖表、HTML `<details>` 與 GFM 警示區塊（`> [!NOTE]`）。若日後需要完整保真度，唯一解是改用 `QWebEngineView`（exe 會增加約 150MB）；渲染層已隔離在 `document.py` 與 `viewer.py`，替換成本不高。
- 無邊框視窗不支援 Windows 11 的 Snap Layouts（滑鼠停在最大化鈕上跳出的版面選單），那需要攔截原生 `WM_NCHITTEST` 訊息。
- 每雙擊一個檔案會開一個新行程。若想要「單一實例、分頁開啟」，可用 `QLocalServer` / `QLocalSocket` 擴充。

---

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| 打包後圖示全部空白 | 確認打包指令有 `--add-data "assets;assets"` 與 `--hidden-import PyQt6.QtSvg` |
| 程式沒有畫面就消失 | 查看 `%LOCALAPPDATA%\MarkdownReader\error.log`，未攔截的例外都會寫在這裡 |
| 雙擊 `.md` 仍由其他程式開啟 | Windows UserChoice 優先，請用「開啟檔案 → 選擇其他應用程式 → 一律使用此應用程式」指定一次 |
| 中文顯示成亂碼 | 狀態列會顯示實際判定的編碼；若標示為 `latin-1（可能不正確）`，代表該檔案編碼超出偵測範圍 |
| 程式碼區塊沒有高亮 | 確認已安裝 `Pygments`（`py -3.13 -m pip install Pygments`） |
