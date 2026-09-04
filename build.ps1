# Markdown 閱讀器打包腳本
#
#   .\build.ps1              單一 exe（隱藏主控台）
#   .\build.ps1 -OneDir      資料夾版（啟動快很多，建議用於檔案關聯）
#   .\build.ps1 -Clean       打包前先清掉 build/ 與 dist/
#
# 需求：py -3.13 -m pip install -r requirements.txt

param(
    [switch]$OneDir,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Invoke-Native {
    <#
    .SYNOPSIS
        跑外部執行檔，成敗**只看結束碼**。

    .DESCRIPTION
        PowerShell 5.1 有兩個坑，這支腳本都踩過：

        1. $ErrorActionPreference = "Stop" 時，外部程式只要往 stderr 寫一行，
           PowerShell 就把它包成 NativeCommandError 當作「終止錯誤」。
           PyInstaller 的進度訊息**全部走 stderr**，於是打包才剛開始腳本就中斷，
           第 5 步（把原生轉交器複製進 dist）永遠不會執行——而檔案關聯正是
           指向那個轉交器。症狀是 .md 的圖示變白紙、雙擊沒反應，而且完全
           沒有錯誤訊息。這個函式把 ErrorActionPreference 暫時降成 Continue。

        2. 同一個包裝也會讓 $? 變成 $false，即使程式回傳 0；而且 $? 會被後續
           任何一次賦值重設。拿 $? 判斷「上一個外部程式成功了嗎」並不可靠，
           唯一可信的是 $LASTEXITCODE。

        stderr 用 2>&1 併進輸出流再轉成字串，是為了在「輸出被擷取」的環境下
        不要整片變成紅字；互動視窗裡看起來和直接執行一樣。

        呼叫端請在呼叫後檢查 $LASTEXITCODE（自動變數，函式外一樣讀得到）。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $File @Arguments 2>&1 | ForEach-Object { [string]$_ }
    } finally {
        $ErrorActionPreference = $previous
    }
}

Write-Host "=== Markdown 閱讀器打包 ===" -ForegroundColor Cyan

# 1. 確認相依套件
Write-Host "`n[1/5] 檢查相依套件..."
Invoke-Native -File "py" -Arguments @(
    "-3.13", "-c",
    "import PyQt6, PyQt6.QtSvg, markdown, pygments, PyInstaller; print('  相依套件齊全')"
)
if ($LASTEXITCODE -ne 0) {
    Write-Host "  缺少套件，請先執行： py -3.13 -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

# 2. 產生圖示
Write-Host "`n[2/5] 產生應用程式圖示..."
if (-not (Test-Path "assets\app.ico")) {
    Invoke-Native -File "py" -Arguments @("-3.13", "tools\make_icon.py")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  產生圖示失敗" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  assets\app.ico 已存在，略過"
}

# 3. 清理
if ($Clean) {
    Write-Host "`n[3/5] 清理舊的建置產物..."
    foreach ($dir in @("build", "dist")) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
            Write-Host "  已刪除 $dir"
        }
    }
} else {
    Write-Host "`n[3/5] 略過清理（加上 -Clean 可強制清除）"
}

# 4. 打包
Write-Host "`n[4/5] 執行 PyInstaller..."
if ($OneDir) {
    # 資料夾版：不需解壓，實測啟動時間約為單一 exe 的一半
    $env:MDREADER_ONEDIR = "1"
    $output = "dist\MarkdownReader-onedir\MarkdownReader.exe"
} else {
    # 單一 exe：方便散布，但每次啟動都要把整包解壓到暫存目錄
    $env:MDREADER_ONEDIR = "0"
    $output = "dist\MarkdownReader.exe"
}
# 用 finally 還原環境變數：打包中斷時也不能把 MDREADER_ONEDIR 留在這個工作階段，
# 否則下一次不帶參數執行會沿用上一次的打包方式。
try {
    Invoke-Native -File "py" -Arguments @(
        "-3.13", "-m", "PyInstaller", "--noconfirm", "--clean", "build.spec"
    )
    $packed = $LASTEXITCODE
} finally {
    Remove-Item Env:\MDREADER_ONEDIR -ErrorAction SilentlyContinue
}
if ($packed -ne 0) {
    # 就地停：接著跑第 5 步只會把轉交器複製到一份不完整的 dist
    Write-Host "`nPyInstaller 失敗（結束碼 $packed），停止。" -ForegroundColor Red
    exit 1
}

