#!/usr/bin/env bash
# build_hip_bundle.sh -- full AMD (HIP) miner bundle, built on a Linux ROCm box.
#
# Prereqs: ROCm (hipcc), Python 3.12, and:
#   pip install numpy blake3 py-pearl-mining pyinstaller
# Usage:
#   bash packaging/build_hip_bundle.sh 1.5.fleet-amd-1 [gfx archs]
#   HIP_ARCHES env also works (see build_hip.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${1:?usage: build_hip_bundle.sh <version>}
if [ $# -ge 2 ]; then export HIP_ARCHES="${*:2}"; fi

# 1. HIP backend library (drop-in libp40cuda.so at the repo root -- the
#    PyInstaller spec picks it up from there automatically).
bash packaging/build_hip.sh

# 2. Frozen miner (same torch-free spec as the CUDA build).
pyinstaller packaging/p40-miner-lite.spec --noconfirm --distpath dist --workpath build_pyi

# 3. Assemble the bundle: manifest + onedir. Engine stays "p40" (same miner,
#    same pool lock, same intensity file); the panel cannot tell AMD rigs from
#    NVIDIA rigs apart except through the reported GPUs.
BUNDLE=dist/bundle-$VERSION
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"
printf '{\n  "version": "%s",\n  "engine": "p40",\n  "entry": "p40-miner/p40-miner",\n  "created": "%s"\n}\n' \
  "$VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$BUNDLE/manifest.json"
cp -r dist/p40-miner "$BUNDLE/p40-miner"

# 4. Zip + hashes.
( cd "$BUNDLE" && zip -qr "../bundle-$VERSION.zip" . )
echo "== AMD bundle ready: dist/bundle-$VERSION.zip"
sha256sum "dist/bundle-$VERSION.zip"
echo "Upload to the panel and set it as target_version for the AMD rigs."
