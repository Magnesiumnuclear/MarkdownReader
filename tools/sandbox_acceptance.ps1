# 安裝檔的沙盒驗收：用「真正的安裝檔」在 Sandboxie-Plus 的沙盒裡走完整流程。
#
#   .\tools\sandbox_acceptance.ps1                    # 用 DefaultBox，安裝檔依 app\__init__.py 的版本找
#   .\tools\sandbox_acceptance.ps1 -Box MyBox         # 換一個沙盒
#   .\tools\sandbox_acceptance.ps1 -Setup dist\X.exe  # 指定安裝檔
#
# 為什麼要有這支：tools\smoke_test.py 的「安裝檔」區塊為了不碰開發機的真關聯，把登錄根改到
# 測試子樹、AppId 也換掉——它驗的是「機制」。這支驗的是「產品」：真正的登錄根、真正的 AppId、
# 真正的安裝目錄，而且會靠檔案關聯把安裝版本體開起來、在本體執行中做升級、再反安裝。
# 所有寫入都留在沙盒裡，跑完清空，主機什麼都不會被改。
#
# 流程與斷言：
#   0. 清空沙盒
#   1. 靜默安裝 → 檔案齊全；command / DefaultIcon 指向安裝目錄的轉交器；.md 的 OpenWithProgids
#      有我們的值而且使用者原有的登記還在；Capabilities、RegisteredApplications、Uninstall 項齊全
#   2. 在沙盒裡 ShellExecute sample.md → 真的起來的是安裝版的 MarkdownReader.exe
#   3. 本體執行中再裝一次（/CLOSEAPPLICATIONS）→ Restart Manager 關掉本體、_internal 被整包清掉
#      （事先放的金絲雀消失）、檔案重鋪、關聯完好
#   4. 靜默反安裝 → 檔案清空；只刪我們的（ProgID、我們那個 OpenWithProgids 值、Capabilities、
#      RegisteredApplications、Uninstall 項），使用者其他程式的登記與 QSettings 都保留
#   5. 全過才清空沙盒；有紅就把沙盒留著給人看，dump 另外複製到 %TEMP%\mdr-sandbox-acceptance\
#
# Sandboxie 的兩個坑（都實測過）：
#   * Start.exe /wait 連續呼叫會每隔一次回 0x40010004 且目標「完全沒執行」（連跑 5 次：
#     3、砍、3、砍、3；每次隔 3 秒則 6/6 正常；失敗後立刻重試 100% 成功）。被砍的那次什麼都沒做，
#     所以重試對安裝器、反安裝器、探針都安全——Sbx() 啟動前緩衝 1.5 秒＋遇該碼重試。
#   * 沙盒內的程式不能用 C:\Sandbox\... 這種主機端路徑啟動（會被擋），要用沙盒內部看到的路徑；
#     delete_sandbox 前要先 /terminate，否則會跳 SBIE2203 對話框卡住。
param(
    [string]$Box = "DefaultBox",
    [string]$SandboxieDir = "",
    [string]$SandboxRoot = "",
    [string]$Setup = ""
)
$ErrorActionPreference = "Continue"
$repo = Split-Path $PSScriptRoot -Parent

# --- 版本與安裝檔（單一來源 app\__init__.py，與 build.ps1 -Installer 同一條規則）------------
$initText = Get-Content (Join-Path $repo "app\__init__.py") -Raw -Encoding UTF8
if ($initText -notmatch '(?m)^__version__\s*=\s*"(\d+\.\d+\.\d+)"') {
    Write-Host "app\__init__.py 找不到純數字三段的 __version__" -ForegroundColor Red
    exit 1
}
$version = $Matches[1]
if (-not $Setup) { $Setup = Join-Path $repo "dist\MarkdownReader-Setup-$version.exe" }
if (-not (Test-Path $Setup)) {
    Write-Host "找不到安裝檔 $Setup，先跑 .\build.ps1 -Installer" -ForegroundColor Red
    exit 1
}

