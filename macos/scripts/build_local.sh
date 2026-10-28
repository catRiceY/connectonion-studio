#!/bin/bash
#
# LOCAL test build (NOT for release): bundles the LOCAL co_studio (your working tree, not PyPI) + your
# Swift changes, Developer-ID signed so Sparkle works, NO notarization / dmg.
# Reuses only the standalone interpreter; runtime dependencies are synced from uv.lock.
#
#   ./scripts/build_local.sh                          # v0.1.5 build 2 (so it DETECTS the 0.1.6 appcast)
#   APP_VERSION=0.1.7 BUILD_NUM=4 ./scripts/build_local.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."                 # -> macos/
ROOT="$(pwd)"; REPO="$(cd "$ROOT/.." && pwd)"; DIST="$ROOT/dist"
APP_VERSION="${APP_VERSION:-0.1.5}"
BUILD_NUM="${BUILD_NUM:-2}"
SIGN_ID="Developer ID Application: Yifan Yang (7V6USF6B96)"
ENT="$ROOT/scripts/python.entitlements"
APP="$DIST/ConnectOnionStudio-local.app"
XC=/Applications/Xcode.app/Contents/Developer

echo "[1/5] install LOCAL source + locked history/mcp/knowledge runtime into dist/python"
[ -x "$DIST/python/bin/python3" ] || { echo "  prepare standalone Python at dist/python first (see macos/README.md); no signing or download has run"; exit 1; }
"$DIST/python/bin/python3" -I "$ROOT/scripts/bundle_runtime.py" install "$DIST/python/bin/python3" --mode source
find "$DIST/python" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "[2/5] xcodebuild Release (v$APP_VERSION build $BUILD_NUM, unsigned)"
( cd "$ROOT/ConnectOnionStudio" && DEVELOPER_DIR="$XC" xcodebuild \
    -project ConnectOnionStudio.xcodeproj -scheme ConnectOnionStudio -configuration Release \
    -derivedDataPath build-local CODE_SIGNING_ALLOWED=NO \
    MARKETING_VERSION="$APP_VERSION" CURRENT_PROJECT_VERSION="$BUILD_NUM" build >/dev/null )

echo "[3/5] assemble app + inject python"
rm -rf "$APP"
ditto "$ROOT/ConnectOnionStudio/build-local/Build/Products/Release/ConnectOnionStudio.app" "$APP"
ditto "$DIST/python" "$APP/Contents/Resources/python"

echo "[4/5] Developer-ID sign inside-out (no timestamp — local only, so Sparkle loads)"
PYDIR="$APP/Contents/Resources/python"
# Sign EVERY Mach-O in python: .so/.dylib AND plain executables (python3 etc.) — missing the plain
# executables leaves "a sealed resource is missing or invalid", which makes Sparkle refuse to check.
{ find "$PYDIR" -type f \( -name '*.so' -o -name '*.dylib' \);
  find "$PYDIR" -type f -perm -u+x ! -name '*.so' ! -name '*.dylib' \
       -exec sh -c 'file "$1" | grep -q Mach-O && echo "$1"' _ {} \; ; } \
  | while IFS= read -r f; do [ -n "$f" ] && codesign --force --options runtime --entitlements "$ENT" -s "$SIGN_ID" "$f"; done
SPARKLE="$APP/Contents/Frameworks/Sparkle.framework"
if [ -d "$SPARKLE" ]; then
  for xpc in "$SPARKLE"/Versions/*/XPCServices/*.xpc; do [ -e "$xpc" ] && codesign --force --options runtime --preserve-metadata=entitlements -s "$SIGN_ID" "$xpc"; done
  for bin in "$SPARKLE"/Versions/*/Autoupdate "$SPARKLE"/Versions/*/Updater.app; do [ -e "$bin" ] && codesign --force --options runtime -s "$SIGN_ID" "$bin"; done
  codesign --force --options runtime -s "$SIGN_ID" "$SPARKLE"
fi
codesign --force --options runtime -s "$SIGN_ID" "$APP"
codesign --verify --strict "$APP" && echo "  ✓ signature valid"

V=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
B=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$APP/Contents/Info.plist")
echo "[5/5] DONE -> $APP  (v$V build $B)"
