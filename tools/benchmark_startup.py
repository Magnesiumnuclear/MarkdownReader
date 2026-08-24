"""啟動速度基準測試。

用途：任何會影響啟動路徑的改動（新增元件、調整渲染流程、換打包方式）之後跑一次，
和記錄下來的基準線比對，確認沒有把啟動變慢。

    py -3.13 tools/benchmark_startup.py                    # 量測並和基準線比對
    py -3.13 tools/benchmark_startup.py --runs 12          # 增加取樣次數（雜訊大時）
    py -3.13 tools/benchmark_startup.py --target source    # 只量原始碼各階段
    py -3.13 tools/benchmark_startup.py --update-baseline  # 把目前結果存成新基準線
    py -3.13 tools/benchmark_startup.py --json             # 機器可讀輸出

量測項目分兩層：
  1. 打包後的 exe —— 從行程啟動到視窗實際出現，這是使用者真正感受到的時間。
     單一 exe 與資料夾版都會量（存在哪個就量哪個）。
  2. 原始碼各階段 —— 匯入、建立 QApplication、建構主視窗、show 到首幀。
     exe 變慢時靠這一層判斷是程式碼問題還是打包問題。

【為什麼用最小值比對】
同一支執行檔連續啟動的時間本來就會跳動，實測單次差距可達 ±150ms（背景程式、
磁碟快取、CPU 排程都有影響）。關鍵在於：這些干擾只會讓時間「變長」，不會讓
程式跑得比實際更快，所以最小值最接近「沒有被干擾時的真實成本」。

實測連續三輪的穩定度（source_total）：
    中位數  674 / 712 / 686 ms   -> 擺盪約 ±5%
    最小值  585 / 596 / 596 ms   -> 擺盪約 ±2%

因此判定一律以最小值為準，中位數只列出來當參考。
另外第一次啟動當暖機不列入統計，預設容忍度 15%（比最小值本身的擺盪大一些，
才不會把雜訊誤判成退步）。

這套判定驗證過確實抓得到問題：在主視窗建構子注入 120ms 延遲後，
source_ctor 的最小值由 331ms 變成 422ms（+28%）、source_total +18%，
兩項都被標為退步，結束碼 1。

（同樣的注入若改用「中位數 + 20% 容忍度」判定則會漏抓——這也是改用最小值的原因。）

【基準線與機器相關】
startup_baseline.json 記錄的是「在某台機器上」的數字，換機器一定會不同。
換到新機器時先跑一次 --update-baseline 重新建立基準。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(PROJECT_ROOT, "tools", "startup_baseline.json")
SAMPLE_DOC = os.path.join(PROJECT_ROOT, "sample.md")

ONEFILE_EXE = os.path.join(PROJECT_ROOT, "dist", "MarkdownReader.exe")
ONEDIR_EXE = os.path.join(
    PROJECT_ROOT, "dist", "MarkdownReader-onedir", "MarkdownReader.exe"
)

# 絕對上限。就算沒有基準線，超過這個值就代表明顯出事了。
ABSOLUTE_BUDGET_MS = {
    "onefile_window": 3000,
    "onedir_window": 1800,
    "source_total": 2000,
}

DEFAULT_TOLERANCE = 0.15
LAUNCH_TIMEOUT_S = 30

# 量測時固定視窗大小與各項設定，避免上次留下的幾何或字級影響版面計算時間
BENCHMARK_SETTINGS = {
    config.KEY_THEME_MODE: "light",
    config.KEY_FONT_SIZE: config.BASE_FONT_POINT_SIZE,
    config.KEY_LINE_HEIGHT: config.DEFAULT_LINE_HEIGHT,
    config.KEY_CONTENT_WIDTH: config.DEFAULT_CONTENT_WIDTH,
    config.KEY_ALWAYS_ON_TOP: False,
    config.KEY_STATUS_VISIBLE: True,
    config.KEY_AUTO_RELOAD: True,
    config.KEY_CONFIRM_LINKS: False,
    config.KEY_MAXIMIZED: False,
}


# --- 視窗偵測（Win32） ------------------------------------------------------
if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def find_window(title_fragment: str) -> int | None:
    """找出標題含指定字串、且目前可見的最上層視窗。

    不靠行程 ID 比對，是因為 PyInstaller 的單一 exe 會再開一個子行程，
    視窗屬於子行程而不是我們啟動的那一個。量測前已把同名行程全部關掉，
    所以用標題比對就足夠可靠。
    """
    if sys.platform != "win32":
        return None

    found: list[int] = []

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        if title_fragment in buffer.value:
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found[0] if found else None


def kill_all(image_name: str) -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", image_name],
        capture_output=True,
        check=False,
    )


# --- QSettings 前置與還原 ---------------------------------------------------
def _settings_handle():
    from PyQt6.QtCore import QCoreApplication, QSettings

    if QCoreApplication.instance() is None:
        QCoreApplication([])  # QSettings 需要一個 application 物件
    return QSettings(config.ORG_NAME, config.APP_NAME)


def snapshot_settings() -> dict:
    settings = _settings_handle()
    return {key: settings.value(key) for key in settings.allKeys()}


def apply_benchmark_settings() -> None:
    settings = _settings_handle()
    for key, value in BENCHMARK_SETTINGS.items():
        settings.setValue(key, value)
    settings.remove(config.KEY_GEOMETRY)  # 用預設視窗大小，量測才可重現
    settings.sync()


def restore_settings(saved: dict) -> None:
    settings = _settings_handle()
    settings.clear()
    for key, value in saved.items():
        settings.setValue(key, value)
    settings.sync()


# --- 量測：打包後的 exe -----------------------------------------------------
def measure_exe(exe_path: str, runs: int) -> list[float]:
    """回傳每次「啟動到視窗出現」的毫秒數（不含暖機那次）。"""
    image_name = os.path.basename(exe_path)
    samples: list[float] = []

    for index in range(runs + 1):  # 第 0 次是暖機
        kill_all(image_name)
        time.sleep(0.7)

        started = time.perf_counter()
        subprocess.Popen(
            [exe_path, SAMPLE_DOC],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )

        hwnd = None
        while time.perf_counter() - started < LAUNCH_TIMEOUT_S:
            hwnd = find_window(config.APP_DISPLAY_NAME)
            if hwnd:
                break
            time.sleep(0.005)
        elapsed = (time.perf_counter() - started) * 1000

        if hwnd is None:
            kill_all(image_name)
            raise RuntimeError(f"{LAUNCH_TIMEOUT_S} 秒內沒有出現視窗：{exe_path}")

        if index > 0:
            samples.append(elapsed)
        time.sleep(0.25)
        kill_all(image_name)

    return samples


# --- 量測：原始碼各階段 -----------------------------------------------------
def _run_probe() -> int:
    """在乾淨的行程裡量測各階段，以 JSON 輸出。由 --_probe 觸發。"""
    import time as _time

    start = _time.perf_counter()
    os.chdir(PROJECT_ROOT)

    mark = _time.perf_counter()
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    from app.viewer import MarkdownViewer

    t_import = (_time.perf_counter() - mark) * 1000

    mark = _time.perf_counter()
    application = QApplication(sys.argv)
    t_qapp = (_time.perf_counter() - mark) * 1000

    mark = _time.perf_counter()
    viewer = MarkdownViewer(SAMPLE_DOC)
    t_ctor = (_time.perf_counter() - mark) * 1000

    mark = _time.perf_counter()
    viewer.show()
    for _ in range(3):
        application.processEvents()
    t_show = (_time.perf_counter() - mark) * 1000

    total = (_time.perf_counter() - start) * 1000
    viewer.close()

    print(
        "PROBE"
        + json.dumps(
            {
                "import": t_import,
                "qapp": t_qapp,
                "ctor": t_ctor,
                "show": t_show,
                "total": total,
            }
        )
    )
    return 0


def measure_source(runs: int) -> dict[str, list[float]]:
    """每次都開新行程，避免 Qt 子系統被前一次暖起來而失真。"""
    collected: dict[str, list[float]] = {
        "import": [],
        "qapp": [],
        "ctor": [],
        "show": [],
        "total": [],
    }
    for index in range(runs + 1):  # 第 0 次是暖機
        result = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--_probe"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        line = next(
            (l for l in result.stdout.splitlines() if l.startswith("PROBE")), None
        )
        if line is None:
            raise RuntimeError(f"量測子行程失敗：\n{result.stdout}\n{result.stderr}")
        if index == 0:
            continue
        for key, value in json.loads(line[len("PROBE") :]).items():
            collected[key].append(value)
    return collected


# --- 統計與輸出 -------------------------------------------------------------
def summarise(samples: list[float]) -> dict:
    return {
        "median": round(statistics.median(samples), 1),
        "min": round(min(samples), 1),
        "max": round(max(samples), 1),
        "stdev": round(statistics.stdev(samples), 1) if len(samples) > 1 else 0.0,
        "runs": len(samples),
    }


def load_baseline() -> dict | None:
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def environment_info() -> dict:
    from PyQt6.QtCore import QT_VERSION_STR

    return {
        "python": sys.version.split()[0],
        "qt": QT_VERSION_STR,
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
    }


def report(metrics: dict, baseline: dict | None, tolerance: float) -> bool:
    """輸出比對表，回傳是否通過。

    判定一律以最小值為準（理由見模組開頭的說明），中位數只列出來作參考。
    """
    print(f"\n{'項目':<20}{'最小值':>10}{'(中位數)':>12}{'基準最小值':>13}   判定")
    print("-" * 76)

    passed = True
    base_metrics = (baseline or {}).get("metrics", {})

    for name, stats in metrics.items():
        value = stats["min"]
        base = base_metrics.get(name, {}).get("min")
        budget = ABSOLUTE_BUDGET_MS.get(name)

        if budget is not None and value > budget:
            verdict = f"超出絕對上限 {budget}ms"
            passed = False
        elif base is not None:
            ratio = (value - base) / base if base else 0.0
            if ratio > tolerance:
                verdict = f"變慢 {ratio:+.0%}  <-- 退步"
                passed = False
            elif ratio < -tolerance:
                verdict = f"變快 {ratio:+.0%}"
            else:
                verdict = f"持平 {ratio:+.0%}"
        else:
            verdict = "無基準線"

        base_text = f"{base:.0f}ms" if base is not None else "—"
        print(
            f"{name:<20}{value:>9.0f}ms{stats['median']:>11.0f}ms"
            f"{base_text:>13}   {verdict}"
        )

    print("-" * 76)
    print("通過" if passed else "未通過：有項目超出容忍範圍")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description="啟動速度基準測試")
    parser.add_argument("--_probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=int, default=8, help="取樣次數（預設 8）")
    parser.add_argument(
        "--target",
        choices=("both", "exe", "source"),
        default="both",
        help="量測對象（預設 both）",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"容忍的變慢比例（預設 {DEFAULT_TOLERANCE:.0%}）",
    )
    parser.add_argument(
        "--update-baseline", action="store_true", help="把這次結果存成新的基準線"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出結果")
    args = parser.parse_args()

    if args._probe:
        return _run_probe()

    if sys.platform != "win32":
        print("這套量測依賴 Win32 的視窗偵測，只能在 Windows 上執行。")
        return 2

    metrics: dict[str, dict] = {}
    saved_settings = snapshot_settings()
    try:
        apply_benchmark_settings()

        if args.target in ("both", "exe"):
            for label, path in (
                ("onefile_window", ONEFILE_EXE),
                ("onedir_window", ONEDIR_EXE),
            ):
                if not os.path.isfile(path):
                    print(f"略過 {label}：找不到 {path}", file=sys.stderr)
                    continue
                print(f"量測 {label}（{args.runs} 次 + 1 次暖機）…", file=sys.stderr)
                metrics[label] = summarise(measure_exe(path, args.runs))

        if args.target in ("both", "source"):
            print(f"量測原始碼各階段（{args.runs} 次 + 1 次暖機）…", file=sys.stderr)
            phases = measure_source(args.runs)
            for phase, samples in phases.items():
                metrics[f"source_{phase}"] = summarise(samples)
    finally:
        restore_settings(saved_settings)

    if not metrics:
        print("沒有任何可量測的目標。請先執行 build.ps1 產生執行檔。")
        return 2

    baseline = load_baseline()
    if args.json:
        print(json.dumps({"environment": environment_info(), "metrics": metrics},
                         ensure_ascii=False, indent=2))
        ok = True
    else:
        ok = report(metrics, baseline, args.tolerance)

    if args.update_baseline:
        payload = {
            "recorded": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "基準線與機器相關，換機器請重新以 --update-baseline 建立。",
            "environment": environment_info(),
            "tolerance": args.tolerance,
            "metrics": metrics,
        }
        with open(BASELINE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"\n已更新基準線：{BASELINE_PATH}")
        return 0

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
