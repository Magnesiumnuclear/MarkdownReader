"""功能回歸測試。

    py -3.13 tools/smoke_test.py                 全部跑一遍
    py -3.13 tools/smoke_test.py --only 分頁     只跑名稱含「分頁」的區塊
    py -3.13 tools/smoke_test.py --list          列出所有區塊
    py -3.13 tools/smoke_test.py --runs 15       提高穩定度測試的次數

全部通過回傳 0，有任何一項失敗回傳 1。

【為什麼要有這個檔案】
一開始每次改動都用臨時腳本驗，跑完就丟。結果分頁功能引進的幾個當機——
「上次最大化就再也打不開」「關掉分頁後滑鼠一動就中止」——全都逃過了，
因為臨時腳本剛好沒踩到那些組合。這裡把每一條驗證固定下來，
每一項都對應一個真的發生過的問題。

【為什麼有些項目要開子行程】
PyQt6 對「虛擬函式裡的未攔截例外」是致命的：不會拋出 Python 例外，而是直接
以 0xC0000409 中止行程。這種問題在同一個行程裡 try/except 不到，只能開子行程
看結束碼。resizeEvent 與 eventFilter 的測試因此都走子行程。

【為什麼要備份 QSettings】
測試會寫入程式真正使用的登錄檔位置（不能改，否則測不到「還原上次狀態」這類
邏輯）。開頭先快照、結束後還原，不會動到使用者自己的設定。

【已知問題：低頻的建構期存取違規（僅測試環境）】
約每四、五次完整跑會有一個區塊以 0xC0000005 死在「視窗建構」期
（faulthandler 疊停在 MarkdownViewer 建構或 showEvent 的 refresh_targets）。
已知事實：
  - 單獨跑任一區塊從未發生（各 10+ 次）；純建毀 120 輪、還原殼建毀 90 輪
    的定向實驗也從未重現——需要完整區段序列＋系統負載。
  - 真實程式路徑乾淨：打包端到端、WM_CLOSE 連測 15/15、從無 error.log。
  - 特徵是「先前某次釋放造成的堆積損壞在無辜行號引爆」，要 PageHeap 級
    工具才能抓到真兇。
處置：區塊已各自隔離成子行程（一個崩不影響其他），stderr 會轉印崩潰疊。
遇到單一區塊紅燈時先重跑該區塊確認；若「單獨跑」也能重現，那就是真回歸。
"""

from __future__ import annotations

# 原生層崩潰（0xC0000005 等）時把 Python 呼叫疊印到 stderr。
# 沒有它，區段以存取違規死掉時連死在哪一行都看不到。
import faulthandler

faulthandler.enable()

import argparse
import re
import os
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(PROJECT_ROOT, "sample.md")
README = os.path.join(PROJECT_ROOT, "README.md")


def _write_big_doc(folder: str, name: str = "big.md", target_bytes: int = 1_200_000) -> str:
    """產生一份夠大的 Markdown 素材（標題、段落、表格、程式碼混合）。

    以前拿 tools/CHANGELOG.md 當大檔——那是別的專案留下的未追蹤檔案，乾淨 clone
    沒有它：忙碌回饋那組檢查會整個被跳過、HTML 快取那組退回 145 行的 README，
    「大檔」測試等於沒測。素材要超過 config.BUSY_FEEDBACK_BYTES 才會觸發忙碌回饋。
    """
    path = os.path.join(folder, name)
    block = (
        "## 小節\n\n這是一段內文，用來把文件撐到有意義的大小，混合中文與 English words。\n\n"
        "| 欄一 | 欄二 | 欄三 |\n|---|---|---|\n| a | b | c |\n| 1 | 2 | 3 |\n\n"
        "```python\ndef f(x):\n    return x + 1\n```\n\n- 清單一\n- 清單二\n\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# 大檔素材\n\n")
        written = 0
        index = 0
        while written < target_bytes:
            chunk = block.replace("小節", f"小節 {index}")
            handle.write(chunk)
            written += len(chunk.encode("utf-8"))
            index += 1
    return path


MERMAID_DOC = os.path.join(PROJECT_ROOT, "docs", "02-Mermaid.md")

