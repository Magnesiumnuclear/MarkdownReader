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
"""

from __future__ import annotations

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
    from PyQt6.QtGui import QDesktopServices
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

    viewer.close()
    pump(300)
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

    check("單一分頁時不顯示分頁列", not viewer.tab_bar.isVisible())
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
    check("關到只剩一個分頁時分頁列隱藏", not viewer.tab_bar.isVisible())

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
            window = _u32.FindWindowW(None, "sample.md — Markdown 閱讀器")
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
            got_tab = bool(_u32.FindWindowW(None, "README.md — Markdown 閱讀器"))
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
SECTIONS = [
    ("渲染與閱讀", section_rendering),
    ("分頁操作", section_tabs),
    ("分頁狀態還原", section_session),
    ("視窗與邊緣縮放", section_window),
    ("單一實例", section_single_instance),
    ("關閉穩定度", section_teardown),
]


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

    selected = [(n, f) for n, f in SECTIONS if args.only in n]
    if not selected:
        print(f"沒有符合「{args.only}」的區塊")
        return 1

    saved = snapshot_settings()
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


if __name__ == "__main__":
    raise SystemExit(main())
