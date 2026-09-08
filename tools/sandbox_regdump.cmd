@echo off
rem Runs INSIDE the Sandboxie box (launched by sandbox_acceptance.ps1).
rem Dumps the registry view the installer touches to %1, a path under C:\ that lands in the
rem sandbox tree, so the host can read it back from <SandboxRoot>\drive\C\...
rem Keep this file ASCII: cmd.exe parses it with the console code page.
set "OUT=%~1"
> "%OUT%" echo === command
reg query "HKCU\Software\Classes\MarkdownReader.md\shell\open\command" /ve >> "%OUT%" 2>&1
echo === defaulticon >> "%OUT%"
reg query "HKCU\Software\Classes\MarkdownReader.md\DefaultIcon" /ve >> "%OUT%" 2>&1
echo === owp >> "%OUT%"
reg query "HKCU\Software\Classes\.md\OpenWithProgids" >> "%OUT%" 2>&1
echo === cap >> "%OUT%"
reg query "HKCU\Software\SamHo\MarkdownReader\Capabilities" >> "%OUT%" 2>&1
echo === regapps >> "%OUT%"
reg query "HKCU\Software\RegisteredApplications" /v MarkdownReader >> "%OUT%" 2>&1
echo === uninstall >> "%OUT%"
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{7B0D2E9E-5C0F-4B2A-9D3A-2F5C7C1E8A41}_is1" >> "%OUT%" 2>&1
echo === parent >> "%OUT%"
reg query "HKCU\Software\SamHo\MarkdownReader" >> "%OUT%" 2>&1
echo === end >> "%OUT%"
exit /b 0