_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []
_SKIP: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        _PASS.append(label)
    else:
        _FAIL.append((label, detail))
        print(f"    [FAIL] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


# _enter_offscreen 建的 QApplication 得抓在模組層：QApplication([]) 建完不留
# 參考會被垃圾回收，平台外掛跟著卸載，之後區塊自建的新 QApplication 只看得到
# 環境變數裡「不帶 configfile」的 offscreen——虛擬螢幕縮回預設的 800x800，
# 內文邊距那幾條就紅了（踩過一次）。
_OFFSCREEN_APP = None


def _offscreen() -> bool:
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QGuiApplication

    app = QCoreApplication.instance()
    if isinstance(app, QGuiApplication):
        return app.platformName() == "offscreen"
    return os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen")


def onscreen_only(label: str) -> bool:
    """這條查的是真視窗系統（原生 HWND、硬體游標、原生 z-order）。

    offscreen 虛擬螢幕上沒有這些東西可問，硬跑只會拿假把手換假答案。
    跳過並明講、記進 _SKIP，不假裝通過——加 --onscreen 才會驗。
    """
    if not _offscreen():
        return True
    _SKIP.append(label)
    print(f"    [僅實機] {label} —— offscreen 沒有原生視窗系統，--onscreen 才驗")
    return False


def _enter_offscreen() -> None:
    """切到 offscreen 虛擬螢幕：視窗全畫在記憶體裡，不佔畫面也不搶焦點。

    兩個坑：
    1. 預設虛擬螢幕只有 800x800，比測試開的視窗還小，幾何全被夾扁
       （內文邊距、搜尋列拖曳的斷言直接紅）——用 configfile 描述一個
       1920x1080 的螢幕。
    2. platform 字串以冒號分隔選項，Windows 磁碟機代號的冒號會把路徑
       截斷，configfile 只吃相對路徑——所以先 chdir 到設定檔的資料夾建
       QApplication（建構當下才讀檔），建完再走回來。
    最後把環境變數改回不帶 configfile 的「offscreen」：測試還會 spawn 別的
    行程（探針、本體實例），它們的工作目錄裡沒有那個檔，繼承了反而找不到；
    它們用不到大螢幕，預設尺寸就夠。
    """
    import json
    import tempfile

    from PyQt6.QtWidgets import QApplication

    cfg_dir = tempfile.mkdtemp(prefix="mdreader-offscreen-")
    with open(os.path.join(cfg_dir, "screen.json"), "w", encoding="utf-8") as fh:
        json.dump({"screens": [{"name": "virt", "x": 0, "y": 0,
                                "width": 1920, "height": 1080,
                                "logicalDpi": 96, "logicalBaseDpi": 96,
                                "dpr": 1.0}]}, fh)
    os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=screen.json"
    cwd = os.getcwd()
    os.chdir(cfg_dir)
    try:
        global _OFFSCREEN_APP
        _OFFSCREEN_APP = QApplication([])
    finally:
        os.chdir(cwd)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


# --- QSettings 備份與還原 ---------------------------------------------------
def _settings_handle():
    from PyQt6.QtCore import QCoreApplication, QSettings

    if QCoreApplication.instance() is None:
        QCoreApplication([])
    return QSettings(config.ORG_NAME, config.APP_NAME)


def _expected_title(file_name: str) -> str:
    """組出視窗標題，來源和程式本身完全相同。

    測試不寫死標題字串：產品名稱會隨介面語言變，寫死等於把測試綁在中文上。
    """
    from app import language

    return language.t(
        "window.title", name=file_name, app=language.t("app.displayName")
    )


def pin_language() -> None:
    """把量測／測試期間的介面語言釘成預設語言。

    測試對 UI 文字的斷言必須有一個確定的語言可比對，否則在英文 Windows 上
    整批紅。用的是既有的 QSettings 快照／還原機制（見 snapshot_settings），
    和 benchmark_startup.BENCHMARK_SETTINGS 同一個路數。
    """
    from app import config, language

    settings = _settings_handle()
    settings.setValue(config.KEY_LANGUAGE_MODE, config.DEFAULT_LANGUAGE)
    settings.sync()
    language.set_current(config.DEFAULT_LANGUAGE)


def snapshot_settings() -> dict:
    settings = _settings_handle()
    return {key: settings.value(key) for key in settings.allKeys()}


def restore_settings(saved: dict) -> None:
    settings = _settings_handle()
    settings.clear()
    for key, value in saved.items():
        if value is not None:
            settings.setValue(key, value)
    settings.sync()


# --- 子行程輔助 -------------------------------------------------------------
_CHILD_HEADER = textwrap.dedent(
    '''
    import os, sys
    sys.path.insert(0, r"{root}")
    os.chdir(r"{root}")
    from PyQt6.QtCore import QSettings, QTimer, QEventLoop, Qt, QEvent, QPointF, QByteArray
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QMouseEvent
    from PyQt6 import sip
    app = QApplication([])
    from app import config
    from app.viewer import MarkdownViewer

    def pump(ms=300):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
        for _ in range(3):
            app.processEvents()

    def settings():
        return QSettings(config.ORG_NAME, config.APP_NAME)

    SAMPLE = os.path.join(r"{root}", "sample.md")
    README = os.path.join(r"{root}", "README.md")
    '''
)


def run_child(name: str, body: str, timeout: int = 120):
    """在獨立行程執行一段程式，回傳 (結束碼, stdout, stderr)。

    子行程一律用 -u（不緩衝）：致命中止會讓緩衝區裡的輸出整段消失，
    那樣連「跑到哪一行才死」都看不出來。
    """
    source = _CHILD_HEADER.format(root=PROJECT_ROOT.replace("\\", "/")) + textwrap.dedent(body)
    path = os.path.join(tempfile.gettempdir(), f"mdreader_smoke_{name}.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    proc = subprocess.run(
        [sys.executable, "-u", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def child_values(stdout: str) -> dict[str, str]:
    """把子行程印出的 `KEY value` 收成字典。"""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isupper():
            values[parts[0]] = parts[1]
    return values


def last_error(stderr: str) -> str:
    lines = [l for l in stderr.strip().splitlines() if "Error" in l]
    return lines[-1] if lines else ""


# ===========================================================================
# 區塊：渲染與閱讀
# ===========================================================================
def section_rendering(args) -> None:
    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer, QUrl
    from PyQt6.QtGui import QColor, QDesktopServices, QImage
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=250):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app import styles
    from app.language import t
    from app.viewer import MarkdownViewer

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    tmp = tempfile.mkdtemp()
    main_doc = os.path.join(tmp, "a.md")
    other_doc = os.path.join(tmp, "b.md")
    with open(main_doc, "w", encoding="utf-8") as handle:
        handle.write(
            "# 主文件\n\n## 章節\n\n~~刪~~ alpha alpha\n\n- [x] 完成\n\n"
            "```py\nx=1\n```\n\n> 引用\n\n[外部](https://example.com) [本機](b.md)\n"
        )
    with open(other_doc, "w", encoding="utf-8") as handle:
        handle.write("# 另一份\n")

    viewer = MarkdownViewer(SAMPLE)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.resize(1120, 820)
    viewer.show()
    pump(500)

    from app import win32

    handle_id = int(viewer.winId())
    viewer.set_always_on_top(True)
    pump(200)
    if onscreen_only("釘選最上層真的生效（查 WS_EX_TOPMOST，不是只看 API 回傳值）"):
        check("釘選最上層真的生效（查 WS_EX_TOPMOST，不是只看 API 回傳值）",
              win32.is_topmost(handle_id))
    viewer.set_always_on_top(False)
    pump(200)

    viewer.toggle_settings()
    pump(300)
    panel = viewer.settings_panel
    check("設定面板顯示", panel.isVisible())
    check("設定面板不蓋住標題列",
          not panel.geometry().intersects(viewer.title_bar.geometry()))
    viewer.set_theme_mode("light")
    pump(250)
    check("主題切換", viewer._theme == "light")
    viewer.set_line_height("compact")
    pump(250)
    check("行高套用到文件 CSS",
          "line-height: 142%" in viewer.browser.document().defaultStyleSheet())
    viewer.set_content_width(720)
    pump(300)
    check("限制內文寬度會加上左右邊距",
          viewer.browser.document().rootFrame().frameFormat().leftMargin() > 60)
    viewer.zoom_in()
    pump(250)
    check("字級縮放", panel._font_value.text() == "12 pt")
    viewer.reset_settings()
    pump(350)
    check("恢復預設值",
          viewer._theme_mode == "system" and viewer._restore_tabs is False)
    viewer._on_escape()
    pump(250)
    check("Esc 收起設定面板", not panel.isVisible())

    viewer.open_path(main_doc, new_tab=True)
    pump(350)
    text = viewer.browser.toPlainText()
    check("開檔並渲染", "主文件" in text)
    check("刪除線擴充有生效", "刪" in text)

    opened: list[str] = []
    original = QDesktopServices.openUrl
    QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toString()) or True)
    viewer._on_anchor_clicked(QUrl("https://example.com"))
    QDesktopServices.openUrl = original
    check("外部連結交給系統瀏覽器，不在閱讀區內跳轉",
          opened == ["https://example.com"], str(opened))

    before = len(viewer._tabs)
    viewer._on_anchor_clicked(QUrl.fromLocalFile(other_doc))
    pump(350)
    check("本機 .md 連結開在新分頁",
          len(viewer._tabs) == before + 1 and "另一份" in viewer.browser.toPlainText())

    index = next(i for i, tab in enumerate(viewer._tabs)
                 if tab.path == os.path.abspath(main_doc))
    viewer.activate_tab(index)
    pump(300)
    viewer.find_bar.activate()
    viewer.find_bar.input.setText("alpha")
    pump(300)
    check("切換分頁後搜尋作用在正確的文件上",
          viewer.find_bar.status.text() == "1 / 2", viewer.find_bar.status.text())
    viewer.find_bar.deactivate()

    with open(main_doc, "w", encoding="utf-8") as handle:
        handle.write("# 主文件\n\ngamma\n")
    pump(900)
    check("存檔後自動重新載入", "gamma" in viewer.browser.toPlainText())

    # 狀態列的路徑可點擊：在檔案總管中顯示該檔
    from app import win32 as _win32

    check("狀態列顯示目前分頁的路徑",
          viewer.status_path_label.text() == os.path.abspath(main_doc),
          viewer.status_path_label.text())
    revealed: list[str] = []
    original_reveal = _win32.reveal_in_explorer
    _win32.reveal_in_explorer = lambda target: revealed.append(target) or True
    viewer.status_path_label.clicked.emit()
    _win32.reveal_in_explorer = original_reveal
    check("點路徑會以目前分頁的檔案呼叫「在檔案總管中顯示」",
          revealed == [os.path.abspath(main_doc)], str(revealed))

    # 連結 hover 在狀態列顯示目標。QTextBrowser.highlighted 帶的是 QUrl 不是
    # str——接成 str 不會報錯，只是真的 hover 時完全不觸發（測試才抓到）。
    meta_line = viewer.status_label.text()
    viewer.browser.highlighted.emit(QUrl("https://example.com/docs"))
    pump(120)
    check("hover 外部連結時狀態列顯示網址",
          "https://example.com/docs" in viewer.status_label.text(),
          viewer.status_label.text())
    check("hover 時不覆蓋可點的路徑欄位",
          viewer.status_path_label.text() == os.path.abspath(main_doc))
    viewer.browser.highlighted.emit(
        QUrl.fromLocalFile(os.path.join("D:", os.sep, "docs", "note.md")))
    pump(120)
    check("hover 本機連結顯示原生反斜線路徑",
          "D:\\docs\\note.md" in viewer.status_label.text(),
          viewer.status_label.text())
    viewer.browser.highlighted.emit(QUrl())
    pump(120)
    check("移開連結後恢復檔案資訊", viewer.status_label.text() == meta_line)

    # 大檔的忙碌回饋：等待游標與「正在載入」必須在凍結『之前』就畫出來，
    # 否則使用者看到的仍是數百毫秒的無反應。用 repaint 當取樣點。
    big_doc = _write_big_doc(tmp)
    observed = {"cursor": False, "text": ""}
    original_repaint = viewer.status_label.repaint

    def sampling_repaint(*args, **kwargs):
        cursor = QApplication.overrideCursor()
        if cursor is not None and cursor.shape() == Qt.CursorShape.WaitCursor:
            observed["cursor"] = True
        observed["text"] = viewer.status_label.text()
        observed["busy"] = viewer._status_is_busy
        return original_repaint(*args, **kwargs)

    viewer.status_label.repaint = sampling_repaint
    viewer.open_path(big_doc, new_tab=True)
    pump(700)
    viewer.status_label.repaint = original_repaint
    check("大檔渲染前就設好等待游標", observed["cursor"])
    # 測旗標而不是文案：文案會隨語言變，_status_is_busy 不會。
    # 這條因此比原本更強——它問的是「忙碌狀態有沒有被正確標記」，
    # 而那正是 _busy_feedback 的 finally 還原邏輯所依賴的東西。
    check("大檔渲染前就標記為忙碌並顯示訊息",
          observed["busy"] and observed["text"], str(observed))
    check("渲染結束後游標已還原（try/finally）",
          QApplication.overrideCursor() is None)

    # 小檔不該閃忙碌提示（低於門檻時渲染在百毫秒內，閃一下只是雜訊）
    calls = {"n": 0}
    plain_repaint = viewer.status_label.repaint

    def counting_repaint(*args, **kwargs):
        calls["n"] += 1
        return plain_repaint(*args, **kwargs)

    viewer.status_label.repaint = counting_repaint
    viewer.open_path(other_doc, new_tab=True)
    pump(300)
    viewer.status_label.repaint = plain_repaint
    check("小檔不觸發忙碌回饋", calls["n"] == 0, str(calls["n"]))

    # --- 讀檔階段也在忙碌回饋之內 ------------------------------------------
    # 以前等待游標到 _render 才出現；大檔光「讀檔＋多重編碼試解＋統計」就凍結
    # 幾百毫秒，看起來像當掉。用 read_text_file 當取樣點：它被呼叫的當下，
    # 覆蓋游標必須已經是等待游標。
    import time as _time

    from app import document as _doc_probe
    from app.document_tab import DocumentTab as _DT

    seen = {"cursor_at_read": None}
    real_read = _doc_probe.read_text_file

    def sampling_read(path):
        cursor = QApplication.overrideCursor()
        seen["cursor_at_read"] = (
            cursor is not None and cursor.shape() == Qt.CursorShape.WaitCursor)
        return real_read(path)

    _doc_probe.read_text_file = sampling_read
    try:
        viewer.open_path(_write_big_doc(tmp, "big2.md"), new_tab=True)
        pump(700)
        check("大檔：讀檔時等待游標已經在（不是等到渲染才出現）",
              seen["cursor_at_read"] is True, str(seen))
        viewer._add_tab(_DT(_write_big_doc(tmp, "big3.md")), activate=False)
        viewer._sync_tab_bar()
        seen["cursor_at_read"] = None
        viewer.activate_tab(viewer.tab_count() - 1)
        pump(700)
        check("延後分頁第一次載入也有等待游標", seen["cursor_at_read"] is True, str(seen))
    finally:
        _doc_probe.read_text_file = real_read
    check("讀檔後游標已還原", QApplication.overrideCursor() is None)

    # --- 字數統計：和逐字元 isspace 完全等價，而且快 -----------------------
    # 舊寫法是逐字元的 Python 迴圈（20 MB 要 0.8 秒）；str.split() 用的正是
    # 同一套 isspace 判定，走 C 迴圈快九倍。樣本刻意塞各種 Unicode 空白。
    probe_text = ("a b" + chr(9) + "c" + chr(10) + chr(0x3000) + "g" + chr(0x1C)
                  + "H" + chr(0xA0) + "i j" + chr(0x2028) + "k" + chr(13) + chr(10))
    probe_path = os.path.join(tmp, "spaces.md")
    with open(probe_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(probe_text)
    _, probe_meta = _doc_probe.read_text_file(probe_path)
    check("字數統計與逐字元 isspace 判定完全等價（全形空白、NBSP、U+001C、U+2028）",
          probe_meta.char_count == sum(1 for c in probe_text if not c.isspace()),
          f"{probe_meta.char_count}")
    ascii_path = os.path.join(tmp, "ascii12mb.md")
    with open(ascii_path, "w", encoding="utf-8") as handle:
        handle.write(("lorem ipsum dolor sit amet consectetur " * 30 + "\n") * 10500)
    t0 = _time.perf_counter()
    _doc_probe.read_text_file(ascii_path)
    elapsed = _time.perf_counter() - t0
    check("12 MB 純文字讀檔含統計在 0.3 秒內（逐字元迴圈要 0.5 秒以上）",
          elapsed < 0.3, f"{elapsed * 1000:.0f} ms")

    # --- 中文標題的錨點 ------------------------------------------------------
    # toc 預設的 slugify 把非 ASCII 全丟掉：`## 表格` 的 id 變成 _1，而手寫的
    # `[跳](#表格)` href 仍是 #表格，兩邊對不上，點了沒反應——連 sample.md 自己的
    # 示範連結都是死的。改用 slugify_unicode 之後 id 就是「表格」。
    anchor_doc = os.path.join(tmp, "anchors.md")
    with open(anchor_doc, "w", encoding="utf-8") as handle:
        handle.write("# 目錄\n\n[跳到表格](#表格)\n\n" + "填充段落。\n\n" * 300
                     + "## 表格\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n## 表格\n\n第二個同名標題。\n")
    with open(anchor_doc, encoding="utf-8") as handle:
        anchor_html = _doc_probe.markdown_to_html(handle.read(), "light")
    check("中文標題的 id 保留中文（toc 用 slugify_unicode），重複標題加 _1",
          'name="表格"' in anchor_html and 'name="表格_1"' in anchor_html
          and 'href="#表格"' in anchor_html,
          str(re.findall(r'name="([^"]+)"', anchor_html)))
    viewer.open_path(anchor_doc, new_tab=True)
    pump(500)
    viewer.flush_pending_chunks()
    pump(300)
    anchor_bar = viewer.browser.verticalScrollBar()
    anchor_bar.setValue(0)
    viewer._on_anchor_clicked(QUrl("#表格"))
    pump(400)
    check("點中文錨點會捲到該標題（以前 id 是 _1，對不上，點了沒反應）",
          anchor_bar.value() > 0, f"scroll={anchor_bar.value()} max={anchor_bar.maximum()}")

    # --- 破圖佔位：圖示 + alt 文字 + 作者寫的路徑 ----------------------------
    # Qt 內建的破圖是 :/qt-project.org/styles/commonstyle/images/file-16.png，
    # 一張 16x16 的灰色小檔案圖，既不顯示 alt、也不說是哪個路徑壞了。
    from PyQt6.QtCore import QUrl as _QUrl
    from PyQt6.QtGui import QTextDocument as _QTextDocument

    from app import document as _document

    broken_doc = os.path.join(tmp, "broken_images.md")
    with open(broken_doc, "w", encoding="utf-8") as handle:
        handle.write(chr(10).join([
            "# 破圖", "",
            "![系統架構資料流圖](docs/nope.png)", "",
            "![](assets/no-alt.png)", "",
            "結尾。",
        ]))
    viewer.open_path(broken_doc, new_tab=True)
    pump(500)

    alts = _document.image_alts(viewer._tab.build_html(viewer._theme))
    check("alt 文字有從 HTML 抽出來",
          alts.get("docs/nope.png") == "系統架構資料流圖", str(alts))
    check("沒有 alt 的圖也會被收進對照表（值為空字串）",
          alts.get("assets/no-alt.png") == "", str(alts))

    # 【這條專門守鍵值解析】loadResource 收到的是 Qt 用 baseUrl 解析過的絕對
    # url，不是 HTML 裡的原始 src。用原始 src 當鍵一定查不到，alt 就會失效。
    base = viewer.browser.document().baseUrl()
    resolved = base.resolved(_QUrl("docs/nope.png")).toString()
    check("前置：解析後的 url 和原始 src 不同（否則這條沒在測東西）",
          resolved != "docs/nope.png", resolved)
    check("alt 對照表以解析後的 url 為鍵",
          viewer.browser._image_alts.get(resolved, ("", ""))[0] == "系統架構資料流圖",
          str(list(viewer.browser._image_alts)[:2]))

    image_type = _QTextDocument.ResourceType.ImageResource.value
    placeholder = viewer.browser.loadResource(image_type, _QUrl(resolved))
    check("載不到的圖片回傳自己畫的替代圖，而不是 null",
          placeholder is not None and not placeholder.isNull())
    # Qt 的預設是 16x16；我們畫的一定比它大得多（要放得下圖示與兩行字）。
    # 先確認拿得到東西才量尺寸：上一條紅的時候 placeholder 可能是 None，
    # 直接 .width() 會讓整個區塊當掉而不是乾淨地報一條失敗。
    have = placeholder is not None and hasattr(placeholder, "width")
    check("替代圖不是 Qt 內建的 16x16 小圖示",
          have and placeholder.width() > 120 and placeholder.height() > 40,
          f"{placeholder.width()}x{placeholder.height()}" if have else "沒有拿到圖")

    # 【高 DPI】替代圖要以實體像素繪製並標上 devicePixelRatio，否則會以 1x
    # 畫好再被放大，文字糊掉。
    expected_dpr = viewer.browser.devicePixelRatioF() or 1.0
    check("替代圖標上了正確的裝置像素比",
          have and abs(placeholder.devicePixelRatio() - expected_dpr) < 0.01,
          str(placeholder.devicePixelRatio()) if have else "沒有拿到圖")

    # 【不能重複套用 DPR】設定 devicePixelRatio 的時機若在 QPainter 之前，
    # Qt 會自己套一次縮放、程式再套一次，內容就會被畫成 1.5 倍大而溢出圖片邊界。
    # 虛線圓角框畫在 (1,1)-(w-3,h-3)，所以最右一整欄像素必定是透明的；
    # 一旦重複縮放，那一欄會被內容佔滿。
    if have:
        edge = [placeholder.pixelColor(placeholder.width() - 1, y).alpha()
                for y in range(placeholder.height())]
        check("替代圖的內容沒有溢出邊界（沒有重複套用 DPR）",
              max(edge) == 0, f"最右欄最大 alpha={max(edge)}")

    ok_png = os.path.join(tmp, "real.png")
    _QImage_ok = QImage(120, 40, QImage.Format.Format_ARGB32)
    _QImage_ok.fill(Qt.GlobalColor.green)
    _QImage_ok.save(ok_png)
    real = viewer.browser.loadResource(image_type, _QUrl.fromLocalFile(ok_png))
    check("正常的圖片不受影響（沒有被換成替代圖）",
          real is not None and not real.isNull() and real.width() == 120,
          str(real.width()) if real is not None else "None")

    viewer.close_tab_at(viewer._active)
    pump(300)

    # --- 搜尋：輸入防抖、上下一筆只換兩筆高亮、高亮上限 ----------------------
    search_doc = os.path.join(tmp, "search_perf.md")
    with open(search_doc, "w", encoding="utf-8") as handle:
        handle.write("# 搜尋效能\n\n" + "\n\n".join(
            f"第 {i} 段 zeta 內容。" for i in range(400)))
    viewer.open_path(search_doc, new_tab=True)
    pump(400)
    bar = viewer.find_bar
    bar.activate()
    pump(200)
    # activate() 會沿用上次留在輸入框裡的搜尋詞，先清乾淨再測防抖，
    # 否則 setText 的值和原本一樣就不算變更、textChanged 根本不發
    bar.input.setText("")
    app.processEvents()

    # 防抖：打完字的當下不該立刻掃整份文件
    bar.input.setText("zeta")
    app.processEvents()
    check("輸入後未過防抖時尚未比對", not bar._matches, str(len(bar._matches)))
    pump(bar.DEBOUNCE_MS + 250)
    check("防抖過後才真的比對", len(bar._matches) == 400, str(len(bar._matches)))

    # 清空是廉價操作，要立刻生效才跟手（不進防抖）
    bar.input.setText("")
    app.processEvents()
    check("清空搜尋詞立刻生效，不必等防抖", not bar._matches)

    # Enter 要先把還在等的防抖兌現，不能拿舊結果來跳
    bar.input.setText("zeta")
    app.processEvents()
    bar.search(forward=True)
    check("Enter 會先兌現防抖再跳（不是拿舊結果）",
          len(bar._matches) == 400 and bar._current_index == 1,
          f"matches={len(bar._matches)} index={bar._current_index}")

    # 上/下一筆只換兩筆高亮：其餘 selection 物件必須是同一批被沿用
    pump(200)
    before_list = list(bar._selections)   # 複製一份，讓舊物件不會被回收
    before_ids = [id(sel) for sel in before_list]
    index_before = bar._current_index
    bar.search(forward=True)
    app.processEvents()
    after_ids = [id(sel) for sel in bar._selections]
    reused = sum(1 for a, b in zip(before_ids, after_ids) if a == b)
    check("前置：高亮筆數與比對數一致（未達上限）",
          len(before_ids) == 400 and len(after_ids) == 400,
          f"{len(before_ids)} / {len(after_ids)}")
    check("下一筆只換掉兩筆高亮，其餘沿用",
          reused == len(before_ids) - 2,
          f"沿用 {reused} / {len(before_ids)}，換掉 {len(before_ids) - reused}")
    check("下一筆確實有前進，且記錄的已上色索引跟著更新",
          bar._current_index == (index_before + 1) % 400
          and bar._painted_current == bar._current_index,
          f"index={bar._current_index} painted={bar._painted_current}")

    # 回上一筆同樣只換兩筆
    back_list = list(bar._selections)     # 同上，抓住參照才能安全比對 id
    back_ids = [id(sel) for sel in back_list]
    bar.search(forward=False)
    app.processEvents()
    reused_back = sum(1 for a, b in zip(back_ids, [id(x) for x in bar._selections])
                      if a == b)
    check("上一筆也只換掉兩筆高亮",
          reused_back == len(back_ids) - 2,
          f"沿用 {reused_back} / {len(back_ids)}")

    # 換配色會連帶重新渲染（setHtml 把文件清空再填回），快取裡的 QTextCursor
    # 會整批被折到位置 0。這兩條同時守住「配色有換新」與「位置沒有塌掉」。
    theme_before = viewer._theme
    other_theme = "light" if theme_before == "dark" else "dark"
    viewer.apply_theme(other_theme)
    pump(400)
    positions = [sel.cursor.selectionStart() for sel in bar._selections]
    check("換配色重新渲染後高亮位置沒有塌到文件開頭",
          len(positions) > 2 and positions == sorted(positions)
          and positions[:3] == bar._matches[:3],
          f"前三筆 {positions[:3]} vs {bar._matches[:3]}")
    others = [sel for i, sel in enumerate(bar._selections)
              if i != bar._current_index - bar._window[0]]
    expected_bg = QColor(styles.palette(other_theme)["find_match_bg"]).name()
    check("換配色後高亮用的是新配色",
          bool(others)
          and others[0].format.background().color().name() == expected_bg,
          others[0].format.background().color().name() if others else "無")
    viewer.apply_theme(theme_before)
    pump(400)

    # 高亮上限：比對數不受限，但交給 Qt 的筆數要被夾住
    original_cap = type(bar).MAX_HIGHLIGHTS
    type(bar).MAX_HIGHLIGHTS = 50
    try:
        bar.input.setText("")
        app.processEvents()
        bar.input.setText("zeta")
        pump(bar.DEBOUNCE_MS + 250)
        check("超過上限時比對數仍是實數", len(bar._matches) == 400,
              str(len(bar._matches)))
        check("超過上限時交給 Qt 的高亮筆數被夾住",
              len(bar._selections) == 50, str(len(bar._selections)))
        check("超過上限時狀態列說明只高亮鄰近筆數",
              "400" in bar.status.text() and "50" in bar.status.text(),
              bar.status.text())
        # 窗格要涵蓋目前這一筆，否則使用者所在位置看不到高亮
        low, high = bar._window
        check("高亮窗格涵蓋目前這一筆",
              low <= bar._current_index < high,
              f"index={bar._current_index} window={bar._window}")
        # 窗格不該每跳一筆就滑動——否則快路徑永遠用不到
        window_before = bar._window
        bar.search(forward=True)
        app.processEvents()
        check("在窗格內前進時窗格不滑動（快路徑仍成立）",
              bar._window == window_before, f"{window_before} -> {bar._window}")
    finally:
        type(bar).MAX_HIGHLIGHTS = original_cap
    bar.deactivate()
    pump(200)

    # --- 搜尋比對選項：區分大小寫（Alt+C）、全字（Alt+W）---------------------
    check("預設不區分大小寫、不限全字",
          not bar.case_button.isChecked() and not bar.word_button.isChecked(),
          f"case={bar.case_button.isChecked()} whole={bar.word_button.isChecked()}")

    opt_doc = os.path.join(tmp, "find_options.md")
    with open(opt_doc, "w", encoding="utf-8") as handle:
        handle.write("# Cat\n\ncat category concat cat.\n\n"
                     "The the THE theme\n\n搜尋 搜尋列 全文搜尋\n")
    viewer.open_path(opt_doc, new_tab=True)
    pump(400)
    bar.activate()
    pump(200)

    def scan(text, case, whole):
        """把兩個選項擺好、搜一次，回傳比對數。"""
        bar.case_button.setChecked(case)
        bar.word_button.setChecked(whole)
        # 先清空再填：值和上次一樣的話 textChanged 根本不發（同 §防抖 那段）
        bar.input.setText("")
        app.processEvents()
        bar.input.setText(text)
        pump(bar.DEBOUNCE_MS + 250)
        return len(bar._matches)

    plain_the = scan("the", False, False)
    cased_the = scan("the", True, False)
    check("區分大小寫會濾掉 The / THE",
          plain_the == 4 and cased_the == 2, f"{plain_the} -> {cased_the}")
    check("兩個選項可以同時生效",
          scan("the", True, True) == 1, str(len(bar._matches)))

    plain_cat = scan("cat", False, False)
    whole_cat = scan("cat", False, True)
    check("全字會濾掉 category / concat（標點結尾仍算全字）",
          plain_cat == 5 and whole_cat == 3, f"{plain_cat} -> {whole_cat}")

    # 中文沒有空白斷詞，Qt 會把一整串中文當成同一個詞：這是 FindWholeWords
    # 的既定語意（和其他編輯器一致），刻意不做中文特例。寫成測試是為了讓
    # 「有人哪天覺得這是 bug 而去加特例」時，先看見這條說明。
    plain_cjk = scan("搜尋", False, False)
    whole_cjk = scan("搜尋", False, True)
    check("全字對中文以空白斷詞：「全文搜尋」內的「搜尋」不算相符",
          plain_cjk == 3 and whole_cjk == 1, f"{plain_cjk} -> {whole_cjk}")

    # 高亮的範圍要跟著選項走，不是只有計數變
    scan("the", True, False)
    cased_texts = {sel.cursor.selectedText() for sel in bar._selections}
    check("區分大小寫時高亮到的文字與搜尋詞完全相同",
          cased_texts == {"the"}, str(cased_texts))
    scan("the", False, False)
    plain_texts = {sel.cursor.selectedText() for sel in bar._selections}
    check("不區分大小寫時高亮涵蓋不同大小寫的寫法",
          len(plain_texts) > 1, str(plain_texts))

    # 點按鈕是一次明確的操作，不該還要等防抖才看到結果
    bar.case_button.setChecked(True)
    app.processEvents()
    check("切換選項立刻重算，不進防抖",
          len(bar._matches) == 2, str(len(bar._matches)))

    # 選項要落盤（比照設定面板：元件送訊號、viewer 寫 QSettings）
    bar.case_button.setChecked(True)
    bar.word_button.setChecked(False)
    app.processEvents()
    stored = QSettings(config.ORG_NAME, config.APP_NAME)
    check("選項變動會寫進 QSettings",
          stored.value(config.KEY_FIND_CASE_SENSITIVE, type=bool) is True
          and stored.value(config.KEY_FIND_WHOLE_WORDS, type=bool) is False,
          f"case={stored.value(config.KEY_FIND_CASE_SENSITIVE)} "
          f"whole={stored.value(config.KEY_FIND_WHOLE_WORDS)}")

    # set_options 是「載入時把外觀對上設定」，不是使用者操作，不該反向寫回去
    stored.setValue(config.KEY_FIND_WHOLE_WORDS, "哨兵")
    bar.set_options(True, True)
    app.processEvents()
    check("set_options 不會反向送出訊號（不覆寫設定）",
          stored.value(config.KEY_FIND_WHOLE_WORDS) == "哨兵",
          str(stored.value(config.KEY_FIND_WHOLE_WORDS)))
    check("set_options 有把按鈕的勾選狀態擺對",
          bar.case_button.isChecked() and bar.word_button.isChecked())

    # 比對規則變了就得重掃，否則按鈕亮著、高亮卻還是舊規則算出來的
    scan("the", False, False)
    bar.set_options(True, False)
    app.processEvents()
    check("搜尋列開著時 set_options 會重掃",
          len(bar._matches) == 2, str(len(bar._matches)))

    # blockSignals 會把 toggled 一起擋掉，而圖示重新著色正是掛在 toggled 上的：
    # 少了 set_checked_silently 裡那次 refresh_icon，按鈕會勾起來卻停在灰色。
    lit = bar.case_button.icon().pixmap(config.ICON_PIXEL_SIZE).toImage()
    bar.set_options(False, False)
    app.processEvents()
    dim = bar.case_button.icon().pixmap(config.ICON_PIXEL_SIZE).toImage()
    check("勾選狀態下圖示改用強調色（set_checked_silently 有補畫）",
          lit != dim)

    # Alt+C／Alt+W：tooltip 上就是這樣寫的，焦點不在輸入框時也必須生效
    from PyQt6.QtTest import QTest

    viewer.activateWindow()
    viewer.raise_()
    pump(300)
    viewer.browser.setFocus()
    pump(150)
    QTest.keyClick(viewer, Qt.Key.Key_C, Qt.KeyboardModifier.AltModifier)
    QTest.keyClick(viewer, Qt.Key.Key_W, Qt.KeyboardModifier.AltModifier)
    pump(200)
    check("焦點在閱讀區時 Alt+C／Alt+W 仍能切換選項",
          bar.case_button.isChecked() and bar.word_button.isChecked(),
          f"case={bar.case_button.isChecked()} whole={bar.word_button.isChecked()}")
    typed = bar.input.text()
    check("Alt+W 不會把 w 打進搜尋框", "w" not in typed, repr(typed))

    # 收起來的搜尋列不該還吃這兩個快捷鍵
    bar.set_options(False, False)
    bar.deactivate()
    pump(200)
    QTest.keyClick(viewer, Qt.Key.Key_C, Qt.KeyboardModifier.AltModifier)
    pump(150)
    check("搜尋列收起時 Alt+C 不生效",
          not bar.case_button.isChecked())

    # 開啟新視窗要沿用上次的選項（讀取發生在 _build_ui 之前）
    stored.setValue(config.KEY_FIND_CASE_SENSITIVE, True)
    stored.setValue(config.KEY_FIND_WHOLE_WORDS, True)
    stored.sync()
    fresh = MarkdownViewer()
    check("新視窗建構時就套用上次的搜尋選項",
          fresh.find_bar.case_button.isChecked()
          and fresh.find_bar.word_button.isChecked(),
          f"case={fresh.find_bar.case_button.isChecked()} "
          f"whole={fresh.find_bar.word_button.isChecked()}")
    fresh.close()
    pump(250)

    # 恢復預設要把這兩項也一起帶回去
    viewer.find_bar.set_options(True, True)
    viewer.reset_settings()
    pump(300)
    check("恢復預設會把搜尋選項一起關掉",
          not bar.case_button.isChecked() and not bar.word_button.isChecked()
          and stored.value(config.KEY_FIND_CASE_SENSITIVE, type=bool) is False
          and stored.value(config.KEY_FIND_WHOLE_WORDS, type=bool) is False,
          f"case={bar.case_button.isChecked()} whole={bar.word_button.isChecked()} "
          f"stored={stored.value(config.KEY_FIND_CASE_SENSITIVE)}/"
          f"{stored.value(config.KEY_FIND_WHOLE_WORDS)}")

    # --- 搜尋列是可拖曳的浮動面板 --------------------------------------------
    from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect
    from PyQt6.QtGui import QMouseEvent

    def send_mouse(widget, kind, pos):
        button = Qt.MouseButton.LeftButton
        buttons = (Qt.MouseButton.NoButton
                   if kind == QEvent.Type.MouseButtonRelease else button)
        app.sendEvent(widget, QMouseEvent(
            kind, QPointF(pos), QPointF(widget.mapToGlobal(pos)),
            button, buttons, Qt.KeyboardModifier.NoModifier))

    def drag_bar(delta):
        """從把手中心按下、拖 delta、放開（走真正的滑鼠事件處理）。"""
        start = bar.grip.mapTo(bar, bar.grip.rect().center())
        send_mouse(bar, QEvent.Type.MouseButtonPress, start)
        send_mouse(bar, QEvent.Type.MouseMove, start + delta)
        send_mouse(bar, QEvent.Type.MouseButtonRelease, start + delta)
        app.processEvents()

    def edges(rect):
        return rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height()

    # 讓位的測試要用長文件（400 段），短文件捲不動
    viewer.activate_tab(viewer._index_of_path(search_doc))
    pump(300)
    stack_before = QRect(viewer.stack.geometry())
    viewer.show_find()
    pump(300)
    check("搜尋列是浮動面板，開啟時不推開內文",
          viewer.stack.geometry() == stack_before,
          f"{edges(stack_before)} -> {edges(viewer.stack.geometry())}")
    sx, sy, sr, sb = edges(viewer.stack.geometry())
    bx, by, br, bb = edges(bar.geometry())
    dx0, dy0 = config.DEFAULT_FIND_BAR_OFFSET
    check("預設停在內文區右上角",
          bar.isVisible() and sr - br == dx0 and by - sy == dy0,
          f"右緣差 {sr - br} 上緣差 {by - sy}")

    # 【浮動面板一定要不透明】純 QWidget 預設不套用 QSS 的 background-color 與
    # border，要開 WA_StyledBackground 才會畫（設定面板早就踩過同一個坑）。
    # 排在版面裡時看不出來，浮到內文上就是整塊透明：文字直接透出來、輸入框
    # 看起來跟頁面同色，像印在文章上。取樣面板左邊框與把手之間那條純背景。
    width_before = viewer._content_width
    viewer.set_content_width(0)          # 不限寬，文字才會鋪到面板底下
    pump(500)
    surface = styles.palette(viewer._theme)["surface"].lower()
    shot = viewer.grab().toImage()
    shot_dpr = shot.width() / viewer.width()   # grab 是實體像素，幾何是邏輯像素
    g = bar.geometry()
    strip = [shot.pixelColor(int((g.x() + 3 + 1) * shot_dpr),
                             int((g.y() + y + 1) * shot_dpr)).name()
             for y in range(12, g.height() - 12)]
    check("搜尋列浮在內文上時不透明（面板背景與外框真的畫出來）",
          bool(strip) and set(strip) == {surface},
          f"面板色 {surface}，取樣到 {sorted(set(strip))[:4]}")
    viewer.set_content_width(width_before)
    pump(400)

    before = bar.geometry()
    drag_bar(QPoint(-300, 200))
    moved = bar.geometry()
    check("拖曳把手會移動面板",
          moved.topLeft() == before.topLeft() + QPoint(-300, 200),
          f"{edges(before)} -> {edges(moved)}")
    check("放開後換算成最近的角落（落在左上半邊 -> TL）",
          bar._corner == "TL"
          and bar._corner_offset == QPoint(moved.x() - sx, moved.y() - sy),
          f"{bar._corner} {bar._corner_offset}")
    check("拖曳放開後位置寫進 QSettings",
          stored.value(config.KEY_FIND_BAR_CORNER) == "TL"
          and stored.value(config.KEY_FIND_BAR_OFFSET_X, type=int) == moved.x() - sx
          and stored.value(config.KEY_FIND_BAR_OFFSET_Y, type=int) == moved.y() - sy,
          f"{stored.value(config.KEY_FIND_BAR_CORNER)} "
          f"{stored.value(config.KEY_FIND_BAR_OFFSET_X)}/"
          f"{stored.value(config.KEY_FIND_BAR_OFFSET_Y)}")

    drag_bar(QPoint(-2000, 2000))
    check("拖出內文區會被夾在邊界內（並錨到左下）",
          viewer.stack.geometry().contains(bar.geometry()) and bar._corner == "BL",
          f"{edges(bar.geometry())} in {edges(viewer.stack.geometry())} {bar._corner}")

    bar.set_placement("TL", 5000, 5000)
    check("位移超出範圍時 reposition 會夾回內文區",
          viewer.stack.geometry().contains(bar.geometry()),
          f"{edges(bar.geometry())} vs {edges(viewer.stack.geometry())}")
    bar.set_placement("BL", 0, 0)

    # 錨定：放在左下的面板，視窗縮放時要一直貼著左下
    viewer.resize(640, 500)
    pump(400)
    sx, sy, sr, sb = edges(viewer.stack.geometry())
    small = bar.geometry()
    viewer.resize(1120, 820)
    pump(400)
    sx2, sy2, sr2, sb2 = edges(viewer.stack.geometry())
    big = bar.geometry()
    check("視窗縮放時面板跟著錨定的角落走（左下）",
          viewer.stack.geometry().contains(big)
          and small.x() - sx == big.x() - sx2 == 0
          and sb - (small.y() + small.height()) == sb2 - (big.y() + big.height()) == 0,
          f"小 {edges(small)}/{(sx, sb)} 大 {edges(big)}/{(sx2, sb2)}")

    # 讓位：面板放在底部「靠左」、蓋住文字欄，連按下一筆，目前那筆不得被壓住。
    # 一定要蓋到文字欄：放在底部正中的話文字（靠左）和面板在水平方向根本不相交，
    # 有沒有讓位邏輯都是 0 次，測試等於沒測（實測拿掉 _reveal_current 照樣全綠）。
    bar.input.setText("")
    app.processEvents()
    bar.input.setText("zeta")
    pump(bar.DEBOUNCE_MS + 300)
    sx, sy, sr, sb = edges(viewer.stack.geometry())
    target = QPoint(sx + 40, sb - bar.height() - 4)
    drag_bar(target - bar.geometry().topLeft())
    first_rect = viewer.browser.cursorRect()
    bar_in_vp = QRect(viewer.browser.viewport().mapFromGlobal(
        bar.mapToGlobal(QPoint(0, 0))), bar.size())
    check("前置：面板在水平方向確實蓋到文字欄（否則下一條測不到東西）",
          bar_in_vp.left() <= first_rect.left() <= bar_in_vp.right(),
          f"文字 x={first_rect.left()} 面板 x={bar_in_vp.left()}..{bar_in_vp.right()}")
    covered = 0
    for _ in range(40):
        bar.search(forward=True)
        app.processEvents()
        vp = viewer.browser.viewport()
        bar_in_vp = QRect(vp.mapFromGlobal(bar.mapToGlobal(QPoint(0, 0))), bar.size())
        if bar_in_vp.intersects(viewer.browser.cursorRect()):
            covered += 1
    check("被面板壓住的相符項會自動讓開（連按 40 次下一筆）",
          len(bar._matches) == 400 and covered == 0,
          f"matches={len(bar._matches)} 壓住 {covered} 次")

    # 狀態列收起：內文區變高，錨在底部的面板要跟著下移
    gap_before = edges(viewer.stack.geometry())[3] - (bar.y() + bar.height())
    viewer.set_status_bar_visible(False)
    pump(300)
    gap_after = edges(viewer.stack.geometry())[3] - (bar.y() + bar.height())
    viewer.set_status_bar_visible(True)
    pump(300)
    check("狀態列收起時錨在底部的面板跟著移動",
          gap_before == gap_after, f"{gap_before} -> {gap_after}")

    # 新視窗建構時就套用上次的位置
    corner_now, offset_now = bar._corner, QPoint(bar._corner_offset)
    fresh = MarkdownViewer(search_doc)
    fresh.resize(900, 600)
    fresh.show()
    pump(500)
    fresh.show_find()
    pump(300)
    check("新視窗建構時就套用上次的搜尋列位置",
          fresh.find_bar._corner == corner_now
          and fresh.find_bar._corner_offset == offset_now,
          f"{fresh.find_bar._corner} {fresh.find_bar._corner_offset} "
          f"vs {corner_now} {offset_now}")
    fresh.close()
    pump(250)

    viewer.reset_settings()
    pump(300)
    check("恢復預設會把搜尋列位置一起帶回右上",
          bar._corner == config.DEFAULT_FIND_BAR_CORNER
          and bar._corner_offset == QPoint(*config.DEFAULT_FIND_BAR_OFFSET)
          and stored.value(config.KEY_FIND_BAR_CORNER) == config.DEFAULT_FIND_BAR_CORNER,
          f"{bar._corner} {bar._corner_offset} "
          f"stored={stored.value(config.KEY_FIND_BAR_CORNER)}")
    bar.deactivate()
    pump(200)

    # -- YAML front matter 屬性表 -------------------------------------------
    # 以前：文件開頭 ---…--- 的 metadata 會被畫成水平線＋setext 大標題。
    # 現在：合格的 YAML front matter 被剝離、組成文件頂端一個淡色的小型鍵／值
    #       屬性表；不合格（未關閉、中間有非 YAML 行、前面有空行、空 front
    #       matter）一律退回原樣交給 Markdown，文件絕不會開不了。
    fm_basic = _doc_probe.markdown_to_html(
        "---\ntitle: 我的文章\nauthor: Sam\n---\n# 內文\n", "light")
    check("front matter 基本鍵值顯示成小型屬性表",
          '<table class="frontmatter"' in fm_basic
          and '<td class="fmkey">title</td>' in fm_basic
          and "我的文章" in fm_basic and "author" in fm_basic,
          fm_basic[:200])

    fm_block = _doc_probe.markdown_to_html(
        "---\ntags:\n  - python\n  - qt\n---\nx\n", "light")
    fm_inline = _doc_probe.markdown_to_html(
        "---\nlangs: [zh, en]\n---\nx\n", "light")
    check("front matter 區塊清單與行內清單都以「、」併呈",
          "python、qt" in fm_block and "zh、en" in fm_inline,
          f"block={('python、qt' in fm_block)} inline={('zh、en' in fm_inline)}")

    fm_quote = _doc_probe.markdown_to_html(
        '---\ntitle: "帶引號"\n---\nx\n', "light")
    check("front matter 值的成對引號被剝除",
          "帶引號" in fm_quote and "&quot;帶引號&quot;" not in fm_quote,
          fm_quote[:200])

    # '...' 後刻意留一行空白：讓「錨點」那條的 body 位移突變（close+2）只吃掉這
    # 行空白而非「內文」，兩條突變因此互相隔離、各自只讓自己那條紅。
    fm_dots = _doc_probe.markdown_to_html("---\ntitle: x\n...\n\n內文\n", "light")
    check("front matter 以 ... 結尾也認得",
          'class="frontmatter"' in fm_dots and "內文" in fm_dots,
          fm_dots[:200])

    # 第一行 --- 後接一個合法鍵行 title: 標題，再接不像 YAML 的散文行「關於本文」，
    # 讓 _parse 能產出非空 pairs——成表與否只由 _looks_like_yaml_line 這一關決定。
    fm_hr = _doc_probe.markdown_to_html(
        "---\ntitle: 標題\n關於本文\n---\n正文\n", "light")
    check("--- 當水平線的普通文件不被誤判成 front matter",
          'class="frontmatter"' not in fm_hr and "關於本文" in fm_hr,
          fm_hr[:200])

    fm_blank = _doc_probe.markdown_to_html("\n---\ntitle: x\n---\n", "light")
    check("front matter 前面有空行就不算（第一行必須正好是 ---）",
          'class="frontmatter"' not in fm_blank, fm_blank[:200])

    fm_esc = _doc_probe.markdown_to_html("---\ntitle: <b>粗</b>\n---\nx\n", "light")
    check("front matter 值裡的 HTML/Markdown 被 escape 不解析",
          "&lt;b&gt;粗&lt;/b&gt;" in fm_esc and "<b>粗</b>" not in fm_esc,
          fm_esc[:200])

    fm_prog_doc = "---\ntitle: 首屏\n---\n\n" + ("## 段\n\n內容一段。\n\n" * 250)
    fm_prog_page = _doc_probe.render_document(
        fm_prog_doc, _doc_probe.meta_for_pasted(fm_prog_doc), "light")
    fm_head, fm_chunks = _doc_probe.split_for_progressive_render(fm_prog_page)
    fm_after_key = (fm_head[fm_head.index("frontmatter"):]
                    if "frontmatter" in fm_head else "")
    check("分段渲染時 front matter 表在首屏且是完整一個表格",
          bool(fm_chunks) and 'class="frontmatter"' in fm_head
          and fm_head.count('class="frontmatter"') == 1
          and "</table>" in fm_after_key,
          f"chunks={len(fm_chunks)} "
          f"count={fm_head.count('class=' + chr(34) + 'frontmatter' + chr(34))}")

    fm_tab = _DT()
    fm_tab.set_pasted("---\nsource: 剪貼簿\n---\n內容\n")
    fm_paste = fm_tab.build_html("light")
    check("貼上的 Markdown 走同一個轉換，front matter 也成表",
          'class="frontmatter"' in fm_paste and "剪貼簿" in fm_paste,
          fm_paste[:200])

    fm_css_light = styles.build_doc_css("light", 11.0, 160, "zh_TW")
    fm_css_dark = styles.build_doc_css("dark", 11.0, 160, "zh_TW")
    check("深淺色主題各有 front matter 屬性表樣式",
          all(sel in fm_css_light for sel in
              ("table.frontmatter", "td.fmkey", "td.fmval"))
          and all(sel in fm_css_dark for sel in
                  ("table.frontmatter", "td.fmkey", "td.fmval")),
          "light/dark 三個選擇器")

    # 渲染路徑會剝離 front matter，但字數統計在 read_text_file 對全文算、不剝離。
    # 對照 full（含 front matter）與 body_only（剝離後）證明兩者不同，再釘死
    # char_count 照 full 算——這才驗到「渲染剝離、統計不剝離」這條不變式。
    fm_wc_path = os.path.join(tmp, "fm_wordcount.md")
    fm_wc_src = "---\ntitle: 統計\ntags: [a, b]\n---\n\n正文一段。\n"
    with open(fm_wc_path, "w", encoding="utf-8") as handle:
        handle.write(fm_wc_src)
    fm_wc_text, fm_wc_meta = _doc_probe.read_text_file(fm_wc_path)
    fm_wc_full = sum(map(len, fm_wc_text.split()))
    fm_wc_body = sum(map(len, _doc_probe.split_front_matter(fm_wc_text)[1].split()))
    check("front matter 不影響原始字數（渲染剝離、統計照全文算）",
          'class="frontmatter"' in _doc_probe.markdown_to_html(fm_wc_text, "light")
          and fm_wc_full > fm_wc_body
          and fm_wc_meta.char_count == fm_wc_full,
          f"full={fm_wc_full} body_only={fm_wc_body} char_count={fm_wc_meta.char_count}")

    fm_anchor = _doc_probe.markdown_to_html("---\ntitle: x\n---\n## 表格\n", "light")
    check("front matter 後的標題錨點與內文不受剝離影響",
          'class="frontmatter"' in fm_anchor and 'name="表格"' in fm_anchor,
          fm_anchor[:200])

    fm_mermaid = _doc_probe.markdown_to_html(
        "---\ntitle: 圖\n---\n\n```mermaid\ngraph TD\nA-->B\n```\n", "light")
    check("front matter 剝離在 Mermaid 前處理之前（兩者並存）",
          'class="frontmatter"' in fm_mermaid and 'src="mermaid:' in fm_mermaid,
          fm_mermaid[:200])

    fm_unclosed = _doc_probe.markdown_to_html(
        "---\ntitle: 未關閉\nauthor: 保留\n", "light")
    check("沒有結尾分隔符的 front matter 退回原樣、不吞內文",
          'class="frontmatter"' not in fm_unclosed and "title: 未關閉" in fm_unclosed,
          fm_unclosed[:200])

    viewer.close()
    pump(300)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()


# ===========================================================================
# 區塊：渲染快取
# ===========================================================================
def section_render_cache(args) -> None:
    """HTML 快取與轉換器重用——重點是「快得對」，不只是快。

    轉換器重用最危險的是狀態累積：Markdown 實例會留住註腳、參考連結、toc
    與 htmlStash（本專案用來把 fenced code 取出再包成表格的那個）。少了
    reset()，第二份文件會帶著第一份的註腳、程式碼區塊會整段錯位。因此這裡
    比對的是「與全新轉換器逐字元相同」，而不是「看起來沒壞」。
    """
    import re

    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=250):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app import document, styles
    from app.viewer import MarkdownViewer

    # 測試文件用 join 組出來（NL 即換行），避免多行字面值在編輯過程被
    # 跳脫層弄斷；內容本身要能同時觸發註腳、參考連結與程式碼區塊三種
    # 會殘留在轉換器狀態裡的東西。
    NL = chr(10)
    doc_a = NL.join([
        "# 文件 A", "", "有註腳[^a1] 與另一個[^a2]。", "",
        "參考 [x][r1]。", "", "[r1]: https://example.com/one", "",
        "```python", "def alpha():", "    return 1", "```", "",
        "[^a1]: 註腳 A1", "[^a2]: 註腳 A2", "",
    ])
    doc_b = NL.join([
        "# 文件 B", "", "不同註腳[^b1]。", "", "[y][rb]", "",
        "[rb]: https://example.org/b", "",
        "```javascript", "const beta = () => 2;", "```", "",
        "[^b1]: 註腳 B1", "",
    ])

    def visible(markup: str) -> str:
        # pygments 會把 def / alpha 拆進不同 span，比對可見文字才有意義
        return re.sub(r"<[^>]+>", "", markup)

    for theme in ("dark", "light"):
        fresh_a = document._build_converter(theme).convert(doc_a)
        fresh_b = document._build_converter(theme).convert(doc_b)
        document._CONVERTERS.clear()
        seq = [document.markdown_to_html(d, theme)
               for d in (doc_a, doc_b, doc_a, doc_b, doc_a)]
        check(f"[{theme}] 重用轉換器：A 與全新結果逐字元相同",
              all(x == fresh_a for x in seq[0::2]))
        check(f"[{theme}] 重用轉換器：B 與全新結果逐字元相同",
              all(x == fresh_b for x in seq[1::2]))
        check(f"[{theme}] 第二份文件沒沾到前一份的註腳", "註腳 A1" not in seq[1])
        check(f"[{theme}] 第二份文件沒沾到前一份的參考連結",
              "example.com/one" not in seq[1])
        text_a, text_b = visible(seq[0]), visible(seq[1])
        check(f"[{theme}] 程式碼區塊沒有跨文件錯位",
              "def alpha():" in text_a and "const beta" in text_b
              and "const beta" not in text_a)

    document._CONVERTERS.clear()
    dark_html = document.markdown_to_html(doc_a, "dark")
    check("深淺色的高亮輸出不同（顏色寫死在 inline style）",
          dark_html != document.markdown_to_html(doc_a, "light"))
    check("換過主題後再轉回來仍與全新一致",
          document.markdown_to_html(doc_a, "dark") == dark_html)

    # --- HTML 快取 ---
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    target = _write_big_doc(tempfile.mkdtemp(prefix="mdbig-"), "cache_big.md")
    viewer = MarkdownViewer(target)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.resize(1000, 700)
    viewer.show()
    pump(600)

    tab = viewer._tab
    check("渲染後留下該主題的快取", viewer._theme in tab._html_cache)
    first_html = tab.build_html(viewer._theme)
    check("同一主題再取是同一個物件（命中而非重算）",
          tab.build_html(viewer._theme) is first_html)

    other_theme = "light" if viewer._theme == "dark" else "dark"
    # 換主題要不要重轉，取決於這份文件有沒有高亮輸出（theme_sensitive）：
    # 有 -> 各主題各留一份；沒有 -> 共用同一份、省整份重轉。兩側的確定性
    # 行為已由上面「無程式碼／含程式碼」兩個固定文件釘死，這裡驗的是
    # 「真實檔案走的路必須和判定一致」，不隨檔案內容變動而失效。
    check("換主題是否重轉，與 theme_sensitive 的判定一致",
          (tab.build_html(other_theme) is first_html)
          == (not document.theme_sensitive(first_html)),
          f"敏感={document.theme_sensitive(first_html)}，"
          f"共用={tab.build_html(other_theme) is first_html}")
    check("兩個主題各留一份，切回原主題直接命中",
          tab.build_html(viewer._theme) is first_html)

    # 內容變了（重新載入）必須自動失效：鍵是文字物件的識別
    tab.text = tab.text + NL + NL + "新增一段" + NL
    check("內容改變後快取自動失效",
          tab.build_html(viewer._theme) is not first_html)

    # 字級縮放不該重跑 Markdown 轉換
    conversions = {"n": 0}
    original_convert = document.markdown_to_html

    def counting_convert(text, theme):
        conversions["n"] += 1
        return original_convert(text, theme)

    document.markdown_to_html = counting_convert
    try:
        viewer._render()          # 暖機，確保目前內容已進快取
        conversions["n"] = 0
        viewer.zoom_in()
        pump(400)
        viewer.zoom_out()
        pump(400)
    finally:
        document.markdown_to_html = original_convert
    check("字級縮放不重跑 Markdown 轉換（快取命中）",
          conversions["n"] == 0, f"轉換了 {conversions['n']} 次")

    # 大檔素材有上百個頂層元素，會走分段渲染；縮放之後剩餘片段還在排隊，
    # resize 裡的 processEvents 會把它們補上、revision 跟著加。先補完，
    # 下面數的才是「縮放引起的重排」。（以前的素材幾乎全是清單，一個 <ul>
    # 算一個元素，根本沒觸發分段，所以沒踩到。）
    viewer.flush_pending_chunks()
    pump(300)

    # --- 內文寬度未變時跳過整份重排 -----------------------------------------
    # _apply_content_width 每個 resize 事件都會被呼叫，而 setFrameFormat 會讓
    # 整份文件重新排版。算出來的邊距沒變就該直接跳過。
    # 用文件的 revision 來數「真的動到文件幾次」：setFrameFormat 每呼叫一次
    # revision 就 +1，連設成同一個值也算——它數的正是實際發生的重排，而不是
    # 「算出來的邊距變了幾次」（後者拿掉守門也一樣，根本測不出差別）。
    def count_reflows(action):
        doc = viewer.browser.document()
        before = doc.revision()
        action()
        return doc.revision() - before

    viewer.set_content_width(720)
    viewer.resize(1100, 700)
    pump(400)
    check("前置：限寬後邊距已套上（不等於預設邊距）",
          viewer._tab.applied_side_margin != styles.DOCUMENT_MARGIN,
          str(viewer._tab.applied_side_margin))

    def resize_height_only():
        for step in range(12):
            viewer.resize(1100, 700 - step * 6)
            app.processEvents()

    check("限寬時只改高度不觸發重排",
          count_reflows(resize_height_only) == 0)

    viewer.resize(1100, 700)
    pump(300)

    def resize_width_only():
        for step in range(12):
            viewer.resize(1100 - step * 10, 700)
            app.processEvents()

    # 限寬時左右邊距是依可視寬度算的，改寬度就真的得重排——這條是對照組，
    # 確認守門不是「一律跳過」那種假優化
    check("限寬時改寬度仍會重排（守門沒有蓋掉必要的重算）",
          count_reflows(resize_width_only) > 0)

    # 不限寬時邊距恆等於預設值，連改寬度都不該重排
    viewer.set_content_width(0)
    viewer.resize(1100, 700)
    pump(400)
    check("不限寬時改寬度也不觸發重排",
          count_reflows(resize_width_only) == 0)

    # 文件重建（setHtml）會把邊距打回預設，快取必須跟著失效
    viewer.set_content_width(720)
    viewer.resize(1100, 700)
    pump(400)
    applied = viewer._tab.applied_side_margin
    viewer._mark_all_dirty()
    viewer._render()
    pump(300)
    check("重新渲染後邊距重新套上（快取沒有誤判為未變）",
          viewer._tab.applied_side_margin == applied,
          f"{viewer._tab.applied_side_margin} vs {applied}")
    frame_fmt = viewer.browser.document().rootFrame().frameFormat()
    check("重新渲染後 root frame 的左邊距確實是算出來的值",
          abs(frame_fmt.leftMargin() - applied) < 0.5,
          f"frame={frame_fmt.leftMargin()} applied={applied}")

    # --- 分段渲染（先見首屏）------------------------------------------------
    # 大文件一次 setHtml 會整份排完版才回來，那段時間介面完全凍結。改成先把
    # 首屏交出去、其餘分批補上。最要緊的性質是「補完後的內容和一次到底完全
    # 一樣」——分段是效能手段，不能改變任何看得到的結果。
    tmp = tempfile.mkdtemp()
    big_doc = os.path.join(tmp, "progressive.md")
    with open(big_doc, "w", encoding="utf-8") as handle:
        handle.write("# 分段渲染" + NL * 2 + (NL * 2).join(
            f"第 {i} 段內容，帶 **粗體** 與 `程式碼`。" for i in range(1300)))

    # 同一個檔案不會重複開（已開著時直接切過去、不重新渲染），
    # 因此每個需要「剛渲染完、還有待補片段」的子測試都要用不同的檔案
    big_doc2 = os.path.join(tmp, "progressive2.md")
    big_doc3 = os.path.join(tmp, "progressive3.md")
    big_doc4 = os.path.join(tmp, "progressive4.md")
    for extra in (big_doc2, big_doc3, big_doc4):
        with open(extra, "w", encoding="utf-8") as handle:
            handle.write("# 分段渲染" + NL * 2 + (NL * 2).join(
                f"第 {i} 段內容，帶 **粗體** 與 `程式碼`。" for i in range(1300)))

    small_doc = os.path.join(tmp, "not_progressive.md")
    with open(small_doc, "w", encoding="utf-8") as handle:
        handle.write((NL * 2).join(f"短文第 {i} 段。" for i in range(20)))

    viewer2 = MarkdownViewer()
    viewer2.resize(1000, 700)
    viewer2.show()
    pump(400)
    # 這裡故意不 pump：片段是靠事件迴圈補的，一 pump 就補完了，
    # 「首屏先出來」這件事只有在 open_path 回來的當下量得到
    viewer2.open_path(big_doc, push_history=False)
    check("大文件開啟後有待補的片段（分段生效）",
          len(viewer2._tab.pending_chunks) > 0,
          str(len(viewer2._tab.pending_chunks)))
    partial_len = len(viewer2.browser.toPlainText())

    # 一次到底的對照組：把門檻調高到不可能達到，等於關掉分段
    saved_min = config.PROGRESSIVE_MIN_ELEMENTS
    config.PROGRESSIVE_MIN_ELEMENTS = 10 ** 9
    try:
        viewer3 = MarkdownViewer(big_doc)
        viewer3.resize(1000, 700)
        viewer3.show()
        pump(400)
        check("對照組：門檻調高後不分段", not viewer3._tab.pending_chunks)
        oneshot_text = viewer3.browser.toPlainText()
        viewer3.close()
        pump(200)
    finally:
        config.PROGRESSIVE_MIN_ELEMENTS = saved_min

    viewer2.flush_pending_chunks()
    pump(200)
    check("補完後不再有待補片段", not viewer2._tab.pending_chunks)
    check("首屏確實只是一部分（補完後內容變多）",
          len(viewer2.browser.toPlainText()) > partial_len,
          f"首屏 {partial_len} -> 補完 {len(viewer2.browser.toPlainText())}")
    check("分段補完的內容與一次到底逐字元相同",
          viewer2.browser.toPlainText() == oneshot_text,
          f"分段 {len(viewer2.browser.toPlainText())} vs 一次 {len(oneshot_text)}")

    # 小文件不該被分段：本來就快，繞路只是多花力氣
    viewer2.open_path(small_doc, new_tab=True)
    pump(400)
    check("小文件不分段", not viewer2._tab.pending_chunks)
    viewer2.close_tab_at(viewer2._active)
    pump(300)

    # 切分頁時要先把離開中的分頁補完，否則它會永遠停在只有首屏的狀態，
    # 而計時器接下來讀的是新分頁的 pending_chunks，舊的再也沒人管。
    # 這裡刻意完全不 pump：片段是靠事件迴圈補的，一 pump 就補完了，
    # 「切走的當下有沒有補完」只有在不回事件迴圈的情況下量得到。
    other_doc2 = os.path.join(tmp, "other_for_switch.md")
    with open(other_doc2, "w", encoding="utf-8") as handle:
        handle.write("# 另一份" + NL * 2 + "只有這一行。")

    viewer2.open_path(big_doc2, new_tab=True)
    big_tab = viewer2._tab
    check("前置：新開的大文件分頁確實有多塊待補",
          len(big_tab.pending_chunks) > 1, str(len(big_tab.pending_chunks)))
    viewer2.open_path(other_doc2, new_tab=True)      # 直接切走，不回事件迴圈
    check("開新分頁切走時，原分頁已被補完（不會停在只有首屏）",
          not big_tab.pending_chunks, str(len(big_tab.pending_chunks)))
    check("切走後原分頁的內容是完整的",
          "第 1299 段" in big_tab.browser.toPlainText())
    pump(300)
    check("切分頁後新分頁的內容沒有被前一個分頁的片段污染",
          "第 1299 段" not in viewer2.browser.toPlainText()
          and "另一份" in viewer2.browser.toPlainText())
    big_index = next(i for i, t in enumerate(viewer2._tabs)
                     if t.path == os.path.abspath(big_doc))
    viewer2.activate_tab(big_index)
    pump(400)
    viewer2.flush_pending_chunks()
    check("切回原分頁時內容仍然完整",
          "第 1299 段" in viewer2.browser.toPlainText())

    # 用分頁列切換走的是 activate_tab，和上面 open_path 開新分頁那條不同，
    # 兩條都要補完（實測拿掉任一條，另一條的測試都不會紅）
    viewer2.open_path(big_doc4, new_tab=True)
    bar_tab = viewer2._tab
    check("前置：分頁列切換前有多塊待補",
          len(bar_tab.pending_chunks) > 1, str(len(bar_tab.pending_chunks)))
    viewer2.activate_tab(0)                          # 中間不回事件迴圈
    check("用分頁列切走時，原分頁也會被補完",
          not bar_tab.pending_chunks, str(len(bar_tab.pending_chunks)))
    check("分頁列切走後原分頁內容完整",
          "第 1299 段" in bar_tab.browser.toPlainText())
    pump(200)

    # 搜尋要掃整份文件：show_find 之前必須補完，否則比對數是錯的
    viewer2.open_path(big_doc3, new_tab=True)
    check("前置：搜尋前確實還有待補片段",
          len(viewer2._tab.pending_chunks) > 1)
    viewer2.show_find()                              # 中間不回事件迴圈
    check("show_find 會先把片段補完", not viewer2._tab.pending_chunks,
          str(len(viewer2._tab.pending_chunks)))
    viewer2.find_bar.input.setText("")
    app.processEvents()
    viewer2.find_bar.input.setText("段內容")
    pump(viewer2.find_bar.DEBOUNCE_MS + 300)
    check("分段渲染的文件搜尋得到全部相符項",
          len(viewer2.find_bar._matches) == 1300,
          str(len(viewer2.find_bar._matches)))
    viewer2.find_bar.deactivate()

    viewer2.close()
    pump(300)

    viewer.close()
    pump(300)

    # --- 無高亮輸出的文件，換主題不重轉 -------------------------------------
    # 主題唯一流進轉換的地方是程式碼高亮的配色；輸出裡連一個 codecell 都
    # 沒有的文件，兩個主題的 HTML 逐位元組相同，重轉純屬白工（3000 行文件
    # 實測約 200ms/次，1MB 文件 518ms）。這組釘住三件事：
    #   1. 無程式碼 -> 換主題直接命中快取（不重轉、兩鍵共用同一份物件）
    #   2. 含程式碼 -> 照舊各轉一次，且兩份 HTML 真的不同（顏色不同）
    #   3. 判定方向保守 -> theme_sensitive 只在輸出含 codecell 時為真
    from datetime import datetime as _dt

    from app.document_tab import DocumentTab as _DT

    _plain = NL.join(f"## 標 {i}" + NL + NL + f"內文 {i}" for i in range(60))
    _coded = _plain + NL + NL + "```python" + NL + "def f():" + NL + "    return 1" + NL + "```" + NL

    _convert_calls = []
    _orig_convert = document.markdown_to_html

    def _counting(text, theme):
        _convert_calls.append(theme)
        return _orig_convert(text, theme)

    def _theme_tab(text):
        tab = _DT.__new__(_DT)
        tab.error = None
        tab.text = text
        tab.meta = document.DocumentMeta(
            path="x.md", encoding="utf-8", size_bytes=len(text),
            modified=_dt(2026, 1, 1), char_count=len(text), line_count=60)
        tab._html_cache = {}
        return tab

    document.markdown_to_html = _counting
    try:
        plain_tab = _theme_tab(_plain)
        _convert_calls.clear()
        plain_light = plain_tab.build_html("light")
        plain_dark = plain_tab.build_html("dark")
        check("無程式碼文件換主題：不重轉（一次轉換、兩鍵共用同一份）",
              len(_convert_calls) == 1 and plain_dark is plain_light,
              f"轉了 {len(_convert_calls)} 次，共用={plain_dark is plain_light}")

        coded_tab = _theme_tab(_coded)
        _convert_calls.clear()
        coded_light = coded_tab.build_html("light")
        coded_dark = coded_tab.build_html("dark")
        check("含程式碼文件換主題：照舊各轉一次，且兩份 HTML 不同",
              len(_convert_calls) == 2 and coded_light != coded_dark,
              f"轉了 {len(_convert_calls)} 次，相同={coded_light == coded_dark}")
    finally:
        document.markdown_to_html = _orig_convert

    check("theme_sensitive 判定方向：有 codecell 才算敏感",
          document.theme_sensitive(coded_light)
          and not document.theme_sensitive(plain_light),
          f"含程式碼={document.theme_sensitive(coded_light)}，"
          f"無程式碼={document.theme_sensitive(plain_light)}")

    QSettings(config.ORG_NAME, config.APP_NAME).clear()


