<#
.SYNOPSIS
  Install the built installer silently on a clean Windows machine and check it works offline.

      deploy\windows\test-installer.ps1 -Installer dist\Lucida-0.4.2-Setup.exe [-AllUsers] [-LabNetwork]

  Installs, blocks the installed Python from the network with a firewall rule (needs an
  administrator shell), runs the acceptance self-test with nothing but Windows itself on PATH,
  starts Lucida and checks the API and UI answer, then uninstalls and checks the case
  data folder was kept (or, with -DeleteCaseData, that /DELETECASEDATA removed it, read-only
  evidence included). With -LabNetwork it chooses the lab-network option and checks that
  Lucida answers over HTTPS with a certificate that verifies against its local authority.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [switch]$AllUsers,
    [switch]$LabNetwork,
    [switch]$DeleteCaseData,
    [string]$Root = "C:\fw-test",
    [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

$App = "$Root\app"
$Data = "$Root\case data"
$mode = if ($AllUsers) { "/ALLUSERS" } else { "/CURRENTUSER" }
if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
New-Item -ItemType Directory $Root | Out-Null

Step "Installing silently ($mode)"
$p = Start-Process -Wait -PassThru -FilePath (Resolve-Path $Installer) -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", $mode, $(if ($LabNetwork) { "/TASKS=startmenu,desktopicon,labnetwork" } else { "/TASKS=startmenu,desktopicon" }),
    "/DIR=`"$App`"", "/DATADIR=`"$Data`"", "/PORT=$Port", "/LOG=`"$Root\install.log`"")
if ($p.ExitCode -ne 0) { Get-Content "$Root\install.log" -Tail 40; throw "installer exited with $($p.ExitCode)" }
Get-Content "$App\workbench.ini"
foreach ($k in "ai_device", "job_workers", "gpu_jobs") {
    if (-not (Select-String -Quiet -Path "$App\workbench.ini" -Pattern "^$k\s*=\s*\S")) { throw "workbench.ini has no $k (system check sizing)" }
}
if (-not (Select-String -Quiet -Path "$Root\install.log" -Pattern "System check: \d+ GB RAM")) { throw "the system check did not run" }
function Assert-FilesChecked($log) {
    # Setup re-hashes every installed file right after copying it.
    if (-not (Select-String -Quiet -Path $log -Pattern "Installed files check: PASS")) {
        Select-String -Path $log -Pattern "Installed files check" | ForEach-Object { $_.Line }
        throw "Setup's installed-files check did not pass"
    }
    Select-String -Path $log -Pattern "Installed files check" | ForEach-Object { $_.Line }
}
Assert-FilesChecked "$Root\install.log"
Select-String -Path "$Root\install.log" -Pattern "System check" | ForEach-Object { $_.Line }
foreach ($f in "python\python.exe", "python\pythonw.exe", "ffmpeg\ffmpeg.exe", "ffmpeg\ffprobe.exe", "frontend\dist\index.html",
               "launcher\workbench_launcher.py", "SHA256SUMS", "BUILDINFO.txt", "unins000.exe") {
    if (-not (Test-Path "$App\$f")) { throw "missing after install: $f" }
}
if (-not (Test-Path "$Data\import")) { throw "case data folder was not created" }
$acl = (Get-Acl $Data).Access
# A per-user install keeps whatever the parent folder grants; an all-users install drops inheritance.
if ($acl | Where-Object { $_.IdentityReference -match "\\Users$|Authenticated Users|Everyone" -and ($AllUsers -or -not $_.IsInherited) }) {
    $acl | Format-Table IdentityReference, FileSystemRights, IsInherited | Out-Host
    throw "other local users can reach the case data folder"
}
if ($AllUsers -and -not ($acl | Where-Object { $_.IdentityReference -match "\\Forensic Examiners$" -and -not $_.IsInherited })) {
    throw "an all-users install should share the case data folder with the Forensic Examiners group"
}
if ($AllUsers) { net localgroup "Forensic Examiners" | Out-Host }
$programs = if ($AllUsers) { "$env:ProgramData\Microsoft\Windows\Start Menu\Programs" } else { "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" }
foreach ($l in "Lucida.lnk", "Lucida in the browser.lnk", "Self-test.lnk", "Case data folder.lnk", "User manual.lnk", "Algorithm guide.lnk", "Amped vs Lucida.lnk") {
    if (-not (Test-Path "$programs\Lucida\$l")) { throw "Start menu shortcut missing: $l" }
}
Get-Content "$App\BUILDINFO.txt"
if ($LabNetwork) {
    if (-not (Select-String -Quiet -Path "$App\workbench.ini" -Pattern '^network\s*=\s*lab')) { throw "lab-network option not written to workbench.ini" }
    if ($AllUsers -and -not (Get-NetFirewallRule -DisplayName "Lucida" -ErrorAction SilentlyContinue)) {
        throw "an all-users lab-network install should open the workbench port in the firewall"
    }
}

