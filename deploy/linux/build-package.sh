#!/usr/bin/env bash
# Build the offline Linux server package for Lucida (x86_64, NVIDIA GPU through CUDA).
#
#   deploy/linux/build-package.sh [--out DIR]
#
# Run on a connected x86_64 Linux machine (or the "Linux server package" GitHub Actions workflow)
# with Node.js 22, Python 3.11+ with pip, curl, xz and tar. It assembles:
#
#   python/     a private, relocatable CPython 3.12 (python-build-standalone, fetched by a pinned uv)
#               with every Python library, ONNX Runtime for CUDA 12 and the NVIDIA CUDA 12 and
#               cuDNN 9 user-space libraries (the server needs only the NVIDIA driver, no CUDA toolkit)
#   ffmpeg/     static FFmpeg and FFprobe
#   frontend/   the built web UI
#   launcher/   the launcher shared with the Windows installer
#   bin/lucida  the command that starts the service, the self-test and the command line
#   docs/, licences/, BUILDINFO.txt, SHA256SUMS, FILESIZES
#
# then runs the acceptance self-test against the payload and writes
# dist/Lucida-<version>-linux-x86_64.tar.gz with its .sha256. The package needs no internet
# connection on the server.
set -euo pipefail

UV_VERSION="0.13.6"          # pins the python-build-standalone release uv downloads (and checks)
PYTHON_SERIES="3.12"
ORT_GPU_VERSION="1.26.0"     # the last ONNX Runtime release built for CUDA 12 (driver R525 or newer)
FFMPEG_FALLBACK_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

repo="$(cd "$(dirname "$0")/../.." && pwd)"
here="$repo/deploy/linux"
out="$repo/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/backend/pyproject.toml" | head -1)"
name="Lucida-$version-linux-x86_64"
work="$repo/build/linux"
payload="$work/$name"
downloads="$work/downloads"

step() { printf '\n== %s\n' "$*"; }

[ "$(uname -m)" = x86_64 ] || { echo "Build on x86_64 Linux" >&2; exit 1; }
command -v node >/dev/null || { echo "Node.js 22 is needed" >&2; exit 1; }
build_py="${BUILD_PYTHON:-python3}"

rm -rf "$payload" && mkdir -p "$payload" "$downloads" "$out"

step "Lucida $version: private Python $PYTHON_SERIES (python-build-standalone via uv $UV_VERSION)"
tools="$work/tools"
rm -rf "$tools" && "$build_py" -m venv "$tools"
"$tools/bin/pip" install -q --disable-pip-version-check "uv==$UV_VERSION"
export UV_PYTHON_INSTALL_DIR="$work/uv-python"
rm -rf "$UV_PYTHON_INSTALL_DIR"
"$tools/bin/uv" python install --no-bin "$PYTHON_SERIES"
uvpy="$("$tools/bin/uv" python find --managed-python "$PYTHON_SERIES")"
pyroot="$(cd "$(dirname "$(readlink -f "$uvpy")")/.." && pwd)"
cp -a "$pyroot" "$payload/python"
py="$payload/python/bin/python3"
"$py" -c "import sys, ssl, sqlite3; assert sys.version_info[:2] == tuple(map(int, '$PYTHON_SERIES'.split('.'))); print(sys.version)"
python_full="$("$py" -c 'import sys; print(sys.version.split()[0])')"
# The private Python belongs to Lucida alone; let pip install into it.
find "$payload/python/lib" -maxdepth 2 -name EXTERNALLY-MANAGED -delete
pyroot_sha="$(cd "$pyroot" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"

step "Python dependencies, ONNX Runtime $ORT_GPU_VERSION for CUDA 12, CUDA 12 and cuDNN 9 libraries"
pipi() { "$py" -m pip install --no-cache-dir --disable-pip-version-check --no-warn-script-location --only-binary=:all: "$@"; }
pipi --upgrade pip
wheels="$work/wheels" && rm -rf "$wheels"
"$py" -m pip wheel -q --no-deps --wheel-dir "$wheels" "$repo/backend"
pipi "$wheels"/lucida_forensic-*.whl
# The backend asks for the CPU build of ONNX Runtime on Linux; the GPU build provides the same
# module, so the CPU one is removed first and the two never overwrite each other's files.
"$py" -m pip uninstall -y -q onnxruntime
pipi "onnxruntime-gpu[cuda,cudnn]==$ORT_GPU_VERSION"
"$py" -c "import onnxruntime as o; p = o.get_available_providers(); print(o.__version__, p); assert 'CUDAExecutionProvider' in p"

step "FFmpeg (static build)"
mkdir -p "$payload/ffmpeg"
if [ -n "${FFMPEG_DIR:-}" ]; then
  # Local test builds only: copy ffmpeg and ffprobe from a folder instead of downloading them.
  install -m 0755 "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe" "$payload/ffmpeg/"
  ff_src="$FFMPEG_DIR (local)"; ff_sha="-"