# --- Sandboxie 位置：參數 > 開始功能表捷徑 > Program Files ------------------------------
if (-not $SandboxieDir) {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($lnk in @(
        "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Sandboxie-Plus\Sandboxie-Plus.lnk",
        "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Sandboxie-Plus\Sandboxie-Plus.lnk"
    )) {
        if (Test-Path $lnk) { $SandboxieDir = Split-Path $shell.CreateShortcut($lnk).TargetPath; break }
    }
    if (-not $SandboxieDir) { $SandboxieDir = "$env:ProgramFiles\Sandboxie-Plus" }
}
$start = Join-Path $SandboxieDir "Start.exe"
if (-not (Test-Path $start)) {
    Write-Host "找不到 Sandboxie 的 Start.exe（$SandboxieDir）。用 -SandboxieDir 指定安裝目錄。" -ForegroundColor Red
    exit 1
}
if (-not $SandboxRoot) { $SandboxRoot = "C:\Sandbox\$env:USERNAME\$Box" }
$sample = Join-Path $repo "sample.md"
$regdump = Join-Path $PSScriptRoot "sandbox_regdump.cmd"
$keep = Join-Path $env:TEMP "mdr-sandbox-acceptance"
New-Item -ItemType Directory -Force $keep | Out-Null
# 沙盒內看到的安裝路徑與主機的環境變數相同（Sandboxie 只是把寫入轉向）
$installDirInside = Join-Path $env:LOCALAPPDATA "Programs\MarkdownReader"

$script:results = @()
$script:SBX_KILLED = 1073807364   # 0x40010004：見檔頭

