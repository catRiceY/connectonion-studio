#!/usr/bin/env bash
# ============================================================
# sync_brand_assets.sh — copy the 8 onion layer PNGs from the
# iOS app's Assets.xcassets into co_studio/frontend/assets/onion/.
#
# Usage:
#   ./scripts/sync_brand_assets.sh [path/to/Assets.xcassets]
#   IOS_XCASSETS=/path/to/Assets.xcassets ./scripts/sync_brand_assets.sh
#
# The iOS catalog stores each layer as <name>.imageset/<name>.png.
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/co_studio/frontend/assets/onion"

DEFAULT_XCASSETS="$REPO_ROOT/../connectonion-ios/ConnectOnion iOS/Assets.xcassets"
SRC="${1:-${IOS_XCASSETS:-$DEFAULT_XCASSETS}}"

LAYERS=(
  onion_1_black
  onion_2_purple
  onion_3_white
  onion_4_purple
  onion_5_white
  onion_6_purple
  onion_7_white
  onion_8_core
)

if [[ ! -d "$SRC" ]]; then
  echo "error: Assets.xcassets not found at: $SRC" >&2
  echo "       pass the path as \$1 or set IOS_XCASSETS" >&2
  exit 1
fi

mkdir -p "$DEST"

copied=0
for name in "${LAYERS[@]}"; do
  src_png="$SRC/$name.imageset/$name.png"
  if [[ ! -f "$src_png" ]]; then
    echo "error: missing layer: $src_png" >&2
    exit 1
  fi
  cp "$src_png" "$DEST/$name.png"
  copied=$((copied + 1))
  echo "  ✓ $name.png"
done

echo "synced $copied/8 onion layers → $DEST"
