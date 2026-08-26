"""轉交速度量測：比較「原生轉交器」與「直接啟動本體」。

    py -3.13 tools/benchmark_handoff.py                # 全部量
    py -3.13 tools/benchmark_handoff.py --runs 10      # 增加取樣
    py -3.13 tools/benchmark_handoff.py --json         # 機器可讀輸出

量兩件事：

  1. 轉交（已經有視窗開著時，再雙擊一個 .md 到分頁出現）
     這是日常最常做、也最有感的操作。量的是「新行程從被建立到結束」，
     因為使用者感受到的延遲就是這段。

  2. 冷啟動（沒有實例，從按下到視窗出現）
     用來確認多墊一層轉交器沒有讓冷啟動變慢。

【為什麼要先量工具本身的開銷】
建立一個行程本身就要時間。先量一個「立刻結束的原生 exe」當作地板，
報告時把它一起列出來，才不會把量測工具的成本算到受測程式頭上。
（先前用 PowerShell 的 Start-Process -Wait 量，地板就有 1007 ms，
整個結論都是錯的。）

【為什麼用最小值】
同 tools/benchmark_startup.py：雜訊只會讓時間變長，不會讓程式跑得比實際更快，
所以最小值最接近真實成本。
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(PROJECT_ROOT, "sample.md")
SECOND = os.path.join(PROJECT_ROOT, "README.md")

ONEDIR_APP = os.path.join(PROJECT_ROOT, "dist", "MarkdownReader-onedir", "MarkdownReader.exe")
ONEDIR_LAUNCHER = os.path.join(PROJECT_ROOT, "dist", "MarkdownReader-onedir", "MarkdownOpen.exe")
ONEFILE_APP = os.path.join(PROJECT_ROOT, "dist", "MarkdownReader.exe")
ONEFILE_LAUNCHER = os.path.join(PROJECT_ROOT, "dist", "MarkdownOpen.exe")

LAUNCH_TIMEOUT_S = 30
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


# --- Win32 小工具 -----------------------------------------------------------
# 【務必保留這些 restype/argtypes 宣告】
# ctypes 在沒有宣告時會把回傳值當成 32 位元的 C int。HANDLE 在 64 位元 Windows
# 是 64 位元，截斷之後 CloseHandle 就關錯對象，真正的握把一直沒被釋放——具名
# 管道會被撐著不死。症狀是「行程都砍光了，下一次啟動卻還是轉交出去、立刻結束、
# 沒有視窗」，而且完全看不出原因。app/win32.py 有同樣的註解與同樣的教訓。
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.FindWindowW.restype = ctypes.c_void_p
_kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p,   # lpFileName
    ctypes.c_uint32,    # dwDesiredAccess
    ctypes.c_uint32,    # dwShareMode
    ctypes.c_void_p,    # lpSecurityAttributes
    ctypes.c_uint32,    # dwCreationDisposition
    ctypes.c_uint32,    # dwFlagsAndAttributes
    ctypes.c_void_p,    # hTemplateFile
]
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.restype = ctypes.c_int
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def find_window(title_fragment: str):
    """找到標題含指定字串的可見視窗。"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
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

    _user32.EnumWindows(callback, None)
    return found[0] if found else None


def kill_app() -> None:
    """砍掉所有相關行程，並等到管道真的消失。

    taskkill 回來不代表行程已經結束。管道只要還在，下一次啟動就會「轉交給
    一個正在死掉的實例」然後自己退出——量測會看到「三十秒內沒有視窗」，
    而那是測試自己造成的，不是程式慢。
    """
    for image in ("MarkdownOpen.exe", "MarkdownReader.exe"):
        subprocess.run(["taskkill", "/F", "/IM", image],
                       capture_output=True, check=False)
    deadline = time.perf_counter() + 10
    while pipe_alive() and time.perf_counter() < deadline:
        time.sleep(0.02)


