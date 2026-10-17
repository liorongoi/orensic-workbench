<#
.SYNOPSIS
  Click through the Lucida setup wizard and uninstaller and save a screenshot of every page.

      deploy\windows\capture-wizard.ps1 -Installer dist\Lucida-0.3.0-Setup.exe -OutDir wizard-screenshots

  Drives the real wizard with UI Automation (no keystrokes, so focus does not matter): accepts
  the licence, keeps the default choices, installs for the current user, then runs the
  uninstaller and answers "No" to deleting case data. Used by the "Windows installer" workflow
  to publish what the wizard looks like; it is not part of the install test.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$OutDir = "wizard-screenshots",
    [string]$Root = "C:\fw-wizard"
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Win {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
}
"@
[Win]::SetProcessDPIAware() | Out-Null
$A = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
$True_ = [System.Windows.Automation.Condition]::TrueCondition

if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
New-Item -ItemType Directory -Force $Root, $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$script:n = 0

function Snap($hwnd, $name) {
    $r = New-Object Win+RECT
    [Win]::GetWindowRect([IntPtr]$hwnd, [ref]$r) | Out-Null
    $w = $r.R - $r.L; $h = $r.B - $r.T
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    [Win]::PrintWindow([IntPtr]$hwnd, $hdc, 2) | Out-Null   # PW_RENDERFULLCONTENT
    $g.ReleaseHdc($hdc); $g.Dispose()
    $script:n++
    $file = Join-Path $OutDir ("{0:D2}_{1}.png" -f $script:n, $name)
    $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    Write-Host "saved $file"
}

function Find-Window($pattern, $seconds = 60) {
    for ($i = 0; $i -lt $seconds * 4; $i++) {
        foreach ($w in $A::RootElement.FindAll($Tree::Children, $True_)) {
            if ($w.Current.Name -match $pattern) { return $w }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "no window matching '$pattern' appeared"
}

function Controls($win, $type) {
    $c = New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $type)
    return $win.FindAll($Tree::Descendants, $c)
}

function Button($win, $name) {
    foreach ($b in Controls $win ([System.Windows.Automation.ControlType]::Button)) {
        if (($b.Current.Name -replace '&', '') -eq $name -and $b.Current.IsEnabled -and -not $b.Current.IsOffscreen) { return $b }
    }
    return $null
}

function Press($b) {
    # Post BM_CLICK rather than UI Automation's Invoke: Invoke waits for the button's handler,
    # and the uninstaller's "Yes" runs the whole uninstall (including our own question box)
    # inside it, which would deadlock this script.
    [Win]::PostMessage([IntPtr]$b.Current.NativeWindowHandle, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
}

function PageName($win) {
    # Name the file after the page heading: the first visible static text.
    foreach ($t in Controls $win ([System.Windows.Automation.ControlType]::Text)) {
        $s = ($t.Current.Name -replace '[^A-Za-z0-9 ]', '').Trim() -replace '\s+', '_'
        if ($s -and -not $t.Current.IsOffscreen) { return $s.Substring(0, [Math]::Min(40, $s.Length)) }
    }
    return "page"
}

Write-Host "== Setup wizard"
$setup = Start-Process -PassThru -FilePath (Resolve-Path $Installer) -ArgumentList "/CURRENTUSER", "/DIR=`"$Root\app`"", "/DATADIR=`"$Root\case data`"", "/LOG=`"$Root\wizard.log`""
$win = Find-Window '^Setup - Lucida'
$hwnd = $win.Current.NativeWindowHandle
for ($page = 0; $page -lt 20; $page++) {
    Start-Sleep -Milliseconds 1200
    # Wait out the installing page.
    for ($i = 0; $i -lt 600 -and -not ((Button $win 'Next >') -or (Button $win 'Install') -or (Button $win 'Finish')); $i++) { Start-Sleep -Milliseconds 500 }
    $accept = Controls $win ([System.Windows.Automation.ControlType]::RadioButton) | Where-Object { $_.Current.Name -replace '&', '' -match '^I accept' -and -not $_.Current.IsOffscreen }
    if ($accept) {
        $accept.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
        Start-Sleep -Milliseconds 300
    }
    Snap $hwnd (PageName $win)
    if ($b = Button $win 'Finish') {
        # Stop here rather than run the post-install options; installation is already complete.
        Stop-Process -Id $setup.Id -Force -ErrorAction SilentlyContinue
        Get-Process | Where-Object { $_.MainWindowTitle -match '^Setup - Lucida' } | Stop-Process -Force -ErrorAction SilentlyContinue
        break
    }
    if ($b = Button $win 'Install') { Press $b; continue }
    if ($b = Button $win 'Next >') { Press $b; continue }
}
if (-not (Test-Path "$Root\app\unins000.exe")) { Get-Content "$Root\wizard.log" -Tail 40; throw "the wizard did not install Lucida" }

Write-Host "== Uninstaller"
Start-Sleep -Seconds 2
Start-Process -FilePath "$Root\app\unins000.exe" | Out-Null
$confirm = Find-Window 'Uninstall'
Snap $confirm.Current.NativeWindowHandle "uninstall_confirm"
Press (Button $confirm 'Yes')
$keep = $null
for ($i = 0; $i -lt 120 -and -not $keep; $i++) {
    Start-Sleep -Milliseconds 500
    foreach ($w in $A::RootElement.FindAll($Tree::Children, $True_)) {
        if ($w.Current.Name -match 'Uninstall' -and (Controls $w ([System.Windows.Automation.ControlType]::Text) | Where-Object { $_.Current.Name -match 'keeps your case data' })) { $keep = $w }
    }
}
if (-not $keep) { throw "the keep-or-delete case data question did not appear" }
Snap $keep.Current.NativeWindowHandle "uninstall_case_data_question"
Press (Button $keep 'No')
$done = $null
for ($i = 0; $i -lt 240 -and -not $done; $i++) {
    Start-Sleep -Milliseconds 500
    foreach ($w in $A::RootElement.FindAll($Tree::Children, $True_)) {
        if ($w.Current.Name -match 'Uninstall' -and (Controls $w ([System.Windows.Automation.ControlType]::Text) | Where-Object { $_.Current.Name -match 'successfully removed' })) { $done = $w }
    }
}
if ($done) { Snap $done.Current.NativeWindowHandle "uninstall_done"; Press (Button $done 'OK') }
Start-Sleep -Seconds 2
if (-not (Test-Path "$Root\case data")) { throw "answering No removed the case data folder" }
Write-Host "Wizard screenshots in $OutDir"
