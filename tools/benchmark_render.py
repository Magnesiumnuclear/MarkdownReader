"""渲染速度基準測試。

用途：任何會影響渲染路徑的改動（qt_html 的樹改寫、document 的轉換器設定、
styles 產生的 CSS、viewer 的 setHtml 流程）之後跑一次，和記錄下來的基準線
比對，確認沒有把渲染變慢。

    py -3.13 tools/benchmark_render.py                    # 量測並和基準線比對
    py -3.13 tools/benchmark_render.py --runs 9           # 增加取樣次數（雜訊大時）
    py -3.13 tools/benchmark_render.py --target md        # 只量 Markdown -> HTML
    py -3.13 tools/benchmark_render.py --update-baseline  # 把目前結果存成新基準線
    py -3.13 tools/benchmark_render.py --json             # 機器可讀輸出

量測項目分兩層：
  1. Markdown -> HTML（純 Python）——`document.markdown_to_html`，其中包含
     本專案的 qt_html 樹改寫。分四種文件輪廓加一份混合文件量，因為不同結構
     壓到的是不同的程式路徑：標題密集壓樹改寫、程式碼密集壓 codehilite、
     表格密集壓 extra 的表格解析、長篇散文是最常見的情況。
  2. Qt 端——`setHtml` 的 HTML 解析與版面計算，在獨立的 QTextDocument 上量
     （不開視窗，理由見 _probe_qt）。「從雙擊到看見視窗」那種端到端數字是
     benchmark_startup 的守備範圍，這裡不重複量。

【為什麼分四種輪廓而不是只量一份大檔】
渲染成本不是只跟位元組數走。實測 prose（174 KB）比 headings（144 KB）還大，
Markdown -> HTML 卻只要 246ms，headings 要 373ms；到了 Qt 端差距更誇張，
headings 的版面計算要 1548ms，而 27.9 KB 的 mixed 只要 180ms——標題密集的文件
會被 qt_html 包成大量單格表格，表格的版面計算遠比段落貴。
只量一份「大檔」會完全看不到這種結構相關的退步；分輪廓比對，退步時能直接
指出是哪一類文件、哪一層變慢。

【為什麼用最小值比對】
理由與 benchmark_startup 相同：干擾（背景程式、GC、CPU 排程）只會讓時間變長，
不會讓程式跑得比實際更快，所以最小值最接近「沒有被干擾時的真實成本」。
中位數只列出來當參考。

實測本機連續三輪的最小值（每個指標各開新行程、--runs 5）：
    md_headings       374 / 364 / 370 ms
    md_prose          244 / 234 / 239 ms
    md_code           149 / 154 / 152 ms
    md_table           60 /  57 /  62 ms
    md_mixed          108 / 110 / 112 ms
    sethtml_mixed     176 / 169 / 171 ms
    sethtml_headings 1330 /1283 /1309 ms
    sethtml_readme     34 /  32 /  32 ms
八項的擺盪都在 ±3% 以內，撐得起 15% 的判定門檻。

達到這個穩定度靠三件事，每一件都是踩過坑才加的（細節見對應的說明）：
  1. 每個指標各開一個新行程——同一行程裡連續量，先量的會把堆積撐大，
     後量的整批快 40%（見 measure_metric）。
  2. Qt 端用獨立的 QTextDocument，不開實際視窗——視窗的合成與非同步繪製
     讓同一份文件跨行程量到 272 / 493 / 490 / 349 ms（見 _probe_qt）。
  3. 丟掉兩次暖機、每次取樣前 gc.collect()（見 timed）。

【這套判定確實抓得到問題】
拿修好之前的 qt_html（`_convert_blocks` 用 list(parent).index + remove +
insert，標題密集時退化成 O(n^2)）跑這支工具，結束碼 1：
    md_headings       373ms ->  562ms  (+51%)  <-- 退步
    md_mixed          113ms ->  128ms  (+13%)  持平
    md_prose          246ms ->  266ms   (+8%)  持平
    md_code           155ms ->  157ms   (+1%)  持平
    md_table           63ms ->   63ms   (-1%)  持平
    sethtml_headings 1548ms -> 1618ms   (+5%)  持平
該紅的紅、該持平的持平，而且同時告訴你「是哪一類文件變慢」以及「慢在哪一層」
——Markdown -> HTML 那層紅、Qt 那層沒事，一眼就看得出問題在樹改寫不在版面。

【基準線與機器相關】
render_baseline.json 記錄的是「在某台機器上」的數字，換機器一定會不同。
換到新機器時先跑一次 --update-baseline 重新建立基準。
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(PROJECT_ROOT, "tools", "render_baseline.json")
# 「真實混合文件」的輪廓：拿真的文件量，補足合成輪廓看不到的結構混雜。
# 素材是 docs/ 八個分冊依檔名順序接起來（約 1 360 行）。README 拆分前這裡量的是
# 1 500 行的 README；拆分後單一分冊最多三百多行，量起來只剩幾毫秒、貼著雜訊，
# 接起來才保住原本的量級與代表性。指標鍵名 sethtml_readme 為了基準線的歷史
# 連續性沿用舊名，不隨素材換掉。
PROFILE_DIR = os.path.join(PROJECT_ROOT, "docs")


def profile_source() -> str:
    parts = []
    for filename in sorted(os.listdir(PROFILE_DIR)):
        if filename.endswith(".md"):
            with open(os.path.join(PROFILE_DIR, filename), encoding="utf-8") as handle:
                parts.append(handle.read())
    if not parts:
        raise SystemExit(f"找不到分冊：{PROFILE_DIR}")
    return "\n\n".join(parts)

THEME = "light"

# 絕對上限。就算沒有基準線，超過這個值就代表明顯出事了。
# 取的是「明顯有問題」的量級而不是貼著現值，避免換台慢一點的機器就整片紅。
ABSOLUTE_BUDGET_MS = {
    "md_headings": 1200,
    "md_prose": 900,
    "md_code": 900,
    "md_table": 500,
    "md_mixed": 700,
    "sethtml_mixed": 700,
    "sethtml_headings": 2500,
    "sethtml_readme": 300,
    "md_mermaid": 600,
    "render_mermaid": 900,
    "render_mermaid_highlight": 900,
}

DEFAULT_TOLERANCE = 0.15

# 合成文件的規模。固定寫死而不是隨機，基準線才有可比性。
DOC_SIZES = {
    "headings": 3000,   # 標題密集：壓 qt_html 的樹改寫（h1/h2 每個都要包表格）
    "prose": 2000,      # 長篇散文：最常見的情況
    "code": 400,        # 程式碼密集：壓 codehilite 的語法高亮
    "table": 300,       # 表格密集：壓 extra 的表格解析
    "mixed": 1500,      # 混合：最接近真實文件
    "mermaid": 40,      # Mermaid：40 張各約 20 節點的流程圖 + 序列圖交錯
}

# Qt 端的排版參數直接寫死，不從 QSettings 讀。
# 這樣這支工具完全不會碰到使用者的設定，也不必存檔／還原；
# 版面計算的成本和文字寬度直接相關，寫死才有可比性。
LAYOUT_FONT_PT = config.BASE_FONT_POINT_SIZE
LAYOUT_LINE_HEIGHT = 160
LAYOUT_TEXT_WIDTH = config.DEFAULT_CONTENT_WIDTH

# 探針行程裡的 QApplication，必須保持存活（見 _probe_qt）
_QAPP = None


# --- 合成文件 ---------------------------------------------------------------
def build_documents() -> dict[str, str]:
    """產生四種輪廓的合成文件 + 一份混合文件。

    內容固定（亂數固定種子），同一台機器上重跑結果才可比。
    """
    docs: dict[str, str] = {}

    docs["headings"] = "\n\n".join(
        f"## 第 {i} 節\n\n這是第 {i} 節的內容，帶一點 `行內程式碼` 與 **粗體**。"
        for i in range(DOC_SIZES["headings"])
    )

    docs["prose"] = "\n\n".join(
        f"第 {i} 段。這裡是一段普通的敘述文字，混一些 **粗體**、*斜體*、"
        f"~~刪除線~~ 與 [連結](https://example.com/{i})，長度接近真實段落。"
        for i in range(DOC_SIZES["prose"])
    )

    docs["code"] = "\n\n".join(
        f"```python\ndef f{i}(x):\n    total = 0\n    for k in range(x):\n"
        f"        total += k * {i}\n    return total\n```"
        for i in range(DOC_SIZES["code"])
    )

    docs["table"] = "\n\n".join(
        f"| 欄 A | 欄 B | 欄 C |\n|---|---|---|\n"
        f"| {i} | 值 {i} | 說明 {i} |\n| {i + 1} | 值 {i + 1} | 說明 {i + 1} |"
        for i in range(DOC_SIZES["table"])
    )

    rng = random.Random(20260828)
    shapes = [
        "# 大標 {i}",
        "## 小節 {i}",
        "第 {i} 段內容，帶 **粗體** 與 `程式碼`。",
        "---",
        "> 第 {i} 段引言。",
        "```py\nx = {i}\n```",
        "- [ ] 待辦 {i}\n- [x] 完成 {i}",
        "| a | b |\n|---|---|\n| {i} | {i} |",
    ]
    docs["mixed"] = "\n\n".join(
        rng.choice(shapes).format(i=i) for i in range(DOC_SIZES["mixed"])
    )

    # Mermaid：轉換期要解析每張圖（md_mermaid），載入期要排版與繪製（render_mermaid）。
    # 流程圖 20 節點含分支與子圖、序列圖 12 則訊息含區塊，接近真實筆記裡的規模。
    # 每張圖的標籤都帶 i：登錄表的鍵是原始碼雜湊，40 張一模一樣的圖只會畫一張，
    # 其餘全是快取命中，量出來的數字會小到沒有意義。
    blocks = []
    for i in range(DOC_SIZES["mermaid"]):
        if i % 2 == 0:
            lines = ["graph TD"]
            for k in range(18):
                shape = ("[步驟 {i}-{k}]", "{{判斷 {i}-{k}}}", "([{i}-{k}])", "[[子 {i}-{k}]]")[k % 4]
                lines.append(f"  N{k}{shape.format(i=i, k=k)} --> N{k + 1}")
                if k % 3 == 0:
                    lines.append(f"  N{k} -->|分支 {i}-{k}| N{k + 2}")
            lines.append(f"  subgraph 群組 {i}\n    N3 --> N19\n  end")
            blocks.append("```mermaid\n" + "\n".join(lines) + "\n```")
        else:
            lines = ["sequenceDiagram", "  participant A", "  participant B", "  participant C"]
            for k in range(12):
                src, dst = ("A", "B") if k % 2 else ("B", "C")
                lines.append(f"  {src}->>{dst}: 訊息 {i}-{k}")
            lines.append(f"  loop 每次 {i}\n    C-->>A: 回應\n  end")
            blocks.append("```mermaid\n" + "\n".join(lines) + "\n```")
    docs["mermaid"] = "\n\n".join(f"## 圖 {i}\n\n{block}" for i, block in enumerate(blocks))
    return docs


# --- 量測工具 ---------------------------------------------------------------
MD_METRICS = ("md_headings", "md_prose", "md_code", "md_table", "md_mixed", "md_mermaid")
QT_METRICS = ("sethtml_mixed", "sethtml_headings", "sethtml_readme",
              "render_mermaid", "render_mermaid_highlight")

# 丟掉前幾次不列入統計。1 次不夠：轉換器要載入並串接八個擴充、pygments 要建
# lexer、Qt 要建字型快取，第二次都還在受影響。
WARMUP_RUNS = 2


def timed(action, runs: int) -> list[float]:
    """跑 WARMUP_RUNS+runs 次，丟掉暖機的那幾次，回傳每次的毫秒數。

    每次取樣前先 gc.collect()（在計時區間外），讓每次取樣都從同一個堆積狀態
    出發。注意這不是為了讓數字變快——實測「停用 GC」反而更慢
    （pygments 配置量大，不回收會傷記憶體區域性），這裡要的只是一致。
    """
    samples: list[float] = []
    for index in range(WARMUP_RUNS + runs):
        gc.collect()
        start = time.perf_counter()
        action()
        elapsed = (time.perf_counter() - start) * 1000
        if index >= WARMUP_RUNS:
            samples.append(elapsed)
    return samples


def summarise(samples: list[float]) -> dict:
    return {
        "median": round(statistics.median(samples), 1),
        "min": round(min(samples), 1),
        "max": round(max(samples), 1),
        "stdev": round(statistics.stdev(samples), 1) if len(samples) > 1 else 0.0,
        "runs": len(samples),
    }


# --- 探針：在自己的行程裡量一個指標 -----------------------------------------
def run_probe(name: str, runs: int) -> int:
    """量一個指標並把樣本以 JSON 印到 stdout。由父行程以 --_probe 呼叫。

    這個函式只印 JSON，不印別的東西，父行程才好解析。
    """
    docs = build_documents()

    if name.startswith("md_"):
        from app import document

        text = docs[name[len("md_") :]]
        samples = timed(lambda: document.markdown_to_html(str(text), THEME), runs)
    elif name.startswith("render_mermaid"):
        samples = _probe_mermaid_render(docs, runs, name.endswith("highlight"))
    else:
        samples = _probe_qt(name, docs, runs)

    print(json.dumps({"samples": samples}))
    return 0


def _probe_qt(name: str, docs: dict[str, str], runs: int) -> list[float]:
    """量 setHtml 的 HTML 解析與版面計算。

    【為什麼用獨立的 QTextDocument，不透過實際的閱讀器視窗】
    一開始是開一個真的 MarkdownViewer 來量，但顯示中的視窗會帶進合成、
    非同步繪製與各種計時器的雜訊：同一份文件跨行程量到 272 / 493 / 490 /
    349 ms，擺盪將近一倍，這種數字撐不起 15% 的判定門檻。改成獨立文件之後
    連續三個行程量到 173.7 / 173.9 / 172.9 ms（±0.6%）。

    這樣量到的仍然是 setHtml 真正的成本——HTML 解析與版面計算就是它的全部，
    少掉的只是視窗合成，而那本來就不該算進渲染基準。至於「從雙擊到看見視窗」
    那種端到端數字，是 benchmark_startup 的守備範圍，不在這裡重複量。

    documentLayout().documentSize() 用來逼版面實際算完——少了它 Qt 會延後
    計算，量到的就只是「把字串收下」。
    """
    from PyQt6.QtGui import QFont, QTextDocument
    from PyQt6.QtWidgets import QApplication

    from app import document, styles

    # QTextDocument 的版面計算需要 QApplication（要字型度量）。
    # 一定要用變數接住並保持存活到量完：不接的話 Python 會立刻回收這個
    # 包裝物件，Qt 底層被拆掉，下一個 QTextDocument 就以 0xC0000409 當場死掉。
    global _QAPP
    _QAPP = QApplication.instance() or QApplication([])

    if name == "sethtml_readme":
        source = profile_source()
    else:
        source = docs[name[len("sethtml_") :]]

    html = document.markdown_to_html(source, THEME)
    css = styles.build_doc_css(THEME, LAYOUT_FONT_PT, LAYOUT_LINE_HEIGHT)

    def action():
        doc = QTextDocument()
        doc.setDefaultStyleSheet(css)
        font = QFont()
        font.setPointSize(LAYOUT_FONT_PT)
        doc.setDefaultFont(font)
        doc.setTextWidth(LAYOUT_TEXT_WIDTH)
        doc.setHtml(html)
        doc.documentLayout().documentSize()

    return timed(action, runs)


def _probe_mermaid_render(
    docs: dict[str, str], runs: int, highlight: bool = False
) -> list[float]:
    """量 Mermaid 圖表從「已解析的模型」到「畫好的 QImage」的成本（版面 + 繪製）。

    這段不在 setHtml 的探針裡：那邊用的是獨立的 QTextDocument，沒有閱讀區覆寫的
    loadResource，mermaid: 圖片根本不會被畫。轉換期的解析成本則已經算在 md_mermaid
    裡（前處理器就是在那時解析並登錄的）。
    """
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    from app import document, mermaid

    global _QAPP
    _QAPP = QApplication.instance() or QApplication([])

    html = document.markdown_to_html(docs["mermaid"], THEME)
    keys = re.findall(r'src="mermaid:([0-9a-f]+)"', html)
    font = QFont()
    font.setPointSize(LAYOUT_FONT_PT)

    # 帶搜尋詞的版本量的是「搜尋列把一張可見的圖重畫成帶高亮」的成本；
    # 場景快取留著（實際情形也是留著的），所以量到的就是繪製本身。
    mark = mermaid.Highlight("步驟", False, False, False) if highlight else None

    def action():
        # 只清圖片快取，登錄表與場景留著：量的是「畫」，不是「解析」或「排版」
        mermaid._images.clear()
        for key in keys:
            mermaid.render(key, THEME, LAYOUT_TEXT_WIDTH, font, 1.0, mark)

    return timed(action, runs)


# --- 父行程：每個指標各開一個新行程 -----------------------------------------
def measure_metric(name: str, runs: int) -> list[float]:
    """開一個新行程量指定的指標。

    【為什麼一定要開新行程】同一個行程裡連續量多個指標，先量的會把堆積撐大，
    後量的成本就跟著變。實測在同一行程依序量五個輪廓，第三輪整批比第一輪快
    約 40%（md_code 260ms -> 152ms、md_table 103ms -> 55ms），而第一個量的
    md_headings 始終穩定——正是「前面量過什麼」在決定後面的數字。
    改成每個指標各開新行程之後，連續三輪的最小值：
        md_code   155 / 160 / 161 ms
        md_prose  242 / 248 / 238 ms
        md_table   65 /  61 /  61 ms
    擺盪收在 ±3% 以內，這才撐得起 15% 的判定門檻。
    """
    result = subprocess.run(
        [
            sys.executable,
            os.path.abspath(__file__),
            "--_probe",
            name,
            "--runs",
            str(runs),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"量測 {name} 失敗（結束碼 {result.returncode}）：\n{result.stderr[-2000:]}"
        )
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)["samples"]
    raise RuntimeError(f"量測 {name} 沒有輸出樣本：\n{result.stdout[-2000:]}")


# --- 比對與輸出 -------------------------------------------------------------
def load_baseline() -> dict | None:
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def environment_info() -> dict:
    info = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
        "doc_sizes": dict(DOC_SIZES),
    }
    try:
        from PyQt6.QtCore import QT_VERSION_STR

        info["qt"] = QT_VERSION_STR
    except ImportError:
        info["qt"] = None
    return info


def report(metrics: dict, baseline: dict | None, tolerance: float) -> bool:
    """輸出比對表，回傳是否通過。

    判定一律以最小值為準（理由見模組開頭），中位數只列出來作參考。
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
    parser = argparse.ArgumentParser(description="渲染速度基準測試")
    parser.add_argument("--_probe", dest="probe", help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=int, default=5, help="取樣次數（預設 5）")
    parser.add_argument(
        "--target",
        choices=("all", "md", "qt"),
        default="all",
        help="只量某一層：md = Markdown -> HTML，qt = Qt 端",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"容忍幅度（預設 {DEFAULT_TOLERANCE:.0%}）",
    )
    parser.add_argument("--update-baseline", action="store_true", help="更新基準線")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args()

    if args.probe:
        return run_probe(args.probe, args.runs)

    names: list[str] = []
    if args.target in ("all", "md"):
        names.extend(MD_METRICS)
    if args.target in ("all", "qt"):
        names.extend(QT_METRICS)

    if not args.json:
        docs = build_documents()
        print("文件輪廓（合成，內容固定）：")
        for name, text in docs.items():
            print(
                f"  {name:<10}{len(text) / 1024:8.1f} KB  {DOC_SIZES[name]} 個區塊"
            )
        print(f"\n每個指標各開一個新行程量測（理由見 measure_metric 的說明）…")

    samples: dict[str, list[float]] = {}
    for name in names:
        if not args.json:
            print(f"  {name} …", end="", flush=True)
        values = measure_metric(name, args.runs)
        samples[name] = values
        if not args.json:
            print(f" {min(values):.0f}ms")

    metrics = {name: summarise(values) for name, values in samples.items()}

    payload = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "environment": environment_info(),
        "metrics": metrics,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    baseline = load_baseline()
    passed = True
    if not args.json:
        passed = report(metrics, baseline, args.tolerance)

    if args.update_baseline:
        with open(BASELINE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        if not args.json:
            print(f"\n已寫入基準線：{BASELINE_PATH}")
        return 0

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