# ===========================================================================
# 區塊：介面語言
# ===========================================================================
def section_language(args) -> None:
    """語言檔的完整性與一致性。

    這些檢查不需要開視窗，成本極低，但擋掉的是最惱人的一類錯誤：翻譯漏一條、
    佔位符打錯名字。前者會讓英文介面夾雜中文，後者會讓那一句永遠格式化失敗、
    默默顯示成未代入的樣板。兩者都不會當掉，只會靜靜地錯。
    """
    import json
    import re

    from app import config, language, resources

    codes = [code for code, _key in config.LANGUAGE_MODES if code != "system"]
    check("語言模式清單裡有具體語言", len(codes) >= 2, str(codes))

    catalogs = {}
    for code in codes:
        target = resources.resource_path(config.LANGUAGE_DIR, code + ".json")
        exists = os.path.isfile(target)
        check("語言檔存在：" + code, exists, target)
        if not exists:
            return
        with open(target, "r", encoding="utf-8") as handle:
            catalogs[code] = json.load(handle)

    base_code = config.DEFAULT_LANGUAGE
    base = catalogs[base_code]
    check("預設語言的字串表不是空的", len(base) > 100, str(len(base)))

    for code, catalog in catalogs.items():
        if code == base_code:
            continue
        # 扁平結構的回報：對齊與否一句話就驗得完
        check("鍵集合與預設語言完全相同：" + code,
              sorted(catalog) == sorted(base),
              "缺少 " + str(sorted(set(base) - set(catalog))[:5])
              + " / 多出 " + str(sorted(set(catalog) - set(base))[:5]))
        check("鍵順序與預設語言一致（方便並排比對）：" + code,
              list(catalog) == list(base))

    placeholder = re.compile("{(" + chr(92) + "w+)}")
    for code, catalog in catalogs.items():
        empty = [key for key, value in catalog.items() if not str(value).strip()]
        check("沒有空字串：" + code, not empty, str(empty[:5]))
        if code == base_code:
            continue
        mismatched = [
            key for key in base
            if key in catalog
            and set(placeholder.findall(base[key])) != set(placeholder.findall(catalog[key]))
        ]
        # 佔位符對不上時 t() 會靜默回傳未格式化的樣板（見 language.t 的說明），
        # 畫面上就是活生生的 "{count} lines"
        check("佔位符與預設語言一致：" + code, not mismatched, str(mismatched[:5]))

    # t() 的三段 fallback：命中 -> 退回預設語言 -> 回傳鍵本身
    language.set_current(base_code)
    check("t() 取得預設語言的字串",
          language.t("app.displayName") == base[("app.displayName")])
    check("t() 會代入具名參數",
          "5" in language.t("settings.font.value", size=5))
    check("t() 找不到鍵時回傳鍵本身",
          language.t("no.such.key.exists") == "no.such.key.exists")
    check("t() 對佔位符打錯不拋例外，回未格式化的樣板",
          "{" in language.t("settings.font.value", wrong_name=5))

    for code in codes:
        language.set_current(code)
        check("resolve 對具體語言原樣回傳：" + code,
              language.resolve(code) == code)
    check("resolve 對 system 會解析成具體語言",
          language.resolve("system") in codes)
    language.set_current(base_code)

    # --- 切換行為（需要開視窗）---------------------------------------------
    import tempfile

    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer
    from PyQt6.QtWidgets import QApplication, QWidget

    from app.viewer import MarkdownViewer
    from app.window_manager import WindowManager

    app = QApplication.instance() or QApplication([])

    def pump(ms=300):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    # CJK 判定：不用跳脫序列寫，避免在各層編輯中被吃掉
    han_lo, han_hi = chr(0x4E00), chr(0x9FFF)

    def has_cjk(text: str) -> bool:
        return any(han_lo <= ch <= han_hi for ch in text)

    other = next(code for code in codes if code != base_code)

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    tmp = tempfile.mkdtemp()
    doc = os.path.join(tmp, "lang.md")
    with open(doc, "w", encoding="utf-8") as handle:
        handle.write("# 標題" + chr(10) * 2
                     + (chr(10) * 2).join(f"第 {i} 段。" for i in range(1200)))

    viewer = MarkdownViewer(doc)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.resize(1000, 720)
    viewer.show()
    pump(600)

    viewer.set_language_mode(other)
    pump(400)
    check("切換後標題列按鈕的 tooltip 換成新語言",
          viewer.title_bar.open_button.toolTip() == language.t("titleBar.open"),
          viewer.title_bar.open_button.toolTip())

    # 一條斷言掃掉整個面板。走的是**實際的子元件**而不是登記表——
    # 掃登記表的話，漏登記的那個 widget 根本不在清單裡，反而永遠掃不到
    # （實測拿掉 _add_row 對 hint 的登記，掃登記表的版本照樣全綠）。
    # 掃子元件才問得出「畫面上還有沒有舊語言的字」這個真正的問題。
    panel = viewer.settings_panel
    self_named = language.t("settings.language.zhTW")   # 語言選擇器顯示各語言自稱
    leftover = [
        widget.text()
        for widget in panel.findChildren(QWidget)
        if hasattr(widget, "text") and isinstance(widget.text(), str)
        and has_cjk(widget.text()) and widget.text() != self_named
    ]
    check("設定面板沒有殘留舊語言的文字", not leftover, str(leftover[:4]))
    tips = [b.toolTip() for b in viewer.title_bar._buttons if has_cjk(b.toolTip())]
    check("標題列沒有殘留舊語言的 tooltip", not tips, str(tips[:4]))
    # 搜尋列走**實際的子元件**而不是 _buttons：漏加進 _buttons 的那顆按鈕不在
    # 清單裡，掃清單反而永遠掃不到（實測把兩顆選項鈕從 _buttons 拿掉，掃清單的
    # 版本照樣全綠）。這和上面設定面板那條是同一個理由。
    # 只掃 tooltip 不掃 text：狀態標籤的「無相符」本來就是中文，掃 text 會假紅。
    find_tips = [w.toolTip() for w in viewer.find_bar.findChildren(QWidget)
                 if has_cjk(w.toolTip())]
    check("搜尋列沒有殘留舊語言的 tooltip", not find_tips, str(find_tips[:4]))
    check("搜尋列的輸入提示也換了語言",
          viewer.find_bar.input.placeholderText() == language.t("find.placeholder"),
          viewer.find_bar.input.placeholderText())

    check("視窗標題以 applicationDisplayName 結尾（Qt 不會再自動補後綴）",
          viewer.windowTitle().endswith(language.t("app.displayName")),
          viewer.windowTitle())
    check("applicationDisplayName 與標題後綴同源",
          app.applicationDisplayName() == language.t("app.displayName"),
          app.applicationDisplayName())

    status = viewer.status_label.text()
    check("狀態列用新語言的分隔符",
          language.t("status.separator") in status, repr(status[:40]))
    check("行數與字數帶千分位",
          re.search(r"\d,\d{3}", status) is not None,
          repr(status[:80]))
    check("修改時間維持 ISO 格式（不隨語言變）",
          re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", status) is not None,
          repr(status[:80]))

    # 大檔警告要跟著換，小檔不該被清快取（快取鍵不含語言，只有大檔例外）
    small_tab = viewer._tab
    cached_before = dict(small_tab._html_cache)
    viewer.set_language_mode(base_code)
    pump(400)
    check("切回原語言後標題也跟著回來",
          viewer.windowTitle().endswith(language.t("app.displayName")),
          viewer.windowTitle())
    check("一般文件切語言不會清掉 HTML 快取（避免重付轉換成本）",
          set(small_tab._html_cache) == set(cached_before),
          f"{sorted(small_tab._html_cache)} vs {sorted(cached_before)}")

    viewer.close()
    pump(300)

    # 建構期就要是對的語言：驗 set_current 發生在 _build_ui 之前
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    handle = QSettings(config.ORG_NAME, config.APP_NAME)
    handle.setValue(config.KEY_LANGUAGE_MODE, other)
    handle.sync()
    fresh = MarkdownViewer(None)
    fresh.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    fresh.resize(800, 600)
    fresh.show()
    pump(500)
    language.set_current(other)
    check("建構當下就是設定裡的語言（不是建好才補套）",
          fresh.title_bar.open_button.toolTip() == language.t("titleBar.open"),
          fresh.title_bar.open_button.toolTip())
    check("歡迎頁也是新語言",
          language.t("app.displayName") in fresh.browser.toPlainText()
          and not has_cjk(fresh.browser.toPlainText()),
          fresh.browser.toPlainText()[:60])
    fresh.close()
    pump(300)

    # 廣播：一個視窗改語言，其他視窗跟著換
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    manager = WindowManager()
    first = manager.create_window(doc)
    first.resize(900, 640)
    first.show()
    second = manager.create_window(None)
    second.resize(700, 520)
    second.show()
    pump(600)

    first.set_language_mode(other)
    pump(500)
    check("廣播：另一個視窗的模式跟著變",
          second._language_mode == other, second._language_mode)
    check("廣播：另一個視窗的文字跟著變",
          second.title_bar.open_button.toolTip() == language.t("titleBar.open"),
          second.title_bar.open_button.toolTip())

    # 關掉一個再切一次：驗「每次重新取 windows()、不快取視窗參照」
    second.close()
    pump(400)
    first.set_language_mode(base_code)
    pump(400)
    check("廣播對象消失後再切語言不會出事",
          first._language_mode == base_code, first._language_mode)

    for window in list(manager.windows()):
        window.close()
    pump(400)
    language.set_current(base_code)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()


