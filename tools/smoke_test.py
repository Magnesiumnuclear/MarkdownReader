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

_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        _PASS.append(label)
    else:
        _FAIL.append((label, detail))
        print(f"    [FAIL] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


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
    big_doc = os.path.join(PROJECT_ROOT, "tools", "CHANGELOG.md")
    if os.path.isfile(big_doc):
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
    big = os.path.join(PROJECT_ROOT, "tools", "CHANGELOG.md")
    target = big if os.path.isfile(big) else README
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
    check("換主題不會命中舊快取",
          tab.build_html(other_theme) is not first_html)
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

    from PyQt6.QtCore import QEventLoop, QRect, QSettings, Qt, QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def pump(ms=250):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        for _ in range(3):
            app.processEvents()

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
    # C++ 轉交器（src_cpp/md_open/main.cpp）用寫死的管道名稱和本體講話。
    # 兩邊改到不同步的話不會有任何錯誤訊息：轉交器每次都「找不到管道」，
    # 安靜退化成每個檔案開一個視窗。這裡直接比對原始碼，改壞立刻紅燈。
    launcher_src = os.path.join(PROJECT_ROOT, "src_cpp", "md_open", "main.cpp")
    if os.path.isfile(launcher_src):
        with open(launcher_src, encoding="utf-8") as handle:
            cpp = handle.read()
        pipe_line = next(
            (line for line in cpp.splitlines()
             if "kPipePath" in line and 'L"' in line),
            "",
        )
        check("C++ 轉交器的管道名稱與 config.IPC_SERVER_NAME 一致",
              config.IPC_SERVER_NAME in pipe_line,
              pipe_line.strip() or "(找不到 kPipePath)")

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
        while _t.perf_counter() < deadline:
            # 用和程式同一個翻譯來源組標題，而不是寫死字串。
            # 【這同時是「標題後綴陷阱」的回歸測試】Qt 在 Windows 上若發現
            # windowTitle 沒有以 applicationDisplayName 結尾，會自動補一段
            # " - <displayName>"。main.py 與 viewer._update_titles 的來源一旦
            # 分家，實際標題就會多出後綴，這裡的精確比對立刻紅燈。
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
            got_tab = bool(_u32.FindWindowW(None, _expected_title("README.md")))
            check("本體忙碌時三連發轉交不會讓它崩潰", alive,
                  "" if alive else f"結束碼 {app_proc.poll() & 0xFFFFFFFF:#x}")
            check("忙碌解除後轉交的檔案有開出來", got_tab)
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

    from PyQt6.QtCore import QEvent, QEventLoop, QPoint, QPointF, QSettings, Qt, QTimer
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

    # 列內排序只有一條規則：游標在按下點右邊就畫右緣、左邊就畫左緣，
    # 不分有沒有離開原位。基準用按下點而不是分頁中心——抓取點可能落在分頁任何
    # 位置，用中心比的話，同樣往右拖一點點，抓左半邊和抓右半邊會得到相反的結果。
    inbar = viewer.tab_bar

    def drag_along(from_index, to_index):
        """把第 from_index 個分頁拖到第 to_index 個分頁，沿路取樣。

        【手勢進行中絕對不要跑巢狀事件迴圈】這裡曾經每一步都 pump(30) 來等
        版面更新，結果是後面的測試隨機以 0xC0000005 崩潰在
        FramelessResizer._drag_controls——巢狀迴圈會把排隊中的 deleteLater
        沖出來，在拖曳中途銷毀元件，留下懸空指標。改成不 pump：
        _move_button 現在會自己 activate() 版面，buttons[i].x() 當場就是對的。
        """
        src = inbar._buttons[from_index]
        origin_x = src.mapToGlobal(src.rect().center())
        app.sendEvent(src, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(src.rect().center()),
            QPointF(origin_x), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        target_btn = inbar._buttons[to_index]
        if to_index > from_index:
            # 往右要拖到目標格的**右緣**：被拖的分頁一往右走，其他分頁就同步
            # 往左遞補，它們的中心跟著左移，只拖到中心點會少跨一格。
            goal = target_btn.mapToGlobal(target_btn.rect().topRight()).x()
        else:
            goal = target_btn.mapToGlobal(target_btn.rect().center()).x()
        stride = 10 if goal > origin_x.x() else -10
        samples = []
        for step_x in range(origin_x.x(), goal, stride):
            app.sendEvent(src, QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(src.mapFromGlobal(QPoint(step_x, origin_x.y()))),
                QPointF(step_x, origin_x.y()),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
            marker = inbar._insert_marker
            samples.append({
                "slot": inbar._buttons.index(src),
                # 前幾步可能還沒超過 startDragDistance，手勢根本還沒開始，
                # 那時當然什麼都不該畫。用 _drag_press_x 有沒有值來分辨，
                # 而不是「跳過第一筆」那種看心情的規則。
                "dragging": inbar._drag_press_x is not None,
                "shown": marker.isVisibleTo(inbar),
                "line": marker.x() + marker.line_x(),
                "left": src.x(),
                "right": src.x() + src.width(),
            })
        app.sendEvent(src, QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(src.mapFromGlobal(QPoint(goal, origin_x.y()))),
            QPointF(goal, origin_x.y()),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        pump(200)
        return samples

    # --- 往右拖：靠右 ---
    right_samples = drag_along(0, 2)
    moved_right = [s for s in right_samples if s["slot"] > 0 and s["dragging"]]
    at_origin_right = [
        s for s in right_samples if s["slot"] == 0 and s["dragging"]]
    check("往右拖：離開原位後指示線會出現",
          bool(moved_right) and all(s["shown"] for s in moved_right),
          str(moved_right[:2]))
    check("往右拖：游標在按下點右邊，指示線畫在**右**緣",
          all(abs(s["line"] - s["right"]) <= 1 for s in moved_right),
          str([(s["slot"], s["line"], s["right"]) for s in moved_right[:3]]))
    # 往右拖時，游標一路都在按下點右邊，所以連還沒換格子的那幾步也該畫右緣
    check("往右拖：還沒換格子時就已經畫在右緣（同一條規則）",
          bool(at_origin_right)
          and all(s["shown"] and abs(s["line"] - s["right"]) <= 1
                  for s in at_origin_right),
          str([(s["slot"], s["shown"], s["line"], s["right"])
               for s in at_origin_right[:3]]))
    check("往右拖：跨過鄰居後指示線跟著換位置",
          len({s["slot"] for s in moved_right}) > 1,
          str(sorted({s["slot"] for s in moved_right})))
    check("列內排序放開後指示線收起",
          not inbar._insert_marker.isVisibleTo(inbar))

    # --- 往左拖：靠左 ---
    # 上一段把第 0 格拖到第 2 格，所以現在從第 2 格往回拖
    back_index = 2
    left_samples = drag_along(back_index, 0)
    moved_left = [
        s for s in left_samples if s["slot"] < back_index and s["dragging"]]
    at_origin_left = [
        s for s in left_samples if s["slot"] == back_index and s["dragging"]]
    check("往左拖：離開原位後指示線會出現",
          bool(moved_left) and all(s["shown"] for s in moved_left),
          str(moved_left[:2]))
    check("往左拖：游標在按下點左邊，指示線畫在**左**緣",
          all(abs(s["line"] - s["left"]) <= 1 for s in moved_left),
          str([(s["slot"], s["line"], s["left"]) for s in moved_left[:3]]))
    check("往左拖：還沒換格子時就已經畫在左緣（同一條規則）",
          bool(at_origin_left)
          and all(s["shown"] and abs(s["line"] - s["left"]) <= 1
                  for s in at_origin_left),
          str([(s["slot"], s["shown"], s["line"], s["left"])
               for s in at_origin_left[:3]]))

    # --- 在原位左右晃動：指示線要跟著滑鼠換邊 ---
    # 這一段完全不跨過鄰居（位移都小於半個分頁寬），所以格子從頭到尾不變，
    # 唯一的依據就是游標在按下點的哪一邊。
    jiggle_src = inbar._buttons[1]
    jiggle_start = jiggle_src.mapToGlobal(jiggle_src.rect().center())
    app.sendEvent(jiggle_src, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(jiggle_src.rect().center()),
        QPointF(jiggle_start), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    jiggle = []
    # 第一步要夠大才會超過拖曳起始距離，手勢真正開始
    for offset in (20, 28, 20, 0, -20, -28, -20):
        point = QPoint(jiggle_start.x() + offset, jiggle_start.y())
        app.sendEvent(jiggle_src, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(jiggle_src.mapFromGlobal(point)),
            QPointF(point), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        marker = inbar._insert_marker
        jiggle.append({
            "offset": offset,
            "slot": inbar._buttons.index(jiggle_src),
            "shown": marker.isVisibleTo(inbar),
            "line": marker.x() + marker.line_x(),
            "left": jiggle_src.x(),
            "right": jiggle_src.x() + jiggle_src.width(),
        })
    app.sendEvent(jiggle_src, QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(jiggle_src.mapFromGlobal(jiggle_start)), QPointF(jiggle_start),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    pump(200)

    check("前置：原位晃動全程沒有換過格子（不然測到的是另一條規則）",
          len({s["slot"] for s in jiggle}) == 1,
          str(sorted({s["slot"] for s in jiggle})))
    check("原位往右晃：畫在右緣",
          all(s["shown"] and abs(s["line"] - s["right"]) <= 1
              for s in jiggle if s["offset"] > 0),
          str([(s["offset"], s["shown"], s["line"], s["right"])
               for s in jiggle if s["offset"] > 0]))
    check("原位往左晃：畫在左緣",
          all(s["shown"] and abs(s["line"] - s["left"]) <= 1
              for s in jiggle if s["offset"] < 0),
          str([(s["offset"], s["shown"], s["line"], s["left"])
               for s in jiggle if s["offset"] < 0]))
    check("游標正好回到按下點時不畫（沒有左右可言）",
          all(not s["shown"] for s in jiggle if s["offset"] == 0),
          str([s for s in jiggle if s["offset"] == 0]))

    # --- 基準是「按下點」，不是「分頁中心」 ---------------------------------
    # 上面每一段都抓分頁正中央，那時按下點正好等於中心，兩種基準給的答案一樣，
    # 分不出來。這一段刻意抓左邊緣附近，再把游標移到「比按下點右、但仍在中心
    # 左邊」的位置：按下點基準 -> 右緣，分頁中心基準 -> 左緣，結論相反。
    off_src = inbar._buttons[1]
    grab = QPoint(8, off_src.height() // 2)          # 靠左邊緣按下
    off_start = off_src.mapToGlobal(grab)
    app.sendEvent(off_src, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(grab), QPointF(off_start),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    # 移到按下點右邊 24px：仍在這一格內（沒跨過鄰居中心），也仍在分頁中心左邊
    off_to = QPoint(off_start.x() + 24, off_start.y())
    app.sendEvent(off_src, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(off_src.mapFromGlobal(off_to)),
        QPointF(off_to), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    # 所有數值都要在**放開之前**取樣：放開會把指示線收起來，
    # 拖到 check() 才讀 isVisibleTo 一定是 False（這裡踩過一次）。
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
    check("基準是按下點而不是分頁中心（抓左緣往右移一點 -> 右緣）",
          off_shown and abs(off_line - off_right) <= 1,
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

        # 上帽避開分頁那條 2px 的上框（量 y=4），下帽量倒數第 3 列
        return width_at(4), width_at(config.TAB_HEIGHT // 2), \
            width_at(config.TAB_HEIGHT - 3)

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
    env_ok = _wm.top_level_widget_at(probe) is host
    check("守門前置：host 是探測點的最上層（紅＝環境遮擋，非機制回歸）",
          env_ok, type(_wm.top_level_widget_at(probe)).__name__)
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
        check("命中測試穿過幽靈，答出的是來源視窗而不是 DragGhost",
              _wm.top_level_widget_at(probe) is host,
              type(_wm.top_level_widget_at(probe)).__name__)
        check("游標落在幽靈內時，放在自己視窗上仍判定為拆分（不被幽靈騙）",
              manager4.drop_target_at(probe, exclude=host) is None,
              repr(manager4.drop_target_at(probe, exclude=host)))
        release_at(btn, QPoint(start.x(), bar.mapToGlobal(bar.rect().center()).y()))
        pump(300)
    else:
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
          and abs(_converted[0] - _pt.x) <= 2 and abs(_converted[1] - _pt.y) <= 2,
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
    # 覆審抓到的：展示用圍欄裡寫著 ```mermaid 是文字不是圖表——README 自己的
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
    with open(README, encoding="utf-8") as handle:
        readme_html = document.markdown_to_html(handle.read(), "light")
    check("整合：README 轉換後沒有任何真的 mermaid 圖或標示（管線圖是展示文字）",
          not IMG_RE.findall(readme_html) and not NOTE_RE.findall(readme_html),
          f"img={len(IMG_RE.findall(readme_html))} note={len(NOTE_RE.findall(readme_html))}")

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
    args = parser.parse_args()

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

    total_pass = total_all = 0
    failed_sections: list[str] = []
    for name, _func in SECTIONS:
        proc = subprocess.run(
            [sys.executable, "-u", os.path.abspath(__file__),
             "--only", name, "--runs", str(args.runs)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        body = proc.stdout.rstrip()
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
    return 1 if failed_sections else 0


if __name__ == "__main__":
    raise SystemExit(main())
