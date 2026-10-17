<#
.SYNOPSIS
  Build the offline Windows installer for Lucida.

.DESCRIPTION
  Run on a connected Windows x64 machine (or the "Windows installer" GitHub Actions workflow)
  with Python 3.12 and Node.js 22 on PATH:

      powershell -ExecutionPolicy Bypass -File deploy\windows\build-installer.ps1

  It assembles a payload with a private embeddable Python, every Python dependency, FFmpeg,
  the Visual C++ runtime, the bundled AI models and the built UI, hashes every file into
  SHA256SUMS, runs the acceptance self-test against the payload, and compiles
  dist\Lucida-<version>-Setup.exe with Inno Setup. The resulting installer needs
  no internet connection on the target machine.
#>
param(
    [string]$BuildPython = "python",
    [string]$OutDir = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Pinned third-party inputs. Each download is checked against its SHA-256 when one is pinned
# here, and every hash is recorded in BUILDINFO.txt inside the installation.
$PythonVersion = "3.12.10"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$PythonSha256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
$FfmpegVersion = "7.1.1"
$FfmpegUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVersion/ffmpeg-$FfmpegVersion-essentials_build.zip"
$FfmpegSha256 = "04861d3339c5ebe38b56c19a15cf2c0cc97f5de4fa8910e4d47e5e6404e4a2d4"
$VcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
# WebView2 Evergreen Standalone Installer (x64), for the desktop window on Windows 10 machines
# that lack the runtime. Setup copies it to a temporary folder and runs it only when needed.
$WebView2Url = "https://go.microsoft.com/fwlink/?linkid=2124701"

$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Version = (Select-String -Path "$Repo\backend\pyproject.toml" -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
if (-not $OutDir) { $OutDir = "$Repo\dist" }
$Work = "$Repo\build\windows"
$Payload = "$Work\payload"
$Downloads = "$Work\downloads"
$Py = "$Payload\python"
$Site = "$Py\Lib\site-packages"

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

function Get-Pinned($url, $dest, $sha) {
    if (-not (Test-Path $dest)) {
        Write-Host "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    }
    $actual = (Get-FileHash -Algorithm SHA256 $dest).Hash.ToLower()
    if ($sha -and $actual -ne $sha.ToLower()) { throw "$dest has SHA-256 $actual, expected $sha" }
    Write-Host "  sha256 $actual"
    return $actual
}

Step "Lucida ${Version}: checking the build tools"
$pyVer = & $BuildPython -c "import sys, struct; print('%d.%d' % sys.version_info[:2], struct.calcsize('P') * 8)"
Check "Python"
if ($pyVer -ne "3.12 64") { throw "Build Python must be 64-bit 3.12 to match the bundled runtime (found $pyVer)" }
node --version | Out-Null; Check "Node.js"

$Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    Write-Host "Inno Setup 6 not found; installing it with Chocolatey"
    choco install innosetup -y --no-progress | Out-Host; Check "choco install innosetup"
    $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
Write-Host "Inno Setup: $Iscc"

if (Test-Path $Payload) { Remove-Item -Recurse -Force $Payload }
New-Item -ItemType Directory -Force $Payload, $Downloads, $OutDir | Out-Null

Step "Python $PythonVersion (embeddable)"
$pyZip = "$Downloads\python-$PythonVersion-embed-amd64.zip"
$pySha = Get-Pinned $PythonUrl $pyZip $PythonSha256
Expand-Archive $pyZip -DestinationPath $Py
$pth = Get-ChildItem "$Py\python*._pth" | Select-Object -First 1
$zipName = (Get-ChildItem "$Py\python3*.zip" | Select-Object -First 1).Name
Set-Content -Encoding ascii -Path $pth.FullName -Value @($zipName, ".", "Lib\site-packages", "import site")
New-Item -ItemType Directory -Force $Site | Out-Null

Step "Visual C++ runtime (next to python.exe, and the redistributable for all-users installs)"
$vcDlls = "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
          "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll", "vcomp140.dll"
foreach ($d in $vcDlls) {
    $src = "$env:SystemRoot\System32\$d"
    if (Test-Path $src) { Copy-Item -Force $src $Py } else { Write-Warning "$d not found in System32" }
}
New-Item -ItemType Directory -Force "$Payload\vcredist" | Out-Null
$vcSha = Get-Pinned $VcRedistUrl "$Payload\vcredist\vc_redist.x64.exe" ""
# aka.ms always serves the current release, so its hash cannot be pinned: require Microsoft's
# Authenticode signature instead, since an all-users install runs it as administrator.
$sig = Get-AuthenticodeSignature "$Payload\vcredist\vc_redist.x64.exe"
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
    throw "vc_redist.x64.exe is not validly signed by Microsoft ($($sig.Status): $($sig.SignerCertificate.Subject))"
}
Write-Host "  signed by $($sig.SignerCertificate.Subject)"

Step "WebView2 runtime installer (desktop window on Windows 10)"
$wv2Dir = "$Work\webview2"
New-Item -ItemType Directory -Force $wv2Dir | Out-Null
$wv2 = "$wv2Dir\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
$wv2Sha = Get-Pinned $WebView2Url $wv2 ""
# The Evergreen installer always serves the current release, so require Microsoft's signature.
$sig = Get-AuthenticodeSignature $wv2
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
    throw "the WebView2 installer is not validly signed by Microsoft ($($sig.Status): $($sig.SignerCertificate.Subject))"
}
Write-Host "  signed by $($sig.SignerCertificate.Subject)"

Step "Python dependencies (wheels for Windows x64, CPython 3.12)"
$wheels = "$Work\wheels"
if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
& $BuildPython -m pip wheel --no-deps --wheel-dir $wheels "$Repo\backend"; Check "building the backend wheel"
$backendWheel = (Get-ChildItem "$wheels\lucida_forensic-*.whl" | Select-Object -First 1).FullName
# The [desktop] extra adds pywebview and its Windows dependencies (pythonnet, clr_loader, cffi,
# bottle, proxy_tools, typing_extensions). proxy_tools is published only as source, so it alone
# may be built here (it is pure Python); everything else must be a binary wheel.
& $BuildPython -m pip install --no-cache-dir --disable-pip-version-check --no-warn-script-location `
    --only-binary=:all: --no-binary=proxy_tools --target $Site "$backendWheel[desktop]"
Check "installing the dependencies"

Step "FFmpeg $FfmpegVersion"
$ffZip = "$Downloads\ffmpeg-$FfmpegVersion-essentials_build.zip"
$ffSha = Get-Pinned $FfmpegUrl $ffZip $FfmpegSha256
$ffTmp = "$Work\ffmpeg-extract"
if (Test-Path $ffTmp) { Remove-Item -Recurse -Force $ffTmp }
Expand-Archive $ffZip -DestinationPath $ffTmp
$ffRoot = (Get-ChildItem $ffTmp -Directory | Select-Object -First 1).FullName
New-Item -ItemType Directory -Force "$Payload\ffmpeg" | Out-Null
Copy-Item "$ffRoot\bin\ffmpeg.exe", "$ffRoot\bin\ffprobe.exe" "$Payload\ffmpeg"
Get-ChildItem $ffRoot -File | Copy-Item -Destination "$Payload\ffmpeg"

Step "User interface"
Push-Location "$Repo\frontend"
try {
    npm ci --no-audit --no-fund; Check "npm ci"
    npm run build; Check "npm run build"
} finally { Pop-Location }
New-Item -ItemType Directory -Force "$Payload\frontend" | Out-Null
Copy-Item -Recurse "$Repo\frontend\dist" "$Payload\frontend\dist"

Step "Launcher, docs and licences"
New-Item -ItemType Directory -Force "$Payload\launcher", "$Payload\docs", "$Payload\licences" | Out-Null
Copy-Item "$PSScriptRoot\launcher\workbench_launcher.py" "$Payload\launcher"
Copy-Item "$Repo\README.md" "$Payload\docs"
Copy-Item -Recurse "$Repo\docs\*" "$Payload\docs"
Copy-Item "$PSScriptRoot\install-notes.txt" "$Payload\docs\INSTALL-NOTES.txt"
$env:PYTHONDONTWRITEBYTECODE = "1"
& "$Py\python.exe" -I -c @"
import importlib.metadata as m

def licence(d):
    md = d.metadata
    lic = md.get('License-Expression') or (md.get('License') or '').strip().split('\n')[0]
    if not lic or len(lic) > 60:
        lic = ', '.join(c.split(' :: ')[-1] for c in (md.get_all('Classifier') or []) if c.startswith('License ::')) or lic[:60]
    return lic or 'see the package metadata'

print('Python packages bundled with Lucida (name, version, licence)')
print()
for d in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower()):
    print('%-32s %-14s %s' % (d.metadata['Name'], d.version, licence(d)))
"@ | Set-Content -Encoding utf8 "$Payload\licences\python-packages.txt"
Check "listing the package licences"
Copy-Item "$Py\LICENSE.txt" "$Payload\licences\python-LICENSE.txt" -ErrorAction SilentlyContinue

Step "Pre-compiling Python modules (the installation folder may be read-only for users)"
& "$Py\python.exe" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash $Site | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning "some third-party files did not compile (usually Python 2 test files); they are not imported" }

Step "Build record and SHA256SUMS"
$commit = (git -C $Repo rev-parse HEAD 2>$null)
@"
Lucida $Version for Windows x64
Built:    $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')) on $env:COMPUTERNAME
Commit:   $commit
Python:   $PythonVersion embeddable, sha256 $pySha
FFmpeg:   $FfmpegVersion essentials (gyan.dev), sha256 $ffSha
VC++:     vc_redist.x64.exe, sha256 $vcSha
WebView2: Evergreen Standalone x64 (run by Setup only if missing), sha256 $wv2Sha
"@ | Set-Content -Encoding utf8 "$Payload\BUILDINFO.txt"
& "$Py\python.exe" -I -c @"
import hashlib, os, sys
root = sys.argv[1]
lines = []
for d, _, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        rel = os.path.relpath(p, root).replace(os.sep, '/')
        if rel == 'SHA256SUMS':
            continue
        h = hashlib.sha256()
        with open(p, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
        lines.append(f'{h.hexdigest()}  {rel}')
lines.sort(key=lambda l: l.split('  ', 1)[1])
open(os.path.join(root, 'SHA256SUMS'), 'w', encoding='utf-8', newline='\n').write('\n'.join(lines) + '\n')
print(f'{len(lines)} files hashed')
"@ $Payload
Check "writing SHA256SUMS"

Step "Acceptance self-test against the payload (offline, private Python only)"
$testData = "$Work\selftest-data"
if (Test-Path $testData) { Remove-Item -Recurse -Force $testData }
$savedPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:FORENSIC_DATA_DIR = $testData
    & "$Py\python.exe" -B -X utf8 "$Payload\launcher\workbench_launcher.py" selftest
    Check "the self-test"
} finally {
    $env:PATH = $savedPath
    Remove-Item Env:FORENSIC_DATA_DIR
}

Step "Desktop window libraries load with the private Python"
& "$Py\python.exe" -I -c "import webview, clr, forensic.desktop; print('pywebview', webview.__version__ if hasattr(webview, '__version__') else '', '- pythonnet loaded')"
Check "loading pywebview and pythonnet"

Step "Compiling the installer"
& $Iscc /Q "/DAppVersion=$Version" "/DPayloadDir=$Payload" "/DOutputDir=$OutDir" "/DWebView2Dir=$wv2Dir" "$PSScriptRoot\installer.iss"
Check "Inno Setup"
$setup = "$OutDir\Lucida-$Version-Setup.exe"
$hash = (Get-FileHash -Algorithm SHA256 $setup).Hash.ToLower()
"$hash  Lucida-$Version-Setup.exe" | Set-Content -Encoding ascii "$setup.sha256"
$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "`nInstaller: $setup ($size MB)`nSHA-256:   $hash" -ForegroundColor Green