else
  # BtbN's static GPL builds of the newest FFmpeg release branch (n7.1, n8.0, ...). The release
  # branch build only takes fixes, unlike the master build. Its SHA-256 goes in BUILDINFO.txt.
  ff_src="$(curl -fsSL --retry 4 ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} \
      https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest \
    | grep -o '"browser_download_url": *"[^"]*ffmpeg-n[0-9.]*-latest-linux64-gpl-[0-9.]*\.tar\.xz"' \
    | sed 's/.*"\(https[^"]*\)"/\1/' | sort -V | tail -1 || true)"
  ff="$downloads/ffmpeg.tar.xz"
  if [ -z "$ff_src" ] || ! curl -fsSL --retry 4 -o "$ff" "$ff_src"; then
    echo "  BtbN build not available; using $FFMPEG_FALLBACK_URL"
    ff_src="$FFMPEG_FALLBACK_URL"
    curl -fsSL --retry 4 -o "$ff" "$ff_src"
  fi
  echo "  $ff_src"
  ff_sha="$(sha256sum "$ff" | cut -d' ' -f1)"
  fx="$work/ffmpeg-extract" && rm -rf "$fx" && mkdir -p "$fx"
  tar -C "$fx" -xJf "$ff"
  for b in ffmpeg ffprobe; do
    src="$(find "$fx" -type f -name "$b" -perm -u+x | head -1)"
    [ -n "$src" ] || { echo "$b is missing from the FFmpeg archive" >&2; exit 1; }
    install -m 0755 "$src" "$payload/ffmpeg/$b"
  done
  find "$fx" -maxdepth 3 -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'README*' \) -exec cp {} "$payload/ffmpeg/" \;
  # A static build runs on any distribution; refuse one that needs shared FFmpeg libraries.
  if ldd "$payload/ffmpeg/ffmpeg" 2>/dev/null | grep -q 'libav'; then echo "FFmpeg is not a static build" >&2; exit 1; fi
fi
ff_version="$("$payload/ffmpeg/ffmpeg" -hide_banner -version | head -1)"
echo "  $ff_version"

step "User interface"
(cd "$repo/frontend" && npm ci --no-audit --no-fund && npm run build)
mkdir -p "$payload/frontend" && cp -r "$repo/frontend/dist" "$payload/frontend/dist"

step "Launcher, scripts, docs and licences"
mkdir -p "$payload/launcher" "$payload/bin" "$payload/docs" "$payload/licences" "$payload/systemd"
cp "$repo/deploy/windows/launcher/workbench_launcher.py" "$payload/launcher/"
install -m 0755 "$here/lucida" "$payload/bin/lucida"
install -m 0755 "$here/check-server.sh" "$payload/bin/check-server.sh"
install -m 0755 "$here/uninstall.sh" "$payload/uninstall.sh"
cp "$here/lucida.service" "$payload/systemd/lucida.service"
cp "$repo/README.md" "$payload/docs/"
cp -r "$repo/docs/." "$payload/docs/"
"$py" -I -c "
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
" > "$payload/licences/python-packages.txt"
cp "$payload/python/lib/python$PYTHON_SERIES/LICENSE.txt" "$payload/licences/python-LICENSE.txt" 2>/dev/null || true

step "Pre-compiling Python modules (unchecked-hash .pyc files, which Python never rewrites)"
"$py" -I -m compileall -f -q -j 0 --invalidation-mode unchecked-hash "$payload/python/lib" >/dev/null \
  || echo "  some third-party files did not compile (usually Python 2 test files); they are not imported"
"$py" -I -m compileall -f -q --invalidation-mode unchecked-hash "$payload/launcher" >/dev/null

step "Build record, SHA256SUMS and FILESIZES"
cat > "$payload/BUILDINFO.txt" <<INFO
Lucida $version for Linux x86_64 (NVIDIA GPU through CUDA 12)
Built:    $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(uname -n), $(. /etc/os-release && echo "$PRETTY_NAME")
Commit:   $(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)
Python:   $python_full, python-build-standalone via uv $UV_VERSION, tree sha256 $pyroot_sha
ONNX:     onnxruntime-gpu $ORT_GPU_VERSION (CUDA 12, cuDNN 9; needs NVIDIA driver R525 or newer)
FFmpeg:   $ff_version, from $ff_src, sha256 $ff_sha
INFO
"$py" -I - "$payload" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
lines, sizes = [], []
for d, dirs, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        rel = os.path.relpath(p, root)
        if rel in ("SHA256SUMS", "FILESIZES") or (os.path.islink(p) and not os.path.exists(p)):
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {rel}")
        sizes.append(f"{os.path.getsize(p)}  {rel}")
key = lambda l: l.split("  ", 1)[1]
open(os.path.join(root, "SHA256SUMS"), "w").write("\n".join(sorted(lines, key=key)) + "\n")
open(os.path.join(root, "FILESIZES"), "w").write("\n".join(sorted(sizes, key=key)) + "\n")
print(f"{len(lines)} files hashed")
PY

step "Acceptance self-test against the payload (private Python and FFmpeg only)"
td="$work/selftest-data" && rm -rf "$td"
env -i HOME="$work" PATH=/usr/bin:/bin LUCIDA_AS_ME=1 LUCIDA_CONFIG=/nonexistent FORENSIC_DATA_DIR="$td" \
  "$payload/bin/lucida" selftest

step "Package"
# install.sh sits next to the payload, so the payload's SHA256SUMS covers exactly what is installed.
pkg="$work/pkg" && rm -rf "$pkg" && mkdir -p "$pkg/$name"
mv "$payload" "$pkg/$name/payload"
install -m 0755 "$here/install.sh" "$pkg/$name/install.sh"
install -m 0755 "$here/check-server.sh" "$pkg/$name/check-server.sh"
cp "$here/INSTALL.txt" "$pkg/$name/INSTALL.txt"
[ -f "$repo/docs/linux-server-install.pdf" ] && cp "$repo/docs/linux-server-install.pdf" "$pkg/$name/"
(cd "$pkg/$name" && sha256sum install.sh check-server.sh payload/SHA256SUMS > PACKAGE.SHA256SUMS)
tarball="$out/$name.tar.gz"
tar -C "$pkg" -cf - "$name" | gzip -6 > "$tarball"
(cd "$out" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
mv "$pkg/$name/payload" "$payload"
echo
echo "Package: $tarball ($(du -m "$tarball" | cut -f1) MB)"
cat "$tarball.sha256"
