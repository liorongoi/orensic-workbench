<#
.SYNOPSIS
  Click through the Lucida setup wizard and uninstaller and save a screenshot of every page.

      deploy\windows\capture-wizard.ps1 -Installer dist\Lucida-0.3.0-Setup.exe -OutDir wizard-screenshots

  Drives the real wizard with plain window messages: finds each button by its caption and posts
  the click to its parent (WM_COMMAND / BN_CLICKED). That needs no keyboard focus and never waits
  on the button's handler, so the uninstaller's modal questions cannot deadlock it. Accepts the
  licence, keeps the default choices, installs for the current user, then runs the uninstaller
  and answers "No" to deleting case data. Used by the "Windows installer" workflow to show what
  the wizard looks like; it is not part of the install test.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$OutDir = "wizard-screenshots",
    [string]$Root = "C:\fw-wizard"
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class Win {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
    [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();

    public static string Text(IntPtr h) { var s = new StringBuilder(1024); GetWindowText(h, s, 1024); return s.ToString(); }
    public static string Class(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, 256); return s.ToString(); }

    public static List<IntPtr> TopWindows() {
        var l = new List<IntPtr>();
        EnumWindows((h, p) => { if (IsWindowVisible(h)) l.Add(h); return true; }, IntPtr.Zero);
        return l;
    }
    public static List<IntPtr> Children(IntPtr parent) {
        var l = new List<IntPtr>();
        EnumChildWindows(parent, (h, p) => { l.Add(h); return true; }, IntPtr.Zero);
        return l;
    }
    // Post the click the way a real one arrives: WM_COMMAND(BN_CLICKED, id) to the parent.
    public static void Click(IntPtr button) {
        IntPtr parent = GetParent(button);
        int id = GetDlgCtrlID(button);
        PostMessage(parent, 0x0111, (IntPtr)(id & 0xFFFF), button);
    }
}
"@
[Win]::SetProcessDPIAware() | Out-Null

if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
New-Item -ItemType Directory -Force $Root, $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$script:n = 0

function Snap([IntPtr]$hwnd, $name) {
    $r = New-Object Win+RECT
    [Win]::GetWindowRect($hwnd, [ref]$r) | Out-Null
    $bmp = New-Object System.Drawing.Bitmap ($r.R - $r.L), ($r.B - $r.T)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    [Win]::PrintWindow($hwnd, $hdc, 2) | Out-Null   # PW_RENDERFULLCONTENT
    $g.ReleaseHdc($hdc); $g.Dispose()
    $script:n++
    $file = Join-Path $OutDir ("{0:D2}_{1}.png" -f $script:n, $name)
    $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    Write-Host "saved $file"
    return $file
}

function Clean($s) { ($s -replace '[&>]', '').Trim() }

function Find-Window($pattern, $pids, $seconds = 60, [scriptblock]$where = { $true }) {
    for ($i = 0; $i -lt $seconds * 4; $i++) {
        foreach ($h in [Win]::TopWindows()) {
            $p = 0; [Win]::GetWindowThreadProcessId($h, [ref]$p) | Out-Null
            if ((-not $pids -or $pids -contains $p) -and [Win]::Text($h) -match $pattern -and (& $where $h)) { return $h }
        }
        Start-Sleep -Milliseconds 250
    }
    return [IntPtr]::Zero
}

function Control($win, $caption) {
    foreach ($c in [Win]::Children($win)) {
        if ((Clean ([Win]::Text($c))) -eq $caption -and [Win]::IsWindowVisible($c) -and [Win]::IsWindowEnabled($c)) { return $c }
    }
    return [IntPtr]::Zero
}

function HasText($win, $pattern) {
    foreach ($c in [Win]::Children($win)) { if ([Win]::Text($c) -match $pattern) { return $true } }
    return $false
}

