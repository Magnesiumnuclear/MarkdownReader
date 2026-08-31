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
    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer
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
    from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer
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
    check("指示線畫在插入處（第一個分頁左緣）",
          abs(marker.x() - max(0, first_button.x() - marker.width() // 2)) <= 1,
          f"{marker.x()} vs {first_button.x()}")
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


SECTIONS = [
    ("渲染與閱讀", section_rendering),
    ("渲染快取", section_render_cache),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown 閱讀器功能回歸測試")
    parser.add_argument("--only", default="", help="只跑名稱含這個字串的區塊")
    parser.add_argument("--list", action="store_true", help="列出所有區塊後結束")
    parser.add_argument("--runs", type=int, default=12,
                        help="穩定度測試的重複次數（預設 12）")
    args = parser.parse_args()

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
