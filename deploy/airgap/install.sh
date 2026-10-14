#!/usr/bin/env bash
# Install the Forensic Workbench from an offline bundle (made by build-bundle.sh).
#
#   ./install.sh /opt/forensic-workbench
#
# Needs: Python 3.11+ with venv, and FFmpeg (ffmpeg, ffprobe) from the OS's offline
# package media. Makes no network connection: pip runs with --no-index.
set -euo pipefail
bundle="$(cd "$(dirname "$0")" && pwd)"
dest="${1:?usage: install.sh <install-dir>}"

echo "Checking the bundle against SHA256SUMS..."
(cd "$bundle" && sha256sum --quiet -c SHA256SUMS)

command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || {
  echo "FFmpeg is not installed; install it from your offline OS packages first." >&2; exit 1; }

mkdir -p "$dest"
cp -r "$bundle/backend" "$bundle/frontend" "$bundle/docs" "$dest/"
python3 -m venv "$dest/venv"
"$dest/venv/bin/pip" install --no-index --find-links "$bundle/wheelhouse" --upgrade pip
"$dest/venv/bin/pip" install --no-index --find-links "$bundle/wheelhouse" "$dest/backend"

cat > "$dest/run.sh" <<RUN
#!/usr/bin/env bash
export FORENSIC_DATA_DIR="\${FORENSIC_DATA_DIR:-$dest/data}"
export FORENSIC_FRONTEND_DIST="$dest/frontend/dist"
export FORENSIC_DETERMINISTIC=1
exec "$dest/venv/bin/uvicorn" forensic.api:app --host 127.0.0.1 --port "\${PORT:-8000}"
RUN
chmod +x "$dest/run.sh"

echo "Running the acceptance self-test..."
"$dest/venv/bin/python" -m forensic.selftest
echo "Installed. Start with $dest/run.sh and open http://127.0.0.1:8000"