function Setup-Pids {
    @(Get-Process | Where-Object { $_.ProcessName -like "Lucida*Setup*" -or $_.ProcessName -like "_iu*" -or $_.ProcessName -like "unins*" } | ForEach-Object { [uint32]$_.Id })
}

Write-Host "== Setup wizard"
$setup = Start-Process -PassThru -FilePath (Resolve-Path $Installer) -ArgumentList "/CURRENTUSER", "/DIR=`"$Root\app`"", "/DATADIR=`"$Root\case data`"", "/LOG=`"$Root\wizard.log`""
$win = Find-Window '^Setup - Lucida' $null 90
if ($win -eq [IntPtr]::Zero) { throw "the setup wizard window did not appear" }
$lastHash = $null; $same = 0
for ($page = 0; $page -lt 25; $page++) {
    # Wait for a usable button (the installing page has none until it finishes).
    $next = $install = $finish = [IntPtr]::Zero
    for ($i = 0; $i -lt 900; $i++) {
        $next = Control $win 'Next'; $install = Control $win 'Install'; $finish = Control $win 'Finish'
        if ($next -ne [IntPtr]::Zero -or $install -ne [IntPtr]::Zero -or $finish -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 400
    }
    $accept = Control $win 'I accept the agreement'
    if ($accept -ne [IntPtr]::Zero) {
        [Win]::Click($accept); Start-Sleep -Milliseconds 500
        $next = Control $win 'Next'
    }
    Start-Sleep -Milliseconds 700
    $file = Snap $win "wizard"
    $hash = (Get-FileHash $file).Hash
    if ($hash -eq $lastHash) { Remove-Item $file; $script:n--; $same++ } else { $same = 0 }
    if ($same -ge 3) {
        $caps = ([Win]::Children($win) | ForEach-Object { "[{0}] {1}" -f [Win]::Class($_), [Win]::Text($_) }) -join "; "
        throw "the wizard did not move on from page $($script:n). Controls: $caps"
    }
    $lastHash = $hash
    if ($finish -ne [IntPtr]::Zero) {
        # Stop here rather than run the post-install options; installation is already complete.
        Stop-Process -Id $setup.Id -Force -ErrorAction SilentlyContinue
        Get-Process | Where-Object { $_.ProcessName -like "Lucida*Setup*" } | Stop-Process -Force -ErrorAction SilentlyContinue
        break
    }
    if ($install -ne [IntPtr]::Zero) { [Win]::Click($install) } elseif ($next -ne [IntPtr]::Zero) { [Win]::Click($next) }
}
if (-not (Test-Path "$Root\app\unins000.exe")) {
    Get-Content "$Root\wizard.log" -Tail 40 -ErrorAction SilentlyContinue
    throw "the wizard did not install Lucida"
}

Write-Host "== Uninstaller"
Start-Sleep -Seconds 2
Start-Process -FilePath "$Root\app\unins000.exe" | Out-Null
$confirm = Find-Window 'Uninstall' $null 60 { param($h) HasText $h 'completely remove' }
if ($confirm -eq [IntPtr]::Zero) { throw "the uninstall confirmation did not appear" }
Snap $confirm "uninstall_confirm" | Out-Null
[Win]::Click((Control $confirm 'Yes'))
$keep = Find-Window 'Uninstall' $null 60 { param($h) HasText $h 'keeps your case data' }
if ($keep -eq [IntPtr]::Zero) { throw "the keep-or-delete case data question did not appear" }
Snap $keep "uninstall_case_data_question" | Out-Null
[Win]::Click((Control $keep 'No'))
$done = Find-Window 'Uninstall' $null 120 { param($h) HasText $h 'successfully removed' }
if ($done -ne [IntPtr]::Zero) { Snap $done "uninstall_done" | Out-Null; [Win]::Click((Control $done 'OK')) }
Start-Sleep -Seconds 2
if (-not (Test-Path "$Root\case data")) { throw "answering No removed the case data folder" }
Write-Host "Wizard screenshots in $OutDir"
