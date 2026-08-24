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

Write-Host "=== Markdown 閱讀器打包 ===" -ForegroundColor Cyan

# 1. 確認相依套件
Write-Host "`n[1/4] 檢查相依套件..."
py -3.13 -c "import PyQt6, PyQt6.QtSvg, markdown, pygments, PyInstaller; print('  相依套件齊全')"
if (-not $?) {
    Write-Host "  缺少套件，請先執行： py -3.13 -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

# 2. 產生圖示
Write-Host "`n[2/4] 產生應用程式圖示..."
if (-not (Test-Path "assets\app.ico")) {
    py -3.13 tools\make_icon.py
} else {
    Write-Host "  assets\app.ico 已存在，略過"
}

# 3. 清理
if ($Clean) {
    Write-Host "`n[3/4] 清理舊的建置產物..."
    foreach ($dir in @("build", "dist")) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
            Write-Host "  已刪除 $dir"
        }
    }
} else {
    Write-Host "`n[3/4] 略過清理（加上 -Clean 可強制清除）"
}

# 4. 打包
Write-Host "`n[4/4] 執行 PyInstaller..."
if ($OneDir) {
    # 資料夾版：不需解壓，實測啟動時間約為單一 exe 的一半
    $env:MDREADER_ONEDIR = "1"
    py -3.13 -m PyInstaller --noconfirm --clean build.spec
    Remove-Item Env:\MDREADER_ONEDIR
    $output = "dist\MarkdownReader-onedir\MarkdownReader.exe"
} else {
    # 單一 exe：方便散布，但每次啟動都要把整包解壓到暫存目錄
    $env:MDREADER_ONEDIR = "0"
    py -3.13 -m PyInstaller --noconfirm --clean build.spec
    Remove-Item Env:\MDREADER_ONEDIR
    $output = "dist\MarkdownReader.exe"
}

if (Test-Path $output) {
    $size = [math]::Round((Get-Item $output).Length / 1MB, 1)
    Write-Host "`n打包完成：$output（$size MB）" -ForegroundColor Green
    Write-Host "`n測試指令："
    Write-Host "  .\$output `"$PSScriptRoot\sample.md`""
    Write-Host "`n設定成 .md 的開啟程式（只寫 HKCU，可反安裝）："
    Write-Host "  py -3.13 tools\install_association.py --target `"$PSScriptRoot\$output`""
} else {
    Write-Host "`n打包失敗，找不到輸出檔案：$output" -ForegroundColor Red
    exit 1
}