function Check($name, $ok, $detail) {
    $script:results += ,@($name, [bool]$ok, "$detail")
    $tag = if ($ok) { "PASS" } else { "FAIL" }
    Write-Host ("[{0}] {1}  {2}" -f $tag, $name, $detail)
}
function Sbx([string[]]$cmdArgs, [int]$timeoutSec = 300) {
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        Start-Sleep -Milliseconds 1500
        $p = Start-Process -FilePath $start -ArgumentList (@("/box:$Box", "/wait") + $cmdArgs) -PassThru
        if (-not $p.WaitForExit($timeoutSec * 1000)) { try { $p.Kill() } catch {}; return -1 }
        if ($p.ExitCode -ne $script:SBX_KILLED) { return $p.ExitCode }
        Write-Host ("   （Start.exe /wait 第 {0} 次回 0x40010004：目標沒跑，重試）" -f $attempt)
    }
    return $script:SBX_KILLED
}
function SbxTerminate() {
    $t = Start-Process -FilePath $start -ArgumentList "/box:$Box", "/terminate" -PassThru
    $t.WaitForExit(15000) | Out-Null
}
function SbxDelete() {
    SbxTerminate
    Start-Sleep -Seconds 2
    for ($i = 0; $i -lt 2; $i++) {
        $d = Start-Process -FilePath $start -ArgumentList "/box:$Box", "delete_sandbox_silent" -PassThru
        if ($d.WaitForExit(30000)) { break }
        # 服務忙碌時會跳 SBIE2203 對話框：關掉再試一次
        Get-Process -Name Start -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force }
        Start-Sleep -Seconds 2
    }
    return (-not (Test-Path $SandboxRoot))
}
function RegDump($tag) {
    $inside = "C:\mdr_sbx\$tag.txt"
    $null = Sbx @("cmd.exe", "/c", "mkdir C:\mdr_sbx 2>nul & `"$regdump`" $inside") 60
    $hostPath = Join-Path $SandboxRoot "drive\C\mdr_sbx\$tag.txt"
    for ($i = 0; $i -lt 20; $i++) { if (Test-Path $hostPath) { break }; Start-Sleep -Milliseconds 250 }
    if (Test-Path $hostPath) {
        Copy-Item $hostPath (Join-Path $keep "$tag.txt") -Force
        return (Get-Content $hostPath -Raw)
    }
    return ""
}
function Section($dump, $name) {
    # cmd 的 `echo === name >> file` 會在標記後留一個空白，要容許
    $m = [regex]::Match($dump, "(?s)=== $name[ \t]*\r?\n(.*?)(?=\r?\n=== )")
    if ($m.Success) { return $m.Groups[1].Value }
    return ""
}
function Absent($section, $pattern) {
    # 否定式檢查不能因為區段沒解析到就通過
    return (($section.Trim().Length -gt 0) -and (-not ($section -match $pattern)))
}
function SandboxedProcesses() {
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -like "$SandboxRoot\*" } catch { $false }
    } | ForEach-Object { "{0}({1})" -f $_.ProcessName, $_.Id }
}

# 主機上 .md 的 OpenWithProgids 原有哪些別的程式：反安裝後它們必須原封不動
$otherApps = @()
$hostOwp = & reg query "HKCU\Software\Classes\.md\OpenWithProgids" 2>$null
foreach ($line in @($hostOwp)) {
    if ($line -match '^\s+(\S+)\s+REG_') { if ($Matches[1] -ne "MarkdownReader.md") { $otherApps += $Matches[1] } }
}

Write-Host "=== Sandboxie 驗收：$Setup → 沙盒 $Box ===" -ForegroundColor Cyan
Write-Host "== 0. 清空沙盒 =="
Check "開跑前沙盒是空的" (SbxDelete) $SandboxRoot

Write-Host "== 1. 靜默安裝（真正的安裝檔、真正的登錄根）=="
$rc = Sbx @("`"$Setup`"", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") 300
Check "靜默安裝結束碼 0" ($rc -eq 0) "rc=$rc"
$launcher = Get-ChildItem $SandboxRoot -Recurse -Filter "MarkdownOpen.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
$appDirHost = if ($launcher) { $launcher.DirectoryName } else { "" }
Check "裝進沙盒內的 LOCALAPPDATA\Programs\MarkdownReader" ($appDirHost -like "*\AppData\Local\Programs\MarkdownReader") $appDirHost
Check "本體、轉交器、反安裝器、_internal 都在" (($appDirHost) -and (Test-Path "$appDirHost\MarkdownReader.exe") -and (Test-Path "$appDirHost\unins000.exe") -and (Test-Path "$appDirHost\_internal\base_library.zip")) ""
$d1 = RegDump "after_install"
$escapedDir = [regex]::Escape($installDirInside)
Check "關聯 command 指向安裝目錄的轉交器" ((Section $d1 "command") -match "$escapedDir\\MarkdownOpen\.exe`" `"%1`"") ((Section $d1 "command").Trim() -replace '\s+', ' ')
Check "DefaultIcon 指向安裝目錄的轉交器" ((Section $d1 "defaulticon") -match "$escapedDir\\MarkdownOpen\.exe,0") ""
Check ".md 的 OpenWithProgids 有我們的值" ((Section $d1 "owp") -match "MarkdownReader\.md") ""
if ($otherApps.Count -gt 0) {
    Check "使用者原有的其他登記還在（我們只加一個值）" (($otherApps | Where-Object { (Section $d1 "owp") -match [regex]::Escape($_) }).Count -eq $otherApps.Count) ($otherApps -join ",")
} else {
    Write-Host "   （主機的 .md 沒有其他程式的登記，略過「原有登記倖存」的檢查）"
}
Check "Capabilities 齊全（ApplicationName）" ((Section $d1 "cap") -match "ApplicationName\s+REG_SZ\s+Markdown Reader") ""
Check "RegisteredApplications 的值存在" ((Section $d1 "regapps") -match "Software\\SamHo\\MarkdownReader\\Capabilities") ""
Check "Uninstall 項存在且 DisplayVersion 正確" ((Section $d1 "uninstall") -match "DisplayVersion\s+REG_SZ\s+$([regex]::Escape($version))") ""

Write-Host "== 2. 在沙盒裡靠檔案關聯開 sample.md =="
Get-Process -Name MarkdownReader -ErrorAction SilentlyContinue | Stop-Process -Force
$rcStart = Sbx @("cmd.exe", "/c", "start `"`" `"$sample`"") 60
$appProc = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    $appProc = Get-Process -Name MarkdownReader -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($appProc) { break }
}
$appStarted = ($null -ne $appProc)
Check "雙擊（ShellExecute）真的啟動了 MarkdownReader.exe" $appStarted $(if ($appProc) { "pid=" + $appProc.Id } else { "start rc=$rcStart" })
$exePath = ""
if ($appProc) { try { $exePath = $appProc.Path } catch { $exePath = "" } }
Check "起來的是安裝版那份，不是 dist" ($exePath -like "*Programs\MarkdownReader\MarkdownReader.exe") $exePath
Start-Sleep -Seconds 2

Write-Host "== 3. 本體執行中升級（同 AppId，/CLOSEAPPLICATIONS）=="
$canary = Join-Path $appDirHost "_internal\canary_from_old_version.txt"
if ($appDirHost) { Set-Content -Path $canary -Value "old" }
$rc2 = Sbx @("`"$Setup`"", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS") 300
Check "升級安裝結束碼 0" ($rc2 -eq 0) "rc=$rc2"
Start-Sleep -Seconds 2
$stillRunning = Get-Process -Name MarkdownReader -ErrorAction SilentlyContinue
Check "執行中的本體被安裝器關掉（Restart Manager）" ($appStarted -and ($null -eq $stillRunning)) $(if ($appStarted) { "" } else { "前置不成立：本體根本沒起來" })
Check "_internal 在重鋪前被整包清掉（金絲雀消失）" (-not (Test-Path $canary)) ""
Check "升級後檔案齊全" ((Test-Path "$appDirHost\MarkdownReader.exe") -and (Test-Path "$appDirHost\MarkdownOpen.exe") -and (Test-Path "$appDirHost\_internal\base_library.zip")) ""
$d2 = RegDump "after_upgrade"
Check "升級後關聯仍指向安裝目錄" ((Section $d2 "command") -match "$escapedDir\\MarkdownOpen\.exe") ""

Write-Host "== 4. 靜默反安裝（預設保留設定）=="
$rc3 = Sbx @("`"$(Join-Path $installDirInside 'unins000.exe')`"", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") 300
Check "靜默反安裝結束碼 0" ($rc3 -eq 0) ("rc=$rc3 (0x{0:X})" -f $rc3)
$gone = $false
for ($i = 0; $i -lt 36; $i++) {
    if (-not (Test-Path "$appDirHost\MarkdownReader.exe")) { $gone = $true; break }
    Start-Sleep -Milliseconds 2500
}
Check "檔案清空" ($gone -and (-not (Test-Path "$appDirHost\_internal"))) ((SandboxedProcesses) -join ",")
$d3 = RegDump "after_uninstall"
Check "關聯 command 不再指向安裝目錄" (-not ((Section $d3 "command") -match "$escapedDir\\MarkdownOpen\.exe")) ((Section $d3 "command").Trim() -replace '\s+', ' ')
Check "DefaultIcon 隨 ProgID 一起刪" (Absent (Section $d3 "defaulticon") "MarkdownOpen") ""
$owp3 = Section $d3 "owp"
Check "我們的 OpenWithProgids 值已刪" (Absent $owp3 "MarkdownReader\.md") ""
if ($otherApps.Count -gt 0) {
    Check "使用者原有的其他登記倖存" (($otherApps | Where-Object { $owp3 -match [regex]::Escape($_) }).Count -eq $otherApps.Count) ($otherApps -join ",")
}
Check "Capabilities 已刪" (Absent (Section $d3 "cap") "ApplicationName") ""
Check "RegisteredApplications 的值已刪" (Absent (Section $d3 "regapps") "Capabilities") ""
Check "Uninstall 項已刪" (Absent (Section $d3 "uninstall") "DisplayVersion") ""
Check "設定的父鍵保留（QSettings 沒被清）" ((Section $d3 "parent") -match "HKEY_CURRENT_USER") ((Section $d3 "parent").Trim() -replace '\s+', ' ')

Write-Host "== 5. 清空沙盒（有紅就保留給人看）=="
$failsSoFar = @($script:results | Where-Object { -not $_[1] })
if ($failsSoFar.Count -eq 0) {
    Check "沙盒已清空" (SbxDelete) $SandboxRoot
} else {
    SbxTerminate
    Write-Host ("   {0} 項失敗：沙盒保留在 {1}，dump 在 {2}" -f $failsSoFar.Count, $SandboxRoot, $keep)
}

$fails = @($script:results | Where-Object { -not $_[1] })
Write-Host ("`n===== {0} / {1} 通過 =====" -f ($script:results.Count - $fails.Count), $script:results.Count)
foreach ($f in $fails) { Write-Host ("  FAIL: {0}  {1}" -f $f[0], $f[2]) }
if ($fails.Count -gt 0) { exit 1 }
exit 0
