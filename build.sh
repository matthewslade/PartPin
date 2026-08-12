#!/usr/bin/env bash
# Build the installable add-on zip: dist/part_pin-<version>.zip
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' part_pin/blender_manifest.toml | head -1)
OUT="dist/part_pin-${VERSION}.zip"

mkdir -p dist
rm -f "$OUT"

# The zip must contain the part_pin/ folder at its root so it installs both
# as a Blender 4.2+ extension and as a legacy add-on.
zip -r "$OUT" part_pin \
    -x "part_pin/__pycache__/*" -x "part_pin/*.pyc" >/dev/null

echo "Built $OUT"
echo "Install in Blender: Edit ▸ Preferences ▸ Add-ons ▸ (v) ▸ Install from Disk…"