# ===========================================================================
# 區塊：分頁操作
# ===========================================================================
def section_tabs(args) -> None:
    import tempfile

    from PyQt6.QtCore import (
        QEvent, QEventLoop, QPoint, QPointF, QRect, QSettings, Qt, QTimer,
    )
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    NL = chr(10)

    app = QApplication.instance() or QApplication([])

    def pump(ms=250):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app.language import t
    from app.viewer import MarkdownViewer

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    viewer = MarkdownViewer(SAMPLE)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.resize(1100, 780)
    viewer.show()
    pump(500)

    # 分頁列永遠顯示（含單分頁）：隱藏的話單開一個 .md 就沒有分頁可抓，
    # 永遠拖不去別的視窗合併（實際使用回報）
    check("單一分頁時分頁列也顯示（要能拖去合併）", viewer.tab_bar.isVisible())
    viewer.open_path(README, new_tab=True)
    pump(400)
    check("多分頁時顯示分頁列", viewer.tab_bar.isVisible())

    before = len(viewer._tabs)
    viewer.open_path(README, new_tab=True)
    pump(300)
    check("同一個檔案不會重複開分頁", len(viewer._tabs) == before)
    check("重開已存在的檔案會切過去",
          viewer._tab.path == os.path.abspath(README))

    viewer.new_tab()
    pump(250)
    check("Ctrl+T 開空白分頁", len(viewer._tabs) == before + 1)
    check("空白分頁顯示歡迎頁", viewer._tab.path is None)
    check("歡迎頁列出分頁快速鍵", "Ctrl + T" in viewer.browser.toPlainText())

    viewer.next_tab()
    pump(150)
    viewer.previous_tab()
    pump(150)
    check("分頁循環切換不會越界", 0 <= viewer._active < len(viewer._tabs))

    viewer.close_tab()
    pump(250)
    check("關閉分頁", len(viewer._tabs) == before)

    while len(viewer._tabs) > 1:
        viewer.close_tab_at(len(viewer._tabs) - 1)
        pump(150)
    check("關到只剩一個分頁時分頁列仍顯示", viewer.tab_bar.isVisible())

    # --- 關閉鈕是疊在檔名上的覆蓋層 -----------------------------------------
    # 改版前關閉鈕排在版面裡：滑鼠移進未選取的分頁會讓它多出 24px，整列跟著
    # 位移，很難瞄準。現在改成絕對定位疊在右端，寬度只由檔名決定。
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QEnterEvent

    from app import styles as _styles
    from app import tab_bar as _tab_bar

    long_name = "非常非常長的檔案名稱用來把整條分頁塞滿測試中間省略"
    long_path = os.path.join(tempfile.mkdtemp(), long_name + ".md")
    with open(long_path, "w", encoding="utf-8") as handle:
        handle.write("# 長檔名\n\n內容。\n")
    viewer.open_path(long_path, new_tab=True)
    pump(400)

    buttons = viewer.tab_bar._buttons
    long_tab = buttons[-1]          # 剛開的是作用中的，關閉鈕一直看得到
    idle_tab = buttons[0]

    check("關閉鈕與襯底都不在版面裡（在版面裡就會撐寬分頁）",
          viewer.tab_bar._buttons[0].layout().count() == 1,
          f"版面裡有 {viewer.tab_bar._buttons[0].layout().count()} 個項目")

    # 【這次改版的重點】滑鼠移進移出，寬度一格都不能變
    widths_before = [b.width() for b in buttons]
    centre = idle_tab.rect().center()
    global_centre = idle_tab.mapToGlobal(centre)
    app.sendEvent(idle_tab, QEnterEvent(
        QPointF(centre), QPointF(global_centre), QPointF(global_centre)))
    pump(250)
    widths_hover = [b.width() for b in buttons]
    check("滑鼠移進分頁不會改變任何分頁的寬度",
          widths_before == widths_hover,
          f"{widths_before} -> {widths_hover}")
    check("滑鼠移進未選取的分頁才顯示關閉鈕", idle_tab._close.isVisibleTo(idle_tab))
    app.sendEvent(idle_tab, QEvent(QEvent.Type.Leave))
    pump(250)
    check("滑鼠移出後寬度仍然一樣",
          [b.width() for b in buttons] == widths_before,
          f"{[b.width() for b in buttons]} vs {widths_before}")

    # 幾何：貼右緣、讓開框線
    close_rect = long_tab._close.geometry()
    back_rect = long_tab._backdrop.geometry()
    check("關閉鈕貼在分頁右緣（讓開 1px 的 border-right）",
          close_rect.right() + 1 == long_tab.width()
          - _tab_bar._TAB_BORDER_RIGHT - config.TAB_CLOSE_MARGIN,
          f"右緣 {close_rect.right() + 1} 分頁寬 {long_tab.width()}")
    check("襯底從關閉鈕再往左讓出漸層寬度，右緣到分頁邊界",
          back_rect.left() == close_rect.left() - config.TAB_CLOSE_FADE
          and back_rect.right() + 1 == long_tab.width() - _tab_bar._TAB_BORDER_RIGHT,
          f"襯底 {back_rect} 關閉鈕 {close_rect}")
    check("覆蓋層讓開頂端 2px 的 border-top（作用中分頁那條強調色不能被蓋掉）",
          back_rect.top() == _tab_bar._TAB_BORDER_TOP,
          f"襯底頂端 {back_rect.top()}")

    # 疊放順序：襯底要在檔名之上、關閉鈕之下
    order = [child.objectName() for child in long_tab.children()
             if child.objectName() in
             ("tabLabel", "tabLabelActive", "tabCloseBackdrop", "tabClose")]
    check("疊放順序是 檔名 → 襯底 → 關閉鈕（襯底寫成 lower() 就會被字蓋住）",
          order == ["tabLabelActive", "tabCloseBackdrop", "tabClose"], str(order))

    # 像素：襯底真的把檔名尾巴蓋掉，而且叉叉看得見
    def _tab_pixels(tab):
        image = tab.grab().toImage()
        ratio = image.width() / max(1, tab.width())
        return image, ratio

    def _ink_in(tab, rect, wanted):
        image, ratio = _tab_pixels(tab)
        return sum(
            1
            for y in range(int(rect.top() * ratio), int(rect.bottom() * ratio))
            for x in range(int(rect.left() * ratio), int(rect.right() * ratio))
            if image.pixelColor(x, y).name() in wanted
        )

    theme_now = viewer.tab_bar._theme
    name_colours = {_styles.palette(theme_now)["text"].lower(),
                    _styles.palette(theme_now)["text_muted"].lower()}
    solid = QRect(close_rect.left(), back_rect.top(),
                  back_rect.right() - close_rect.left() + 1, back_rect.height())
    check("前置：這個檔名長到會撞上關閉鈕（不然遮蓋測不到）",
          _ink_in(long_tab, QRect(0, back_rect.top(), back_rect.left(),
                                  back_rect.height()), name_colours) > 0,
          "襯底左邊沒有字，換一個更長的檔名")

    def _masking_worst(tab):
        """襯底的實色段裡，和實色底差最多的那個像素差多少（越小越好）。

        量之前要先把叉叉收起來：色票裡 icon_active 和 text 是同一個色碼
        （#1f2328），叉叉自己的像素會被算成「漏出來的檔名」。

        逐欄掃過整個高度，不能只取中線：中文字的筆畫上下分佈不均，只看一列
        會剛好落在字的空隙裡，遮蓋壞掉也量不出來。而且不能用「顏色完全相符」
        來判斷——襯底若只遮一半，字會和底色混成一個中間色，和 text 的色碼並
        不相等，數色碼的版本會綠得莫名其妙。
        """
        was_visible = tab._close.isVisibleTo(tab)
        tab._close.setVisible(False)
        pump(150)
        image, ratio = _tab_pixels(tab)
        tab._close.setVisible(was_visible)
        pump(150)
        back = tab._backdrop.geometry()
        close = tab._close.geometry()
        reference = image.pixelColor(
            int((back.right() - 2) * ratio),
            int((back.top() + back.height() // 2) * ratio),
        ).lightness()
        return max(
            abs(image.pixelColor(int(x * ratio), int(y * ratio)).lightness()
                - reference)
            for x in range(close.left() + 1, back.right() - 1)
            for y in range(back.top() + 1, back.bottom() - 1)
        )

    masked_image, masked_ratio = _tab_pixels(long_tab)
    worst_active = _masking_worst(long_tab)
    check("作用中的分頁：襯底把檔名尾巴蓋掉（整條都是實色，不是半透明）",
          worst_active <= 6, f"最大亮度差 {worst_active}（>6 代表字透出來了）")

    # 漸層存不存在只能從 QSS 判斷，不能量像素：淡出區底下沒有字的時候，
    # 「透明疊在分頁底色上」和「實色」本來就一模一樣（襯底的實色就是分頁底色），
    # 量出來永遠是「第 0 欄就實色」。改成檢查樣板算出來的 stop 位置。
    backdrop_qss = re.search(
        r"QFrame#tabCloseBackdrop\s*\{.*?stop:0\s.*?stop:([0-9.]+)",
        _styles.build_qss(theme_now), re.S)
    fade_stop = float(backdrop_qss.group(1)) if backdrop_qss else -1.0
    check("襯底左側留了一段漸層（stop 落在 0 與 1 之間，不是硬邊）",
          0.05 < fade_stop < 0.95, f"stop = {fade_stop}")

    image, ratio = _tab_pixels(long_tab)
    base = image.pixelColor(int((close_rect.left() + 1) * ratio),
                            int((close_rect.top() + 1) * ratio))
    glyph = sum(
        1
        for y in range(int(close_rect.top() * ratio), int(close_rect.bottom() * ratio))
        for x in range(int(close_rect.left() * ratio), int(close_rect.right() * ratio))
        if abs(image.pixelColor(x, y).lightness() - base.lightness()) > 25
    )
    check("叉叉真的畫在襯底上（不是被字蓋掉或畫在畫面外）", glyph > 10,
          f"只有 {glyph} 個和襯底不同色的像素")

    # 襯底的顏色是用 `#tabActive QFrame#tabCloseBackdrop` 這種後代選擇器挑的，
    # 切換作用中分頁時如果沒有一起重跑選擇器，顏色會停在另一個狀態。
    def _backdrop_colour(tab):
        image, ratio = _tab_pixels(tab)
        rect = tab._backdrop.geometry()
        return image.pixelColor(int((rect.right() - 2) * ratio),
                                int((rect.top() + rect.height() // 2) * ratio)).name()

    active_colour = _backdrop_colour(long_tab)
    viewer.activate_tab(0)
    pump(300)
    # 【不能讓「襯底被收起來」當成通過】切走之後關閉鈕本來就會收起來，
    # 拿 isVisible 當逃生口的話這條會恆真（實測拿掉重跑選擇器那行，照樣全綠）。
    # 強制把它顯示出來，兩個狀態都看得到，才比得出配色有沒有跟著換。
    long_tab._show_close(True)
    pump(200)
    idle_colour = _backdrop_colour(long_tab)
    check("切走之後襯底改用非作用中的底色（後代選擇器要跟著重跑）",
          idle_colour != active_colour,
          f"切換前後都是 {active_colour}")
    # 非作用中走的是另一條 QSS 規則（`QFrame#tabCloseBackdrop`，沒有
    # `#tabActive` 前綴），遮蓋要在這個狀態下也量一次——只量作用中的話，
    # 把非作用中那條規則改壞了測試照樣全綠。
    worst_idle = _masking_worst(long_tab)
    check("非作用中被指著時：襯底一樣把檔名尾巴蓋掉",
          worst_idle <= 6, f"最大亮度差 {worst_idle}")
    long_tab._show_close(False)
    viewer.activate_tab(len(viewer._tabs) - 1)
    pump(300)
    check("切回來又是作用中的底色",
          _backdrop_colour(long_tab) == active_colour,
          f"{_backdrop_colour(long_tab)} vs {active_colour}")

    while len(viewer._tabs) > 1:
        viewer.close_tab_at(len(viewer._tabs) - 1)
        pump(150)

    # 回歸：標題列的 X 曾被誤接到 close_tab——開著多個分頁時按視窗的關閉鈕，
    # 視窗不關、只少一個分頁。視窗控制鈕必須關整個視窗。
    viewer.open_path(README, new_tab=True)
    pump(300)
    tabs_before = len(viewer._tabs)
    viewer.title_bar.closeRequested.emit()
    pump(400)
    # WA_DeleteOnClose：視窗真的關了的話 C++ 物件會被銷毀，
    # 摸它會丟 RuntimeError——那正是我們要的結果。
    from PyQt6 import sip as _sip
    closed = _sip.isdeleted(viewer) or not viewer.isVisible()
    check("多分頁時按標題列的 X 會關閉整個視窗", closed,
          f"視窗仍在，分頁 {tabs_before} -> {len(viewer._tabs)}")
    if not closed:
        viewer.close()
    pump(300)

    # --- 「＋」只做一件事：開新分頁 ---------------------------------------
    # 這顆按鈕有過兩次行為錯誤。第一次是整顆接 open_dialog，和它自己的提示
    #「開新分頁 (Ctrl+T)」對不上；第二次是右側加了一條 14px 的展開箭頭，選單
    # 裡唯一的「開啟檔案…」和標題列資料夾鈕接的是同一個 open_dialog——同一個
    # 功能兩個入口，代價是「＋」最右緣那一條點下去不會開分頁。現在箭頭拿掉，
    # 整顆都是開新分頁；開檔的入口是標題列的資料夾鈕與 Ctrl+O。
    v3 = MarkdownViewer()
    v3.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    v3.resize(1000, 700)
    v3.show()
    pump(400)
    plus = v3.tab_bar.new_button
    check("「＋」是一般按鈕，沒有展開選單（箭頭那條窄帶不存在）",
          not hasattr(plus, "menu_widget"),
          type(plus).__name__)
    check("「＋」的寬度不再替箭頭留位置（30 而不是 30+14）",
          plus.width() == 30, str(plus.width()))
    check("分頁列不再有 openFileRequested 訊號（開檔入口只剩標題列與 Ctrl+O）",
          not hasattr(v3.tab_bar, "openFileRequested"))

    # 【檔案對話框一律換成替身】QFileDialog.getOpenFileName 是 modal，真的彈
    # 出來就沒有人會去關它，整個測試永遠卡住。這裡踩過一次：某個突變把「＋」
    # 接回 open_dialog，測試一點下去就掛死二十分鐘。
    # 換成替身之後，「有沒有開對話框」反而變成可以直接斷言的訊號——
    # 按「＋」誤開對話框（正是最早的行為）當場就會被抓到。
    from PyQt6.QtWidgets import QFileDialog as _QFileDialog
    from PyQt6.QtWidgets import QMenu as _QMenu

    dialog_calls: list = []
    _real_get_open = _QFileDialog.getOpenFileName
    _QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (dialog_calls.append(1), ("", ""))[1]
    )
    # QMenu.exec 同樣是等人操作的巢狀迴圈。正確的程式已經沒有選單可彈，但這個
    # 替身要留著：箭頭若被裝回去，少了它測試會卡死到逾時（實測 25 分鐘後才被
    # 砍掉）而不是變紅——沒有訊號比錯誤訊號更糟。有了替身，「有沒有彈出選單」
    # 就變成可以直接斷言的東西。
    menu_pops: list = []
    _real_menu_exec = _QMenu.exec
    _QMenu.exec = lambda self, *a, **k: menu_pops.append(1)
    new_hits: list = []
    v3.tab_bar.newTabRequested.connect(lambda: new_hits.append(1))

    def click_plus(x_in_button):
        point = QPoint(x_in_button, plus.height() // 2)
        target = plus.mapToGlobal(point)
        for kind, held in (
            (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
        ):
            app.sendEvent(plus, QMouseEvent(
                kind, QPointF(point), QPointF(target),
                Qt.MouseButton.LeftButton, held, Qt.KeyboardModifier.NoModifier))
        pump(120)

    tabs_before_click = v3.tab_count()
    click_plus(10)
    check("點「＋」＝開新分頁（不是開檔對話框）",
          len(new_hits) == 1 and not dialog_calls
          and v3.tab_count() == tabs_before_click + 1,
          f"new={len(new_hits)} 對話框={len(dialog_calls)} "
          f"分頁 {tabs_before_click}->{v3.tab_count()}")
    # 最右緣就是以前箭頭佔走的那一條。以前點這裡只會展開選單，不會開分頁。
    click_plus(plus.width() - 2)
    check("點「＋」最右緣（以前的箭頭區）也是開新分頁，不會彈出選單",
          len(new_hits) == 2 and not menu_pops and not dialog_calls
          and v3.tab_count() == tabs_before_click + 2,
          f"new={len(new_hits)} 選單={len(menu_pops)} 對話框={len(dialog_calls)} "
          f"分頁 {tabs_before_click}->{v3.tab_count()}")
    # 開檔的功能沒有消失，只是入口收斂到標題列那一顆
    v3.title_bar.open_button.click()
    pump(120)
    check("標題列的資料夾鈕仍然開得了檔案對話框（開檔功能沒跟著箭頭消失）",
          len(dialog_calls) == 1, f"對話框={len(dialog_calls)}")
    _QFileDialog.getOpenFileName = _real_get_open
    _QMenu.exec = _real_menu_exec

    # --- 空白分頁可以直接貼上 Markdown 原始碼 -----------------------------
    while v3.tab_count() > 1:
        v3.close_tab_at(v3.tab_count() - 1)
        pump(100)
    v3.new_tab()
    pump(200)
    check("貼上前是歡迎頁，分頁名為「新分頁」",
          v3._tab.display_name == t("tab.newTab") and not v3._tab.pasted
          and v3._tab.meta is None)
    check("歡迎頁有提示可以直接貼上",
          "Ctrl+V" in v3.browser.document().toPlainText(),
          v3.browser.document().toPlainText()[:80])

    QApplication.clipboard().setText(
        "# 貼上的標題" + NL + NL + "這是 **粗體** 內文。" + NL)
    v3.paste_markdown()
    pump(300)
    pasted_text = v3.browser.document().toPlainText()
    check("貼上後真的渲染成 Markdown（星號被吃掉、內容出現）",
          "貼上的標題" in pasted_text and "粗體" in pasted_text
          and "**" not in pasted_text,
          pasted_text[:80])
    check("貼上的分頁改名、標記為貼上、沒有檔案路徑",
          v3._tab.display_name == t("tab.pasted") and v3._tab.pasted
          and v3._tab.path is None and v3._tab.meta is not None,
          f"名稱={v3._tab.display_name} pasted={v3._tab.pasted} "
          f"path={v3._tab.path}")

    QApplication.clipboard().setText("# 第二次貼上" + NL)
    v3.paste_markdown()
    pump(300)
    again = v3.browser.document().toPlainText()
    check("再貼一次會覆蓋原本的內容",
          "第二次貼上" in again and "貼上的標題" not in again, again[:60])

    # 正在讀檔案的分頁不能被貼上蓋掉
    v3.open_path(SAMPLE, new_tab=True)
    pump(300)
    QApplication.clipboard().setText("# 不該出現的內容" + NL)
    v3.paste_markdown()
    pump(200)
    check("已開檔案的分頁不會被 Ctrl+V 覆蓋",
          "不該出現的內容" not in v3.browser.document().toPlainText()
          and v3._tab.path is not None)

    # 搜尋框有焦點時 Ctrl+V 是它的貼上，不能被視窗的捷徑吃掉
    v3.show_find()
    pump(200)
    v3.find_bar.input.setFocus()
    pump(150)
    QApplication.clipboard().setText("搜尋關鍵字")
    v3.paste_markdown()
    pump(200)
    check("焦點在搜尋框時，Ctrl+V 貼進搜尋框而不是分頁",
          v3.find_bar.input.text() == "搜尋關鍵字",
          repr(v3.find_bar.input.text()))
    v3.find_bar.deactivate()
    pump(150)

    # 貼上的分頁沒有路徑，工作階段本來就只存有路徑的分頁
    v3.set_restore_tabs(True)
    v3._save_session()
    saved_paths = QSettings(config.ORG_NAME, config.APP_NAME).value(
        config.KEY_OPEN_TABS, [], type=list)
    check("貼上的分頁不會被寫進工作階段（重開不還原）",
          all(p for p in saved_paths) and len(saved_paths) < v3.tab_count(),
          f"存了 {saved_paths}，分頁數 {v3.tab_count()}")
    v3.set_restore_tabs(False)

    # --- 貼上的分頁關閉時詢問存檔 -------------------------------------------
    # 以前貼上的分頁一關就沒了（沒有路徑、不進工作階段），使用者貼了一大段
    # 東西按到 Ctrl+W 就全部消失。現在關分頁與關視窗都會問「儲存／不儲存／
    # 取消」，選儲存就跳原生存檔對話框。
    #
    # 【三個模態對話框一律換替身】QMessageBox.question / warning 與
    # QFileDialog.getSaveFileName 都是等人按的巢狀迴圈，offscreen 下沒有人會去
    # 按，真的彈出來就是整份測試卡到逾時而不是變紅（這個專案踩過兩次）。
    # 替身在這裡就要裝好：v3 此刻還握著一個貼上分頁，底下的 v3.close() 會進
    # closeEvent 詢問。整段用 try/finally 包住，任何一條炸掉都要還原替身。
    from PyQt6.QtWidgets import QMessageBox as _QMessageBox
    _SB = _QMessageBox.StandardButton
    _SAVE_DISCARD_CANCEL = _SB.Save | _SB.Discard | _SB.Cancel
    _real_question = _QMessageBox.question
    _real_warning = _QMessageBox.warning
    _real_get_save = _QFileDialog.getSaveFileName
    ask_calls: list = []          # 每次詢問記 (訊息, 按鈕組合)
    warn_calls: list = []
    save_calls: list = []
    # answers 是「依序回覆」的佇列（關窗會連問好幾次），用完退回 answer
    reply: dict = {"answer": _SB.Discard, "answers": [], "path": ""}

    def _fake_question(parent, title, text, buttons=_SB.Ok, default=_SB.NoButton):
        ask_calls.append((text, buttons))
        if reply["answers"]:
            return reply["answers"].pop(0)
        return reply["answer"]

    def _fake_warning(parent, title, text, *a, **k):
        warn_calls.append(text)
        return _SB.Ok

    def _fake_get_save(parent=None, caption="", directory="", filter="", *a, **k):
        save_calls.append(directory)
        return (reply["path"], filter)

    _QMessageBox.question = staticmethod(_fake_question)
    _QMessageBox.warning = staticmethod(_fake_warning)
    _QFileDialog.getSaveFileName = staticmethod(_fake_get_save)
    paste_dir = tempfile.mkdtemp(prefix="mdpaste-")
    try:
        # 直接建構、不掛 WA_DeleteOnClose：關窗被取消之後要能查 isVisible 與
        # _closing，掛了的話選「不儲存」關掉後物件就是已銷毀的包裝
        vq = MarkdownViewer()
        vq.resize(900, 640)
        vq.show()
        pump(400)
        vq.open_path(SAMPLE)
        pump(300)
        # 兩個檔案分頁墊底：關分頁的那幾條要一直走 close_tab_at（多分頁）而不是
        # 掉進「最後一個分頁→關視窗」，某條紅了也不會把後面的全部拖下水
        vq.open_path(README, new_tab=True)
        pump(300)

        def paste_new(text):
            vq.new_tab()
            pump(120)
            QApplication.clipboard().setText(text)
            vq.paste_markdown()
            pump(200)
            return vq._tab

        def ensure_kept(tab, text):
            # 前一條若把分頁誤關了（那條已經紅），補一個回來讓這條測自己的事
            if any(t_ is tab for t_ in vq._tabs):
                vq.activate_tab(vq._tabs.index(tab))
                pump(100)
                return tab
            return paste_new(text)

        # 1. 不儲存：有問、分頁照關、沒開存檔對話框
        paste_new("# 貼上一" + NL)
        before_q = vq.tab_count()
        asked_before = len(ask_calls)
        reply["answer"] = _SB.Discard
        vq.close_tab()
        pump(200)
        check("關貼上分頁會先問（儲存／不儲存／取消，以前一關就沒了）；選不儲存就關",
              len(ask_calls) == asked_before + 1
              and ask_calls[-1][1] == _SAVE_DISCARD_CANCEL
              and vq.tab_count() == before_q - 1 and not save_calls,
              f"問了 {len(ask_calls) - asked_before} 次 按鈕={ask_calls[-1][1] if ask_calls else None} "
              f"分頁 {before_q}->{vq.tab_count()} 存檔對話框={len(save_calls)}")

        # 2. 取消：分頁留著、內容不變
        kept = paste_new("# 貼上二" + NL)
        before_q = vq.tab_count()
        reply["answer"] = _SB.Cancel
        vq.close_tab()
        pump(200)
        check("關貼上分頁選取消：分頁留著（以前沒得取消）",
              vq.tab_count() == before_q and any(t_ is kept for t_ in vq._tabs)
              and kept.pasted,
              f"分頁 {before_q}->{vq.tab_count()} pasted={kept.pasted}")

        # 4. 儲存但在存檔對話框按取消：等同取消，分頁留著
        kept = ensure_kept(kept, "# 貼上二" + NL)
        before_q = vq.tab_count()
        reply["answer"] = _SB.Save
        reply["path"] = ""
        saves_before = len(save_calls)
        vq.close_tab()
        pump(200)
        check("選儲存卻在存檔對話框按取消：等同取消關閉、分頁仍是貼上狀態",
              len(save_calls) == saves_before + 1 and vq.tab_count() == before_q
              and kept.pasted and kept.path is None,
              f"存檔對話框={len(save_calls) - saves_before} 分頁 {before_q}->{vq.tab_count()} "
              f"pasted={kept.pasted} path={kept.path}")

        # 10. 寫檔失敗（目錄不存在）：顯示錯誤、分頁留著、不崩潰
        kept = ensure_kept(kept, "# 貼上二" + NL)
        before_q = vq.tab_count()
        reply["answer"] = _SB.Save
        reply["path"] = os.path.join(paste_dir, "沒有這個目錄", "x.md")
        warns_before = len(warn_calls)
        vq.close_tab()
        pump(200)
        check("寫檔失敗（目錄不存在）：顯示錯誤訊息、分頁留著不關、仍是貼上狀態",
              len(warn_calls) == warns_before + 1 and vq.tab_count() == before_q
              and kept.pasted and kept.path is None,
              f"錯誤框={len(warn_calls) - warns_before} 分頁 {before_q}->{vq.tab_count()} "
              f"pasted={kept.pasted} path={kept.path}")

        # 3. 儲存成功：檔案寫出、內容一致（\r\n 正規化成 \n）、分頁關掉
        kept = ensure_kept(kept, "# 貼上二" + NL)
        before_q = vq.tab_count()
        saved_one = os.path.join(paste_dir, "one.md")
        reply["answer"] = _SB.Save
        reply["path"] = saved_one
        kept_text = kept.text
        vq.close_tab()
        pump(200)
        read_back = ""
        if os.path.isfile(saved_one):
            with open(saved_one, "r", encoding="utf-8") as handle:
                read_back = handle.read()
        check("選儲存並給路徑：檔案以 UTF-8 寫出、內容與貼上的一致、分頁關掉",
              read_back == kept_text.replace("\r\n", "\n")
              and vq.tab_count() == before_q - 1,
              f"讀回={read_back[:40]!r} 分頁 {before_q}->{vq.tab_count()}")

        # 5. 全是空白的貼上分頁：沒東西會遺失，不問
        vq.new_tab()
        pump(120)
        vq._tab.set_pasted("   " + NL + chr(9) + NL)
        before_q = vq.tab_count()
        asked_before = len(ask_calls)
        vq.close_tab()
        pump(200)
        check("內容只有空白的貼上分頁：不問直接關（空的新分頁也一樣）",
              len(ask_calls) == asked_before and vq.tab_count() == before_q - 1,
              f"問了 {len(ask_calls) - asked_before} 次 分頁 {before_q}->{vq.tab_count()}")

        # 9. 拖到別的視窗（take_tab / adopt_tab）不是關閉，不問
        moved = paste_new("# 搬去別的視窗" + NL)
        asked_before = len(ask_calls)
        taken = vq.take_tab(vq._tabs.index(moved))
        pump(150)
        vq.adopt_tab(taken)
        pump(200)
        check("把貼上分頁拖到別的視窗（take_tab/adopt_tab）不是關閉，不會問存檔",
              len(ask_calls) == asked_before and taken is moved and moved.pasted
              and any(t_ is moved for t_ in vq._tabs),
              f"問了 {len(ask_calls) - asked_before} 次 taken={taken is moved}")
        reply["answer"] = _SB.Discard
        vq.close_tab_at(vq._tabs.index(moved))
        pump(200)

        # 6. 關視窗：有貼上分頁、選取消 → 視窗留著、_closing 不能被設成 True
        first = paste_new("# 關窗一" + NL)
        before_q = vq.tab_count()
        reply["answer"] = _SB.Cancel
        asked_before = len(ask_calls)
        vq.close()
        pump(300)
        check("關視窗時有貼上分頁、選取消：視窗留著且 _closing 仍為 False（外部開檔不會被路由走）",
              vq.isVisible() and not vq._closing and len(ask_calls) == asked_before + 1
              and first.pasted and vq.tab_count() == before_q,
              f"visible={vq.isVisible()} _closing={vq._closing} "
              f"問了 {len(ask_calls) - asked_before} 次 分頁={vq.tab_count()}")

        # 7. 關視窗、兩個貼上分頁：第一個儲存、第二個取消 → 視窗留著、
        #    第一個已變成一般檔案分頁（路徑、meta、監看都到位），第二個仍是貼上
        second = paste_new("# 關窗二" + NL)
        saved_two = os.path.join(paste_dir, "two.md")
        reply["answers"] = [_SB.Save, _SB.Cancel]
        reply["path"] = saved_two
        asked_before = len(ask_calls)
        vq.close()
        pump(300)
        watched = list(vq._watcher.files())
        check("關視窗逐一詢問每個貼上分頁（不是只問第一個）；中途取消就整個中止、視窗留著",
              vq.isVisible() and not vq._closing
              and len(ask_calls) == asked_before + 2 and second.pasted,
              f"visible={vq.isVisible()} 問了 {len(ask_calls) - asked_before} 次 "
              f"second.pasted={second.pasted}")
        check("存檔成功的分頁變成一般檔案分頁：path 設好、pasted=False、meta 是磁碟上的檔案",
              first.path == os.path.abspath(saved_two) and not first.pasted
              and first.meta is not None and first.meta.path == first.path
              and first.file_stamp is not None,
              f"path={first.path} pasted={first.pasted} "
              f"meta.path={getattr(first.meta, 'path', None)} stamp={first.file_stamp}")
        check("存檔成功的分頁加入檔案監看（外部改了會重載，和開檔的分頁一樣）",
              os.path.abspath(saved_two) in [os.path.abspath(p) for p in watched],
              f"監看={watched}")
        check("存檔後這個分頁再關就不會再問（已經是檔案分頁）",
              not (first.pasted and first.text.strip()),
              f"pasted={first.pasted}")

        # 8. 關視窗、最後一個貼上分頁選儲存 → 視窗關閉、路徑進工作階段
        vq.set_restore_tabs(True)
        saved_three = os.path.join(paste_dir, "three.md")
        reply["answers"] = [_SB.Save]
        reply["path"] = saved_three
        vq.close()
        pump(300)
        session_paths = [os.path.abspath(p) for p in QSettings(
            config.ORG_NAME, config.APP_NAME).value(config.KEY_OPEN_TABS, [], type=list)]
        check("關視窗時把貼上分頁存成檔案：視窗關閉、新檔案的路徑進工作階段（下次會還原）",
              not vq.isVisible() and os.path.isfile(saved_three)
              and os.path.abspath(saved_three) in session_paths
              and os.path.abspath(saved_two) in session_paths,
              f"visible={vq.isVisible()} 工作階段={session_paths}")
        # 還原設定，別讓後面建的視窗把這幾個檔案還原回來
        _qs = QSettings(config.ORG_NAME, config.APP_NAME)
        _qs.setValue(config.KEY_RESTORE_TABS, False)
        _qs.remove(config.KEY_OPEN_TABS)
        _qs.remove(config.KEY_ACTIVE_TAB)
        _qs.sync()
        vq.deleteLater()
        pump(200)

        # v3 還握著一個貼上分頁：選「不儲存」關掉
        reply["answer"] = _SB.Discard
        reply["answers"] = []
        v3.close()
        pump(300)
        check("v3 帶著貼上分頁關閉、選不儲存：視窗真的關了",
              _sip.isdeleted(v3) or not v3.isVisible())
    finally:
        _QMessageBox.question = _real_question
        _QMessageBox.warning = _real_warning
        _QFileDialog.getSaveFileName = _real_get_save
    pump(200)

    # --- 一次開多個檔案（多選、拖放、命令列共用的原語）-----------------------
    # 檔案總管多選是「每個檔案叫一次開啟指令」（實測 MultiSelectModel 與 %*
    # 都改不了），所以十個檔案原本要付十次「讀檔＋轉 Markdown＋排版」。
    # open_paths 只讓第一個真的載入，其餘建成延後分頁，切過去才讀。
    batch_dir = tempfile.mkdtemp(prefix="mdmulti-")
    batch = []
    for index in range(5):
        target = os.path.join(batch_dir, f"batch{index}.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(f"# 批次 {index}{NL}{NL}內容 {index}{NL}")
        batch.append(target)

    v4 = MarkdownViewer(batch[0])
    v4.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    v4.resize(900, 640)
    v4.show()
    pump(400)
    opened = v4.open_paths(batch[1:])
    pump(400)
    check("一次開多個：分頁數等於檔案數", v4.tab_count() == 5,
          f"{v4.tab_count()} 個分頁，回報開了 {opened} 個")
    check("一次開多個：整批只有第一個載入，其餘是延後分頁",
          [tab.loaded for tab in v4._tabs[1:]] == [True, False, False, False],
          str([tab.loaded for tab in v4._tabs]))
    check("一次開多個：延後分頁的標題直接用檔名（不是「新分頁」）",
          [t.display_name for t in v4._tabs] ==
          [os.path.basename(p) for p in batch],
          str([t.display_name for t in v4._tabs]))
    check("一次開多個：作用中的是整批的第一個（後面的不會把它搶走）",
          v4._active == 1 and v4._tabs[1].path == os.path.abspath(batch[1]),
          f"active={v4._active}")
    # 切過去才載入，而且內容正確
    v4.activate_tab(3)
    pump(400)
    check("切到延後分頁時才讀檔，內容正確",
          v4._tabs[3].loaded and "內容 3" in v4.browser.toPlainText(),
          v4.browser.toPlainText()[:40])

    # 重複的路徑不會開出第二個分頁
    before = v4.tab_count()
    again = v4.open_paths([batch[1], batch[1], batch[2]])
    pump(300)
    check("一次開多個：已經開著的檔案不重複開",
          v4.tab_count() == before and again == 0,
          f"{before} -> {v4.tab_count()}，回報 {again}")
    check("一次開多個：整批第一個若已開著就切過去",
          v4._tabs[v4._active].path == os.path.abspath(batch[1]),
          str(v4._tabs[v4._active].path))

    # 壞檔案不會中斷整批：其餘照樣開出來，切過去才顯示錯誤頁
    missing = os.path.join(batch_dir, "不存在.md")
    v4.open_paths([missing], activate_first=False)
    pump(300)
    check("一次開多個：讀不到的檔案照樣建分頁，不中斷整批",
          v4.tab_count() == before + 1)
    v4.activate_tab(v4.tab_count() - 1)
    pump(400)
    check("切到讀不到的延後分頁會顯示錯誤頁，不崩潰",
          v4._tabs[v4._active].error is not None)

    # 拖放多個檔案：以前只開第一個，其餘無聲無息地消失
    import shutil

    from PyQt6.QtCore import QMimeData, QUrl
    from PyQt6.QtGui import QDropEvent

    drop_dir = tempfile.mkdtemp(prefix="mddrop-")
    drops = []
    for index in range(3):
        target = os.path.join(drop_dir, f"drop{index}.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(f"# 拖放 {index}{NL}")
        drops.append(target)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in drops] +
                 [QUrl.fromLocalFile(os.path.join(drop_dir, "忽略.png"))])
    before = v4.tab_count()
    event = QDropEvent(QPointF(10.0, 10.0), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    v4.dropEvent(event)
    pump(500)
    check("拖放多個檔案會全部開出來（不支援的副檔名略過）",
          v4.tab_count() == before + 3,
          f"{before} -> {v4.tab_count()}")
    check("拖放多個檔案：只有第一個載入，其餘延後",
          [t.loaded for t in v4._tabs[-3:]] == [True, False, False],
          str([t.loaded for t in v4._tabs[-3:]]))
    v4.close()
    pump(300)

    # --- 關窗與計時器回呼的競態 ----------------------------------------------
    # 這些回呼是 QTimer.singleShot 排到下一回合的，視窗在那之前被關掉時，
    # _tabs 已清空、browser 可能已銷毀；例外漏出計時器回呼就是行程中止。
    from PyQt6 import sip as _sip
    from app.document_tab import DocumentTab as _DT2

    vt = MarkdownViewer(batch[0])
    vt.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    vt.resize(700, 500)
    vt.show()
    pump(400)
    ghost_tab = _DT2(batch[1])
    _sip.delete(ghost_tab.browser)      # 模擬分頁已被搬走／它的閱讀區已銷毀
    safe = True
    try:
        vt._restore_adopted_scroll(ghost_tab, 0.5)
    except Exception as exc:      # noqa: BLE001
        safe = False
        detail = repr(exc)
    check("收養後的捲動還原：分頁不在這個視窗就不碰它的閱讀區（已銷毀也不崩）",
          safe, "" if safe else detail)
    vt._batch_sync_timer.start(60)
    vt.close()                          # 不 pump：停在關閉中的狀態
    check("關窗時連發尾工的計時器也停了（和另外三個計時器一致）",
          not vt._batch_sync_timer.isActive())
    safe = True
    try:
        vt._apply_scroll_ratio(0.5)
    except Exception as exc:      # noqa: BLE001
        safe = False
        detail = repr(exc)
    check("關窗後排到下一回合的捲動還原不會碰空的分頁清單", safe, "" if safe else detail)
    pump(400)

    # --- 多選連發：一批轉交只切一次分頁、只搶一次前景 ------------------------
    # 檔案總管多選 = N 條轉交訊息在約 150 ms 內陸續進來。route_external_open
    # 認這個連發，同一批只有第一條走完整路徑。時鐘是可注入的，不然這裡會變成
    # 又慢又飄的計時測試。
    from app.window_manager import WindowManager as _WM

    manager_b = _WM()
    fake_now = {"t": 1000.0}
    manager_b._clock = lambda: fake_now["t"]
    host_b = manager_b.create_window(batch[0])
    host_b.resize(900, 640)
    pump(400)
    foreground_calls = []
    host_b.take_foreground = lambda: foreground_calls.append(1)

    manager_b.route_external_open(batch[1])
    pump(300)
    check("連發第一條：走完整路徑（切過去、載入、搶前景）",
          host_b.tab_count() == 2 and host_b._tabs[host_b._active].loaded
          and host_b._tabs[host_b._active].path == os.path.abspath(batch[1])
          and len(foreground_calls) == 1,
          f"分頁={host_b.tab_count()} 前景={len(foreground_calls)}")

    fake_now["t"] += 0.05
    manager_b.route_external_open(batch[2])
    fake_now["t"] += 0.05
    manager_b.route_external_open(batch[3])
    pump(400)
    check("同一批的後續：分頁開出來但不切過去",
          host_b.tab_count() == 4
          and host_b._tabs[host_b._active].path == os.path.abspath(batch[1]),
          f"分頁={host_b.tab_count()} active={host_b._tabs[host_b._active].path}")
    check("同一批的後續：只建延後分頁，不多渲染",
          [t.loaded for t in host_b._tabs] == [True, True, False, False],
          str([t.loaded for t in host_b._tabs]))
    check("同一批的後續：不再重複搶前景（十個檔案不會閃十次）",
          len(foreground_calls) == 1, str(len(foreground_calls)))

    # 連發時的尾工要合併：每條都重建分頁列的話，舊按鈕 setParent(None) 會在被
    # 收掉之前短暫變成頂層視窗，畫面上閃出一堆無標題小視窗（實測十個檔案九個）
    syncs = []
    real_sync = host_b._sync_tab_bar
    host_b._sync_tab_bar = lambda: (syncs.append(1), real_sync())[1]
    fake_now["t"] += 0.05
    manager_b.route_external_open(batch[4])
    check("連發的尾工延後合併：當下不重建分頁列", syncs == [], str(len(syncs)))
    pump(host_b.BATCH_SYNC_MS + 250)
    check("連發靜下來之後補做一次尾工", len(syncs) == 1, str(len(syncs)))
    host_b._sync_tab_bar = real_sync

    # 視窗不在前面時，連發的後續仍要把它叫出來——不然使用者按了開啟卻什麼都
    # 沒發生（檔案安靜地開在看不到的視窗裡），那比「沒切過去」糟得多
    fake_now["t"] += 0.05
    host_b.isActiveWindow = lambda: False
    manager_b.route_external_open(batch[0])
    pump(200)
    check("連發中視窗若不在前面，仍會把它叫到前景",
          len(foreground_calls) == 2, str(len(foreground_calls)))
    del host_b.isActiveWindow

    # 【blocker 回歸】連發的計時要在「做完事之後」蓋章。渲染是同步的，一份大
    # 文件可能就吃掉整個窗口；在開始前蓋章的話，第一個檔案越大越容易被判成
    # 不同批——正好在最需要批次的時候失效。
    fake_now["t"] += manager_b.BURST_SECONDS + 0.1     # 先讓上一批過期
    slow = host_b.handle_external_open

    def slow_open(path):
        fake_now["t"] += 1.0      # 假裝第一份文件渲染了一秒
        slow(path)

    host_b.handle_external_open = slow_open
    manager_b.route_external_open(batch[1])
    pump(300)
    host_b.handle_external_open = slow
    active_before = host_b._tabs[host_b._active].path
    fake_now["t"] += 0.05
    manager_b.route_external_open(batch[2])
    pump(300)
    check("第一個檔案渲染很久也不會把整批拆散（計時在做完之後才起算）",
          host_b._tabs[host_b._active].path == active_before,
          f"active={host_b._tabs[host_b._active].path}")

    # 冷啟動多選：第一個檔案是命令列開的，沒經過 route_external_open
    fake_now["t"] += manager_b.BURST_SECONDS + 0.1
    manager_b.note_batch_started(host_b)
    active_before = host_b._tabs[host_b._active].path
    fake_now["t"] += 0.05
    manager_b.route_external_open(batch[3])
    pump(300)
    check("冷啟動多選：命令列開的第一個檔案也會起一批（第二個不搶走畫面）",
          host_b._tabs[host_b._active].path == active_before,
          f"active={host_b._tabs[host_b._active].path}")

    # 空訊息（無參數啟動）只是「把視窗叫到前面」，不該起一批也不該延長
    fake_now["t"] += manager_b.BURST_SECONDS + 0.1
    manager_b.route_external_open("")
    fake_now["t"] += 0.05
    manager_b.route_external_open(batch[4])
    pump(300)
    check("空訊息不會起一批（之後的檔案照樣切過去）",
          host_b._tabs[host_b._active].path == os.path.abspath(batch[4]),
          f"active={host_b._tabs[host_b._active].path}")

    # 大小寫不同是同一個檔案，不該開出第二個分頁
    before = host_b.tab_count()
    host_b.open_paths([os.path.abspath(batch[4]).upper()])
    pump(300)
    check("大小寫不同的同一個檔案不會重複開",
          host_b.tab_count() == before, f"{before} -> {host_b.tab_count()}")

    # 超過連發窗口＝使用者刻意再開一個檔案，要切過去
    fake_now["t"] += manager_b.BURST_SECONDS + 0.1
    fg_before = len(foreground_calls)
    manager_b.route_external_open(batch[0])
    pump(400)
    check("超過連發窗口就是新的一批：切過去並搶前景",
          host_b._tabs[host_b._active].path == os.path.abspath(batch[0])
          and len(foreground_calls) == fg_before + 1,
          f"active={host_b._tabs[host_b._active].path} "
          f"前景={fg_before}->{len(foreground_calls)}")

    # 連發中的空訊息（無參數啟動）只是「把視窗叫到前面」，不能被整批吞掉
    fake_now["t"] += 0.05
    tabs_before = host_b.tab_count()
    fg_before = len(foreground_calls)
    host_b.isActiveWindow = lambda: False
    manager_b.route_external_open("")
    pump(200)
    del host_b.isActiveWindow
    check("連發中的空訊息仍會把視窗叫到前面，且不開分頁",
          host_b.tab_count() == tabs_before
          and len(foreground_calls) == fg_before + 1,
          f"分頁={tabs_before}->{host_b.tab_count()} "
          f"前景={fg_before}->{len(foreground_calls)}")

    # 空訊息不屬於任何一批，也不該把連發窗口往後延——否則使用者在多選之後
    # 刻意開的下一個檔案會被前面那條無關的訊息拖進同一批而不切過去
    fake_now["t"] += manager_b.BURST_SECONDS + 0.1     # 先讓上一批過期
    manager_b.route_external_open(batch[1])           # 起一批（真的路徑）
    pump(300)
    fake_now["t"] += manager_b.BURST_SECONDS - 0.05   # 還在窗口內
    manager_b.route_external_open("")                 # 空訊息：不該延長
    fake_now["t"] += 0.1                              # 累計已超過窗口
    manager_b.route_external_open(batch[2])
    pump(300)
    check("連發中的空訊息不會把窗口往後延",
          host_b._tabs[host_b._active].path == os.path.abspath(batch[2]),
          f"active={host_b._tabs[host_b._active].path}")

    # 連發途中目標視窗被關掉：檔案不能掉進正在銷毀的視窗裡。
    # 【不要 pump】closeEvent 到 destroyed 之間才是危險窗口：視窗還在管理器的
    # 清單裡，分頁卻已經清空。pump 過了 deleteLater 就跑完，剛好繞過這一段。
    fake_now["t"] += 0.05
    late = os.path.join(batch_dir, "late.md")
    with open(late, "w", encoding="utf-8") as handle:
        handle.write("# 關閉中的視窗不能吃掉這個檔案" + NL)
    keep = manager_b.create_window(batch[2])
    keep.resize(700, 500)
    pump(300)
    # 先讓上一批過期，這一條才會起新的一批、目標才會是 keep
    fake_now["t"] += manager_b.BURST_SECONDS + 0.1
    manager_b.route_external_open(batch[3])     # 讓 keep 成為連發目標
    pump(300)
    check("連發前置：連發目標確實是剛開的那個視窗",
          manager_b._burst_target is keep)
    fake_now["t"] += 0.05
    keep.close()                                # 不 pump，停在關閉中的狀態
    # 讓「最後作用中」也指著這個關閉中的視窗：不這樣的話路由本來就會挑到別人，
    # 這條檢查會變成不管有沒有跳過關閉中的視窗都綠
    manager_b._last_active = keep
    crashed = ""
    try:
        manager_b.route_external_open(late)
    except Exception as exc:      # noqa: BLE001
        crashed = repr(exc)

    # 【不要 pump 就判定】等 deleteLater 跑完，關閉中的視窗會從清單消失，
    # 「檔案掉進去了」和「檔案沒開成」就分不出來了
    def _holds(window, target):
        key = os.path.normcase(os.path.abspath(target))
        return any(tab.path and os.path.normcase(tab.path) == key
                   for tab in window._tabs)

    went_to_closing = _holds(keep, late)
    landed = any(w is not keep and _holds(w, late)
                 for w in manager_b.windows())
    check("連發途中視窗被關掉：不崩潰", not crashed, crashed)
    check("連發途中視窗被關掉：檔案不會掉進正在關閉的視窗", not went_to_closing)
    check("連發途中視窗被關掉：檔案落在活著的視窗裡，不會消失", landed,
          f"視窗數={len(manager_b.windows())}")
    pump(500)
    for window in list(manager_b.windows()):
        window.close()
    pump(400)
    for folder in (batch_dir, drop_dir):
        shutil.rmtree(folder, ignore_errors=True)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()


# ===========================================================================
# 區塊：分頁狀態還原
# ===========================================================================
def section_session(args) -> None:
    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=300):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app.viewer import MarkdownViewer

    def settings():
        return QSettings(config.ORG_NAME, config.APP_NAME)

    settings().clear()
    tmp = tempfile.mkdtemp()
    files = []
    for i in range(3):
        path = os.path.join(tmp, f"doc{i}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"# 文件 {i}\n\n內容 {i}\n")
        files.append(os.path.abspath(path))

    def new_viewer(target=None):
        viewer = MarkdownViewer(target)
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        viewer.show()
        pump(400)
        return viewer

    viewer = new_viewer(files[0])
    for path in files[1:]:
        viewer.open_path(path, new_tab=True)
        pump(250)
    viewer.close()
    pump(350)
    check("預設不記錄分頁", not settings().value(config.KEY_OPEN_TABS))

    viewer = new_viewer(files[0])
    viewer.set_restore_tabs(True)
    pump(200)
    for path in files[1:]:
        viewer.open_path(path, new_tab=True)
        pump(250)
    viewer.activate_tab(1)
    pump(250)
    viewer.close()
    pump(400)
    saved = settings().value(config.KEY_OPEN_TABS)
    check("開啟設定後關閉時會記錄分頁",
          isinstance(saved, list) and len(saved) == 3, str(saved))
    check("記錄作用中的分頁索引",
          int(settings().value(config.KEY_ACTIVE_TAB)) == 1)

    viewer = new_viewer(None)
    check("還原分頁數量正確", len(viewer._tabs) == 3, str(len(viewer._tabs)))
    check("還原到原本作用中的分頁", viewer._active == 1, str(viewer._active))
    check("作用中分頁有載入內容", "文件 1" in viewer.browser.toPlainText())
    check("背景分頁延後載入", not viewer._tabs[2].loaded)
    viewer.activate_tab(2)
    pump(300)
    check("切過去才載入背景分頁",
          viewer._tabs[2].loaded and "文件 2" in viewer.browser.toPlainText())
    viewer.close()
    pump(300)

    # 回歸：作用索引為 0 時 activate_tab 會被「已經在這一頁」擋掉，
    # 導致第一個分頁永遠沒載入，畫面一片空白。
    settings().setValue(config.KEY_RESTORE_TABS, True)
    settings().setValue(config.KEY_OPEN_TABS, files[:2])
    settings().setValue(config.KEY_ACTIVE_TAB, 0)
    settings().sync()
    viewer = new_viewer(None)
    check("還原且作用索引為 0 時，該分頁有被載入", viewer._tabs[0].loaded)
    check("還原且作用索引為 0 時，畫面有內容",
          "文件 0" in viewer.browser.toPlainText())
    check("還原且作用索引為 0 時，堆疊切到該分頁",
          viewer.stack.currentWidget() is viewer._tabs[0].browser)
    viewer.close()
    pump(300)

    viewer = new_viewer(files[2])
    paths = [tab.path for tab in viewer._tabs]
    check("命令列檔案已在還原清單時不重複開",
          len(paths) == len(set(paths)), str(paths))
    check("切到命令列指定的分頁", viewer._tab.path == files[2])
    viewer.close()
    pump(300)

    viewer = new_viewer(files[0])
    viewer.set_restore_tabs(False)
    pump(200)
    viewer.close()
    pump(350)
    check("關掉設定後清除分頁紀錄", not settings().value(config.KEY_OPEN_TABS))
    settings().clear()

    # --- 回歸：先開的視窗關閉時，不可以把後來改過的設定蓋回舊值 -------------
    # closeEvent 原本會把「自己記憶體裡的」每一項設定回寫一次。每個 setter
    # 在改動當下就已經寫過了，那次回寫純屬多餘——而且有害：A 視窗握著的是它
    # 開啟當下的值，B 視窗之後改的設定會在 A 關閉時被舊值蓋回去。
    # 實際症狀是「在 B 視窗調大字級，關掉 A 視窗，下次啟動又變回原樣」。
    settings().clear()
    window_a = new_viewer(files[0])
    window_b = new_viewer(files[1])
    default_width = window_a._content_width
    other_width = next(
        w for w, _key in config.CONTENT_WIDTH_OPTIONS if w != default_width
    )
    window_b.set_content_width(other_width)
    pump(200)
    check("前置：B 視窗改過的設定已經落地",
          settings().value(config.KEY_CONTENT_WIDTH, type=int) == other_width,
          str(settings().value(config.KEY_CONTENT_WIDTH)))
    window_a.close()          # A 還握著舊值
    pump(350)
    check("關閉先開的視窗不會把別的視窗改過的設定蓋回去",
          settings().value(config.KEY_CONTENT_WIDTH, type=int) == other_width,
          str(settings().value(config.KEY_CONTENT_WIDTH)))
    window_b.close()
    pump(350)
    settings().clear()


# ===========================================================================
# 區塊：視窗與邊緣縮放（含兩個會讓行程中止的回歸）
# ===========================================================================
def section_window(args) -> None:
    # --- 回歸：上次最大化就再也打不開 ---------------------------------------
    # showMaximized() 會在 __init__ 中段就把視窗顯示出來，那時第一個分頁還沒
    # 建立，resizeEvent 便去存取 self._tabs[0] -> IndexError -> 行程中止。
    # 而 closeEvent 會把 isMaximized() 存回設定，所以會一直復發。
    code, out, err = run_child(
        "maximized",
        '''
        real = settings()
        backup = {k: real.value(k) for k in real.allKeys()}
        real.clear()
        real.setValue(config.KEY_MAXIMIZED, True)
        real.sync()
        try:
            v = MarkdownViewer(None)
            v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            v.show(); pump(500)
            print("MAXIMIZED %s" % v.isMaximized())
            print("TABS %d" % len(v._tabs))
            print("RENDERED %s" % (len(v.browser.toPlainText().strip()) > 10))
            v.close(); pump(200)
        finally:
            real.clear()
            for k, val in backup.items():
                if val is not None:
                    real.setValue(k, val)
            real.sync()
        ''',
    )
    values = child_values(out)
    check("上次最大化時能正常啟動（不中止）", code == 0,
          f"結束碼 {code} {last_error(err)}")
    check("還原成最大化", values.get("MAXIMIZED") == "True", out.strip())
    check("最大化啟動時內容有渲染", values.get("RENDERED") == "True", out.strip())

    # --- 回歸：關掉分頁後滑鼠一動就中止 -------------------------------------
    # 邊緣縮放的捲軸名單是 showEvent 拍下的快照。分頁一關，它的捲軸 C++ 物件
    # 就沒了，殘留的 sip 包裝在 eventFilter 裡被碰到會丟 RuntimeError，
    # 而虛擬函式裡的例外對 PyQt6 是致命的。
    code, out, err = run_child(
        "resizer",
        '''
        real = settings(); real.clear(); real.sync()
        v = MarkdownViewer(README)
        v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        v.resize(1000, 700); v.show(); pump(700)
        v.open_path(SAMPLE, new_tab=True); pump(500)

        new_viewport = v._tabs[-1].browser.viewport()
        seen = {"n": 0}
        original = v._resizer.eventFilter
        def spy(watched, event):
            if event.type() == QEvent.Type.MouseMove and watched is new_viewport:
                seen["n"] += 1
            return original(watched, event)
        v._resizer.eventFilter = spy
        g = v.frameGeometry()
        gp = QPointF(float(g.left() + 3), float(g.center().y()))
        ev = QMouseEvent(QEvent.Type.MouseMove,
                         new_viewport.mapFromGlobal(gp.toPoint()).toPointF(), gp,
                         Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        app.sendEvent(new_viewport, ev)
        print("FILTER_ON_NEW_TAB %d" % seen["n"])
        v._resizer.eventFilter = original

        v.close_tab_at(0); pump(300)
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete); pump(300)
        print("STALE %d" % sum(1 for w in v._resizer._drag_controls if sip.isdeleted(w)))

        gp2 = QPointF(float(g.center().x()), float(g.center().y()))
        ev2 = QMouseEvent(QEvent.Type.MouseMove,
                          v.mapFromGlobal(gp2.toPoint()).toPointF(), gp2,
                          Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
        app.sendEvent(v, ev2)
        print("MOUSEMOVE_SURVIVED 1")
        v.close(); pump(200)
        real.clear()
        ''',
    )
    values = child_values(out)
    check("關閉分頁後移動滑鼠不會中止行程", code == 0,
          f"結束碼 {code} {last_error(err)}")
    check("關閉分頁後不留下已刪除的捲軸",
          values.get("STALE") == "0", out.strip())
    check("啟動後新增的分頁有裝上縮放事件過濾器",
          values.get("FILTER_ON_NEW_TAB", "0") != "0", out.strip())
    check("移動滑鼠後仍存活", values.get("MOUSEMOVE_SURVIVED") == "1", out.strip())

    # --- 四邊四角與捲軸讓位 ---------------------------------------------------
    from PyQt6.QtCore import QEventLoop, QPoint, QSettings, Qt, QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=250):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app.viewer import _CURSOR_BY_EDGES, MarkdownViewer

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    viewer = MarkdownViewer(README)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.resize(1100, 800)
    viewer.show()
    pump(500)

    geometry = viewer.frameGeometry()
    resizer = viewer._resizer
    edges = (
        ("左", geometry.left() + 2, geometry.center().y()),
        ("右", geometry.right() - 2, geometry.center().y()),
        ("上", geometry.center().x(), geometry.top() + 2),
        ("下", geometry.center().x(), geometry.bottom() - 2),
        ("右下角", geometry.right() - 3, geometry.bottom() - 3),
    )
    for label, x, y in edges:
        check(f"邊緣縮放-{label}",
              _CURSOR_BY_EDGES.get(resizer._edges_at(x, y)) is not None)
    check("視窗中央不觸發縮放",
          _CURSOR_BY_EDGES.get(
              resizer._edges_at(geometry.center().x(), geometry.center().y())) is None)
    if viewer.browser.verticalScrollBar().isVisible():
        check("捲軸位置讓給拖曳，不被縮放搶走",
              resizer._edges_at(geometry.right() - 7, geometry.center().y()) == 0)

    # --- 回歸：關閉作用分頁後搜尋列殘留舊文件的比對位置 ----------------------
    tmp = tempfile.mkdtemp()
    big = os.path.join(tmp, "big.md")
    small = os.path.join(tmp, "small.md")
    with open(big, "w", encoding="utf-8") as handle:
        handle.write("# 大\n\n" + ("alpha " * 200))
    with open(small, "w", encoding="utf-8") as handle:
        handle.write("# 小\n")
    viewer.open_path(big, new_tab=True)
    pump(400)
    viewer.open_path(small, new_tab=True)
    pump(300)
    index = next(i for i, tab in enumerate(viewer._tabs)
                 if tab.path == os.path.abspath(big))
    viewer.activate_tab(index)
    pump(300)
    viewer.find_bar.activate()
    viewer.find_bar.input.setText("alpha")
    pump(400)
    matched = len(viewer.find_bar._matches)
    check("搜尋列在大文件上有比對結果", matched > 100, str(matched))
    viewer.close_tab_at(viewer._active)
    pump(400)
    check("關閉作用分頁後搜尋列會關閉，不留舊文件的比對位置",
          not viewer.find_bar.isVisible() and not viewer.find_bar._matches,
          f"visible={viewer.find_bar.isVisible()} matches={len(viewer.find_bar._matches)}")

    viewer.close()
    pump(300)

    # --- 縮放器的名單不能留著別的視窗的元件 ---------------------------------
    # FramelessResizer 用 _drag_controls 記著「縮放要讓開」的捲軸。這份名單是
    # 快照：分頁被別的視窗收養之後，它的捲軸就改屬別人了。留著不只是髒資料
    # ——對方視窗一關就是懸空指標，而懸空指標在 eventFilter 裡是 0xC0000005
    # 硬崩潰，try/except 接不到。
    #
    # 這一條是確定性的：直接量名單的內容，不去賭那個間歇崩潰會不會發生。
    from PyQt6.QtWidgets import QScrollBar as _QScrollBar

    from app.window_manager import WindowManager as _WM

    def _foreign(window):
        """名單裡有幾個已經不屬於這個視窗。"""
        mine = set(window.findChildren(_QScrollBar))
        return [w for w in window._resizer._drag_controls if w not in mine]

    manager_r = _WM()
    keeper = manager_r.create_window(README)
    keeper.open_path(SAMPLE, new_tab=True)
    keeper.resize(700, 500)
    keeper.show()
    pump(500)
    check("前置：縮放器登記到了捲軸",
          len(keeper._resizer._drag_controls) > 0,
          str(len(keeper._resizer._drag_controls)))

    keeper._on_tab_detached(1, QPoint(1400, 820))
    pump(400)
    check("交出一個分頁後（視窗還有分頁）名單裡沒有別的視窗的元件",
          not _foreign(keeper), str(_foreign(keeper)))

    emptied = manager_r.create_window(SAMPLE)
    emptied.resize(700, 500)
    emptied.move(160, 160)
    emptied.show()
    pump(500)
    before_n = len(emptied._resizer._drag_controls)
    taken = emptied.take_tab(0)
    pump(300)
    # 【這條抓的是那個早退路徑】take_tab 在「視窗變空」時會提早 return，
    # 舊版就這樣跳過了重新登記——實測六筆裡有兩筆變成外來元件。
    check("交出**最後一個**分頁後名單裡也沒有別的視窗的元件",
          not _foreign(emptied),
          f"外來 {len(_foreign(emptied))} 筆，交出前共 {before_n} 筆")
    check("前置：交出最後一個分頁確實會讓名單變短（不然上一條是恆真）",
          len(emptied._resizer._drag_controls) < before_n,
          f"{before_n} -> {len(emptied._resizer._drag_controls)}")

    # 名單在視窗還沒顯示時也要更新：舊版整個 _refresh_resizer_targets 都被
    # isVisible() 擋掉，交出分頁的時機剛好在隱藏之後就照樣留著外來元件。
    hidden = manager_r.create_window(README)
    hidden.resize(600, 400)
    hidden.show()
    pump(400)
    hidden.open_path(SAMPLE, new_tab=True)
    pump(300)
    hidden.hide()
    pump(200)
    hidden._on_tab_detached(1, QPoint(1400, 900))
    pump(400)
    check("視窗隱藏時交出分頁，名單一樣不留外來元件",
          not _foreign(hidden), str(_foreign(hidden)))

    # 已被銷毀的包裝不能讓 eventFilter 爆掉
    probe = manager_r.create_window(README)
    probe.resize(600, 400)
    probe.show()
    pump(400)
    from PyQt6 import sip as _sip
    corpse = _QScrollBar(probe)
    probe._resizer._drag_controls = list(probe._resizer._drag_controls) + [corpse]
    _sip.delete(corpse)
    crashed = False
    try:
        probe._resizer._over_drag_control(10, 10)
    except Exception as exc:      # noqa: BLE001
        crashed = True
        detail = repr(exc)
    check("名單裡混進已銷毀的包裝時，命中測試不會丟例外",
          not crashed, detail if crashed else "")
    check("已銷毀的包裝會被就地剔除",
          all(not _sip.isdeleted(w) for w in probe._resizer._drag_controls))

    # 直接把「別的視窗的」捲軸塞進名單，命中測試要把它剔掉。
    # 上面那幾條走的是「重新登記」那條路（名單根本不會髒），這一條測的是
    # 最後一道防線本身——名單真的髒掉時，命中測試會不會自己清乾淨。
    other = manager_r.create_window(SAMPLE)
    other.resize(500, 360)
    other.move(520, 520)
    other.show()
    pump(400)
    outsider = other.findChildren(_QScrollBar)[0]
    probe._resizer._drag_controls = (
        list(probe._resizer._drag_controls) + [outsider])
    probe._resizer._over_drag_control(10, 10)
    check("名單裡混進別的視窗的元件時，命中測試會把它剔除",
          outsider not in probe._resizer._drag_controls,
          f"還留著 {len(probe._resizer._drag_controls)} 筆")
    check("剔除的只是外來的那個，本視窗的元件要留著",
          all(w.window() is probe for w in probe._resizer._drag_controls)
          and len(probe._resizer._drag_controls) > 0,
          str(len(probe._resizer._drag_controls)))

    for window in list(manager_r.windows()):
        window.close()
    pump(400)

    QSettings(config.ORG_NAME, config.APP_NAME).clear()


# ===========================================================================
# 區塊：單一實例
# ===========================================================================
def section_single_instance(args) -> None:
    """一定要用兩個行程，而且要用專屬的管道名稱。

    兩個行程：送方的 waitFor* 會把事件迴圈鎖住，同一個行程裡收方根本沒機會
    處理連線，測起來每次都「收到空字串」——那是測試的假象，不是程式的問題。

    專屬名稱：Windows 允許同一個管道名稱有多個監聽者。開發時如果剛好有一個
    真的 MarkdownReader 在跑，送方會連到它去，測試伺服器什麼都收不到，
    看起來像功能壞了。這裡改用帶行程編號的名稱，測試才不受環境影響。
    """
    from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer

    app = QCoreApplication.instance() or QCoreApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    from app import single_instance

    # --- 管道名稱一致性 ---
    # 管道名稱＝基底名稱＋登入工作階段編號，Python（config.IPC_SERVER_NAME）與
    # C++ 轉交器（src_cpp/md_open/main.cpp 的 pipe_path）各算一次。改到不同步的話
    # 不會有任何錯誤訊息：轉交器每次都「找不到管道」，安靜退化成每個檔案開一個
    # 視窗。三條都比對原始碼／在行程內算，改壞立刻紅燈。
    #
    # 【為什麼不拿真的轉交器連進來當端對端測試】試過，但殺不動「C++ 端把編號算錯」
    # 這種突變：轉交器連錯名字→找不到→改用命令列參數啟動本體，而那個本體是編號
    # 正確的產物，一起來就連上測試自己開的監聽端、把路徑交回來——路徑照樣抵達，
    # 突變被「被啟動的本體救回來」給遮住。那個測試因此只能證明 Python 名稱與產物
    # 相符（＝下面第三條已經涵蓋的事），卻要付出重編轉交器、殘留本體行程與時序
    # 不穩的代價，不划算。真正的轉交往返由既有的單一實例測試（用測試專屬管道名）
    # 與 onscreen 的「忙碌解除後轉交的檔案有開出來」涵蓋。
    import ctypes

    launcher_src = os.path.join(PROJECT_ROOT, "src_cpp", "md_open", "main.cpp")
    if os.path.isfile(launcher_src):
        with open(launcher_src, encoding="utf-8") as handle:
            cpp = handle.read()
        base_line = next(
            (line for line in cpp.splitlines()
             if "kPipeBase" in line and 'L"' in line),
            "",
        )
        check("C++ 轉交器的管道基底名稱與 config.IPC_SERVER_BASE 一致",
              f'pipe\\\\{config.IPC_SERVER_BASE}"' in base_line,
              base_line.strip() or "(找不到 kPipeBase)")
        check("C++ 轉交器在執行時把登入工作階段編號接在基底名稱後面（和 Python 端同一個算法）",
              "ProcessIdToSessionId(GetCurrentProcessId(), &session)" in cpp
              and re.search(r'wsprintfW\(path,\s*L"%s\.%lu",\s*kPipeBase,\s*session\)', cpp)
              is not None)

    session = ctypes.c_ulong(0)
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    check("管道名稱帶登入工作階段編號（兩個使用者同時登入不再互搶；以前註解說有、其實沒有）",
          config.IPC_SERVER_NAME == f"{config.IPC_SERVER_BASE}.{session.value}",
          config.IPC_SERVER_NAME)

    # --- build.ps1 不能被 stderr 中斷 ---
    # 為什麼這條在「單一實例」這一節：轉交器只有 build.ps1 的第 5 步會產出並
    # 複製進 dist。腳本若在第 4 步中斷，第 5 步不會跑，dist 裡就沒有
    # MarkdownOpen.exe——而檔案關聯正是指向它。症狀是 .md 圖示變白紙、雙擊
    # 完全沒反應，也沒有任何錯誤訊息，等於單一實例的快路徑整條消失。真的發生過。
    LF = chr(10)
    build_script = os.path.join(PROJECT_ROOT, "build.ps1")
    if os.path.isfile(build_script):
        script_bytes = open(build_script, "rb").read()
        check("build.ps1 存成 UTF-8 with BOM"
              "（沒有 BOM 的話 PowerShell 5.1 當成 cp950，中文全毀）",
              script_bytes.startswith(b"\xef\xbb\xbf"),
              str(script_bytes[:4]))
        ps1 = script_bytes.decode("utf-8-sig")

        # 要連大括號一起比對：只找 "function Invoke-Native" 的話，
        # 改名成 Invoke-NativeRenamed 也會通過（子字串）。
        check("build.ps1 有 Invoke-Native（外部程式的統一入口）",
              re.search(r"function\s+Invoke-Native\s*\{", ps1) is not None)
        # PowerShell 5.1 在 $ErrorActionPreference = "Stop" 下，外部程式往 stderr
        # 寫任何一行都會被包成終止錯誤。PyInstaller 的進度訊息全走 stderr。
        bare_calls = [
            line.strip() for line in ps1.splitlines()
            if "PyInstaller" in line and "Invoke-Native" not in line
            and not line.strip().startswith("#")
            and ("py -3.13" in line or "& " in line)
        ]
        check("PyInstaller 一律經由 Invoke-Native 呼叫（裸呼叫會被 stderr 中斷）",
              not bare_calls, str(bare_calls))
        # $? 在外部程式之後不可靠：stderr 被包裝會讓它變 False，而且任何一次
        # 賦值都會把它重設。唯一可信的是 $LASTEXITCODE。
        # 只看會執行的行：說明為什麼別用 $? 的註解本身也含 $?，
        # 不濾掉的話這條會被自己的文件釘死。
        code_only = re.sub(r"<#.*?#>", "", ps1, flags=re.S)
        code_only = LF.join(
            line for line in code_only.splitlines()
            if not line.strip().startswith("#")
        )
        check("不用 $? 判斷外部程式的成敗（改用 $LASTEXITCODE）",
              "$?" not in code_only,
              next((line.strip() for line in code_only.splitlines()
                    if "$?" in line), ""))
        check("PyInstaller 失敗會就地停，不讓第 5 步在半成品上跑",
              "$packed -ne 0" in ps1 and "exit 1" in ps1)
        check("第 5 步會把轉交器複製進 dist 的兩個位置",
              'foreach ($dest in @("dist", "dist\\MarkdownReader-onedir"))' in ps1,
              "找不到複製轉交器那一段")
        check("成功時明確回 0（不然會沿用最後一個外部程式的結束碼）",
              "exit 0" in ps1)

    # --- 檔案關聯目標的自動解析：速度階梯 -----------------------------------
    # resolve_target 無參數時曾經只認 dist\MarkdownReader.exe（onefile），
    # 照文件跑無參數安裝就把「本體開著、再雙擊 .md」綁在最慢的路徑上
    # （每次先自解壓，約一秒；轉交器 ~10ms）。這組檢查釘住優先序：
    # onedir 轉交器 > dist 轉交器 > onedir 本體 > onefile 本體 > 開發模式。
    import contextlib
    import io as _io
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "install_association", os.path.join(PROJECT_ROOT, "tools",
                                            "install_association.py"))
    assoc = _ilu.module_from_spec(spec)
    spec.loader.exec_module(assoc)
    _dist = os.path.join(PROJECT_ROOT, "dist")
    _tiers = [
        os.path.join(_dist, "MarkdownReader-onedir", "MarkdownOpen.exe"),
        os.path.join(_dist, "MarkdownOpen.exe"),
        os.path.join(_dist, "MarkdownReader-onedir", "MarkdownReader.exe"),
        os.path.join(_dist, "MarkdownReader.exe"),
    ]

    def _resolve_with(existing):
        """把 isfile 換成假清單後解析，回傳 (目標, 命令列, 印出的文字)。"""
        real = assoc.os.path.isfile
        table = {os.path.normcase(p) for p in existing}
        assoc.os.path.isfile = lambda p: os.path.normcase(p) in table
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                target, command = assoc.resolve_target(None)
        finally:
            assoc.os.path.isfile = real
        return target, command, buf.getvalue()

    t, _c, _o = _resolve_with(_tiers)
    check("關聯解析①：四個目標都在時挑 onedir 轉交器（開著時 ~10ms）",
          t == _tiers[0], t)
    t, _c, _o = _resolve_with(_tiers[1:])
    check("關聯解析②：沒有 onedir 轉交器時退到 dist 根目錄的轉交器",
          t == _tiers[1], t)
    t, _c, _o = _resolve_with(_tiers[2:])
    check("關聯解析③：只剩本體時挑 onedir（冷啟動快近一倍）",
          t == _tiers[2], t)
    t, _c, warned = _resolve_with(_tiers[3:])
    check("關聯解析④：只有 onefile 時仍可用，但要印自解壓警告",
          t == _tiers[3] and "自解壓" in warned, f"{t} / 印出={warned[:40]}")
    _t, cmd, _o = _resolve_with([])
    check("關聯解析⑤：dist 全空時退回開發模式（pythonw + main.py）",
          "main.py" in cmd, cmd)
    explicit_t, explicit_c = assoc.resolve_target(
        os.path.join(PROJECT_ROOT, "tools", "install_association.py"))
    check("關聯解析⑥：--target 明確指定時照用、不走階梯",
          explicit_t.endswith("install_association.py"), explicit_c)

    # --- 安裝檔腳本的靜態檢查 -------------------------------------------------
    # 動態安裝／反安裝（「安裝檔」區塊）要有 ISCC 與打包產物，offscreen/CI 大多
    # 會 [略過]；真正可靠的護欄是這裡對 .iss 與 build.ps1 字面做的斷言，每一條
    # 都有對應的突變會讓它變紅。只看會生效的行——註解裡會提到「不能用
    # uninsdeletekey」這類字樣，不濾掉會被自己的說明釘死。
    iss_path = os.path.join(PROJECT_ROOT, "installer", "MarkdownReader.iss")
    isl_path = os.path.join(PROJECT_ROOT, "installer", "ChineseTraditional.isl")
    check("安裝檔：腳本存在（installer\\MarkdownReader.iss）", os.path.isfile(iss_path))
    iss_raw = open(iss_path, encoding="utf-8-sig").read() if os.path.isfile(iss_path) else ""
    iss = LF.join(line for line in iss_raw.splitlines()
                  if not line.lstrip().startswith(";"))
    ps1_text = open(os.path.join(PROJECT_ROOT, "build.ps1"),
                    encoding="utf-8-sig").read()

    def _setup_value(key):
        found = re.search(r"(?m)^" + re.escape(key) + r"\s*=\s*(.+?)\s*$", iss)
        return found.group(1) if found else None

    check("安裝檔：每使用者安裝、免系統管理員（PrivilegesRequired=lowest）",
          _setup_value("PrivilegesRequired") == "lowest",
          str(_setup_value("PrivilegesRequired")))
    check("安裝檔：AppId 是固定 GUID（同一個才會就地升級）",
          re.search(r'#define AppId "\{\{[0-9A-F-]{36}\}"', iss) is not None)
    check("安裝檔：ChangesAssociations=yes（通知檔案總管重整關聯）",
          _setup_value("ChangesAssociations") == "yes")
    check("安裝檔：CloseApplications=yes（升級前請本體關閉）",
          _setup_value("CloseApplications") == "yes")
    check("安裝檔：64 位元安裝模式",
          _setup_value("ArchitecturesInstallIn64BitMode") == "x64compatible")
    check("安裝檔：AppVersion 沒有預設值，未定義就 #error（版本只能來自 build.ps1）",
          re.search(r"#ifndef AppVersion\s*\n\s*#error", iss) is not None)
    check("安裝檔：[Files] 明列 MarkdownOpen.exe（來源缺檔要讓 ISCC 直接失敗）",
          re.search(r'Source:\s*"\{#SourceDir\}\\MarkdownOpen\.exe"', iss) is not None)
    # 轉交器的版本資源以前寫死在 app.rc，升版沒人記得改——1.1.0 的安裝檔裡
    # 裝著一個標示 1.0.0.0 的執行檔。現在由 CMake 從 app/__init__.py 產生
    # version.h；這兩條擋的是「有人又把版本抄回 .rc 裡」。
    rc_path = os.path.join(PROJECT_ROOT, "src_cpp", "md_open", "app.rc")
    with open(rc_path, encoding="utf-8") as handle:
        rc_text = handle.read()
    cmake_path = os.path.join(PROJECT_ROOT, "src_cpp", "md_open", "CMakeLists.txt")
    with open(cmake_path, encoding="utf-8") as handle:
        cmake_text = handle.read()
    check("轉交器：app.rc 不寫死版本號，用 CMake 產生的巨集",
          "APP_VERSION_CSV" in rc_text and "APP_VERSION_STR" in rc_text
          and re.search(r"FILEVERSION\s+\d+\s*,", rc_text) is None,
          re.search(r"FILEVERSION.*", rc_text).group(0) if "FILEVERSION" in rc_text else "沒有 FILEVERSION")
    # regdump 查的是寫死的 AppId；和 .iss 對不上時，「Uninstall 項已刪」那條會
    # 因為永遠查不到鍵而假通過——反安裝根本沒驗到。
    iss_appid = re.search(r'#define\s+AppId\s+"\{\{([0-9A-Fa-f-]+)\}?"', iss)
    regdump_path = os.path.join(PROJECT_ROOT, "tools", "sandbox_regdump.cmd")
    with open(regdump_path, encoding="utf-8", errors="replace") as handle:
        regdump_text = handle.read()
    check("安裝檔：沙盒 regdump 查的 AppId 與 .iss 相同（否則反安裝檢查會假通過）",
          iss_appid is not None and iss_appid.group(1) in regdump_text,
          iss_appid.group(1) if iss_appid else "在 .iss 找不到 AppId")
    check("轉交器：CMakeLists 從 app/__init__.py 讀版本（版本來源只有一個）",
          "app/__init__.py" in cmake_text.replace("\\", "/")
          and "__version__" in cmake_text and "configure_file" in cmake_text)
    # 打包排除清單：名字對不上就等於沒排除——1.0.0 與 1.1.0 寫的是改名前的
    # libcrypto-3.dll，產物裡叫 libcrypto-3-x64.dll，於是 6.8MB 的 OpenSSL 白背了兩版。
    spec_path = os.path.join(PROJECT_ROOT, "build.spec")
    with open(spec_path, encoding="utf-8") as handle:
        spec_text = handle.read()
    prefix_block = re.search(r"EXCLUDE_BINARY_PREFIXES\s*=\s*\((.*?)\)", spec_text, re.S)
    prefixes = re.findall(r'"([^"]+)"', prefix_block.group(1)) if prefix_block else []
    import PyQt6 as _pyqt6_pkg

    search_dirs = [os.path.join(os.path.dirname(_pyqt6_pkg.__file__), "Qt6", "bin"),
                   os.path.join(os.path.dirname(sys.executable), "DLLs")]
    real_names = {n.lower() for d in search_dirs if os.path.isdir(d) for n in os.listdir(d)}
    unmatched = [p for p in prefixes if not any(n.startswith(p) for n in real_names)]
    check("打包排除清單的每個前綴都對得上 Qt 或 Python 裡真實存在的檔名",
          bool(prefixes) and not unmatched, f"對不上：{unmatched}")
    check("OpenSSL 的三個使用者（_ssl、ssl、_hashlib）在模組排除清單裡",
          all(f'"{m}"' in spec_text for m in ("_ssl", "ssl", "_hashlib")))
    check("安裝檔：升級前整包清 {app}\\_internal（殘留舊版 Qt 外掛會當機）",
          re.search(r'\[InstallDelete\]\s*\n\s*Type:\s*filesandordirs;\s*'
                    r'Name:\s*"\{app\}\\_internal"', iss) is not None)
    check("安裝檔：關聯 command 指向轉交器，字面與 install_association 相同",
          r'"""{app}\MarkdownOpen.exe"" ""%1"""' in iss
          and r'{app}\MarkdownReader.exe"" ""%1' not in iss)
    check("安裝檔：DefaultIcon 也指向轉交器", r'{app}\MarkdownOpen.exe,0' in iss)
    owp_exts = set(re.findall(r'\{#ClassesRoot\}\\(\.[a-z]+)\\OpenWithProgids', iss))
    fa_exts = set(re.findall(
        r'FileAssociations";\s*ValueType:\s*string;\s*ValueName:\s*"(\.[a-z]+)"', iss))
    check("安裝檔：OpenWithProgids 的副檔名與 config.MARKDOWN_SUFFIXES 逐一相符",
          owp_exts == set(config.MARKDOWN_SUFFIXES),
          f"差集 {sorted(owp_exts ^ set(config.MARKDOWN_SUFFIXES))}")
    check("安裝檔：Capabilities\\FileAssociations 的副檔名與 config 逐一相符",
          fa_exts == set(config.MARKDOWN_SUFFIXES),
          f"差集 {sorted(fa_exts ^ set(config.MARKDOWN_SUFFIXES))}")
    # 反安裝旗標矩陣：三種鍵配三種旗標，錯一個就會砍到使用者的東西
    progid_line = next((line for line in iss.splitlines()
                        if r'Subkey: "{#ClassesRoot}\{#ProgId}"' in line), "")
    check("安裝檔：ProgID 整棵用 uninsdeletekey（整棵都是我們的）",
          "uninsdeletekey" in progid_line, progid_line[-70:])
    owp_lines = [line for line in iss.splitlines() if r'\OpenWithProgids"' in line]
    check("安裝檔：副檔名底下只刪自己的值（uninsdeletevalue），鍵空了才清",
          bool(owp_lines) and all("uninsdeletevalue" in line
                                  and "uninsdeletekeyifempty" in line
                                  for line in owp_lines),
          str([line[-70:] for line in owp_lines
               if "uninsdeletevalue" not in line][:2]))
    check("安裝檔：副檔名那一層絕不出現 uninsdeletekey（那是使用者的鍵）",
          not any(re.search(r"\buninsdeletekey\b", line) for line in owp_lines))
    reg_root_lines = [line for line in iss.splitlines()
                      if r'Subkey: "{#AppRegRoot}' in line]
    check("安裝檔：QSettings 的父鍵不出現 uninsdeletekey（只能砍 Capabilities 子樹）",
          bool(reg_root_lines) and all(
              r"\Capabilities" in line or "uninsdeletekey" not in line
              for line in reg_root_lines))
    check("安裝檔：全部只寫 HKCU，不碰 HKLM",
          "HKLM" not in iss and "Root: HKA" not in iss and "Root: HKCU" in iss)
    check("安裝檔：繁中語言檔 vendored 且以相對路徑引用",
          os.path.isfile(isl_path)
          and 'MessagesFile: "ChineseTraditional.isl"' in iss)
    check("安裝檔：不寫副檔名預設值（UserChoice 下無效，反安裝也難處理）",
          re.search(r'Subkey:\s*"\{#ClassesRoot\}\\\.[a-z]+";', iss) is None)
    check("安裝檔：反安裝時詢問是否移除設定，預設保留",
          "CustomMessage('RemoveSettings')" in iss and "IDNO) = IDYES" in iss)
    check("build.ps1 -Installer：三道前置檢查都在（onedir 本體、轉交器、ISCC）",
          all(marker in ps1_text for marker in (
              r'dist\MarkdownReader-onedir\MarkdownReader.exe"',
              r'dist\MarkdownReader-onedir\MarkdownOpen.exe"',
              "ISCC.exe")))
    check("build.ps1 -Installer：ISCC 經 Invoke-Native 呼叫並檢查 $LASTEXITCODE",
          re.search(r"Invoke-Native -File \$iscc[^\n]*\n\s*if \(\$LASTEXITCODE -ne 0\)",
                    ps1_text) is not None)
    check("build.ps1 -Installer：版本只接受純數字三段",
          r'(\d+\.\d+\.\d+)' in ps1_text)

    pipe_name = f"MarkdownReaderSmokeTest.{os.getpid()}"
    original_pipe = config.IPC_SERVER_NAME
    config.IPC_SERVER_NAME = pipe_name

    sender_source = textwrap.dedent(
        '''
        import sys, time
        sys.path.insert(0, r"{root}")
        from app import config
        config.IPC_SERVER_NAME = "{pipe}"
        from app import single_instance
        start = time.perf_counter()
        ok = single_instance.send_to_existing({payload})
        print("%s %.2f" % (ok, 1000 * (time.perf_counter() - start)))
        '''
    ).format(root=PROJECT_ROOT.replace("\\", "/"), pipe=pipe_name, payload="{payload}")

    server = single_instance.InstanceServer()
    check("具名管道監聽成功", server.listen())
    received: list[str] = []
    server.pathReceived.connect(received.append)

    def send(payload: str):
        proc = subprocess.Popen(
            [sys.executable, "-c", sender_source.format(payload=payload)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        while proc.poll() is None:
            pump(20)
        out, _err = proc.communicate(timeout=10)
        pump(150)
        parts = out.strip().split()
        return (parts[0] == "True", float(parts[1])) if len(parts) == 2 else (None, -1.0)

    timings = []
    for i in range(3):
        ok, elapsed = send(f'r"C:/doc{i}.md"')
        timings.append(elapsed)
        check(f"第 {i + 1} 次轉交回報成功", ok is True)
    check("三次都收到", len(received) == 3, str(received))
    check("轉交內容正確",
          received == [f"C:/doc{i}.md" for i in range(3)], str(received))
    check("轉交夠快（未逾時空等）", max(timings) < 200,
          f"最慢 {max(timings):.1f} ms")

    def last_received() -> str | None:
        return received[-1] if received else None

    send("None")
    check("沒有指定檔案時送出空字串（只喚醒視窗）", last_received() == "",
          repr(last_received()))

    chinese = "D:/我的 文件/測試 檔.md"
    send(f'r"{chinese}"')
    check("含中文與空白的路徑正確傳遞", last_received() == chinese, repr(last_received()))

    long_path = "D:/" + "a" * 200 + "/深/長/路徑.md"
    send(f'r"{long_path}"')
    check("長路徑完整傳遞", last_received() == long_path)

    server.close()
    pump(150)
    import time as _time

    start = _time.perf_counter()
    missed = single_instance.send_to_existing("C:/none.md")
    elapsed = 1000 * (_time.perf_counter() - start)
    check("沒有實例在跑時回報 False", missed is False)
    check("沒有實例時的探測幾乎不花時間（不拖慢正常啟動）",
          elapsed < 25, f"{elapsed:.2f} ms")
    config.IPC_SERVER_NAME = original_pipe

    # --- 回歸：本體忙碌時三條轉交連線背靠背 ---
    # C++ 轉交器的忙碌重試讓多條連線能在本體恢復後一口氣灌進來。收方若在
    # socket 的 readyRead 回呼裡直接做完「拆 socket -> 開分頁 -> 渲染」，
    # 管道回呼會與拆除中的 socket 狀態交錯，必定以 0xC0000005 死在原生層
    # （當時 6 / 6 重現）。修法是 deliver() 用 singleShot(0) 延後派送。
    # 這裡用掛起行程重現「本體正忙」，比等它渲染大檔更可控。
    launcher = os.path.join(PROJECT_ROOT, "dist", "MarkdownReader-onedir",
                            "MarkdownOpen.exe")
    if os.path.isfile(launcher):
        import ctypes as _ct
        _k32 = _ct.windll.kernel32
        _k32.OpenProcess.restype = _ct.c_void_p
        _ntdll = _ct.windll.ntdll
        py313 = sys.executable
        subprocess.run(["taskkill", "/F", "/IM", "MarkdownReader.exe"],
                       capture_output=True, check=False)
        app_proc = subprocess.Popen(
            [py313, "main.py", SAMPLE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=PROJECT_ROOT,
        )
        import time as _t
        deadline = _t.perf_counter() + 30
        window = None
        _u32 = _ct.windll.user32
        if _offscreen():
            # offscreen 沒有原生視窗標題可找，改探單一實例管道：連得上就是
            # 本體起來了。「標題後綴陷阱」的精確標題比對只有實機驗得到，
            # 由下面的 onscreen_only 明講跳過。
            from PyQt6.QtNetwork import QLocalSocket
            while _t.perf_counter() < deadline:
                sock = QLocalSocket()
                sock.connectToServer(config.IPC_SERVER_NAME)
                if sock.waitForConnected(200):
                    sock.abort()
                    window = 1
                    break
                _t.sleep(0.05)
        else:
            while _t.perf_counter() < deadline:
                # 用和程式同一個翻譯來源組標題，而不是寫死字串。
                # 【這同時是「標題後綴陷阱」的回歸測試】Qt 在 Windows 上若發現
                # windowTitle 沒有以 applicationDisplayName 結尾，會自動補一段
                # " - <displayName>"。main.py 與 viewer._update_titles 的來源
                # 一旦分家，實際標題就會多出後綴，這裡的精確比對立刻紅燈。
                window = _u32.FindWindowW(None, _expected_title("sample.md"))
                if window:
                    break
                _t.sleep(0.05)
        if not window:
            check("忙碌背靠背回歸：第一個視窗有起來", False)
            app_proc.kill()
        else:
            _t.sleep(0.6)
            handle = _k32.OpenProcess(0x1F0FFF, False, app_proc.pid)
            _ntdll.NtSuspendProcess(_ct.c_void_p(handle))
            senders = [subprocess.Popen([launcher, README]) for _ in range(3)]
            _t.sleep(1.5)
            _ntdll.NtResumeProcess(_ct.c_void_p(handle))
            _k32.CloseHandle(_ct.c_void_p(handle))
            for proc in senders:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
            _t.sleep(2.0)
            alive = app_proc.poll() is None
            check("本體忙碌時三連發轉交不會讓它崩潰", alive,
                  "" if alive else f"結束碼 {app_proc.poll() & 0xFFFFFFFF:#x}")
            check("三個轉交器都順利交棒（結束碼 0）",
                  all(p.returncode == 0 for p in senders),
                  str([p.returncode for p in senders]))
            if onscreen_only("忙碌解除後轉交的檔案有開出來（查原生視窗標題）"):
                got_tab = bool(
                    _u32.FindWindowW(None, _expected_title("README.md")))
                check("忙碌解除後轉交的檔案有開出來（查原生視窗標題）", got_tab)
            if alive:
                app_proc.terminate()
            try:
                app_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app_proc.kill()
        subprocess.run(["taskkill", "/F", "/IM", "MarkdownReader.exe"],
                       capture_output=True, check=False)


# ===========================================================================
# 區塊：關閉穩定度
# ===========================================================================
def section_teardown(args) -> None:
    """關閉時的存取違規是機率性的，必須連續跑多次才測得出來。

    分頁功能引進後實測 12 次會崩 4 次——跑三次的話有相當機率全部矇混過關。

    【子行程一定要跑 app.exec()】
    WA_DeleteOnClose 是把主視窗排進 deleteLater，要有事件迴圈才會真的拆除。
    若只 processEvents() 幾輪就結束直譯器，視窗的 C++ 物件會活到 Python 回收
    區域變數時才拆，那時 QApplication 往往已經不在——實測 20 次會崩 3 次。
    但那是測試自己造出來的情境，正式路徑（main.py）一定會跑 app.exec()。
    這裡照著正式路徑寫，測到的才是使用者真的會遇到的行為。
    """
    body = '''
        real = settings(); real.clear(); real.sync()
        v = MarkdownViewer(SAMPLE)
        v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        v.open_path(README, new_tab=True)
        v.show()
        QTimer.singleShot(400, v.close)
        app.exec()
        print("CLOSED 1")
        real.clear()
    '''
    clean = 0
    for index in range(args.runs):
        code, out, err = run_child(f"teardown{index}", body)
        if code == 0 and "CLOSED 1" in out:
            clean += 1
    check(f"連續開關 {args.runs} 次都乾淨結束", clean == args.runs,
          f"{clean} / {args.runs}")

    # main.py 的物件所有權：server 若掛在 viewer 底下，WA_DeleteOnClose 會把
    # QLocalServer 的 C++ 物件一起銷毀，app.exec() 之後的 server.close() 就爆。
    code, out, err = run_child(
        "ownership",
        '''
        from app import single_instance
        real = settings(); real.clear(); real.sync()
        server = single_instance.InstanceServer()
        if not server.listen():
            server = None
        v = MarkdownViewer(SAMPLE)
        v.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        if server is not None:
            server.setParent(app)
            server.pathReceived.connect(v.handle_external_open)
        v.show()
        QTimer.singleShot(800, v.close)
        app.exec()
        if server is not None:
            server.close()
        print("SHUTDOWN 1")
        real.clear()
        ''',
    )
    check("關閉視窗後 server.close() 不會踩到已銷毀的物件",
          code == 0 and "SHUTDOWN 1" in out,
          f"結束碼 {code} {last_error(err)}")


# ===========================================================================
def section_tab_dnd(args) -> None:
    """瀏覽器式分頁操作：拖曳排序、拆分成新視窗、合併回別的視窗。

    這些檢查走 viewer 層的 API（move_tab / _on_tab_detached / drop_target_at），
    拖曳手勢本身另以合成滑鼠事件驗證重排。座標的教訓：測「拖到空白處」時，
    空白點必須先把所有視窗移開再選——上一版用 (3000,3000)，結果剛拆出去的
    視窗就停在那裡，變成測到合併。
    """
    import tempfile as _tempfile

    from PyQt6.QtCore import (
        QEvent, QEventLoop, QPoint, QPointF, QRect, QSettings, Qt, QTimer,
    )
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=280):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app import styles as _styles
    from app.window_manager import WindowManager

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    tmp = _tempfile.mkdtemp()
    docs = []
    for i in range(3):
        path = os.path.join(tmp, f"d{i}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"# 文件{i}\n\n內容 {i}\n")
        docs.append(os.path.abspath(path))

    manager = WindowManager()
    viewer = manager.create_window(docs[0])
    viewer.move(80, 80)
    for path in docs[1:]:
        viewer.open_path(path, new_tab=True)
        pump()
    pump()

    def names(w):
        return [t.display_name for t in w._tabs]

    # --- 移動（資料層）---
    viewer.activate_tab(0)
    pump(150)
    viewer.move_tab(0, 2)
    check("拖曳排序：順序正確", names(viewer) == ["d1.md", "d2.md", "d0.md"], str(names(viewer)))
    check("拖曳排序：作用中分頁跟著移動", viewer._tab.display_name == "d0.md")
    viewer.move_tab(2, 0)

    # --- 移動（真實拖曳手勢：合成滑鼠事件掃過鄰居中心）---
    buttons = viewer.tab_bar._buttons
    src = buttons[0]
    start = src.mapToGlobal(src.rect().center())
    press = QMouseEvent(QEvent.Type.MouseButtonPress,
                        QPointF(src.rect().center()), QPointF(start),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    app.sendEvent(src, press)
    target_x = buttons[1].mapToGlobal(buttons[1].rect().center()).x() + 10
    for step_x in range(start.x(), target_x, 12):
        move = QMouseEvent(QEvent.Type.MouseMove,
                           QPointF(src.mapFromGlobal(QPoint(step_x, start.y()))),
                           QPointF(step_x, start.y()),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        app.sendEvent(src, move)
    release = QMouseEvent(QEvent.Type.MouseButtonRelease,
                          QPointF(src.mapFromGlobal(QPoint(target_x, start.y()))),
                          QPointF(target_x, start.y()),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
    app.sendEvent(src, release)
    pump(200)
    check("真實拖曳手勢能重排（跨過右鄰的中心）",
          names(viewer)[1] == "d0.md", str(names(viewer)))
    check("手勢重排後資料與按鈕一致",
          [b._name for b in viewer.tab_bar._buttons] == names(viewer))
    check("放開後列內的指示線收起", not viewer.tab_bar._insert_marker.isVisible())

    # 列內排序只有一條規則：游標落在被拖分頁的哪一半，就畫那一側的緣——
    # 右半畫右緣、左半畫左緣。看的是游標**此刻**相對被拖分頁的位置，不是相對
    # 按下點：往右拖到底再往回晃，一過中心就翻左緣，不會停在「你往右移過」。
    inbar = viewer.tab_bar

    def _side(sample):
        """這一筆該畫哪一側：靠左緣傳 "L"、靠右緣傳 "R"、沒顯示傳 "-"。"""
        if not sample["shown"]:
            return "-"
        if abs(sample["line"] - sample["left"]) <= 1:
            return "L"
        if abs(sample["line"] - sample["right"]) <= 1:
            return "R"
        return "?"

    def sweep(src, xs):
        """按下 src，游標依序移到 xs 這些全域 x，每一步取樣，最後放開。

        【手勢進行中絕對不要跑巢狀事件迴圈】這裡曾經每一步 pump(30)，害後面的
        測試隨機 0xC0000005 崩在 FramelessResizer——巢狀迴圈把排隊的 deleteLater
        沖出來，拖曳中途銷毀元件留下懸空指標。改成不 pump：_move_button 會自己
        activate() 版面，buttons[i].x() 當場就是對的。
        """
        y = src.mapToGlobal(src.rect().center()).y()
        app.sendEvent(src, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(src.rect().center()),
            QPointF(src.mapToGlobal(src.rect().center())),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        bar_left = inbar.mapToGlobal(inbar.rect().topLeft()).x()
        bar_right = bar_left + inbar.rect().width()
        samples = []
        for raw_gx in xs:
            gx = max(bar_left + 6, min(raw_gx, bar_right - 6))
            pt = QPoint(gx, y)
            app.sendEvent(src, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(src.mapFromGlobal(pt)),
                QPointF(pt), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
            marker = inbar._insert_marker
            strip_x = inbar._strip.mapFromGlobal(pt).x()
            samples.append({
                "slot": inbar._buttons.index(src),
                # 手勢還沒真的開始（沒超過 startDragDistance）時 _drag_button 是
                # None，那時什麼都不該畫。用它當旗標，不用「跳過第一筆」的土法。
                "dragging": inbar._drag_button is not None,
                "shown": marker.isVisibleTo(inbar),
                "line": marker.x() + marker.line_x(),
                "left": src.x(),
                "right": src.x() + src.width(),
                # 游標此刻相對被拖分頁中心：>0 在右半、<0 在左半
                "rel": strip_x - (src.x() + src.width() // 2),
            })
        app.sendEvent(src, QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(src.mapFromGlobal(pt)),
            QPointF(pt), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        pump(200)
        return samples

    def _rule_holds(samples):
        """有顯示的每一筆，側邊都要吻合「游標落在哪一半」。中心線±2px 的模糊帶
        不計（版面剛換槽位時中心會抖一兩像素）。"""
        bad = []
        for s in samples:
            if not s["dragging"] or not s["shown"] or abs(s["rel"]) <= 2:
                continue
            want = "R" if s["rel"] > 0 else "L"
            if _side(s) != want:
                bad.append((s["slot"], s["rel"], _side(s), want))
        return bad

    src0 = inbar._buttons[0]
    base = src0.mapToGlobal(src0.rect().center()).x()

    # --- 往右拖：游標一路在被拖分頁右半 -> 一路右緣 ---
    right_samples = sweep(src0, list(range(base, base + 400, 8)))
    active_right = [s for s in right_samples if s["dragging"] and s["shown"]]
    check("往右拖：離開原位後指示線會出現",
          bool(active_right) and any(s["slot"] > 0 for s in active_right),
          str([(s["slot"], _side(s)) for s in active_right[:3]]))
    check("往右拖：游標在被拖分頁右半，指示線畫在**右**緣",
          all(_side(s) == "R" for s in active_right),
          str([(s["slot"], _side(s), s["rel"]) for s in active_right[:4]]))
    check("往右拖：跨過鄰居後指示線跟著換位置",
          len({s["slot"] for s in active_right}) > 1,
          str(sorted({s["slot"] for s in active_right})))
    check("列內排序放開後指示線收起",
          not inbar._insert_marker.isVisibleTo(inbar))

    # --- 往左拖回來：游標一路在左半 -> 一路左緣 ---
    src_now = next(b for b in inbar._buttons if b is src0)
    left_from = src_now.mapToGlobal(src_now.rect().center()).x()
    left_samples = sweep(src_now, list(range(left_from, left_from - 400, -8)))
    active_left = [s for s in left_samples if s["dragging"] and s["shown"]]
    check("往左拖：游標在被拖分頁左半，指示線畫在**左**緣",
          bool(active_left) and all(_side(s) == "L" for s in active_left),
          str([(s["slot"], _side(s), s["rel"]) for s in active_left[:4]]))

    # --- 往右拖到底再往回晃：關鍵——一過中心就翻左緣，不停在「往右移過」---
    # 這條抓的正是使用者要的：以游標當下位置判左右，而不是相對按下點。
    src_r = inbar._buttons[0]
    r_base = src_r.mapToGlobal(src_r.rect().center()).x()
    forward = list(range(r_base, r_base + 400, 8))
    backward = list(range(r_base + 400, r_base - 20, -8))
    reverse_samples = sweep(src_r, forward + backward)
    check("往右拖再往回晃：任何時刻的側邊都吻合游標落在哪一半",
          not _rule_holds(reverse_samples),
          str(_rule_holds(reverse_samples)[:4]))
    # 回晃途中一定要出現「曾經右緣、後來左緣」，才證明它真的翻過來
    active_rev = [_side(s) for s in reverse_samples if s["dragging"] and s["shown"]]
    check("往右拖再往回晃：側邊確實從右緣翻成左緣（不是一路右緣）",
          "R" in active_rev and "L" in active_rev
          and active_rev.index("L") > active_rev.index("R"),
          str(active_rev))

    # --- 在中心線附近左右晃：右半右緣、左半左緣、正中不畫 ---
    jig = inbar._buttons[1]
    jig_centre = jig.mapToGlobal(jig.rect().center())
    app.sendEvent(jig, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(jig.rect().center()),
        QPointF(jig_centre), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    jiggle = []
    for offset in (20, 28, 20, 0, -20, -28, -20):
        point = QPoint(jig_centre.x() + offset, jig_centre.y())
        app.sendEvent(jig, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(jig.mapFromGlobal(point)),
            QPointF(point), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        marker = inbar._insert_marker
        jiggle.append({
            "offset": offset,
            "slot": inbar._buttons.index(jig),
            "shown": marker.isVisibleTo(inbar),
            "line": marker.x() + marker.line_x(),
            "left": jig.x(),
            "right": jig.x() + jig.width(),
        })
    app.sendEvent(jig, QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(jig.mapFromGlobal(jig_centre)), QPointF(jig_centre),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    pump(200)
    check("前置：中心線附近晃動全程沒有換過格子（不然測到的是別的東西）",
          len({s["slot"] for s in jiggle}) == 1,
          str(sorted({s["slot"] for s in jiggle})))
    check("游標在中心右邊：畫右緣",
          all(_side(s) == "R" for s in jiggle if s["offset"] > 0),
          str([(s["offset"], _side(s)) for s in jiggle if s["offset"] > 0]))
    check("游標在中心左邊：畫左緣",
          all(_side(s) == "L" for s in jiggle if s["offset"] < 0),
          str([(s["offset"], _side(s)) for s in jiggle if s["offset"] < 0]))
    check("游標落在正中心：仍顯示一側（拖曳中不會 flicker 成空白）",
          all(s["shown"] for s in jiggle if s["offset"] == 0),
          str([(s["offset"], _side(s)) for s in jiggle if s["offset"] == 0]))

    # --- 基準是「被拖分頁中心」，不是「按下點」 -----------------------------
    # 抓分頁左邊緣按下，再把游標移到「比按下點右、但仍在分頁中心左邊」。
    # 中心基準 -> 左緣（游標在左半）；若還用按下點基準 -> 右緣。結論相反，
    # 這條專門把「不是按下點」釘住。
    off_src = inbar._buttons[1]
    grab = QPoint(8, off_src.height() // 2)          # 靠左邊緣按下
    off_start = off_src.mapToGlobal(grab)
    app.sendEvent(off_src, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(grab), QPointF(off_start),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    off_to = QPoint(off_start.x() + 24, off_start.y())
    app.sendEvent(off_src, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(off_src.mapFromGlobal(off_to)),
        QPointF(off_to), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    off_marker = inbar._insert_marker
    off_slot = inbar._buttons.index(off_src)
    off_shown = off_marker.isVisibleTo(inbar)
    off_line = off_marker.x() + off_marker.line_x()
    off_left = off_src.x()
    off_right = off_src.x() + off_src.width()
    off_centre_x = off_src.mapToGlobal(off_src.rect().center()).x()
    app.sendEvent(off_src, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(off_src.mapFromGlobal(off_to)),
        QPointF(off_to), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    pump(200)
    check("前置：這一步沒有換格子，而且游標還在分頁中心的左邊",
          off_slot == 1 and off_to.x() < off_centre_x,
          f"格子 {off_slot}，游標 {off_to.x()} vs 中心 {off_centre_x}")
    check("基準是被拖分頁中心（抓左緣往右移到中心左側 -> 仍是左緣）",
          off_shown and abs(off_line - off_left) <= 1,
          f"線 {off_line}，左緣 {off_left}，右緣 {off_right}，顯示={off_shown}")

    # 上面三段拖曳把順序打亂了，後面的拆分／合併測試預期 d0, d1, d2。
    for wanted, name in enumerate(sorted(names(viewer))):
        viewer.move_tab(names(viewer).index(name), wanted)
        pump(80)
    viewer.activate_tab(0)
    pump(150)
    check("前置：拖曳測試後順序已復原成 d0, d1, d2",
          names(viewer) == ["d0.md", "d1.md", "d2.md"], str(names(viewer)))

    # 容忍帶（列外 48px 內）仍會繼續重排，但線不能再畫在自己身上——
    # 那時游標可能正停在別的視窗的分頁列上，兩條線同時亮就分不清會插到哪。
    src3 = inbar._buttons[0]
    start3 = src3.mapToGlobal(src3.rect().center())
    app.sendEvent(src3, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(src3.rect().center()),
        QPointF(start3), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    below = QPoint(start3.x() + 30, start3.y() + config.TAB_HEIGHT)
    for point in (QPoint(start3.x() + 10, start3.y()), below):
        app.sendEvent(src3, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(src3.mapFromGlobal(point)),
            QPointF(point), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
    check("游標離開本列（仍在容忍帶內）時，自己的線收起",
          not inbar._insert_marker.isVisible())
    app.sendEvent(src3, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(src3.mapFromGlobal(below)),
        QPointF(below), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    pump(200)
    viewer.move_tab(names(viewer).index("d0.md"), 0)
    viewer.activate_tab(0)
    pump(150)
    viewer.move_tab(names(viewer).index("d0.md"), 0)
    viewer.activate_tab(0)
    pump(150)

    # --- 拆分 ---
    empty_spot = QPoint(1100, 700)   # 視窗都在左上，這裡保證是空白
    viewer._on_tab_detached(1, empty_spot)
    pump(400)
    check("拆分：多出一個視窗", len(manager.windows()) == 2, str(len(manager.windows())))
    others = [w for w in manager.windows() if w is not viewer]
    if not others:
        check("拆分失敗，後續合併測試跳過", False)
        QSettings(config.ORG_NAME, config.APP_NAME).clear()
        return
    new_window = others[0]
    check("拆分：新視窗只有拆出去的那個分頁", names(new_window) == ["d1.md"], str(names(new_window)))
    check("拆分：內容直接搬移（不重讀檔）", "內容 1" in new_window.browser.toPlainText())
    # 拆分後兩邊滑鼠移動都要存活（縮放器殘留快照的教訓）
    for w in (viewer, new_window):
        g = w.frameGeometry()
        gp = QPointF(float(g.center().x()), float(g.center().y()))
        ev = QMouseEvent(QEvent.Type.MouseMove, w.mapFromGlobal(gp.toPoint()).toPointF(),
                         gp, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        app.sendEvent(w, ev)
    check("拆分後兩邊滑鼠移動存活", True)

    # --- 合併 ---
    x, y, width, height = viewer.tab_bar.global_drop_rect()
    drop = QPoint(x + 10, y + height // 2)
    hit = manager.drop_target_at(drop, exclude=new_window)
    check("合併：命中測試找到目標視窗與插入位置",
          hit is not None and hit[0] is viewer and hit[1] == 0, str(hit))

    # 插入位置指示線：徽章說「合併」但不說插在哪，這條線補上落點。
    # 它與實際落點共用同一個 drop_target_at 結果，不會說一套做一套。
    marker = viewer.tab_bar._insert_marker
    check("平常插入指示線是隱藏的", not marker.isVisible())
    intent = new_window._drag_intent_at(drop, False)
    check("拖到目標分頁列時預告為合併", intent == "merge", intent)
    check("目標視窗顯示插入指示線", marker.isVisible())
    check("來源視窗不顯示插入指示線",
          not new_window.tab_bar._insert_marker.isVisible())
    first_button = viewer.tab_bar._buttons[0]
    check("指示線的線心對準那條縫（第一個分頁左緣）",
          abs(marker.x() + marker.line_x() - first_button.x()) <= 1,
          f"線心 {marker.x() + marker.line_x()} vs 縫 {first_button.x()}")
    check("容器比線寬，才放得下三角帽",
          marker.width() > config.TAB_INSERT_MARKER_WIDTH,
          f"{marker.width()} vs {config.TAB_INSERT_MARKER_WIDTH}")

    # 【這條抓的是「有樣式規則但畫不出來」】指示線曾經只寫在拖曳幽靈的那份
    # QSS 裡（幽靈是獨立頂層視窗，自己套一份），主視窗的子元件吃不到，
    # isVisible() 一路是 True 卻一個像素都沒畫。只看 isVisible 的檢查全綠。
    # 量的是指示線那一欄「每一列有多寬」。這樣線、上帽、下帽三者可以各自被
    # 單獨打破：少了線中間那列會歸零，少了帽子該端就縮成線寬。
    # 只掃指示線自己的 x 範圍——作用中的分頁有一條 2px 的強調色上框，
    # 掃整列的話上面那列永遠是滿的。
    def marker_row_widths(window):
        bar = window.tab_bar
        block = bar._insert_marker
        want = _styles.palette(bar._theme)["accent"].lower()
        bar.show_insert_marker(1)
        pump(150)
        image = bar._strip.grab().toImage()
        bar.hide_insert_marker()
        # grab() 回傳裝置像素，換算回邏輯寬度才能跟線寬比
        dpr = image.width() / max(1, bar._strip.width())
        left = max(0, int(block.x() * dpr))
        right = min(image.width(), int((block.x() + block.width()) * dpr))

        def width_at(logical_y):
            y = min(image.height() - 1, max(0, int(logical_y * dpr)))
            hits = sum(
                1
                for x in range(left, right)
                if image.pixelColor(x, y).name() == want
            )
            return hits / dpr

        # 帽子掃一段列取最寬，不押單一列：三角形落在整數像素網格上，
        # 每一列「純強調色」的核心寬度會隨 DPR 跳動——實機縮放下 y=4 夠寬，
        # offscreen 的 DPR=1 同一列只剩 4px，差 1px 就紅。取帶內最寬後兩種
        # 平台都穩，而帽子沒畫出來時整段仍只有線寬，照樣抓得到。
        # 上帽從 y=3 起，避開作用中分頁那條 2px 的強調色上框。
        top = max(width_at(y) for y in range(3, 8))
        bottom = max(width_at(config.TAB_HEIGHT - y) for y in range(3, 8))
        return top, width_at(config.TAB_HEIGHT // 2), bottom

    line_width = config.TAB_INSERT_MARKER_WIDTH
    top_w, mid_w, bottom_w = marker_row_widths(viewer)
    check("線真的畫得出來（不是只有 isVisible 為真）",
          abs(mid_w - line_width) <= 1,
          f"中間那列寬 {mid_w:.1f}，線寬應為 {line_width}")
    check("上面有三角帽（那一列比線寬）",
          top_w > mid_w + 1, f"上 {top_w:.1f} vs 中 {mid_w:.1f}")
    check("下面有三角帽（那一列比線寬）",
          bottom_w > mid_w + 1, f"下 {bottom_w:.1f} vs 中 {mid_w:.1f}")
    check("整塊沒有超出容器寬度",
          max(top_w, mid_w, bottom_w) <= marker.width(),
          f"{max(top_w, mid_w, bottom_w):.1f} vs {marker.width()}")
    before_theme = viewer.tab_bar._theme
    viewer.apply_theme("light" if before_theme == "dark" else "dark")
    pump(200)
    top2, mid2, bottom2 = marker_row_widths(viewer)
    check("換主題後線和兩個三角帽都改用新主題的強調色",
          abs(mid2 - line_width) <= 1 and top2 > mid2 + 1 and bottom2 > mid2 + 1,
          f"上 {top2:.1f} 中 {mid2:.1f} 下 {bottom2:.1f}")
    viewer.apply_theme(before_theme)
    pump(200)
    new_window._drag_intent_at(QPoint(1500, 950), True)
    check("游標離開目標後指示線收起", not marker.isVisible())
    # 拖曳被取消（分頁列重建、Esc 等）也不能留下殘影
    new_window._drag_intent_at(drop, False)
    new_window._clear_insert_markers()
    check("取消拖曳後所有視窗的指示線都清掉", not marker.isVisible())

    new_window._on_tab_detached(0, drop)
    pump(400)
    check("合併：分頁插到目標視窗最前面", names(viewer) == ["d1.md", "d0.md", "d2.md"],
          str(names(viewer)))
    check("合併：切換到搬來的分頁", viewer._tab.display_name == "d1.md")
    check("合併：空掉的來源視窗自動關閉", len(manager.windows()) == 1,
          str(len(manager.windows())))

    # --- 單一分頁拖到真正的空白處：不動作 ---
    viewer.move(80, 80)
    while viewer.tab_count() > 1:
        viewer.close_tab_at(viewer.tab_count() - 1)
        pump(120)
    viewer._on_tab_detached(0, empty_spot)
    pump(250)
    check("單一分頁拖到空白處不拆分（等同拖整個視窗）",
          len(manager.windows()) == 1 and viewer.tab_count() == 1)

    viewer.close()
    pump(300)

    # =====================================================================
    # 對抗式審查抓出的回歸（每一項都真的發生過）
    # =====================================================================
    def settings():
        return QSettings(config.ORG_NAME, config.APP_NAME)

    settings().clear()
    settings().setValue(config.KEY_RESTORE_TABS, True)
    settings().setValue(config.KEY_OPEN_TABS, docs[:2])
    settings().setValue(config.KEY_ACTIVE_TAB, 0)
    settings().sync()

    manager2 = WindowManager()
    main_win = manager2.create_window(None)
    pump(400)
    check("開啟還原設定時第一個視窗照常還原", main_win.tab_count() == 2)
    main_win.move(80, 80)
    main_win.open_path(docs[2], new_tab=True)
    pump(300)

    # 拆分建立的視窗曾把上次 session 的殼分頁整批復活塞進來
    main_win._on_tab_detached(2, QPoint(1200, 700))
    pump(400)
    torn = [w for w in manager2.windows() if w is not main_win][0]
    check("拆出的視窗只含被拖的分頁（不復活舊 session）",
          torn.tab_count() == 1 and torn._tabs[0].display_name == "d2.md",
          str([t.display_name for t in torn._tabs]))
    check("即使上次最大化關閉，拆出的視窗也不最大化", not torn.isMaximized())

    # 合併走最後一個分頁：空視窗的 closeEvent 曾在 _save_session 裡
    # IndexError（Qt 虛擬函式內＝行程中止），或把空清單寫進 session
    saved_before = settings().value(config.KEY_OPEN_TABS)
    x, y, _w, h = main_win.tab_bar.global_drop_rect()
    torn._on_tab_detached(0, QPoint(x + 10, y + h // 2))
    pump(500)
    check("合併走最後一個分頁：空視窗關閉不崩潰", len(manager2.windows()) == 1)
    check("空視窗關閉不清空已存的 session",
          settings().value(config.KEY_OPEN_TABS) == saved_before)

    # 閱讀位置要跟著分頁走（adopt 的重新渲染曾把捲軸打回頂端）
    long_doc = os.path.join(tmp, "long.md")
    with open(long_doc, "w", encoding="utf-8") as handle:
        handle.write("# 長文\n\n" + "\n\n".join(f"段落 {i}" for i in range(200)))
    main_win.open_path(long_doc, new_tab=True)
    pump(400)
    bar = main_win.browser.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(200)
    ratio_before = main_win._tab.scroll_ratio()
    main_win._on_tab_detached(main_win._active, QPoint(1200, 700))
    pump(600)
    moved_win = [w for w in manager2.windows() if w is not main_win][0]
    ratio_after = moved_win._tabs[0].scroll_ratio()
    check("拆分後閱讀位置保留",
          abs(ratio_after - ratio_before) < 0.1,
          f"{ratio_before:.2f} -> {ratio_after:.2f}")

    # 搬走的閱讀區曾殘留「來源視窗」的縮放事件過濾器：兩窗重疊時，
    # 游標在新視窗內會讓舊視窗跳出縮放游標、按下去縮放到舊視窗
    from PyQt6.QtWidgets import QApplication as _QApp
    geometry = main_win.frameGeometry()
    moved_win.move(geometry.right() - 200, geometry.top() + 100)
    pump(300)
    viewport = moved_win._tabs[0].browser.viewport()
    probe = QPointF(float(geometry.right() - 2), float(geometry.top() + 200))
    hover = QMouseEvent(QEvent.Type.MouseMove,
                        viewport.mapFromGlobal(probe.toPoint()).toPointF(), probe,
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    app.sendEvent(viewport, hover)
    pump(100)
    check("搬走的閱讀區不再觸發來源視窗的縮放游標",
          _QApp.overrideCursor() is None)

    for w in manager2.windows():
        w.close()
    pump(400)

    # =====================================================================
    # 實際使用回饋的回歸（2026-08-27）
    # =====================================================================
    def gesture_drag(button, points):
        """在 button 上合成 按下 -> 逐點移動 -> 在最後一點放開。"""
        start_pos = button.mapToGlobal(button.rect().center())
        app.sendEvent(button, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(button.rect().center()),
            QPointF(start_pos), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        for gp in points:
            app.sendEvent(button, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(button.mapFromGlobal(gp)),
                QPointF(gp), Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            app.processEvents()
        end = points[-1]
        app.sendEvent(button, QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(button.mapFromGlobal(end)),
            QPointF(end), Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    manager3 = WindowManager()
    solo = manager3.create_window(docs[0])
    solo.move(80, 80)
    pump(400)
    # 回報 2：單開一個 .md 沒有分頁可拖 -> 分頁列永遠顯示
    check("單開一個檔也有分頁列可拖", solo.tab_bar.isVisible()
          and len(solo.tab_bar._buttons) == 1)

    # 回報 3：兩窗相鄰（間距 20px < 容忍帶 48px）、分頁列同高，橫向拖過去
    # 放開在對方分頁列上。舊版撕下判定只看垂直、且放開點不重新判定，
    # 這個幾何下 torn 永遠是 False，放開什麼都不做。
    #
    # 建 neighbor 前再清一次工作階段：前段那些延遲關閉的視窗會在上面的
    # pump 期間把 session 寫回登錄檔，neighbor 建構時吃到還原分頁的話，
    # 這條會以「tabs=4」的樣子偽紅（實測 11 輪出現過 1 次）。
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    neighbor = manager3.create_window(docs[1])
    pump(400)
    neighbor.move(solo.frameGeometry().right() + 20, 80)
    pump(300)
    grab_btn = solo.tab_bar._buttons[0]
    grab_start = grab_btn.mapToGlobal(grab_btn.rect().center())
    nx, ny, nw, nh = neighbor.tab_bar.global_drop_rect()
    drop_end = QPoint(nx + 40, grab_start.y())
    gesture_drag(grab_btn,
                 [QPoint(x, grab_start.y())
                  for x in range(grab_start.x(), drop_end.x(), 20)] + [drop_end])
    pump(500)
    check("相鄰視窗橫向拖曳（容忍帶內）能合併",
          neighbor.tab_count() == 2 and len(manager3.windows()) == 1,
          f"tabs={neighbor.tab_count()} windows={len(manager3.windows())}")

    # 回報 1：撕下期間要有幽靈分頁跟著游標
    neighbor.activate_tab(0)
    pump(200)
    tear_btn = neighbor.tab_bar._buttons[0]
    tear_start = tear_btn.mapToGlobal(tear_btn.rect().center())
    app.sendEvent(tear_btn, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(tear_btn.rect().center()),
        QPointF(tear_start), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    ghost_seen = False
    badge_texts: set[str] = set()
    for i in range(1, 11):
        gp = QPoint(tear_start.x() + i * 10, tear_start.y() + i * 30)
        app.sendEvent(tear_btn, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(tear_btn.mapFromGlobal(gp)),
            QPointF(gp), Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        ghost = neighbor.tab_bar._ghost
        if ghost is not None and ghost.isVisible():
            ghost_seen = True
            badge_texts.add(ghost._badge.text())
    far = QPoint(tear_start.x() + 100, tear_start.y() + 300)
    app.sendEvent(tear_btn, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(tear_btn.mapFromGlobal(far)),
        QPointF(far), Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    pump(500)
    from PyQt6.QtWidgets import QApplication as _QAppG
    check("撕下期間幽靈分頁跟著游標", ghost_seen)
    # 幽靈下方的徽章要預告放開的結果；拖向遠處空白的路徑上應出現「拆分」
    from app import language

    check("徽章預告下一步（拆分為新視窗）",
          language.t("tab.drag.detach") in badge_texts, str(badge_texts))
    check("放開後幽靈與覆蓋游標清乾淨",
          neighbor.tab_bar._ghost is None and _QAppG.overrideCursor() is None)
    check("拖到遠處仍能拆分", len(manager3.windows()) == 2)

    # 手滑：超出分頁列一點點（容忍帶內、游標下沒有其他視窗）不噴新視窗
    stray = [w for w in manager3.windows() if w is not neighbor][0]
    stray.move(80, 620)
    pump(200)
    neighbor.open_path(docs[2], new_tab=True)
    pump(300)
    state_before = (neighbor.tab_count(), len(manager3.windows()))
    slip_btn = neighbor.tab_bar._buttons[0]
    slip_start = slip_btn.mapToGlobal(slip_btn.rect().center())
    bar_bottom = neighbor.tab_bar.mapToGlobal(
        neighbor.tab_bar.rect().bottomLeft()).y()
    gesture_drag(slip_btn, [QPoint(slip_start.x() + 40, slip_start.y()),
                            QPoint(slip_start.x(), bar_bottom + 20)])
    pump(400)
    check("手滑超出一點點（容忍帶內、無目標）不噴出新視窗",
          (neighbor.tab_count(), len(manager3.windows())) == state_before)

    for w in manager3.windows():
        w.close()
    pump(400)

    # =====================================================================
    # 幽靈相對抓取位置跟隨（2026-08-27）
    # =====================================================================
    # 舊行為是固定偏移（游標 +12,+12），抓在分頁哪裡都一樣；改成「按下時抓的
    # 那一點永遠在游標底下」（瀏覽器行為）。副作用是幽靈從此蓋住游標，合併的
    # 命中測試不能再用 QApplication.topLevelAt——最後那組測試就是在守這件事。
    from PyQt6.QtCore import QRect

    from app import window_manager as _wm

    def press_at(button, local: QPoint) -> QPoint:
        """在 button 的區域座標 local 按下，回傳對應的全域點。

        直接 sendEvent 給 button（沿用本區塊既有手勢的作法），因此不經過
        命中測試——關閉鈕不會把事件吃掉，可以測到靠近右緣的抓取點。
        """
        gp = button.mapToGlobal(local)
        app.sendEvent(button, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(local), QPointF(gp),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        return gp

    def move_to(button, gp: QPoint) -> None:
        app.sendEvent(button, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(button.mapFromGlobal(gp)), QPointF(gp),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    def release_at(button, gp: QPoint) -> None:
        app.sendEvent(button, QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(button.mapFromGlobal(gp)),
            QPointF(gp), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    manager4 = WindowManager()
    host = manager4.create_window(docs[0])
    host.move(120, 120)
    host.open_path(docs[1], new_tab=True)
    pump(400)
    host.activate_tab(0)
    pump(200)

    bar = host.tab_bar
    btn = bar._buttons[0]
    bar_bottom = bar.mapToGlobal(bar.rect().bottomLeft()).y()

    def tear_and_measure(local: QPoint, drop_y_offset: int):
        """在 local 按下、往下拖出分頁列，回傳 (幽靈, 落點, 抓取點)。

        至少走三步：第一步才跨過 startDragDistance 進入拖曳，第二步之後
        幽靈才會被建立。全部走完再量，避免量到還沒定位的那一幀。
        """
        start = press_at(btn, local)
        end = QPoint(start.x() + 30, bar_bottom + drop_y_offset)
        for step in (1, 2, 3):
            move_to(btn, QPoint(
                start.x() + 10 * step,
                start.y() + (end.y() - start.y()) * step // 3,
            ))
        move_to(btn, end)
        return bar._ghost, end, local

    # --- 抓在左上角 ---------------------------------------------------------
    ghost, end, local = tear_and_measure(QPoint(5, 5), 200)
    check("撕下後幽靈存在且可見", ghost is not None and ghost.isVisible())
    delta_a = None
    if ghost is not None:
        top_left = ghost.frameGeometry().topLeft()
        want = end - local
        delta_a = top_left - end   # 幽靈相對游標的偏移，供第二段做差分比對
        # 容許 1px：邏輯座標經由 dpr 換算成原生像素再換回來會有捨入
        check("抓在 (5,5)：幽靈左上角＝游標−抓取點",
              abs(top_left.x() - want.x()) <= 1 and abs(top_left.y() - want.y()) <= 1,
              f"got={top_left} want={want}")
        check("游標真的落在幽靈內（相對跟隨的必然結果）",
              ghost.frameGeometry().contains(end), str(ghost.frameGeometry()))
        check("縮影左上角＝幽靈左上角（相對跟隨的前提）",
              ghost._snapshot.pos() == QPoint(0, 0), str(ghost._snapshot.pos()))
    release_at(btn, QPoint(end.x(), bar.mapToGlobal(bar.rect().center()).y()))
    pump(300)

    # --- 抓在右下角：定點偏移下這兩組會量到一樣的偏移，相對跟隨才會不同 -----
    host.activate_tab(0)
    pump(200)
    btn = bar._buttons[0]
    corner = QPoint(btn.width() - 6, btn.height() - 6)
    # 這個點目前離關閉鈕的下緣還有 2px。分頁或圖示的尺寸一改就可能壓上去，
    # 屆時真人按這裡是按到關閉鈕、拖曳根本不會開始，而測試用 sendEvent 繞過
    # 命中測試照樣綠——先用這條把「測試情境仍然真實」釘住。
    check("前置：右下角抓取點沒有壓在關閉鈕上（真人按得到）",
          not btn._close.geometry().contains(corner),
          f"corner={corner} close={btn._close.geometry()}")
    ghost, end, local = tear_and_measure(corner, 200)
    check("右下角撕下後幽靈存在且可見", ghost is not None and ghost.isVisible())
    if ghost is not None:
        top_left = ghost.frameGeometry().topLeft()
        want = end - local
        check("抓在右下角：幽靈左上角＝游標−抓取點",
              abs(top_left.x() - want.x()) <= 1 and abs(top_left.y() - want.y()) <= 1,
              f"got={top_left} want={want} local={local}")
        # 差分比對：兩段「幽靈相對游標的偏移」之差必須等於抓取點之差。
        # 定點偏移（不管偏多少）會讓兩段偏移相同而在這裡紅——這才是
        # 「抓哪裡就從哪裡拖」的可證版本。
        if delta_a is not None:
            delta_b = top_left - end
            diff = delta_a - delta_b
            expect = corner - QPoint(5, 5)
            check("兩段偏移之差＝抓取點之差（定點偏移在此必紅）",
                  abs(diff.x() - expect.x()) <= 2 and abs(diff.y() - expect.y()) <= 2,
                  f"diff={diff} expect={expect}")

        # 徽章換文案會 adjustSize；縮影原點不能跟著跑。set_intent 對相同 intent
        # 會早退不重算，所以要先短再長，否則這個前置條件會靜默失效。
        ghost.set_intent("none")
        app.processEvents()
        narrow = ghost._badge.width()
        ghost.set_intent("detach")
        app.processEvents()
        wide = ghost._badge.width()
        check("徽章文案變長後寬度確實變過（前置條件）", wide > narrow,
              f"none={narrow} detach={wide}")
        # 與變寬前「同一次拖曳、同一套捨入」的量測值比，理應完全相等；
        # 拿 end-local 比會把捨入誤差混進來，兩種容差標準並存誰也說不清
        ghost.follow(end)
        check("徽章變寬後相對位置不變",
              ghost._snapshot.pos() == QPoint(0, 0)
              and ghost.frameGeometry().topLeft() == top_left,
              f"snap={ghost._snapshot.pos()} tl={ghost.frameGeometry().topLeft()}")
    release_at(btn, QPoint(end.x(), bar.mapToGlobal(bar.rect().center()).y()))
    pump(300)

    # --- 免手勢的單元式檢查：夾值與窄縮影 ------------------------------------
    from app.tab_bar import DragGhost as _DragGhost

    # 夾值：抓取點是按下當下量的、縮影是之後才 grab 的，中間分頁寬度變過的話
    # 抓取點可能超出縮影——沒夾住的話幽靈會整個飛到游標外面去。
    probe_pt = QPoint(600, 400)
    wild = _DragGhost(btn.grab(), host._theme, QPoint(9999, 9999))
    wild.set_intent("none")
    wild.follow(probe_pt)
    check("超出縮影的抓取點被夾回矩形內（游標仍在幽靈裡）",
          wild.frameGeometry().contains(probe_pt), str(wild.frameGeometry()))
    wild.deleteLater()
    # 窄縮影：縮影比徽章窄時（TAB_MIN_WIDTH=92 < 「拆分為新視窗」徽章約 94），
    # AlignHCenter 才真的會把縮影推離 (0,0)——寬縮影下那兩條原點檢查是常綠的
    narrow_pix = btn.grab().copy(0, 0, 60, btn.height())
    narrow_ghost = _DragGhost(narrow_pix, host._theme, QPoint(0, 0))
    narrow_ghost.set_intent("detach")
    app.processEvents()
    check("縮影比徽章窄時仍貼齊左上角（AlignLeft 的真正考驗）",
          narrow_ghost._snapshot.pos() == QPoint(0, 0),
          str(narrow_ghost._snapshot.pos()))
    narrow_ghost.deleteLater()

    # --- 守門：游標壓在幽靈上時，命中測試仍要答出底下真正的視窗 -------------
    # 把另一個視窗整個塞到 host 底下，讓它的分頁列落在 host 的內容區裡。
    # 只改 follow()、不改命中測試的話，topLevelAt 會回傳幽靈 -> 堆疊判定失效
    # -> 退回幾何掃描 -> 找到那個被完全遮住的視窗，分頁就併進使用者看不見的
    # 地方。這一條就是逼出配套修正的那條。
    under = manager4.create_window(docs[2])
    host_geo = host.frameGeometry()
    under.move(host_geo.left() + 30, host_geo.top() + 240)
    pump(400)
    host.raise_()
    host.activateWindow()
    # 從背景行程（agent、排程）跑時 raise_ 沒有前景權，視窗疊不上去，
    # WindowFromPoint 會答出蓋在上面的「別的行程」的視窗——守門兩條會以
    # 機制回歸的樣子偽紅。用專案現成的 AttachThreadInput 繞過前景鎖。
    from app import win32 as _win32

    _win32.force_foreground(int(host.winId()))
    pump(400)          # Windows 的 z-order 變更不是同步反映到 WindowFromPoint

    ux, uy, uw, uh = under.tab_bar.global_drop_rect()
    probe = QPoint(ux + 40, uy + uh // 2)
    # 不用放置區矩形驗 probe——probe 就是拿同一個矩形造的，那是恆真式；
    # 改驗它落在 under 的框架內（放置區含容忍帶，可能超出框架）
    check("守門前置：探測點落在底下視窗的框架內",
          under.frameGeometry().contains(probe),
          f"probe={probe} under={under.frameGeometry()}")
    check("守門前置：探測點同時落在來源視窗的框架內",
          host.frameGeometry().contains(probe),
          f"probe={probe} host={host.frameGeometry()}")

    # 環境前置：此刻幽靈還不存在，這條紅只可能是桌面環境——探測點被別的
    # 視窗（含另一份併行測試）蓋住、或無前景權——不是機制回歸。
    # 紅的話守門三條直接略過，免得跟著紅誤導成命中測試壞了。
    # 這一組問的是原生 WindowFromPoint 的 z-order，offscreen 整組僅實機。
    if onscreen_only("守門前置＋幽靈命中測試（查原生 z-order 的那八條）"):
        env_ok = _wm.top_level_widget_at(probe) is host
        check("守門前置：host 是探測點的最上層（紅＝環境遮擋，非機制回歸）",
              env_ok, type(_wm.top_level_widget_at(probe)).__name__)
    else:
        env_ok = False
    if env_ok:
        host.activate_tab(0)
        pump(200)
        btn = bar._buttons[0]
        start = press_at(btn, QPoint(8, 8))
        for step in (1, 2, 3):
            move_to(btn, QPoint(
                start.x() + (probe.x() - start.x()) * step // 3,
                start.y() + (probe.y() - start.y()) * step // 3,
            ))
        move_to(btn, probe)
        ghost = bar._ghost
        check("守門前置：幽靈確實蓋住探測點",
              ghost is not None and ghost.isVisible()
              and ghost.frameGeometry().contains(probe),
              str(ghost.frameGeometry()) if ghost is not None else "無幽靈")
        top_now = _wm.top_level_widget_at(probe)
        check("命中測試穿過幽靈，答出的是自家視窗而不是 DragGhost",
              top_now is host or top_now is under, type(top_now).__name__)
        # 以前這裡斷言「放在自己視窗上＝拆分」。現在來源本體蓋住 under 的
        # 分頁列要看穿：命中的是 under（真實 z-order 由 win32.window_stack_at
        # 走出來，這條就是它的實機驗證）。移動事件一路上 _drag_intent_at 已經
        # 把被蓋住的 under 疊上來了，下一條驗的就是那個 raise 真的生效。
        info = manager4.drop_target_info_at(probe, exclude=host)
        check("游標落在幽靈內、來源本體蓋住 under 的分頁列：看穿命中 under",
              info is not None and info[0] is under, repr(info))
        pump(300)
        check("被蓋住的目標被疊上來：此刻探測點的最上層是 under（原生 z-order）",
              _wm.top_level_widget_at(probe) is under,
              type(_wm.top_level_widget_at(probe)).__name__)
        release_at(btn, QPoint(start.x(), bar.mapToGlobal(bar.rect().center()).y()))
        pump(400)
        check("拖曳結束（放開在自己列上）：來源疊回最上層",
              _wm.top_level_widget_at(probe) is host,
              type(_wm.top_level_widget_at(probe)).__name__)

        # 釘選置頂的來源蓋住目標：raise_ 疊不過去，改暫時置頂目標；結束還原。
        # 直接用 win32 設置頂（不走標題列的釘選鈕，host 自己的旗標維持 False）。
        _win32.set_topmost(int(host.winId()), True)
        pump(200)
        host.activate_tab(0)
        pump(200)
        btn = bar._buttons[0]
        start = press_at(btn, QPoint(8, 8))
        for step in (1, 2, 3):
            move_to(btn, QPoint(
                start.x() + (probe.x() - start.x()) * step // 3,
                start.y() + (probe.y() - start.y()) * step // 3,
            ))
        move_to(btn, probe)
        pump(300)
        check("釘選置頂的來源蓋住目標：目標被暫時置頂，探測點的最上層是 under",
              _win32.is_topmost(int(under.winId()))
              and _wm.top_level_widget_at(probe) is under,
              f"topmost={_win32.is_topmost(int(under.winId()))} "
              f"top={type(_wm.top_level_widget_at(probe)).__name__}")
        release_at(btn, QPoint(start.x(), bar.mapToGlobal(bar.rect().center()).y()))
        pump(400)
        check("拖曳結束：目標的置頂還原（本來沒釘選）、來源仍置頂",
              not _win32.is_topmost(int(under.winId()))
              and _win32.is_topmost(int(host.winId())),
              f"under={_win32.is_topmost(int(under.winId()))} "
              f"host={_win32.is_topmost(int(host.winId()))}")
        _win32.set_topmost(int(host.winId()), False)
        pump(200)
    elif not _offscreen():
        print("    [略過] 桌面環境遮住探測點，守門三條未執行")

    # --- 對照組：底下的視窗露出來時，合併照樣要成立 -------------------------
    under.move(host_geo.right() + 20, host_geo.top())
    pump(400)
    ux, uy, uw, uh = under.tab_bar.global_drop_rect()
    exposed = QPoint(ux + 40, uy + uh // 2)
    check("對照組前置：探測點已不在來源視窗框架內",
          not host.frameGeometry().contains(exposed))
    check("對照組：目標視窗露出來時仍判定為合併",
          (manager4.drop_target_at(exposed, exclude=host) or (None,))[0] is under,
          repr(manager4.drop_target_at(exposed, exclude=host)))

    # --- 座標換算健檢 -------------------------------------------------------
    # 命中測試把邏輯座標換成原生像素才能問 WindowFromPoint。換錯的話上面兩條
    # 會一起紅，這條負責指出是換算壞了、還是命中測試壞了。
    # 注意：單螢幕機器上原點是 (0,0)，「每螢幕原點守恆」與「天真全域乘 dpr」
    # 輸出相同，這條只驗得到縮放係數；多螢幕的原點項只有多螢幕機器驗得到。
    if onscreen_only("邏輯座標換算與 GetCursorPos 一致（每軸誤差 ≤2px）"):
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        from PyQt6.QtGui import QCursor as _QCursor

        _u32 = _ctypes.windll.user32
        _u32.GetCursorPos.argtypes = [_ctypes.POINTER(_wintypes.POINT)]
        _u32.GetCursorPos.restype = _wintypes.BOOL
        _pt = _wintypes.POINT()
        _u32.GetCursorPos(_ctypes.byref(_pt))
        _converted = _wm._native_point(_QCursor.pos())
        check("邏輯座標換算與 GetCursorPos 一致（每軸誤差 ≤2px）",
              _converted is not None
              and abs(_converted[0] - _pt.x) <= 2
              and abs(_converted[1] - _pt.y) <= 2,
              f"converted={_converted} native={(_pt.x, _pt.y)}")

    # --- 指示線殘留：拖去 B 畫了線、拖回自己列上放開 -------------------------
    # 兩窗並排、列同高時，滑鼠可以一步從 B 的列跳回本列，中間點永遠不會落在
    # 兩列之外——清線的機會不存在。少了「回到本列也探一次」與 drag_ended
    # 收斂，B 的 accent 線會殘留到下一次拖曳。
    under.move(host_geo.right() + 20, host_geo.top())
    pump(300)
    host.activate_tab(0)
    pump(200)
    btn = bar._buttons[0]
    ux, uy, uw, uh = under.tab_bar.global_drop_rect()
    on_b = QPoint(ux + 40, uy + uh // 2)
    start = press_at(btn, QPoint(10, 10))
    for step in (1, 2, 3):
        move_to(btn, QPoint(
            start.x() + (on_b.x() - start.x()) * step // 3,
            start.y() + (on_b.y() - start.y()) * step // 3,
        ))
    move_to(btn, on_b)
    check("前置：拖到 B 的列上時 B 顯示插入指示線",
          under.tab_bar._insert_marker.isVisible())
    back_home = bar.mapToGlobal(bar.rect().center())
    move_to(btn, back_home)     # 一步跳回本列（並排視窗的真實事件步幅）
    check("拖回本列時 B 的指示線即刻收起",
          not under.tab_bar._insert_marker.isVisible())
    release_at(btn, back_home)
    pump(300)
    check("放開後 B 的指示線沒有殘留",
          not under.tab_bar._insert_marker.isVisible())

    # --- 拖曳中換主題：幽靈不能死，手勢要活下來 -----------------------------
    host.activate_tab(0)
    pump(200)
    btn = bar._buttons[0]
    start = press_at(btn, QPoint(10, 10))
    away = QPoint(start.x() + 40, bar.mapToGlobal(bar.rect().bottomLeft()).y() + 200)
    for step in (1, 2, 3):
        move_to(btn, QPoint(
            start.x() + 10 * step,
            start.y() + (away.y() - start.y()) * step // 3,
        ))
    move_to(btn, away)
    other = "light" if host._theme == "dark" else "dark"
    original = host._theme
    host.apply_theme(other)
    app.processEvents()
    ghost = bar._ghost
    check("拖曳中換主題：幽靈仍在且可見", ghost is not None and ghost.isVisible())
    if ghost is not None:
        before = ghost.frameGeometry().topLeft()
        ghost.follow(away)
        check("拖曳中換主題：相對跟隨不受影響",
              ghost.frameGeometry().topLeft() == before)
    host.apply_theme(original)
    release_at(btn, QPoint(away.x(), bar.mapToGlobal(bar.rect().center()).y()))
    pump(300)

    # --- 拖曳中關窗：全域游標與幽靈不能殘留 ---------------------------------
    # 中鍵按在被拖的分頁上、或拖曳中 Ctrl+W，都會在放開事件之前把視窗關掉。
    # 少了 closeEvent 的 cancel_active_drag，全域拖曳游標永遠不會還原、
    # 無父件的置頂幽靈會掛在畫面上等世代 GC。
    from PyQt6.QtWidgets import QApplication as _QAppC

    def _is_alive_and_visible(widget) -> bool:
        # deleteLater 落地後 sip 包裝碰一下就 RuntimeError——那正代表已收掉
        try:
            return widget.isVisible()
        except RuntimeError:
            return False

    closer = manager4.create_window(docs[1])
    closer.move(host_geo.left(), host_geo.top() + 300)
    pump(400)
    cbtn = closer.tab_bar._buttons[0]
    cstart = press_at(cbtn, QPoint(10, 10))
    far_out = QPoint(cstart.x() + 60, cstart.y() + 260)
    for step in (1, 2, 3):
        move_to(cbtn, QPoint(
            cstart.x() + 20 * step,
            cstart.y() + (far_out.y() - cstart.y()) * step // 3,
        ))
    move_to(cbtn, far_out)
    ghost_ref = closer.tab_bar._ghost
    check("前置：關窗前拖曳已撕下（有幽靈、有覆蓋游標）",
          ghost_ref is not None and ghost_ref.isVisible()
          and _QAppC.overrideCursor() is not None)
    closer.close()          # 拖曳中直接關（等同中鍵關最後一個分頁）
    pump(400)
    check("拖曳中關窗：全域覆蓋游標已還原", _QAppC.overrideCursor() is None)
    check("拖曳中關窗：幽靈已收掉（不殘留在畫面最上層）",
          ghost_ref is None or not _is_alive_and_visible(ghost_ref))

    # --- 看穿自家視窗的本體：分頁列被蓋住也能合併 -----------------------------
    # 使用者的抱怨：視窗 1 的本體蓋住視窗 2 的分頁列，分頁就永遠併不進去。
    # 真實 z-order 只有實機問得到（上面 onscreen 那組），這裡換掉
    # manager.stack_probe 餵假堆疊，逐條驗 drop_target_info_at 的規則本身：
    # 來源本體看穿、來源放置區是重排地盤、別的程式的視窗擋、最上層就是別的
    # 程式時退回幾何掃描、問不到堆疊也退回；再驗視窗層「被蓋住就疊上來、
    # 拖曳結束把來源疊回去」。
    from app import window_manager as _wm_rules

    under.move(host_geo.left() + 30, host_geo.top() + 240)
    pump(300)
    ux, uy, uw, uh = under.tab_bar.global_drop_rect()
    covered = QPoint(ux + 40, uy + uh // 2)
    check("看穿前置：探測點在 under 的放置區、也在 host 的框架內",
          host.frameGeometry().contains(covered)
          and QRect(ux, uy, uw, uh).contains(covered),
          f"covered={covered} host={host.frameGeometry()}")
    saved_probe = manager4.stack_probe

    def _fake_stack(stack):
        return lambda _pos: stack

    try:
        manager4.stack_probe = _fake_stack([host, under])
        info = manager4.drop_target_info_at(covered, exclude=host)
        check("看穿：來源本體蓋住 under 的分頁列，命中 under 並列出蓋住它的來源",
              info is not None and info[0] is under and info[2] == (host,), repr(info))
        check("看穿：兩元組版本與三元組同一個答案",
              info is not None
              and manager4.drop_target_at(covered, exclude=host) == (info[0], info[1]),
              repr(manager4.drop_target_at(covered, exclude=host)))
        manager4.stack_probe = _fake_stack([under, host])
        info = manager4.drop_target_info_at(covered, exclude=host)
        check("看穿：under 就在最上層時命中且沒有人蓋住它",
              info is not None and info[0] is under and info[2] == (), repr(info))
        manager4.stack_probe = _fake_stack([host, _wm_rules.FOREIGN, under])
        check("看穿：來源本體底下先碰到別的程式的視窗，不合併",
              manager4.drop_target_info_at(covered, exclude=host) is None,
              repr(manager4.drop_target_info_at(covered, exclude=host)))
        geo = manager4._scan_by_geometry(covered, host)
        check("看穿前置：幾何掃描本身找得到 under", geo is not None and geo[0] is under, repr(geo))
        manager4.stack_probe = _fake_stack(None)
        check("看穿：問不到堆疊 → 退回幾何掃描",
              manager4.drop_target_at(covered, exclude=host) == geo,
              repr(manager4.drop_target_at(covered, exclude=host)))
        manager4.stack_probe = _fake_stack([])
        check("看穿：堆疊是空的 → 也退回幾何掃描",
              manager4.drop_target_at(covered, exclude=host) == geo,
              repr(manager4.drop_target_at(covered, exclude=host)))
        # 別的程式的視窗在最上層（游標壓在它身上）：點在 under 框架內代表那條
        # 分頁列被它蓋住 → 不併；放置區超出框架上緣的那一小圈（TEAR_OFF_MARGIN
        # 48 － 標題列 36 = 12px）落在桌面上，分頁列本身看得到 → 照舊合併
        manager4.stack_probe = _fake_stack([_wm_rules.FOREIGN])
        check("看穿：游標壓在別的程式的視窗上、點在 under 框架內 → 不併（分頁列被它蓋住）",
              manager4.drop_target_info_at(covered, exclude=host) is None,
              repr(manager4.drop_target_info_at(covered, exclude=host)))
        overshoot = QPoint(ux + 40, under.frameGeometry().top() - 4)
        check("看穿前置：溢出點在 under 的放置區內、框架外",
              QRect(ux, uy, uw, uh).contains(overshoot)
              and not under.frameGeometry().contains(overshoot),
              f"overshoot={overshoot} zone={(ux, uy, uw, uh)} frame={under.frameGeometry()}")
        hit = manager4.drop_target_info_at(overshoot, exclude=host)
        check("看穿：溢出框架的容忍帶落在別的程式上，分頁列看得到 → 照舊合併",
              hit is not None and hit[0] is under and hit[2] == (), repr(hit))
        # 來源自己的放置區是列內重排的地盤：即使 under 的放置區也含這個點，
        # 仍然是 None。把 under 挪到和 host 分頁列同高、水平交疊來製造這個點。
        under.move(host.x() + 60, host.y())
        pump(300)
        hx, hy, hw, hh = host.tab_bar.global_drop_rect()
        ux2, uy2, uw2, uh2 = under.tab_bar.global_drop_rect()
        both = QPoint(max(hx, ux2) + 20, hy + hh // 2)
        check("看穿前置：這個點同時落在 host 的分頁列與 under 的放置區",
              QRect(hx, hy, hw, hh).contains(both) and QRect(ux2, uy2, uw2, uh2).contains(both),
              f"both={both} host={(hx, hy, hw, hh)} under={(ux2, uy2, uw2, uh2)}")
        manager4.stack_probe = _fake_stack([host, under])
        check("看穿：點在來源自己的放置區 → None（重排的地盤，不看穿）",
              manager4.drop_target_info_at(both, exclude=host) is None,
              repr(manager4.drop_target_info_at(both, exclude=host)))
        under.move(host_geo.left() + 30, host_geo.top() + 240)
        pump(300)
        # 第三個視窗（單分頁，放置區是標題列）的本體蓋住 under 的分頁列：一樣看穿
        third = manager4.create_window(docs[1])
        third.move(host_geo.right() + 40, host_geo.top() + 300)
        pump(300)
        manager4.stack_probe = _fake_stack([third, under])
        info = manager4.drop_target_info_at(covered, exclude=host)
        check("看穿：第三個視窗的本體蓋住 under 的分頁列，也看穿並列出它",
              info is not None and info[0] is under and info[2] == (third,), repr(info))

        # 解析器：原生 HWND 清單 → 視窗／FOREIGN 的對應（正式路徑的
        # window_stack_at）。offscreen 沒有 HWND 可問，暫時換掉平台守門、座標
        # 換算與原生查詢，餵一串假的 raw 清單：自家 HWND 對回視窗、不認得的
        # 自家頂層略過、第一個別的程式收成 FOREIGN 就停。
        saved_native = (_wm_rules._is_offscreen, _wm_rules._native_point,
                        _wm_rules.win32.window_stack_at)
        try:
            _wm_rules._is_offscreen = lambda: False
            _wm_rules._native_point = lambda _pos: (1, 1)
            raw = [(int(host.winId()), True), (999999, True), (int(third.winId()), True),
                   (424242, False), (int(under.winId()), True)]
            _wm_rules.win32.window_stack_at = lambda _x, _y: list(raw)
            stack = manager4.window_stack_at(covered)
            check("解析器：自家 HWND 對回視窗、不認得的頂層略過、別的程式收成 FOREIGN 就停",
                  stack == [host, third, _wm_rules.FOREIGN], repr(stack))
            _wm_rules.win32.window_stack_at = lambda _x, _y: None
            check("解析器：原生查詢問不出來 → None（走幾何掃描）",
                  manager4.window_stack_at(covered) is None)
        finally:
            (_wm_rules._is_offscreen, _wm_rules._native_point,
             _wm_rules.win32.window_stack_at) = saved_native

        # 視窗層：被蓋住的目標要疊上來讓使用者看到；同一目標只疊一次；
        # 疊不過釘選置頂的遮擋就暫時置頂、結束時還原；拖曳結束把來源疊回去。
        # offscreen 沒有真 z-order，用實例屬性遮住 raise_ 記錄呼叫，並換掉
        # pin_over 與 win32.set_topmost 記錄置頂動作。
        raised: list[str] = []
        pins: list = []
        tops: list = []
        under.raise_ = lambda: raised.append("under")
        host.raise_ = lambda: raised.append("host")
        saved_pin = manager4.pin_over
        saved_set_topmost = _wm_rules.win32.set_topmost
        manager4.pin_over = lambda target, occluders: (pins.append((target, occluders)), False)[1]
        _wm_rules.win32.set_topmost = lambda handle, on: (tops.append((handle, on)), True)[1]
        try:
            manager4.stack_probe = _fake_stack([host, under])
            host._drag_raised = None
            intent = host._drag_intent_at(covered, True)
            check("視窗層：被蓋住的目標預告為合併，而且被疊上來",
                  intent == "merge" and raised == ["under"], f"{intent} {raised}")
            check("視窗層：疊上來時帶著蓋住它的視窗去問要不要暫時置頂",
                  pins == [(under, (host,))], repr(pins))
            host._drag_intent_at(covered, True)
            check("視窗層：游標在同一目標上滑動不重複疊",
                  raised == ["under"] and len(pins) == 1, f"{raised} {len(pins)}")
            check("視窗層：目標分頁列有指示線",
                  under.tab_bar._insert_marker.isVisible())
            host._clear_insert_markers()
            check("視窗層：拖曳結束把來源疊回去、狀態清空",
                  raised == ["under", "host"] and host._drag_raised is None,
                  f"{raised} {host._drag_raised}")
            # 暫時置頂：pin_over 說「有置頂」→ 記在案，結束時還原成目標自己的
            # 釘選狀態（under 沒釘選 → 取消置頂）
            manager4.pin_over = lambda target, occluders: True
            raised.clear()
            host._drag_intent_at(covered, True)
            check("視窗層：暫時置頂的目標記在案", host._drag_pinned == [under],
                  repr(host._drag_pinned))
            host._clear_insert_markers()
            check("視窗層：拖曳結束還原目標自己的釘選狀態（沒釘選 → 取消置頂）",
                  tops == [(int(under.winId()), False)] and host._drag_pinned == [],
                  f"{tops} {host._drag_pinned}")
            # 目標在拖曳中被關掉：還原只做身分比較、不碰已刪除的包裝，不能崩
            gone = manager4.create_window(docs[1])
            gone.move(host_geo.left() + 30, host_geo.top() + 240)
            pump(300)
            gbar = gone.title_bar
            gtl = gbar.mapToGlobal(gbar.rect().topLeft())
            gone_pt = QPoint(gtl.x() + 40, gtl.y() + gbar.height() // 2)
            manager4.stack_probe = _fake_stack([host, gone])
            host._drag_raised = None
            host._drag_intent_at(gone_pt, True)
            check("視窗層前置：疊上來並暫時置頂的是 gone",
                  host._drag_raised is gone and host._drag_pinned == [gone],
                  f"{host._drag_raised} {host._drag_pinned}")
            gone.close()
            pump(400)
            tops.clear()
            host._clear_insert_markers()
            check("視窗層：目標在拖曳中被關掉，結束時不崩、狀態清空",
                  host._drag_pinned == [] and host._drag_raised is None and tops == [],
                  f"{host._drag_pinned} {host._drag_raised} {tops}")
            manager4.pin_over = lambda target, occluders: False
            raised.clear()
            manager4.stack_probe = _fake_stack([under, host])
            host._drag_intent_at(covered, True)
            check("視窗層：目標沒被蓋住就不疊", raised == [], str(raised))
            host._clear_insert_markers()
            check("視窗層：沒疊過別人，結束時也不動來源", raised == [], str(raised))
        finally:
            manager4.pin_over = saved_pin
            _wm_rules.win32.set_topmost = saved_set_topmost
            del under.raise_
            del host.raise_
        third.close()
        pump(300)
    finally:
        manager4.stack_probe = saved_probe

    for w in manager4.windows():
        w.close()
    pump(400)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()


# ===========================================================================
# 區塊：Mermaid 圖表（子集渲染器：解析、版面、序列圖、繪製、與閱讀器的整合）
# ===========================================================================
def section_mermaid(args) -> None:
    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer, QUrl
    from PyQt6.QtGui import QFont, QTextCursor, QTextDocument
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=250):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

    from app import document, mermaid
    from app.mermaid import flowchart, sequence
    from app.mermaid import layout as flow_layout
    from app.mermaid import model
    from app.mermaid.model import FixedMeasurer, MermaidError, UnsupportedDiagram  # noqa: F401
    from app.viewer import MarkdownViewer

    def intersects(a, b):
        return not (a.right <= b.x or b.right <= a.x or a.bottom <= b.y or b.bottom <= a.y)

    # --- 流程圖：解析 --------------------------------------------------------
    flow_src = "\n".join([
        "graph LR",
        "  %% 整行註解",
        "  A[方形] --> B(圓角); B --> C([體育場])",
        "  C --> D[[子程序]] --> E[(資料庫)] --> F((圓))",
        "  F --> G{菱形} --> H{{六角}} --> I>旗標]",
        "  I --> J[/平行/] --> K[\\反向\\] --> L[/梯形\\] --> M[\\倒梯/]",
        "  A -- 文字一 --> N",
        "  A -->|文字二| O",
        "  A -. 文字三 .-> P",
        "  A == 文字四 ==> Q",
        "  A --- R",
        "  A -.- S",
        "  A === T",
        "  A --x U",
        "  A --o V",
        "  A <--> W",
        "  x1 & x2 --> y1 & y2",
        '  Z["引號 [裡面] 可以有 <br> 換行"]:::cls',
        "  classDef cls fill:#fff",
        "  style A fill:#eee",
        '  click A "https://example.com"',
        "  subgraph outer [外層]",
        "    subgraph inner",
        "      I2 --> I3",
        "    end",
        "    O1 --> I2",
        "  end",
    ])
    chart = flowchart.parse_flowchart(flow_src)
    check("流程圖：方向 LR", chart.direction == "LR", chart.direction)
    expected_shapes = dict(zip(
        "ABCDEFGHIJKLM",
        ["rect", "round", "stadium", "subroutine", "cylinder", "circle", "diamond",
         "hexagon", "asym", "lean_right", "lean_left", "trapezoid", "trapezoid_alt"],
    ))
    got_shapes = {k: chart.nodes[k].shape for k in expected_shapes if k in chart.nodes}
    check("流程圖：13 種節點形狀全部解析對", got_shapes == expected_shapes,
          str({k: v for k, v in got_shapes.items() if expected_shapes.get(k) != v}))
    check("流程圖：形狀裡的文字是標籤", chart.nodes["A"].label == "方形"
          and chart.nodes["G"].label == "菱形", f"{chart.nodes['A'].label}/{chart.nodes['G'].label}")

    def edge(src, dst):
        return next(e for e in chart.edges if e.source == src and e.target == dst)

    check("流程圖：三種標籤寫法都拿到文字",
          edge("A", "N").label == "文字一" and edge("A", "O").label == "文字二"
          and edge("A", "P").label == "文字三" and edge("A", "Q").label == "文字四",
          f"{edge('A', 'N').label}/{edge('A', 'O').label}/{edge('A', 'P').label}/{edge('A', 'Q').label}")
    check("流程圖：線型與端點（點線、粗線、無箭頭、叉、圈、雙向）",
          edge("A", "P").style == "dotted" and edge("A", "Q").style == "thick"
          and edge("A", "R").head == "none" and edge("A", "S").style == "dotted"
          and edge("A", "T").style == "thick" and edge("A", "U").head == "cross"
          and edge("A", "V").head == "circle"
          and edge("A", "W").head == "arrow" and edge("A", "W").tail == "arrow",
          str([(e.source, e.target, e.style, e.head, e.tail)
               for e in chart.edges if e.source == "A"]))
    fanout = {(e.source, e.target) for e in chart.edges if e.source in ("x1", "x2")}
    check("流程圖：& 展開成笛卡兒積",
          fanout == {("x1", "y1"), ("x1", "y2"), ("x2", "y1"), ("x2", "y2")}, str(fanout))
    chain = [(e.source, e.target) for e in chart.edges if e.source in ("C", "D", "E")]
    check("流程圖：鏈 C --> D --> E --> F 拆成三條邊",
          ("C", "D") in chain and ("D", "E") in chain and ("E", "F") in chain, str(chain))
    z = chart.nodes["Z"].label
    check("流程圖：引號標籤保留括號、<br> 變換行、:::class 被忽略",
          "引號 [裡面]" in z and "\n" in z and "換行" in z and ":::" not in z, repr(z))
    check("流程圖：style/classDef/click 被忽略而不是報錯",
          "classDef" not in chart.nodes and "style" not in chart.nodes, str(list(chart.nodes)[-4:]))
    outer = chart.subgraphs[0] if chart.subgraphs else None
    check("流程圖：子圖巢狀結構與成員",
          outer is not None and outer.id == "outer" and outer.title == "外層"
          and "O1" in outer.nodes and outer.children and outer.children[0].id == "inner"
          and set(outer.children[0].nodes) == {"I2", "I3"},
          f"{outer.id if outer else None} {outer.nodes if outer else None} "
          f"{[c.id for c in outer.children] if outer else None}")

    # --- 流程圖：錯誤與上限 --------------------------------------------------
    def error_of(fn):
        try:
            fn()
        except MermaidError as error:
            return error
        except UnsupportedDiagram as error:
            return error
        return None

    err = error_of(lambda: flowchart.parse_flowchart("graph TD\n A --> B\n ??? !!!\n B --> C"))
    check("流程圖：看不懂的行回報正確行號與片段",
          isinstance(err, MermaidError) and err.line == 3 and err.key == model.ERR_SYNTAX
          and "???" in str(err.params.get("token", "")), f"{err and err.__dict__}")
    err = error_of(lambda: flowchart.parse_flowchart("graph TD\n subgraph S\n A --> B"))
    check("流程圖：少了 end 指到 subgraph 那一行",
          isinstance(err, MermaidError) and err.key == model.ERR_UNCLOSED and err.line == 2,
          f"{err and (err.key, err.line)}")
    err = error_of(lambda: flowchart.parse_flowchart("graph TD\n A --> B\n end"))
    check("流程圖：多出來的 end 報 unexpectedEnd",
          isinstance(err, MermaidError) and err.key == model.ERR_UNEXPECTED_END,
          f"{err and (err.key, err.line)}")
    err = error_of(lambda: flowchart.parse_flowchart(
        "graph TD\n A --> B\n B --> C\n C --> D", max_nodes=3))
    check("流程圖：節點超過上限報 tooLarge 並帶上限值",
          isinstance(err, MermaidError) and err.key == model.ERR_TOO_LARGE
          and err.params.get("limit") == 3, f"{err and (err.key, err.params)}")
    err = error_of(lambda: mermaid.parse("classDiagram\n  A <|-- B"))
    check("對外介面：未支援類型拋 UnsupportedDiagram 並帶類型名",
          isinstance(err, UnsupportedDiagram) and err.kind == "classDiagram",
          f"{type(err).__name__} {getattr(err, 'kind', None)}")
    err = error_of(lambda: mermaid.parse("%% c\n\ngraph TD\nA-->B\nbad !!\n"))
    check("對外介面：前言（註解、空行）被略過後行號仍指向原始區塊的行",
          isinstance(err, MermaidError) and err.line == 5, f"{err and err.line}")
    check("對外介面：frontmatter 與 %%{init}%% 指令會被略過",
          isinstance(mermaid.parse("---\ntitle: x\n---\n%%{init: {'theme':'dark'}}%%\ngraph TD\nA-->B"),
                     model.Flowchart))

    # --- 流程圖：版面不變量 --------------------------------------------------
    engine = flow_layout.LayeredLayout()
    measurer = FixedMeasurer()

    def lay_out(src):
        parsed = flowchart.parse_flowchart(src)
        placed = engine.layout(
            parsed, flowchart.measure_nodes(parsed, measurer),
            flowchart.measure_edge_labels(parsed, measurer), model.LayoutSpacing(),
            flowchart.measure_subgraph_titles(parsed, measurer))
        return parsed, placed

    parsed_lr, placed_lr = lay_out(flow_src)
    boxes = list(placed_lr.nodes.values())
    overlaps = sum(1 for i in range(len(boxes)) for j in range(i + 1, len(boxes))
                   if intersects(boxes[i], boxes[j]))
    check("版面：節點兩兩不重疊（LR，30+ 節點）", overlaps == 0, f"重疊 {overlaps} 對")
    forward = [e for e in parsed_lr.edges if e.source != e.target]
    wrong = [(e.source, e.target) for e in forward
             if placed_lr.nodes[e.target].x < placed_lr.nodes[e.source].right - 1]
    check("版面：LR 時所有邊的 target 都在 source 右邊（真的有轉置）",
          not wrong, str(wrong[:5]))
    parsed_tb, placed_tb = lay_out(flow_src.replace("graph LR", "graph TD", 1))
    wrong_tb = [(e.source, e.target) for e in parsed_tb.edges if e.source != e.target
                if placed_tb.nodes[e.target].y < placed_tb.nodes[e.source].bottom - 1]
    check("版面：TD 時所有邊的 target 都在 source 下方", not wrong_tb, str(wrong_tb[:5]))
    outer_box = placed_lr.subgraphs.get("outer")
    inner_box = placed_lr.subgraphs.get("inner")
    check("版面：子圖外框包住所有成員，巢狀子圖框在父框內",
          outer_box is not None and inner_box is not None
          and all(intersects(outer_box, placed_lr.nodes[n]) and
                  outer_box.x <= placed_lr.nodes[n].x and placed_lr.nodes[n].right <= outer_box.right
                  for n in ("O1", "I2", "I3"))
          and outer_box.x <= inner_box.x and inner_box.right <= outer_box.right
          and outer_box.y <= inner_box.y and inner_box.bottom <= outer_box.bottom,
          f"outer={outer_box} inner={inner_box}")
    def on_box(pt, box, tol=2.0):
        return (box.x - tol <= pt[0] <= box.right + tol
                and box.y - tol <= pt[1] <= box.bottom + tol)

    bad_ends = [
        (e.source, e.target)
        for r in placed_lr.edges
        for e in [parsed_lr.edges[r.index]]
        if e.source != e.target
        and not (on_box(r.points[0], placed_lr.nodes[e.source])
                 and on_box(r.points[-1], placed_lr.nodes[e.target]))
    ]
    check("版面：邊的路徑起點貼在 source 框上、終點貼在 target 框上（含被反向的回邊）",
          not bad_ends and all(len(r.points) >= 2 for r in placed_lr.edges),
          str(bad_ends[:4]))
    _again, placed_again = lay_out(flow_src)
    check("版面：同樣的輸入兩次算出一模一樣的結果（可重現）",
          repr(placed_again) == repr(placed_lr))
    # 覆審抓到的：_exclude 掃描的層範圍拿成員清單的頭尾當上下界，但清單是
    # 「首次提到」的順序，跟層無關——掃錯範圍，非成員就被留在子圖框裡
    exc, placed_exc = lay_out("flowchart TB\nN0 --> N4\nN3 --> N0\nsubgraph S0\nN4\nN3\nend")
    n0, s0 = placed_exc.nodes["N0"], placed_exc.subgraphs["S0"]
    check("版面：非成員不會落在子圖框裡（exclude 掃對層範圍）",
          not intersects(n0, s0), f"N0={n0} S0={s0}")

    # 標題比成員寬的子圖：框與版面都要撐開，否則字被畫出圖片外
    wide_title = "A very long subgraph title far wider than the member"
    wt, placed_wt = lay_out(f"flowchart TB\nsubgraph S [{wide_title}]\nA\nend")
    need = measurer.measure(wide_title).w + 16
    check("版面：子圖標題比成員寬時框與版面跟著撐開",
          placed_wt.subgraphs["S"].w >= need
          and placed_wt.width >= placed_wt.subgraphs["S"].right,
          f"box.w={placed_wt.subgraphs['S'].w} need={need}")

    # 假節點要受預算約束：病態圖（長邊 × 幾百層）不受控會疊出七萬個、卡秒級
    big_lines = ["flowchart TB"] + [f"N{i} --> N{i+1}" for i in range(299)]
    big_lines += [f"N{i % 50} -->|l{i}| N{299 - (i % 40)}" for i in range(301)]
    _big, placed_big = lay_out("\n".join(big_lines))
    interior = sum(len(r.points) - 2 for r in placed_big.edges)
    check("版面：假節點總數受預算約束（病態圖不會卡住 UI）",
          interior <= flow_layout.DUMMY_BUDGET + 10, f"中繼點 {interior}")

    # 只有空子圖的圖要畫出佔位框，不是一張看不見的小空圖
    _e, placed_empty = lay_out("flowchart TB\nsubgraph S [Hello]\nend")
    check("版面：只有空子圖也畫得出佔位框",
          "S" in placed_empty.subgraphs and placed_empty.width > 60,
          f"{placed_empty.subgraphs} w={placed_empty.width}")

    # 巢狀太深要在解析期擋下——版面是逐層遞迴的，不擋會打穿直譯器（當機而不是標示）
    deep = "flowchart TB\n" + "subgraph s\n" * 100 + "A --> B\n" + "end\n" * 100
    err = error_of(lambda: flowchart.parse_flowchart(deep))
    check("流程圖：巢狀超過上限報 tooDeep（不是 RecursionError）",
          isinstance(err, MermaidError) and err.key == model.ERR_TOO_DEEP,
          f"{type(err).__name__} {getattr(err, 'key', None)}")
    deep_seq = "sequenceDiagram\n" + "loop x\n" * 100 + "A->>B: t\n" + "end\n" * 100
    err = error_of(lambda: sequence.parse_sequence(deep_seq))
    check("序列圖：巢狀超過上限報 tooDeep",
          isinstance(err, MermaidError) and err.key == model.ERR_TOO_DEEP,
          f"{type(err).__name__} {getattr(err, 'key', None)}")

    # 模糊測試：覆審的 parser-robustness 掃描——失敗一律是自家例外，
    # 任何別的例外（IndexError、RecursionError…）都會讓整份文件的轉換當掉
    nasty = [
        "", "graph", "graph XX", "graph TD\nA[--", 'graph TD\nA["', "graph TD\n&",
        "graph TD\nA -->", "graph TD\n--> B", "graph TD\nA -->|", "graph TD\nsubgraph",
        "graph TD\nA((", "graph TD\nA{{{}}}", "graph TD\nA --> B %% c %% d",
        "sequenceDiagram\nA->", "sequenceDiagram\n->>B: x", "sequenceDiagram\nend",
        "sequenceDiagram\nNote over : x", "sequenceDiagram\nbox\nend",
        "graph TD\n" + ";" * 3000, "---\nonly frontmatter",
        "graph TD\nA[" + "x" * 3000 + "]",
    ]
    foreign = []
    for source_text in nasty:
        try:
            mermaid.parse(source_text)
        except (MermaidError, UnsupportedDiagram):
            pass
        except Exception as caught:   # noqa: BLE001（就是要抓「別的」例外）
            foreign.append(f"{type(caught).__name__}: {source_text[:20]!r}")
    check("解析：怪輸入的失敗一律是自家例外（不會炸掉整份文件的轉換）",
          not foreign, str(foreign[:3]))

    cyc, placed_cyc = lay_out("graph TD\n A --> B --> C --> A\n C --> C")
    check("版面：有環與自環的圖也排得出來且不重疊",
          len(placed_cyc.nodes) == 3 and len(placed_cyc.edges) == 4
          and not any(intersects(a, b) for a in placed_cyc.nodes.values()
                      for b in placed_cyc.nodes.values() if a is not b),
          str(placed_cyc.nodes))

    # --- 序列圖：解析與場景 --------------------------------------------------
    seq_src = "\n".join([
        "sequenceDiagram",
        "  title 標題",
        "  autonumber",
        "  actor U as 使用者",
        "  participant S",
        "  U->>+S: 請求",
        "  S-->>-U: 回應<br>兩行",
        "  S->S: 自己",
        "  Note over U,S: 跨越",
        "  Note left of U: 左",
        "  Note right of S: 右",
        "  loop 每次",
        "    U-xS: 叉",
        "  end",
        "  alt 成功",
        "    S-)U: 開放",
        "  else 失敗",
        "    S--)U: 虛線開放",
        "  end",
        "  activate S",
        "  deactivate S",
    ])
    seq = sequence.parse_sequence(seq_src)
    check("序列圖：標題與 autonumber", seq.title == "標題" and seq.autonumber, f"{seq.title!r} {seq.autonumber}")
    check("序列圖：參與者依宣告順序，actor 有標籤與旗標",
          [p.id for p in seq.participants] == ["U", "S"] and seq.participants[0].actor
          and seq.participants[0].label == "使用者" and not seq.participants[1].actor,
          str([(p.id, p.label, p.actor) for p in seq.participants]))
    messages = [i for i in seq.items if isinstance(i, model.Message)]
    check("序列圖：+/- 啟用旗標與 <br> 換行",
          messages[0].activate and messages[1].deactivate and "\n" in messages[1].text,
          f"{messages[0].activate} {messages[1].deactivate} {messages[1].text!r}")
    check("序列圖：八種箭頭的線型與端點（實/虛、箭頭/叉/開放）",
          (messages[0].line, messages[0].head) == ("solid", "arrow")
          and (messages[1].line, messages[1].head) == ("dotted", "arrow")
          and messages[2].source == messages[2].target == "S",
          str([(m.source, m.target, m.line, m.head) for m in messages[:3]]))
    notes = [i for i in seq.items if isinstance(i, model.Note)]
    check("序列圖：三種便條位置",
          [n.position for n in notes] == ["over", "left", "right"]
          and notes[0].participants == ["U", "S"], str([(n.position, n.participants) for n in notes]))
    blocks = [i for i in seq.items if isinstance(i, model.Block)]
    check("序列圖：loop 一段、alt/else 兩段，訊息在各自的段落裡",
          [b.kind for b in blocks] == ["loop", "alt"]
          and [s.label for s in blocks[0].sections] == ["每次"]
          and [s.label for s in blocks[1].sections] == ["成功", "失敗"]
          and isinstance(blocks[0].sections[0].items[0], model.Message)
          and blocks[0].sections[0].items[0].head == "cross"
          and blocks[1].sections[0].items[0].head == "open"
          and blocks[1].sections[1].items[0].line == "dotted",
          str([(b.kind, [s.label for s in b.sections]) for b in blocks]))
    err = error_of(lambda: sequence.parse_sequence("sequenceDiagram\n A->>B: x\n 亂七八糟\n"))
    check("序列圖：看不懂的行回報行號",
          isinstance(err, MermaidError) and err.line == 3 and err.key == model.ERR_SYNTAX,
          f"{err and (err.key, err.line)}")
    err = error_of(lambda: sequence.parse_sequence("sequenceDiagram\n loop x\n A->>B: y\n"))
    check("序列圖：少了 end 指到 loop 那一行",
          isinstance(err, MermaidError) and err.key == model.ERR_UNCLOSED and err.line == 2,
          f"{err and (err.key, err.line)}")
    err = error_of(lambda: sequence.parse_sequence(
        "sequenceDiagram\n A->>B: 1\n B->>A: 2\n A->>B: 3", max_messages=2))
    check("序列圖：訊息超過上限報 tooLarge",
          isinstance(err, MermaidError) and err.key == model.ERR_TOO_LARGE,
          f"{err and err.key}")
    scene = sequence.to_scene(seq, measurer)
    headers = [p for p in scene.items if isinstance(p, model.Rect)
               and p.fill == "header_fill" and p.radius == 4]
    msg_paths = [p for p in scene.items if isinstance(p, model.Path)
                 and p.stroke == "edge" and p.line != "dashed"]
    activations = [p for p in scene.items if isinstance(p, model.Rect)
                   and p.fill == "node_fill" and abs(p.box.w - 10) < 0.01]
    frames = [p for p in scene.items if isinstance(p, model.Rect)
              and p.stroke == "frame" and p.fill == "none"]
    # actor 畫的是小人不是方框，所以框數 = 非 actor 參與者 × 上下兩排；
    # 訊息要連區塊裡的一起算
    def all_messages(items):
        for item in items:
            if isinstance(item, model.Message):
                yield item
            elif isinstance(item, model.Block):
                for section in item.sections:
                    yield from all_messages(section.items)
    total_messages = len(list(all_messages(seq.items)))
    boxed = [p for p in seq.participants if not p.actor]
    check("序列圖場景：參與者框上下各一排、訊息線與訊息數一致、有啟用框與區塊框",
          len(headers) == 2 * len(boxed) and len(msg_paths) == total_messages == 6
          and len(activations) >= 2 and len(frames) == 2 and scene.width > 0 and scene.height > 0,
          f"headers={len(headers)}/{2 * len(boxed)} paths={len(msg_paths)}/{total_messages} "
          f"act={len(activations)} frames={len(frames)}")

    # --- 繪製：對外介面 render() ----------------------------------------------
    font = QFont("Microsoft JhengHei")
    font.setPixelSize(15)
    key = mermaid.register(chart, flow_src)
    image = mermaid.render(key, "light", 900, font, 1.5)
    check("繪製：回傳非空的 QImage，標上正確的裝置像素比",
          image is not None and not image.isNull() and abs(image.devicePixelRatio() - 1.5) < 0.01,
          f"{image and image.devicePixelRatio()}")
    logical_w = image.width() / image.devicePixelRatio()
    check("繪製：寬度不超過內文欄寬（過寬的圖等比縮小）", logical_w <= 900 + 0.5, str(logical_w))
    right = max(image.pixelColor(image.width() - 1, y).alpha() for y in range(image.height()))
    bottom = max(image.pixelColor(x, image.height() - 1).alpha() for x in range(image.width()))
    check("繪製：內容沒有溢出邊界（沒有重複套用 DPR）", right == 0 and bottom == 0,
          f"右欄 alpha={right} 底列 alpha={bottom}")
    dark = mermaid.render(key, "dark", 900, font, 1.5)
    check("繪製：深淺色主題畫出來的圖不同", dark is not None and dark != image)
    check("繪製：同樣的參數第二次直接命中快取（同一個物件）",
          mermaid.render(key, "light", 900, font, 1.5) is image)
    narrow = mermaid.render(key, "light", 300, font, 1.0)
    check("繪製：欄寬變窄圖跟著縮", narrow is not None and narrow.width() <= 300 + 1, str(narrow and narrow.width()))
    big_font = QFont(font)
    big_font.setPixelSize(22)
    small_chart_key = mermaid.register(flowchart.parse_flowchart("graph TD\n A --> B"), "graph TD\n A --> B")
    small_15 = mermaid.render(small_chart_key, "light", 900, font, 1.0)
    small_22 = mermaid.render(small_chart_key, "light", 900, big_font, 1.0)
    check("繪製：字級變大圖跟著變大（快取鍵含字級）",
          small_22.width() > small_15.width() and small_22.height() > small_15.height(),
          f"{small_15.width()}x{small_15.height()} -> {small_22.width()}x{small_22.height()}")
    point_font = QFont("Microsoft JhengHei")
    point_font.setPointSize(11)
    check("繪製：點數字型也能畫（快取鍵用換算後的像素字級，不會全部撞在 -1）",
          mermaid.render(small_chart_key, "light", 900, point_font, 1.0) is not None)
    check("對外介面：查無此鍵回傳 None", mermaid.render("no-such-key", "light", 900, font, 1.0) is None)

    # --- 與閱讀器整合 ---------------------------------------------------------
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    tmp = tempfile.mkdtemp()
    doc_path = os.path.join(tmp, "mermaid.md")
    with open(doc_path, "w", encoding="utf-8") as handle:
        handle.write("# Mermaid\n\n```mermaid\n" + flow_src + "\n```\n\n段落。\n\n"
                     "```mermaid\n" + seq_src + "\n```\n\n"
                     "```mermaid\nclassDiagram\n  Animal <|-- Duck\n```\n\n"
                     "```mermaid\ngraph TD\n  A --> B\n  這行壞了 !!!\n```\n")
    # 覆審抓到的：展示用圍欄裡寫著 ```mermaid 是文字不是圖表——docs/02-Mermaid.md 自己的
    # 管線說明就是這樣寫的，被攔走整個區塊會被打散
    IMG_RE = re.compile(r'<img[^>]+src="mermaid:')
    NOTE_RE = re.compile(r'<table class="notice mermaid-note"')
    fenced = ("````markdown\n```mermaid\ngraph TD\n A --> B\n```\n````\n\n"
              "```\n```mermaid 圍欄 --(轉換期)--> 解析\n```\n\n"
              "```mermaid\ngraph TD\nX-->Y\n```\n")
    fenced_html = document.markdown_to_html(fenced, "light")
    check("整合：別的圍欄裡的 ```mermaid 是文字；圍欄外的照畫",
          len(IMG_RE.findall(fenced_html)) == 1 and not NOTE_RE.findall(fenced_html)
          and "graph TD" in fenced_html,
          f"img={len(IMG_RE.findall(fenced_html))} note={len(NOTE_RE.findall(fenced_html))}")
    with open(MERMAID_DOC, encoding="utf-8") as handle:
        mermaid_doc_html = document.markdown_to_html(handle.read(), "light")
    check("整合：docs/02-Mermaid.md 轉換後沒有任何真的 mermaid 圖或標示（管線圖是展示文字）",
          not IMG_RE.findall(mermaid_doc_html) and not NOTE_RE.findall(mermaid_doc_html),
          f"img={len(IMG_RE.findall(mermaid_doc_html))} note={len(NOTE_RE.findall(mermaid_doc_html))}")
    # 上一條曾經是空過的：docs/02 第一段以「```` ```mermaid ````」這個行內碼開頭，
    # 前處理器把它當成沒關上的四反引號圍欄，之後整份文件的 mermaid 都不畫——
    # 所以「沒有任何真的圖」永遠成立。突變（在檔尾接一張真的圖）抓到的。
    # 這兩條釘住：行首行內碼不是圍欄；docs/02 接一張真的圖要真的畫出來。
    with open(MERMAID_DOC, encoding="utf-8") as handle:
        mermaid_doc_source = handle.read()
    appended_html = document.markdown_to_html(
        mermaid_doc_source + "\n```mermaid\ngraph TD\n  A --> B\n```\n", "light")
    check("整合：docs/02-Mermaid.md 檔尾接一張真的圖會畫出來（上一條不是空過）",
          len(IMG_RE.findall(appended_html)) == 1 and not NOTE_RE.findall(appended_html),
          f"img={len(IMG_RE.findall(appended_html))} note={len(NOTE_RE.findall(appended_html))}")
    span_html = document.markdown_to_html(
        "```` ```mermaid ```` 是行內碼，不是圍欄。\n\n"
        "```mermaid\ngraph TD\n  A --> B\n```\n", "light")
    check("整合：行首的四反引號行內碼不是圍欄，後面的 mermaid 圍欄照畫",
          len(IMG_RE.findall(span_html)) == 1 and not NOTE_RE.findall(span_html)
          and "```mermaid" in span_html,
          f"img={len(IMG_RE.findall(span_html))} note={len(NOTE_RE.findall(span_html))}")

    viewer = MarkdownViewer(doc_path)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    viewer.resize(1120, 820)
    viewer.show()
    pump(600)
    viewer.flush_pending_chunks()
    pump(200)
    html = viewer._tab.build_html(viewer._theme)
    check("整合：支援的區塊變成 mermaid: 圖片，不支援與壞掉的變成標示＋原始碼",
          html.count('src="mermaid:') == 2 and html.count("mermaid-note") == 2
          and "classDiagram" in html and 'class="code"' in html,
          f"img={html.count('src=\"mermaid:')} note={html.count('mermaid-note')}")
    check("整合：圖片段落帶 imgblock（避免行高留白）",
          '<p class="imgblock"><img alt="Mermaid" src="mermaid:' in html)
    check("整合：alt 對照表也記到 mermaid 圖片",
          any(src.startswith("mermaid:") and alt == "Mermaid"
              for src, alt in document.image_alts(html).items()))
    keys = re.findall(r'src="mermaid:([0-9a-f]+)"', html)
    image_type = QTextDocument.ResourceType.ImageResource.value
    loaded = viewer.browser.loadResource(image_type, QUrl(f"mermaid:{keys[0]}"))
    expected_dpr = viewer.browser.devicePixelRatioF() or 1.0
    check("整合：loadResource 對 mermaid: 回傳畫好的圖（不是破圖佔位）",
          loaded is not None and not loaded.isNull() and loaded.height() / expected_dpr > 150
          and abs(loaded.devicePixelRatio() - expected_dpr) < 0.01,
          f"{loaded and (loaded.width(), loaded.height(), loaded.devicePixelRatio())}")
    # 【排版當下用的寬度】直接呼叫 loadResource 拿到的是「現在」的欄寬，測不到
    # setHtml 那一刻用的是什麼；要看文件自己快取的那張圖（document().resource）。
    # 每個分頁各有一個閱讀區，新分頁的閱讀區在第一次 setHtml 前還沒被 resize
    # 餵過欄寬——沒有 _render_now 裡那次預先餵入，會用 viewport 減邊距（約 1030）
    # 來畫，超出 900 的內文欄。這張圖自然寬 1800 多，一定會被縮，縮到多寬就是證據。
    doc2_path = os.path.join(tmp, "mermaid2.md")
    with open(doc2_path, "w", encoding="utf-8") as handle:
        handle.write("```mermaid" + chr(10) + flow_src + chr(10) + "```" + chr(10))
    viewer.open_path(doc2_path, new_tab=True)
    pump(600)
    viewer.flush_pending_chunks()
    pump(200)
    html2 = viewer._tab.build_html(viewer._theme)
    key2 = re.findall(r'src="mermaid:([0-9a-f]+)"', html2)[0]
    cached = viewer.browser.document().resource(image_type, QUrl(f"mermaid:{key2}"))
    cached_w = cached.width() / (cached.devicePixelRatio() or 1.0) if cached is not None else -1
    check("整合：新分頁第一次排版就用內文欄寬縮圖（欄寬在 setHtml 之前就餵入）",
          cached is not None and cached_w <= viewer._content_width + 0.5,
          f"{cached_w} vs 欄寬 {viewer._content_width}")
    viewer.close_tab_at(viewer._active)
    pump(300)
    before_theme = viewer._theme
    viewer.apply_theme("dark" if before_theme == "light" else "light")
    pump(500)
    reloaded = viewer.browser.loadResource(image_type, QUrl(f"mermaid:{keys[0]}"))
    check("整合：換主題後重畫的圖不同（配色跟主題走）", reloaded != loaded)
    viewer.apply_theme(before_theme)
    pump(400)
    small = viewer.browser.loadResource(image_type, QUrl(f"mermaid:{keys[1]}"))
    viewer.zoom_in()
    pump(500)
    bigger = viewer.browser.loadResource(image_type, QUrl(f"mermaid:{keys[1]}"))
    check("整合：字級放大後圖跟著變大", bigger.width() > small.width(),
          f"{small.width()} -> {bigger.width()}")
    viewer.zoom_reset()
    pump(400)
    viewer.resize(560, 700)
    pump(700)          # 等 _on_resize_settled（180ms）觸發重繪
    # 不能直接呼叫 loadResource——那會拿「現在」的欄寬重畫一張，永遠是對的，
    # 測不到「文件有沒有真的重新排版」。要看文件自己快取的那張
    # （實測把 has_scalable_images 的正則改成排除 mermaid: 後，直接呼叫的
    # 版本照樣全綠，看文件快取的版本會紅）。
    check("前置：mermaid 圖片算 scalable（視窗縮放要觸發重繪）",
          viewer._tab.has_scalable_images)
    narrow_img = viewer.browser.document().resource(image_type, QUrl(f"mermaid:{keys[0]}"))
    column = viewer.browser.column_width()
    narrow_w = (narrow_img.width() / (narrow_img.devicePixelRatio() or 1.0)
                if narrow_img is not None else -1)
    check("整合：視窗變窄後文件排版用的那張圖重新貼合欄寬",
          narrow_img is not None and narrow_w <= column + 0.5,
          f"{narrow_w} vs {column}")
    viewer.resize(1120, 820)
    pump(500)
    viewer.set_language_mode("en")
    pump(500)
    html_en = viewer._tab.build_html(viewer._theme)
    check("整合：切換語言後標示文字跟著換（含標示的 HTML 快取被清掉）",
          "not supported inline" in html_en and "尚未內嵌支援" not in html_en
          and "parse error" in html_en,
          html_en[html_en.find("mermaid-note"):][:120])
    viewer.set_language_mode(config.DEFAULT_LANGUAGE)
    pump(300)
    viewer.close()
    pump(300)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()

    # --- 搜尋找得到圖表裡的字（只重畫螢幕看得到的）---------------------------
    from app import styles as _styles
    from app.mermaid import search as _msearch

    # 比對語意必須和內文完全同源，否則會出現「同一個字內文找得到、圖表找不到」
    semantic_bad = []
    for probe_text, probe_needle in (
        ("The the THE theme", "the"), ("cat category concat cat.", "cat"),
        ("搜尋 搜尋列 全文搜尋 搜", "搜尋"), ("a_b b_c b", "b"),
        ("讀取設定\n資料庫\n快取命中?", "資料"), ("my_var var variable", "var"),
    ):
        for cs in (False, True):
            for ww in (False, True):
                probe_doc = QTextDocument()
                probe_doc.setPlainText(probe_text)
                probe_flags = QTextDocument.FindFlag(0)
                if cs:
                    probe_flags |= QTextDocument.FindFlag.FindCaseSensitively
                if ww:
                    probe_flags |= QTextDocument.FindFlag.FindWholeWords
                probe_cursor = QTextCursor(probe_doc)
                want = 0
                while True:
                    probe_cursor = probe_doc.find(probe_needle, probe_cursor, probe_flags)
                    if probe_cursor.isNull():
                        break
                    want += 1
                got = _msearch.matches_in(probe_text, probe_needle, cs, ww)
                if len(got) != want or any(
                    probe_text[s:e].casefold() != probe_needle.casefold() for s, e in got
                ):
                    semantic_bad.append((probe_text, probe_needle, cs, ww, want, got))
    check("圖表比對的語意與位置和內文同源（含大小寫、全字、底線、CJK）",
          not semantic_bad, str(semantic_bad[:2]))

    # 圖層：帶不帶搜尋詞畫出來的差別
    hl_src = "graph TD\n  A[讀取設定] --> B[(資料庫)]"
    hl_key = mermaid.register(mermaid.parse(hl_src), hl_src)
    plain_img = mermaid.render(hl_key, "light", 900, font, 1.0)
    match_img = mermaid.render(hl_key, "light", 900, font, 1.0,
                               mermaid.Highlight("資料庫", False, False, -1))
    cur_img = mermaid.render(hl_key, "light", 900, font, 1.0,
                             mermaid.Highlight("資料庫", False, False, 0))

    def count_colour(img, palette_key):
        want_name = _styles.palette("light")[palette_key].lower()
        return sum(1 for y in range(img.height()) for x in range(img.width())
                   if img.pixelColor(x, y).name() == want_name)

    check("高亮真的畫在圖片裡（不帶搜尋詞時一個都沒有）",
          count_colour(match_img, "find_match_bg") > 0
          and count_colour(plain_img, "find_match_bg") == 0,
          f"帶 {count_colour(match_img, 'find_match_bg')} / "
          f"不帶 {count_colour(plain_img, 'find_match_bg')}")
    check("目前所在那一筆用強調色，和一般相符不同",
          count_colour(cur_img, "find_current_bg") > 0 and cur_img != match_img)
    check("加了高亮的圖尺寸不變（推回文件不會害重排）",
          match_img.size() == plain_img.size() == cur_img.size(),
          f"{match_img.size()} {plain_img.size()}")
    check("場景快取回傳同一個物件（重畫高亮不必重算版面）",
          mermaid.scene_for(hl_key, font) is mermaid.scene_for(hl_key, font))

    # 整合：20 張圖的文件，只有一兩張看得到
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    many = os.path.join(tmp, "diagram_search.md")
    with open(many, "w", encoding="utf-8") as handle:
        for i in range(20):
            handle.write(f"## 第 {i} 節\n\n第 {i} 段內文，也有資料庫。\n\n")
            handle.write("```mermaid\ngraph TD\n"
                         + "\n".join(f"  N{k}[步驟 {i}-{k} 資料庫] --> N{k+1}"
                                     for k in range(8))
                         + "\n```\n\n")
    dsv = MarkdownViewer(many)
    dsv.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dsv.resize(1000, 760)
    dsv.show()
    pump(800)
    dsv.flush_pending_chunks()
    pump(300)
    dbar = dsv.find_bar

    # 數 render 的呼叫次數：這是「只重畫可見的」唯一問得到的問題
    render_calls = []
    real_render = mermaid.render

    def counting_render(key, theme, column_width, render_font, dpr, highlight=None):
        render_calls.append(key)
        return real_render(key, theme, column_width, render_font, dpr, highlight)

    mermaid.render = counting_render
    try:
        dsv.show_find()
        pump(300)
        dbar.input.setText("")
        app.processEvents()
        render_calls.clear()
        dbar.input.setText("資料庫")
        pump(dbar.DEBOUNCE_MS + 400)

        def visible_diagrams():
            doc = dsv.browser.document()
            layout = doc.documentLayout()
            span_top = dsv.browser.verticalScrollBar().value()
            span_bottom = span_top + dsv.browser.viewport().height()
            return [
                (pos, k) for pos, k in dbar._diagram_images()
                if layout.blockBoundingRect(doc.findBlock(pos)).bottom() >= span_top
                and layout.blockBoundingRect(doc.findBlock(pos)).top() <= span_bottom
            ]

        all_images = dbar._diagram_images()
        seen = visible_diagrams()
        per_diagram = {
            pos: len(mermaid.diagram_hits(
                k, dsv.browser.document().defaultFont(),
                dsv.browser.column_width(), "資料庫"))
            for pos, k in all_images
        }
        check("搜尋找得到圖表裡的字（每張圖都有命中）",
              len(dbar._diagram_matches) == len(all_images) == 20,
              f"相符 {len(dbar._diagram_matches)} 圖 {len(all_images)}")
        check("一張圖有幾筆命中就算幾筆（同一個位置放幾次）",
              all(dbar._matches.count(p) == n for p, n in per_diagram.items())
              and min(per_diagram.values()) > 1,
              f"每張 {sorted(set(per_diagram.values()))} 實際 "
              f"{[dbar._matches.count(p) for p in list(per_diagram)[:3]]}")
        check("圖表那幾筆併進 _matches，且仍照文件先後排序",
              all(p in dbar._matches for p in dbar._diagram_matches)
              and dbar._matches == sorted(dbar._matches)
              and len(dbar._matches) > 20,
              f"{len(dbar._matches)} 筆")
        check("狀態列的總數含圖表那幾筆",
              dbar.status.text().endswith(str(len(dbar._matches))), dbar.status.text())
        # 【效能護欄】20 張圖全部命中，但只有看得到的那一兩張該被重畫
        check("只重畫螢幕上看得到的圖（效能護欄）",
              0 < len(render_calls) <= len(seen) + 1,
              f"可見 {len(seen)} 張，render 被呼叫 {len(render_calls)} 次")

        before_calls = len(render_calls)
        dbar._sync_diagram_highlights()
        check("狀態沒變就完全不重畫（_pushed 生效）",
              len(render_calls) == before_calls,
              f"多了 {len(render_calls) - before_calls} 次")

        # 文件裡那張圖真的換成帶高亮的了
        image_type = QTextDocument.ResourceType.ImageResource.value
        lit_key = next(k for _p, k in all_images if dbar._pushed.get(k) is not None)
        lit = dsv.browser.document().resource(image_type, QUrl(f"mermaid:{lit_key}"))
        theme_match = _styles.palette(dsv._theme)["find_match_bg"].lower()
        theme_cur = _styles.palette(dsv._theme)["find_current_bg"].lower()

        def highlight_pixels(img):
            return sum(1 for y in range(0, img.height(), 2)
                       for x in range(0, img.width(), 2)
                       if img.pixelColor(x, y).name() in (theme_match, theme_cur))

        check("文件裡的那張圖真的換成帶高亮的版本", highlight_pixels(lit) > 0)

        # 圖表那一筆不能用 ExtraSelection 上色（會把整張圖刷成一片）
        lit_pos = next(p for p, k in all_images if k == lit_key)
        sel_index = dbar._matches.index(lit_pos) - dbar._window[0]
        sel = (dbar._selections[sel_index]
               if 0 <= sel_index < len(dbar._selections) else None)
        check("圖表那一筆的 ExtraSelection 沒有背景色（否則整張圖會被塗滿）",
              sel is not None
              and sel.format.background().style() == Qt.BrushStyle.NoBrush,
              str(sel.format.background().style()) if sel else "None")

        # 跳到圖表那一筆：該張要換成強調色
        render_calls.clear()
        target = dbar._matches.index(lit_pos)
        dbar._current_index = (target - 1) % len(dbar._matches)
        dbar.search(forward=True)
        pump(300)
        check("跳到圖表那一筆時該張改用強調色",
              dbar._pushed.get(lit_key) is not None
              and dbar._pushed[lit_key].current_ordinal >= 0,
              str(dbar._pushed.get(lit_key)))

        # 捲動之後補畫新進畫面的圖
        render_calls.clear()
        dsv.browser.verticalScrollBar().setValue(4000)
        pump(dbar.SCROLL_DEBOUNCE_MS + 400)
        now_visible = visible_diagrams()
        check("捲動後補畫新進畫面的圖", len(render_calls) > 0,
              f"render {len(render_calls)} 次")
        check("捲動補畫也只做可見的",
              len(render_calls) <= len(now_visible) + 1,
              f"可見 {len(now_visible)} 張，render {len(render_calls)} 次")

        # 關掉搜尋要把圖還原
        dbar.deactivate()
        pump(300)
        restored = dsv.browser.document().resource(
            image_type, QUrl(f"mermaid:{lit_key}"))
        check("關掉搜尋後圖還原成沒有高亮的版本",
              highlight_pixels(restored) == 0 and not dbar._pushed,
              f"還剩 {highlight_pixels(restored)} 個高亮像素")
    finally:
        mermaid.render = real_render
        dsv.close()
        pump(300)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()

    # --- 完整計數：序號的順序、以及捲到圖裡那一筆 ---------------------------
    # 序列圖的參與者標籤上下各畫一排，兩排都會被標色，所以兩排各算一筆；
    # 但序號必須照畫面（上、訊息、下），不能照場景清單（上、下、訊息）
    seq_hits_src = ("sequenceDiagram\n  participant U as 使用者\n"
                    "  participant DB as 資料庫\n  U->>DB: 查詢\n"
                    "  loop 每次寫入\n    U->>DB: 更新資料庫\n  end")
    seq_hits_key = mermaid.register(mermaid.parse(seq_hits_src), seq_hits_src)
    seq_hits = mermaid.diagram_hits(seq_hits_key, font, 900, "資料庫")
    seq_scene = mermaid.scene_for(seq_hits_key, font)
    check("序列圖上下兩排參與者各算一筆（照畫面數）",
          len(seq_hits) == 3, f"{len(seq_hits)} 筆")
    check("序號照畫面由上而下，不是照場景清單的順序",
          [h.y for h in seq_hits] == sorted(h.y for h in seq_hits)
          and [h.item for h in seq_hits] != sorted(h.item for h in seq_hits),
          f"y={[round(h.y) for h in seq_hits]} item={[h.item for h in seq_hits]}")
    check("每一筆的矩形都落在含有搜尋詞的那個原語上",
          all("資料庫" in seq_scene.items[h.item].text for h in seq_hits),
          str([seq_scene.items[h.item].text for h in seq_hits]))
    twice_src = "graph TD\n  A[資料庫連到資料庫] --> B[結束]"
    twice_key = mermaid.register(mermaid.parse(twice_src), twice_src)
    twice_hits = mermaid.diagram_hits(twice_key, font, 900, "資料庫")
    check("同一個標籤裡出現兩次就算兩筆，且由左而右",
          len(twice_hits) == 2
          and twice_hits[0].item == twice_hits[1].item
          and twice_hits[0].x < twice_hits[1].x,
          f"{len(twice_hits)} 筆 x={[round(h.x) for h in twice_hits]}")

    narrow_hits = mermaid.diagram_hits(seq_hits_key, font, 120, "資料庫")
    want_scale = 120 / seq_scene.width
    check("欄寬變窄時命中座標跟著等比縮小（和畫出來的同一份算法）",
          len(narrow_hits) == len(seq_hits)
          and all(abs(n.y - h.y * want_scale) < 0.01
                  and abs(n.x - h.x * want_scale) < 0.01
                  for n, h in zip(narrow_hits, seq_hits)),
          f"比例 {want_scale:.3f} y={[round(h.y, 1) for h in narrow_hits]}")

    def current_top(img):
        """圖裡強調色那一塊的頂端 y（沒有就回 None）。"""
        want_name = _styles.palette("light")["find_current_bg"].lower()
        rows = [y for y in range(img.height()) for x in range(img.width())
                if img.pixelColor(x, y).name() == want_name]
        return min(rows) if rows else None

    drawn = [
        current_top(mermaid.render(seq_hits_key, "light", 900, font, 1.0,
                                   mermaid.Highlight("資料庫", False, False, k)))
        for k in range(len(seq_hits))
    ]
    check("第 k 筆的強調色就畫在 diagram_hits 說的第 k 個位置",
          all(top is not None and abs(top - h.y) <= 3
              for top, h in zip(drawn, seq_hits)),
          f"畫在 {drawn} vs 說在 {[round(h.y, 1) for h in seq_hits]}")

    # 捲到圖裡那一筆：用一張遠比視窗高的圖
    QSettings(config.ORG_NAME, config.APP_NAME).clear()
    tall = os.path.join(tmp, "tall_diagram.md")
    with open(tall, "w", encoding="utf-8") as handle:
        handle.write("# 長圖\n\n```mermaid\ngraph TD\n")
        for k in range(20):
            word = "資料庫" if k in (0, 9, 19) else "步驟"
            handle.write(f"  N{k}[{word} {k}] --> N{k + 1}\n")
        handle.write("```\n")
    tallv = MarkdownViewer(tall)
    tallv.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    tallv.resize(1000, 700)
    tallv.show()
    pump(800)
    tallv.flush_pending_chunks()
    pump(300)
    tbar = tallv.find_bar
    tallv.show_find()
    pump(300)
    tbar.input.setText("資料庫")
    pump(tbar.DEBOUNCE_MS + 500)
    tdoc = tallv.browser.document()
    view_h = tallv.browser.viewport().height()
    tpos, tkey = tbar._diagram_images()[0]
    thits = mermaid.diagram_hits(
        tkey, tdoc.defaultFont(), tallv.browser.column_width(), "資料庫")
    check("整張圖只有一個字元，卻算得出圖裡的每一筆",
          len(thits) == 3 and tbar._matches.count(tpos) == 3
          and len(tbar._matches) == 3,
          f"圖裡 {len(thits)} 筆，_matches {tbar._matches}")
    check("前置：命中散佈得比一個視窗還開（不然測不到捲動）",
          thits[-1].y - thits[0].y > view_h,
          f"y={[round(h.y) for h in thits]} 視窗高 {view_h}")
    # 走使用者真正的路徑：在搜尋框裡按 Enter，一次前進一筆。
    # 手動指定索引測不到「連按 Enter 會不會被吃掉」。
    from PyQt6.QtTest import QTest

    tbar._current_index = tbar._matches.index(tpos)
    tbar.input.setFocus()
    ordinals, offsets = [], []
    for k in range(len(thits)):
        if k:
            QTest.keyClick(tbar.input, Qt.Key.Key_Return)
        else:
            tbar._goto_current()
        ordinals.append(tbar._hit_ordinal(tpos))
        pump(300)
        scrolled = tallv.browser.verticalScrollBar().value()
        block_top = tdoc.documentLayout().blockBoundingRect(
            tdoc.findBlock(tpos)).top()
        hit_y = block_top + thits[k].y
        offsets.append((k, round(hit_y), scrolled,
                        scrolled - 5 <= hit_y <= scrolled + view_h + 5))
    check("圖裡逐筆前進時序號依序遞增",
          ordinals == list(range(len(thits))), str(ordinals))
    check("按下一筆會捲到圖裡的那一個字（每一筆都進畫面）",
          all(row[3] for row in offsets), str(offsets))
    check("捲軸真的跟著那一筆走（不是三次都停在同一處）",
          len({row[2] for row in offsets}) == len(offsets),
          str([row[2] for row in offsets]))
    tallv.close()
    pump(300)
    QSettings(config.ORG_NAME, config.APP_NAME).clear()

    mermaid.clear_caches()


# ===========================================================================
# 區塊：安裝檔
# ===========================================================================
def section_installer(args) -> None:
    """安裝檔的動態檢查：編譯測試變體、靜默安裝到暫存目錄、驗證、靜默反安裝、驗證。

    【完全不碰真正的關聯】測試變體把登錄根改到 HKCU\\Software\\SamHoTest、
    AppId 與群組名也換掉，所以就算開發機上裝著正式版，也不會被當成升級、
    不會被覆蓋、更不會在反安裝時被連坐刪除。先前用「reg export 備份、跑完 import
    還原」的想法不成立——import 是合併不是回滾，救不回被刪掉的鍵。

    需要 Inno Setup 6 的 ISCC.exe 與已打包的 dist\\MarkdownReader-onedir（含轉交器）；
    沒有就印 [略過] 明講並記進僅實機清單，不假裝通過。
    """
    import shutil
    import subprocess
    import tempfile
    import time
    import winreg

    candidates = (
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    )
    iscc = next((p for p in candidates if os.path.isfile(p)), None)
    onedir = os.path.join(PROJECT_ROOT, "dist", "MarkdownReader-onedir")
    internal = os.path.join(onedir, "_internal")
    if os.path.isdir(internal):
        leftovers = [n for n in os.listdir(internal)
                     if n.lower().startswith(("libcrypto-", "libssl-", "_ssl.", "_hashlib."))]
        check("打包產物裡沒有 OpenSSL（libcrypto／libssl／_ssl／_hashlib）",
              not leftovers, str(leftovers))
        check("打包產物裡沒有 TLS 外掛目錄",
              not os.path.isdir(os.path.join(internal, "PyQt6", "Qt6", "plugins", "tls")))
        translations = os.path.join(internal, "PyQt6", "Qt6", "translations")
        check("打包產物的 Qt 翻譯檔只剩 qtbase_zh_TW.qm",
              os.path.isdir(translations) and os.listdir(translations) == ["qtbase_zh_TW.qm"],
              str(os.listdir(translations)) if os.path.isdir(translations) else "無目錄")
    else:
        label = "打包產物瘦身檢查（需要 dist\\MarkdownReader-onedir）"
        _SKIP.append(label)
        print(f"    [略過] {label}")
    if iscc is None or not os.path.isfile(os.path.join(onedir, "MarkdownOpen.exe")):
        label = "安裝檔動態檢查（需要 Inno Setup 6 與 dist\\MarkdownReader-onedir 含轉交器）"
        _SKIP.append(label)
        print(f"    [略過] {label}")
        return

    test_root = r"Software\SamHoTest"

    def reg_value(subkey, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def reg_key_exists(subkey):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey):
                return True
        except OSError:
            return False

    def reg_delete_tree(subkey):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
                children = []
                index = 0
                while True:
                    try:
                        children.append(winreg.EnumKey(key, index))
                        index += 1
                    except OSError:
                        break
        except FileNotFoundError:
            return
        for child in children:
            reg_delete_tree(subkey + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)

    real_key = r"Software\Classes\MarkdownReader.md\shell\open\command"
    real_before = reg_value(real_key, "")
    tmp = tempfile.mkdtemp(prefix="mdr-installer-")
    app_dir = os.path.join(tmp, "app")
    try:
        compile_args = [
            iscc, "/Q", "/DAppVersion=9.9.9",
            "/DAppId={{0E7C1F6A-2D3B-4C2B-9A1D-5E6F7A8B9C0D}",
            "/DGroupName=MarkdownReader-Test",
            "/DClassesRoot=" + test_root + r"\Classes",
            "/DAppRegRoot=" + test_root + r"\MarkdownReader",
            "/DRegisteredAppsKey=" + test_root + r"\RegisteredApplications",
            "/DUserDataDir=" + os.path.join(tmp, "userdata"),
            "/DOutputDir=" + tmp,
            "/DOutputBaseFilename=Setup-test",
            os.path.join(PROJECT_ROOT, "installer", "MarkdownReader.iss"),
        ]
        compiled = subprocess.run(compile_args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=300)
        check("安裝檔動態：測試變體可編譯（登錄根改到 SamHoTest）",
              compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-300:])
        if compiled.returncode != 0:
            return
        setup = os.path.join(tmp, "Setup-test.exe")
        installed = subprocess.run(
            [setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=" + app_dir],
            timeout=300)
        check("安裝檔動態：靜默安裝結束碼 0", installed.returncode == 0,
              str(installed.returncode))
        check("安裝檔動態：本體、轉交器、反安裝器、_internal 都裝進去",
              all(os.path.isfile(os.path.join(app_dir, name))
                  for name in ("MarkdownReader.exe", "MarkdownOpen.exe", "unins000.exe"))
              and os.path.isdir(os.path.join(app_dir, "_internal")))
        command = reg_value(test_root + r"\Classes\MarkdownReader.md\shell\open\command", "")
        check("安裝檔動態：關聯 command 指向安裝目錄的轉交器",
              command == '"' + os.path.join(app_dir, "MarkdownOpen.exe") + '" "%1"',
              str(command))
        check("安裝檔動態：六個副檔名的 OpenWithProgids 都登記了",
              all(reg_value(test_root + r"\Classes\{}\OpenWithProgids".format(ext),
                            "MarkdownReader.md") is not None
                  for ext in config.MARKDOWN_SUFFIXES))
        check("安裝檔動態：Capabilities 與 RegisteredApplications 齊全",
              reg_value(test_root + r"\MarkdownReader\Capabilities", "ApplicationName")
              == "Markdown Reader"
              and reg_value(test_root + r"\MarkdownReader\Capabilities\FileAssociations", ".md")
              == "MarkdownReader.md"
              and reg_value(test_root + r"\RegisteredApplications", "MarkdownReader")
              == test_root + r"\MarkdownReader\Capabilities")
        check("安裝檔動態：真正的關聯完全沒被動到",
              reg_value(real_key, "") == real_before, str(reg_value(real_key, "")))

        removed = subprocess.run(
            [os.path.join(app_dir, "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES",
             "/NORESTART"], timeout=300)
        check("安裝檔動態：靜默反安裝結束碼 0", removed.returncode == 0,
              str(removed.returncode))
        # 反安裝器會先把自己複製到暫存目錄再接手，原行程先退出；等它真的收完尾
        for _ in range(75):
            if (not os.path.isfile(os.path.join(app_dir, "MarkdownReader.exe"))
                    and not reg_key_exists(test_root + r"\Classes\MarkdownReader.md")):
                break
            time.sleep(0.2)
        check("安裝檔動態：反安裝後 ProgID 整棵刪除",
              not reg_key_exists(test_root + r"\Classes\MarkdownReader.md"))
        check("安裝檔動態：反安裝後副檔名鍵本身保留、空掉的 OpenWithProgids 清掉",
              reg_key_exists(test_root + r"\Classes\.md")
              and not reg_key_exists(test_root + r"\Classes\.md\OpenWithProgids"))
        check("安裝檔動態：反安裝後 Capabilities 刪、設定父鍵保留（預設不清設定）",
              not reg_key_exists(test_root + r"\MarkdownReader\Capabilities")
              and reg_key_exists(test_root + r"\MarkdownReader"))
        check("安裝檔動態：反安裝後 RegisteredApplications 的值刪掉",
              reg_value(test_root + r"\RegisteredApplications", "MarkdownReader") is None)
        check("安裝檔動態：反安裝後檔案清空",
              not os.path.isfile(os.path.join(app_dir, "MarkdownReader.exe"))
              and not os.path.isdir(os.path.join(app_dir, "_internal")))
    finally:
        reg_delete_tree(test_root)
        shutil.rmtree(tmp, ignore_errors=True)


SECTIONS = [
    ("渲染與閱讀", section_rendering),
    ("渲染快取", section_render_cache),
    ("Mermaid 圖表", section_mermaid),
    ("介面語言", section_language),
    ("分頁操作", section_tabs),
    ("分頁狀態還原", section_session),
    ("分頁拖曳（移動/拆分/合併）", section_tab_dnd),
    ("視窗與邊緣縮放", section_window),
    ("單一實例", section_single_instance),
    ("關閉穩定度", section_teardown),
    ("安裝檔", section_installer),
]


def _run_sections_in_process(selected, args) -> int:
    """在本行程內跑指定區塊（--only 與子行程模式走這裡）。"""
    saved = snapshot_settings()
    # 所有區塊統一釘住介面語言：對 UI 文字的斷言需要一個確定的語言可比對，
    # 否則在英文 Windows 上會整批紅。設定在 finally 裡還原。
    pin_language()
    try:
        for name, func in selected:
            print(f"[{name}]")
            before = len(_FAIL)
            func(args)
            failed = len(_FAIL) - before
            passed = len(_PASS)
            print(f"    {'不通過' if failed else '通過'}"
                  f"（累計 {passed} 項通過，{len(_FAIL)} 項失敗）")
    finally:
        restore_settings(saved)

    total = len(_PASS) + len(_FAIL)
    print()
    print(f"===== {len(_PASS)} / {total} 通過 =====")
    if _SKIP:
        print(f"（另有 {len(_SKIP)} 項僅實機，offscreen 模式跳過；"
              f"加 --onscreen 在真螢幕上驗）")
    if _FAIL:
        print("失敗項目：")
        for label, detail in _FAIL:
            print(f"  - {label}" + (f"  ({detail})" if detail else ""))
    return 1 if _FAIL else 0


def _running_instance() -> bool:
    """還有別的閱讀器實例活著嗎？

    為什麼要擋：測試會直接讀寫真正的 QSettings（`HKCU\\Software\\SamHo\\
    MarkdownReader`），也會佔用單一實例的具名管道。另一個實例同時在跑時，
    兩邊會互相清設定——症狀是一整片「讀出來是 None」的失敗，看起來像功能壞了，
    其實只是環境髒了。更糟的是實測會偶發 0xC0000005：乾淨的機器上單跑
    分頁拖曳區塊 30 次沒有一次崩潰，故意留一個實例在跑則 6 次崩 1 次。

    孤兒是自我延續的：區塊崩潰時它 spawn 的子行程會活下來，繼續污染後面每一輪。
    所以這道檢查放在最前面，寧可不跑也不要產出一份看不懂的失敗清單。
    """
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtNetwork import QLocalSocket

    if QCoreApplication.instance() is None:
        QCoreApplication([])
    probe = QLocalSocket()
    probe.connectToServer(config.IPC_SERVER_NAME)
    alive = probe.waitForConnected(300)
    probe.abort()
    return alive


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown 閱讀器功能回歸測試")
    parser.add_argument("--only", default="", help="只跑名稱含這個字串的區塊")
    parser.add_argument("--list", action="store_true", help="列出所有區塊後結束")
    parser.add_argument("--runs", type=int, default=12,
                        help="穩定度測試的重複次數（預設 12）")
    parser.add_argument(
        "--ignore-running-instance", action="store_true",
        help="即使偵測到別的實例在跑也照跑（結果不可信，只在確定無妨時用）",
    )
    parser.add_argument(
        "--onscreen", action="store_true",
        help="在真實螢幕上開視窗跑（預設走 offscreen 虛擬螢幕，不佔畫面；"
             "只有原生視窗系統的那幾條檢查需要這個模式）",
    )
    args = parser.parse_args()

    if not args.onscreen:
        _enter_offscreen()

    if not args.ignore_running_instance and _running_instance():
        print("偵測到另一個 Markdown 閱讀器實例正在執行。")
        print("測試會讀寫真正的 QSettings 並佔用單一實例的管道，兩邊會打架：")
        print("  * 一整片「設定讀出來是 None」的失敗")
        print("  * 偶發 0xC0000005（乾淨機器 30 次不崩，留一個實例 6 次崩 1 次）")
        print("請先關掉它再跑；崩潰留下的孤兒用這行清：")
        print('  powershell -Command "Get-CimInstance Win32_Process -Filter '
              "\"Name='python.exe' OR Name='py.exe'\" | Where-Object "
              "{ $_.CommandLine -like '*probe*' } | ForEach-Object "
              '{ Stop-Process -Id $_.ProcessId -Force }"')
        print("確定無妨的話加 --ignore-running-instance 跳過這道檢查。")
        return 2

    if args.list:
        for name, _ in SECTIONS:
            print(f"  {name}")
        return 0

    if args.only:
        selected = [(n, f) for n, f in SECTIONS if args.only in n]
        if not selected:
            print(f"沒有符合「{args.only}」的區塊")
            return 1
        return _run_sections_in_process(selected, args)

    # 【完整跑：每個區塊各開一個子行程】
    # 同一個行程連跑多個區塊時，前面區塊大量建毀視窗（WA_DeleteOnClose、
    # 監看器、延遲刪除）累積的拆除工作，會和後面區塊的建構交錯，偶發
    # 0xC0000005——實測約每四、五次完整跑出現一次，單跑任一區塊永遠正常。
    # 這種行程級的拆除競態沒辦法在同行程內「修」，隔離才是正解，
    # 副作用是每個區塊各付一次 Qt 啟動成本（約多十幾秒）。
    import re as _re

    total_pass = total_all = total_skip = 0
    failed_sections: list[str] = []
    for name, _func in SECTIONS:
        proc = subprocess.run(
            [sys.executable, "-u", os.path.abspath(__file__),
             "--only", name, "--runs", str(args.runs)]
            + (["--onscreen"] if args.onscreen else []),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        body = proc.stdout.rstrip()
        skip_note = _re.search(r"另有 (\d+) 項僅實機", body)
        if skip_note:
            total_skip += int(skip_note.group(1))
        summary = _re.search(r"===== (\d+) / (\d+) 通過", body)
        if proc.returncode == 0 and summary:
            total_pass += int(summary.group(1))
            total_all += int(summary.group(2))
            print(f"[{name}] 通過（{summary.group(1)} 項）")
        else:
            failed_sections.append(name)
            print(f"[{name}] 失敗（子行程結束碼 {proc.returncode}）")
            for line in body.splitlines():
                print(f"    {line}")
            # faulthandler 的原生崩潰疊在 stderr，不轉印就等於白裝
            stderr_tail = proc.stderr.strip().splitlines()[-25:]
            for line in stderr_tail:
                print(f"    [stderr] {line}")
            if summary:
                total_pass += int(summary.group(1))
                total_all += int(summary.group(2))

    print()
    print(f"===== {total_pass} / {total_all} 通過，"
          f"{len(failed_sections)} 個區塊失敗 =====")
    if total_skip:
        print(f"（另有 {total_skip} 項僅實機，offscreen 模式跳過；"
              f"加 --onscreen 在真螢幕上驗）")
    return 1 if failed_sections else 0


if __name__ == "__main__":
    raise SystemExit(main())
