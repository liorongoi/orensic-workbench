<#
.SYNOPSIS
  Install the built installer silently on a clean Windows machine and check it works offline.

      deploy\windows\test-installer.ps1 -Installer dist\ForensicWorkbench-0.3.0-Setup.exe [-AllUsers]

  Installs, blocks the installed Python from the network with a firewall rule (needs an
  administrator shell), runs the acceptance self-test with nothing but Windows itself on PATH,
  starts the workbench and checks the API and UI answer, then uninstalls and checks the case
  data folder was kept.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [switch]$AllUsers,
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
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", $mode, "/TASKS=desktopicon",
    "/DIR=`"$App`"", "/DATADIR=`"$Data`"", "/PORT=$Port", "/LOG=`"$Root\install.log`"")
if ($p.ExitCode -ne 0) { Get-Content "$Root\install.log" -Tail 40; throw "installer exited with $($p.ExitCode)" }
Get-Content "$App\workbench.ini"
foreach ($f in "python\python.exe", "ffmpeg\ffmpeg.exe", "ffmpeg\ffprobe.exe", "frontend\dist\index.html",
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
foreach ($l in "Forensic Workbench.lnk", "Self-test.lnk", "Case data folder.lnk") {
    if (-not (Test-Path "$programs\Forensic Workbench\$l")) { throw "Start menu shortcut missing: $l" }
}
Get-Content "$App\BUILDINFO.txt"

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

    Step "Starting the workbench"
    $out = "$Root\server.out.txt"; $err = "$Root\server.err.txt"
    $server = Start-Process -PassThru -FilePath $py -ArgumentList "-B", "-X", "utf8", "`"$launcher`"", "run", "--no-browser" `
        -RedirectStandardOutput $out -RedirectStandardError $err -WorkingDirectory $App
    $base = "http://127.0.0.1:$Port"
    $health = $null
    for ($i = 0; $i -lt 120 -and -not $health; $i++) {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod "$base/api/health" -TimeoutSec 2 } catch { }
        if ($server.HasExited) { break }
    }
    if (-not $health) { Get-Content $out, $err -ErrorAction SilentlyContinue; throw "the workbench did not answer on $base" }
    $health | ConvertTo-Json -Depth 5
    $page = Invoke-WebRequest "$base/" -UseBasicParsing
    if ($page.Content -notmatch 'id="root"') { throw "the UI page was not served" }
    $models = Invoke-RestMethod "$base/api/models"
    Write-Host "UI served; $(@($models).Count) model entries listed"
    $case = Invoke-RestMethod -Method Post "$base/api/cases" -Headers @{ "X-Examiner" = "Installer test" } `
        -ContentType "application/json" -Body '{"name": "Installer test", "examiner": "Installer test"}'
    Write-Host "Created case $($case.id) through the API"
    if (-not (Test-Path "$Data\forensic.sqlite3")) { throw "case database not written to the case data folder" }

    Step "Launcher reuses a running workbench"
    & $py -B -X utf8 $launcher run --no-browser
    if ($LASTEXITCODE -ne 0) { throw "second launch did not detect the running workbench" }
    Stop-Process -Id $server.Id -Force
    Start-Sleep -Seconds 2
} finally {
    Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
}

Step "Uninstalling"
$u = Start-Process -Wait -PassThru -FilePath "$App\unins000.exe" -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
if ($u.ExitCode -ne 0) { throw "uninstaller exited with $($u.ExitCode)" }
Start-Sleep -Seconds 3
if (Test-Path "$App\python\python.exe") { throw "programs were not removed" }
if (-not (Test-Path "$Data\forensic.sqlite3")) { throw "uninstall removed case data" }
if (Test-Path "$programs\Forensic Workbench") { throw "Start menu folder was not removed" }
Write-Host "`nInstaller test passed ($mode)." -ForegroundColor Green