def pipe_alive() -> bool:
    """管道還在不在（有沒有實例在監聽）。

    注意這會真的建立一條連線，收方會收到一個空訊息並把視窗叫到前景。
    只用在「確認東西都死透了」的清理流程，不要放進量測迴圈裡。
    """
    handle = _kernel32.CreateFileW(
        f"\\\\.\\pipe\\{config.IPC_SERVER_NAME}",
        0xC0000000, 0, None, 3, 0, None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        return False
    _kernel32.CloseHandle(handle)
    return True


# --- 量測 -------------------------------------------------------------------
def measure_process(command: list[str], runs: int) -> list[float]:
    """量「行程被建立到結束」的毫秒數。"""
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        proc = subprocess.Popen(command, creationflags=DETACHED)
        proc.wait(timeout=LAUNCH_TIMEOUT_S)
        samples.append((time.perf_counter() - started) * 1000)
        time.sleep(0.15)
    return samples


def measure_handoff(app: str, forwarder: str, runs: int) -> list[float]:
    """量轉交：先開一個視窗，再用 forwarder 送第二個檔案。"""
    kill_app()
    time.sleep(0.2)
    subprocess.Popen([app, SAMPLE], creationflags=DETACHED)
    deadline = time.perf_counter() + LAUNCH_TIMEOUT_S
    while time.perf_counter() < deadline:
        if find_window("sample.md"):
            break
        time.sleep(0.01)
    else:
        kill_app()
        raise RuntimeError(f"第一個視窗沒起來：{app}")
    time.sleep(0.4)

    samples = measure_process([forwarder, SECOND], runs + 1)[1:]  # 第一次當暖機

    if not find_window("README.md"):
        kill_app()
        raise RuntimeError("轉交後沒有切到 README 分頁，量到的數字沒有意義")
    kill_app()
    return samples


def measure_cold(command: list[str], runs: int) -> list[float]:
    """量冷啟動：從行程建立到視窗出現。"""
    samples = []
    for index in range(runs + 1):  # 第 0 次暖機
        kill_app()
        time.sleep(0.2)
        started = time.perf_counter()
        subprocess.Popen(command, creationflags=DETACHED)
        hwnd = None
        while time.perf_counter() - started < LAUNCH_TIMEOUT_S:
            hwnd = find_window(config.APP_DISPLAY_NAME)
            if hwnd:
                break
            time.sleep(0.005)
        elapsed = (time.perf_counter() - started) * 1000
        if hwnd is None:
            kill_app()
            raise RuntimeError(f"{LAUNCH_TIMEOUT_S} 秒內沒有視窗：{command}")
        if index > 0:
            samples.append(elapsed)
        time.sleep(0.25)
    kill_app()
    return samples


def summarise(samples: list[float]) -> dict:
    return {
        "min": round(min(samples), 1),
        "median": round(statistics.median(samples), 1),
        "max": round(max(samples), 1),
        "runs": len(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="轉交速度量測")
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    missing = [p for p in (ONEDIR_APP, ONEDIR_LAUNCHER, ONEFILE_APP, ONEFILE_LAUNCHER)
               if not os.path.isfile(p)]
    if missing:
        print("以下檔案不存在，請先執行 build.ps1（含 -OneDir）：")
        for path in missing:
            print(f"  {path}")
        return 1

    results: dict[str, dict] = {}

    # 量測工具本身的地板
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "cmd.exe")
    results["量測地板（cmd /c exit）"] = summarise(
        measure_process([cmd_exe, "/c", "exit"], args.runs)
    )

    print("量測轉交…", flush=True)
    results["轉交：資料夾版 · 直接啟動本體"] = summarise(
        measure_handoff(ONEDIR_APP, ONEDIR_APP, args.runs))
    results["轉交：資料夾版 · 原生轉交器"] = summarise(
        measure_handoff(ONEDIR_APP, ONEDIR_LAUNCHER, args.runs))
    results["轉交：單一 exe · 直接啟動本體"] = summarise(
        measure_handoff(ONEFILE_APP, ONEFILE_APP, args.runs))
    results["轉交：單一 exe · 原生轉交器"] = summarise(
        measure_handoff(ONEFILE_APP, ONEFILE_LAUNCHER, args.runs))

    print("量測冷啟動…", flush=True)
    results["冷啟動：資料夾版 · 直接"] = summarise(
        measure_cold([ONEDIR_APP, SAMPLE], args.runs))
    results["冷啟動：資料夾版 · 經過轉交器"] = summarise(
        measure_cold([ONEDIR_LAUNCHER, SAMPLE], args.runs))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print()
    width = max(len(name) for name in results)
    print(f"{'項目'.ljust(width)}   最小值    (中位數)")
    print("-" * (width + 24))
    for name, data in results.items():
        print(f"{name.ljust(width)}   {data['min']:>6.0f}ms   {data['median']:>6.0f}ms")
    print("-" * (width + 24))

    floor = results["量測地板（cmd /c exit）"]["min"]
    print(f"\n扣掉量測地板（{floor:.0f} ms）之後的實際轉交成本：")
    for name, data in results.items():
        if name.startswith("轉交"):
            print(f"  {name.ljust(width)} {max(0.0, data['min'] - floor):>7.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