Step "Blocking the installed Python from the network"
$rule = "fw-test block bundled python"
New-NetFirewallRule -DisplayName $rule -Direction Outbound -Action Block -Program "$App\python\python.exe" | Out-Null
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue
$py = "$App\python\python.exe"
$launcher = "$App\launcher\workbench_launcher.py"

try {
    Step "Acceptance self-test from the installation"
    & $py -B -X utf8 $launcher selftest
    if ($LASTEXITCODE -ne 0) { throw "self-test failed" }
    if (-not (Get-ChildItem "$Data\logs\selftest-*.txt")) { throw "self-test log not written to the case data folder" }

    Step "Command line (lucida.cmd): profile and verify"
    & "$App\lucida.cmd" profile
    if ($LASTEXITCODE -ne 0) { throw "lucida profile failed" }
    & "$App\lucida.cmd" verify
    if ($LASTEXITCODE -ne 0) { throw "lucida verify failed" }

    Step "Starting Lucida"
    $out = "$Root\server.out.txt"; $err = "$Root\server.err.txt"
    $server = Start-Process -PassThru -FilePath $py -ArgumentList "-B", "-X", "utf8", "`"$launcher`"", "run", "--no-browser" `
        -RedirectStandardOutput $out -RedirectStandardError $err -WorkingDirectory $App
    $base = "http://127.0.0.1:$Port"
    if ($LabNetwork) {
        # 127.0.0.1, not localhost: the workbench listens on IPv4, and a refused ::1 attempt costs Windows 2 s.
        $base = "https://127.0.0.1:$Port"
        # The checks below talk to the API; the certificate itself is verified with curl further down.
        $PSDefaultParameterValues["Invoke-RestMethod:SkipCertificateCheck"] = $true
        $PSDefaultParameterValues["Invoke-WebRequest:SkipCertificateCheck"] = $true
    }
    $health = $null
    for ($i = 0; $i -lt 120 -and -not $health; $i++) {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod "$base/api/health" -TimeoutSec 2 } catch { }
        if ($server.HasExited) { break }
    }
    if (-not $health) { Get-Content $out, $err -ErrorAction SilentlyContinue; throw "Lucida did not answer on $base" }
    $health | ConvertTo-Json -Depth 5
    if ($LabNetwork) {
        $ca = "$Data\tls\workbench-ca.crt"
        if (-not (Test-Path $ca)) { throw "no local certificate authority in the case data folder" }
        foreach ($name in "127.0.0.1", "localhost", $env:COMPUTERNAME.ToLower()) {
            # --ssl-no-revoke: an offline authority publishes no revocation list for Windows to check.
            & "$env:SystemRoot\System32\curl.exe" --fail --silent --show-error --ssl-no-revoke --cacert $ca "https://${name}:$Port/api/health" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "HTTPS to $name did not verify against the workbench authority" }
        }
        Write-Host "HTTPS verified for 127.0.0.1, localhost and $($env:COMPUTERNAME.ToLower())"
    }
    $page = Invoke-WebRequest "$base/" -UseBasicParsing
    if ($page.Content -notmatch 'id="root"') { throw "the UI page was not served" }
    # No sign-in: the API answers at once, as the built-in local examiner.
    $me = Invoke-RestMethod -Method Patch "$base/api/me" -ContentType "application/json" `
        -Body '{"full_name": "Installer test"}' -SessionVariable web
    if ($me.full_name -ne "Installer test") { throw "the local examiner could not be named" }
    $models = Invoke-RestMethod "$base/api/models" -WebSession $web
    Write-Host "UI served; $(@($models).Count) model entries listed"
    if (-not $LabNetwork) {
        # The "Lucida in Firefox" shortcut's Firefox must run from the installation and load the interface.
        $fx = "$App\firefox\firefox.exe"
        if (-not (Test-Path $fx)) { throw "the installation has no Firefox" }
        if (-not (Test-Path "$App\firefox\distribution\policies.json")) { throw "the bundled Firefox has no policies.json" }
        $shot = "$Root\firefox-ui.png"
        New-Item -ItemType Directory -Force "$Root\fxprofile" | Out-Null
        $fxp = Start-Process -PassThru -FilePath $fx -ArgumentList "-no-remote", "-headless", "-profile", "`"$Root\fxprofile`"", `
            "-window-size=1280,800", "-screenshot", "`"$shot`"", "$base/"
        if (-not $fxp.WaitForExit(180000)) { Stop-Process -Id $fxp.Id -Force -ErrorAction SilentlyContinue; throw "the bundled Firefox did not finish" }
        if (-not (Test-Path $shot) -or (Get-Item $shot).Length -lt 5000) { throw "the bundled Firefox did not render Lucida" }
        # The page is empty until the interface's script runs, which then writes its first text ("Loading...") at
        # the top left. Text means shades; an empty page is one colour.
        Add-Type -AssemblyName System.Drawing
        $bmp = [System.Drawing.Bitmap]::FromFile($shot)
        try {
            $shades = @{}
            for ($y = 0; $y -lt [Math]::Min(120, $bmp.Height); $y++) {
                for ($x = 0; $x -lt [Math]::Min(400, $bmp.Width); $x++) { $shades[$bmp.GetPixel($x, $y).ToArgb()] = 1 }
            }
        } finally { $bmp.Dispose() }
        if ($shades.Count -lt 5) { throw "the bundled Firefox shows an empty page: the interface's script did not run ($($shades.Count) colour(s))" }
        Write-Host "Bundled Firefox $((Get-Item $fx).VersionInfo.ProductVersion) rendered the interface ($((Get-Item $shot).Length) bytes)"
        if ($env:GITHUB_WORKSPACE) { Copy-Item $shot (Join-Path $env:GITHUB_WORKSPACE "firefox-ui.png") }
    }
    $case = Invoke-RestMethod -Method Post "$base/api/cases" -WebSession $web `
        -ContentType "application/json" -Body '{"name": "Installer test", "examiner": "Installer test"}'
    Write-Host "Created case $($case.id) through the API"
    $queue = Invoke-RestMethod "$base/api/jobs" -WebSession $web
    Write-Host "Job queue: $($queue.slots.cpu) CPU and $($queue.slots.gpu) GPU slots"
    if (-not (Test-Path "$Data\forensic.sqlite3")) { throw "case database not written to the case data folder" }

    Step "Launcher reuses a running workbench"
    & $py -B -X utf8 $launcher run --no-browser
    if ($LASTEXITCODE -ne 0) { throw "second launch did not detect the running workbench" }
    if (-not $LabNetwork) {
        # The desktop shortcut must not start a second server on the same case data.
        & $py -B -X utf8 $launcher desktop
        if ($LASTEXITCODE -ne 0) { throw "the desktop command did not reuse the running workbench" }
    }

    Step "Desktop window libraries"
    & $py -B -I -c "import webview, clr, forensic.desktop; print('pywebview and pythonnet load')"
    if ($LASTEXITCODE -ne 0) { throw "pywebview or pythonnet does not load in the installation" }
    $wv = Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" -ErrorAction SilentlyContinue
    Write-Host "WebView2 runtime: $(if ($wv) { $wv.pv } else { 'per-user or missing' })"

    Step "Installed files unchanged after Lucida has run"
    # Starting Lucida must not rewrite any installed file (for example a compiled .pyc).
    & $py -B -X utf8 $launcher verify
    if ($LASTEXITCODE -ne 0) { throw "running Lucida changed installed files" }

    Step "Installing again over the running Lucida"
    # Setup has to close the running server itself and replace the runtime, compiled caches included.
    $p = Start-Process -Wait -PassThru -FilePath (Resolve-Path $Installer) -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", $mode, "/DIR=`"$App`"", "/LOG=`"$Root\reinstall.log`"")
    if ($p.ExitCode -ne 0) { Get-Content "$Root\reinstall.log" -Tail 40; throw "installing over the running Lucida exited with $($p.ExitCode)" }
    Assert-FilesChecked "$Root\reinstall.log"
    if (-not (Select-String -Quiet -Path "$Root\reinstall.log" -Pattern "Lucida process\(es\) running")) { throw "Setup did not see the running Lucida" }
    if (-not $server.WaitForExit(10000)) { throw "Setup did not close the running Lucida" }
    if (-not (Test-Path "$Data\forensic.sqlite3")) { throw "reinstalling lost the case database" }
    # Setup sets the case data permissions again on an upgrade; the files already there must stay readable.
    & icacls.exe "$Data\forensic.sqlite3"
    try { [IO.File]::ReadAllBytes("$Data\forensic.sqlite3") | Out-Null } catch { throw "after reinstalling, the case database cannot be read: $_" }
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
} finally {
    Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
}

Step "Starting Lucida again, to uninstall it while it runs"
# A Lucida left running holds its programs and the case database open; the uninstaller has to close it.
# No console input: if Lucida stops, it must not wait for Enter.
Set-Content "$Root\no-input.txt" ""
$server = Start-Process -PassThru -FilePath $py -ArgumentList "-B", "-X", "utf8", "`"$launcher`"", "run", "--no-browser" `
    -RedirectStandardInput "$Root\no-input.txt" -RedirectStandardOutput "$Root\server2.out.txt" `
    -RedirectStandardError "$Root\server2.err.txt" -WorkingDirectory $App
$up = $false
for ($i = 0; $i -lt 120 -and -not $up; $i++) {
    Start-Sleep -Milliseconds 500
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); $up = $true } catch { }
    if ($server.HasExited) { break }
}
if (-not $up) { Get-Content "$Root\server2.out.txt", "$Root\server2.err.txt" -ErrorAction SilentlyContinue; throw "Lucida did not start again" }

Step "Uninstalling$(if ($DeleteCaseData) { ' and deleting case data' })"
$uargs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
if ($DeleteCaseData) {
    # Stored evidence is read-only; the uninstaller has to clear that to remove it.
    Set-Content "$Data\readonly-evidence-test.bin" "x"
    Set-ItemProperty "$Data\readonly-evidence-test.bin" -Name IsReadOnly -Value $true
    $uargs += "/DELETECASEDATA"
}
$u = Start-Process -PassThru -FilePath "$App\unins000.exe" -ArgumentList $uargs
if (-not $u.WaitForExit(600000)) { throw "the uninstaller did not finish in 10 minutes" }
if ($u.ExitCode -ne 0) { throw "uninstaller exited with $($u.ExitCode)" }
if (-not $server.WaitForExit(10000)) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue; throw "the uninstaller did not close the running Lucida" }
Start-Sleep -Seconds 3
if (Test-Path "$App\python\python.exe") { throw "programs were not removed" }
if ($DeleteCaseData) {
    if (Test-Path $Data) { Get-ChildItem -Recurse $Data | Select-Object -First 20 | Out-Host; throw "/DELETECASEDATA left the case data folder behind" }
} elseif (-not (Test-Path "$Data\forensic.sqlite3")) { throw "uninstall removed case data" }
if (Test-Path "$programs\Lucida") { throw "Start menu folder was not removed" }
if ($LabNetwork -and $AllUsers -and (Get-NetFirewallRule -DisplayName "Lucida" -ErrorAction SilentlyContinue)) {
    throw "uninstall left the firewall rule behind"
}
foreach ($d in "python", "launcher", "frontend", "ffmpeg") {
    if (Test-Path "$App\$d") { Get-ChildItem -Recurse "$App\$d" | Select-Object -First 10 | Out-Host; throw "uninstall left $d behind" }
}

Step "Installing again after uninstalling, over damaged leftovers"
# Leftover runtime files, including one that is truncated, must not survive into the new install.
New-Item -ItemType Directory -Force "$App\python\Lib\__pycache__", "$App\launcher" | Out-Null
Set-Content "$App\python\python.exe" "truncated"
Set-Content "$App\python\Lib\__pycache__\stale.cpython-312.pyc" "stale"
Set-Content "$App\launcher\workbench_launcher.py" "# old"
$p = Start-Process -Wait -PassThru -FilePath (Resolve-Path $Installer) -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", $mode, "/DIR=`"$App`"", "/DATADIR=`"$Data`"", "/LOG=`"$Root\install2.log`"")
if ($p.ExitCode -ne 0) { Get-Content "$Root\install2.log" -Tail 40; throw "installing after uninstall exited with $($p.ExitCode)" }
Assert-FilesChecked "$Root\install2.log"
if (Test-Path "$App\python\Lib\__pycache__\stale.cpython-312.pyc") { throw "Setup kept a leftover file from before" }
$u = Start-Process -Wait -PassThru -FilePath "$App\unins000.exe" -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
if ($u.ExitCode -ne 0) { throw "second uninstall exited with $($u.ExitCode)" }
Step "Refusing shared case data folders"
# Setup replaces the permissions of the case data folder, so a folder other programs use must be refused
# before anything is installed: a user profile, a whole drive, or a folder that already holds other files.
$n = 0
foreach ($bad in $env:USERPROFILE, "$env:SystemDrive\", $Root) {
    $n++; $log = "$Root\refuse-$n.log"
    $p = Start-Process -PassThru -FilePath (Resolve-Path $Installer) -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", $mode, "/DIR=`"$App`"", "/DATADIR=`"$($bad.TrimEnd('\'))`"", "/LOG=`"$log`"")
    if (-not $p.WaitForExit(120000)) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Get-Content $log -Tail 20 -ErrorAction SilentlyContinue; throw "Setup with $bad as the case data folder did not finish"
    }
    if ($p.ExitCode -eq 0) { throw "Setup accepted $bad as the case data folder" }
    if (-not (Select-String -Quiet -Path $log -Pattern "Case data folder refused")) { Get-Content $log -Tail 20; throw "Setup failed for $bad, but not by refusing the folder" }
    if (Test-Path "$App\python\python.exe") { throw "Setup installed Lucida with $bad as the case data folder" }
    Write-Host "Refused $bad"
}

Write-Host "`nInstaller test passed ($mode$(if ($LabNetwork) { ', lab network' }))." -ForegroundColor Green
