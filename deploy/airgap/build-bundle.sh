#!/usr/bin/env bash
# Build an offline installation bundle on a connected machine of the SAME OS and CPU
# architecture as the air-gapped target (e.g. both Ubuntu 24.04 x86_64, Python 3.12).
#
#   deploy/airgap/build-bundle.sh [output-dir]
#
# The bundle contains: Python wheels for every dependency (wheelhouse/), the backend
# source with its bundled model weights, the built UI (frontend/dist), docs, and
# SHA256SUMS over every file. Optionally, with --docker, the container image as a tar.
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
docker=0
out=""
for a in "$@"; do
  case "$a" in
    --docker) docker=1 ;;
    *) out="$a" ;;
  esac
done
version="$(python3 -c "import re,sys;print(re.search(r'^version = \"(.+)\"', open('$here/backend/pyproject.toml').read(), re.M).group(1))")"
out="${out:-$here/dist/forensic-workbench-$version-airgap}"
rm -rf "$out" && mkdir -p "$out"

echo "Building the UI..."
(cd "$here/frontend" && (npm ci || npm install) && npm run build)
mkdir -p "$out/frontend" && cp -r "$here/frontend/dist" "$out/frontend/dist"

echo "Copying the backend, models and docs..."
mkdir -p "$out/backend"
tar -C "$here/backend" --exclude '__pycache__' --exclude '.pytest_cache' --exclude './data' --exclude '*.egg-info' \
  --exclude './build' -cf - . | tar -C "$out/backend" -xf -
cp -r "$here/docs" "$out/docs"
cp "$here/README.md" "$here/deploy/airgap/install.sh" "$out/"

echo "Downloading wheels for this platform..."
python3 -m pip wheel --wheel-dir "$out/wheelhouse" "$here/backend"
python3 -m pip download --dest "$out/wheelhouse" pip setuptools wheel

if [ "$docker" = 1 ]; then
  echo "Building and saving the container image..."
  docker build -t "forensic-workbench:$version" "$here"
  docker save "forensic-workbench:$version" | gzip > "$out/forensic-workbench-$version-image.tar.gz"
  cp "$here/Dockerfile" "$here/docker-compose.yml" "$out/"
fi

echo "Verifying the bundled models..."
(cd "$here/backend" && python3 -m forensic.models verify)

echo "Writing SHA256SUMS..."
(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
echo "Bundle ready: $out"
echo "Copy it to the target, then run: ./install.sh <install-dir>"