# 5. 原生轉交器（有 g++ 才編；沒有就沿用舊產物或略過，主程式不受影響）
#    它讓「已開著時再雙擊 .md」從約 170 ms 降到約 10 ms，
#    檔案關聯建議指向它而不是本體（見 README「原生轉交器」一節）。
# 旗標的單一事實來源在 src_cpp\md_open\CMakeLists.txt，
# 工具鏈（g++ / windres / make 的路徑）釘在同目錄的 CMakePresets.json，
# VS Code 的 CMake「建置」按鈕與這裡走的是同一套定義。
$cmake = "C:\Program Files\CMake\bin\cmake.exe"
$gxx = "C:\msys64\ucrt64\bin\g++.exe"
if ((Test-Path $cmake) -and (Test-Path $gxx)) {
    Write-Host "`n[5/5] 編譯原生轉交器（CMake）..."
    Push-Location "src_cpp\md_open"
    Invoke-Native -File $cmake -Arguments @("--preset", "ucrt64-release") | Out-Null
    Pop-Location
    Invoke-Native -File $cmake -Arguments @("--build", "build\cmake\release")
    # 這裡一定要用 $LASTEXITCODE：$? 會被下一行的賦值重設成 $true，
    # 原本寫成 `$exe = ...` 之後才 `if ($?)`，等於完全沒檢查 cmake 的成敗。
    $built = $LASTEXITCODE
    $exe = "build\cmake\release\MarkdownOpen.exe"
    if (($built -eq 0) -and (Test-Path $exe)) {
        foreach ($dest in @("dist", "dist\MarkdownReader-onedir")) {
            if (Test-Path $dest) {
                Copy-Item $exe $dest -Force
            }
        }
        Write-Host "  MarkdownOpen.exe 已放到 dist（$([math]::Round((Get-Item $exe).Length / 1KB)) KB）"
    } else {
        Write-Host "  轉交器編譯失敗，沿用舊產物（不影響主程式）" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n[5/5] 缺 CMake 或 g++，略過轉交器編譯（不影響主程式）" -ForegroundColor Yellow
}

if (Test-Path $output) {
    # 資料夾版要算整個目錄；只報執行檔大小會嚴重低估（實際要連 _internal 一起帶走）
    if ($OneDir) {
        $bytes = (Get-ChildItem "dist\MarkdownReader-onedir" -Recurse -File | Measure-Object -Property Length -Sum).Sum
        $sizeText = "整個資料夾 {0:N0} MB" -f ($bytes / 1MB)
    } else {
        $sizeText = "{0:N1} MB" -f ((Get-Item $output).Length / 1MB)
    }
    Write-Host "`n打包完成：$output（$sizeText）" -ForegroundColor Green
    Write-Host "`n測試指令："
    Write-Host "  .\$output `"$PSScriptRoot\sample.md`""
    Write-Host "`n設定成 .md 的開啟程式（只寫 HKCU，可反安裝）："
    $assocTarget = $output
    $launcherPath = (Split-Path $output) + "\MarkdownOpen.exe"
    if (Test-Path $launcherPath) {
        # 有轉交器就建議指向它：已開著時再雙擊 .md 由約 170 ms 降到約 10 ms
        $assocTarget = $launcherPath
    }
    Write-Host "  py -3.13 tools\install_association.py --target `"$PSScriptRoot\$assocTarget`""
    # 明確回 0。沒有這一行時，腳本的結束碼會沿用最後一個外部程式的 $LASTEXITCODE
    # ——轉交器編譯失敗（那只是警告，主程式照樣打包好了）會讓整支腳本回非零，
    # 畫面上寫著「打包完成」卻回報失敗。
    exit 0
} else {
    Write-Host "`n打包失敗，找不到輸出檔案：$output" -ForegroundColor Red
    exit 1
}
