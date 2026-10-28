#!/bin/bash
#
# Build a signed, distributable ConnectOnion Studio.app + .dmg (macOS, Apple Silicon / arm64).
#
#   STUDIO_VERSION=<published-checkout-version> ./scripts/build_release.sh
#   SOURCE_BUILD=1 ./scripts/build_release.sh  # explicitly labelled source-preview
#
# Produces  dist/ConnectOnionStudio-<APP_VERSION>.dmg  (NOT yet notarized).
# Notarization needs YOUR Apple credentials, so it's a separate manual step (printed at the end).
#
set -euo pipefail

cd "$(dirname "$0")/.."                 # -> macos/
ROOT="$(pwd)"
DIST="$ROOT/dist"

SOURCE_BUILD="${SOURCE_BUILD:-0}"
STUDIO_VERSION="${STUDIO_VERSION:-}"
if [[ "$SOURCE_BUILD" != "0" && "$SOURCE_BUILD" != "1" ]]; then
  echo "SOURCE_BUILD must be 0 or 1" >&2; exit 1
fi
if [[ "$SOURCE_BUILD" != "1" && -z "$STUDIO_VERSION" ]]; then
  echo "Set STUDIO_VERSION to a published version matching this checkout, or SOURCE_BUILD=1 for a source preview." >&2
  exit 1
fi
command -v uv >/dev/null || { echo "uv is required for locked bundle dependencies" >&2; exit 1; }
APP_VERSION="0.1.8"                                # keep in sync with Xcode MARKETING_VERSION
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260718/cpython-3.12.13%2B20260718-aarch64-apple-darwin-install_only.tar.gz"
SIGN_ID="Developer ID Application: Yifan Yang (7V6USF6B96)"
ENT="$ROOT/scripts/python.entitlements"
APP="$DIST/ConnectOnionStudio.app"
DMG="$DIST/ConnectOnionStudio-$APP_VERSION.dmg"
if [[ "$SOURCE_BUILD" == "1" ]]; then
  APP="$DIST/ConnectOnionStudio-source-preview.app"
  DMG="$DIST/ConnectOnionStudio-source-preview-$APP_VERSION.dmg"
fi

echo "[1/6] fetch relocatable Python (arm64)"
mkdir -p "$DIST"
rm -rf "$DIST/python" "$DIST/python.tar.gz"
curl --fail --location --retry 2 --connect-timeout 20 "$PY_URL" -o "$DIST/python.tar.gz"
tar -xzf "$DIST/python.tar.gz" -C "$DIST"          # -> $DIST/python

echo "[2/6] install verified Studio + locked history/mcp/knowledge runtime"
if [[ "$SOURCE_BUILD" == "1" ]]; then
  "$DIST/python/bin/python3" -I "$ROOT/scripts/bundle_runtime.py" install "$DIST/python/bin/python3" --mode source
else
  "$DIST/python/bin/python3" -I "$ROOT/scripts/bundle_runtime.py" install "$DIST/python/bin/python3" --mode pypi --version "$STUDIO_VERSION"
fi

echo "[3/6] trim regenerables (pyc caches) + the 90MB Google API discovery cache (fetched online if ever needed)"
find "$DIST/python" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DIST/python" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "$DIST/python/lib/python3.12/site-packages/googleapiclient/discovery_cache/documents"

echo "[4/6] xcodebuild Release + inject Python into Resources"
( cd "$ROOT/ConnectOnionStudio" && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
    xcodebuild -project ConnectOnionStudio.xcodeproj -scheme ConnectOnionStudio \
      -configuration Release -derivedDataPath build-release CODE_SIGNING_ALLOWED=NO build >/dev/null )
rm -rf "$APP"
ditto "$ROOT/ConnectOnionStudio/build-release/Build/Products/Release/ConnectOnionStudio.app" "$APP"
ditto "$DIST/python" "$APP/Contents/Resources/python"

echo "[5/6] sign inside-out (Developer ID + hardened runtime; retry on flaky Apple timestamps)"
PYDIR="$APP/Contents/Resources/python"
FILES=$( { find "$PYDIR" -type f \( -name '*.so' -o -name '*.dylib' \);
           find "$PYDIR" -type f -perm -u+x ! -name '*.so' ! -name '*.dylib' \
                -exec sh -c 'file "$1" | grep -q Mach-O && echo "$1"' _ {} \; ; } )
for attempt in 1 2 3 4; do
  fails=""
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if codesign --force --timestamp --options runtime --entitlements "$ENT" -s "$SIGN_ID" "$f" 2>&1 | grep -qi timestamp; then
      fails="$fails$f"$'\n'
    fi
  done <<< "$FILES"
  nf=$(printf '%s' "$fails" | grep -c . || true); nf=${nf:-0}
  echo "     attempt $attempt: $nf timestamp retries"
  [ "$nf" -eq 0 ] && break
  FILES="$fails"; sleep 2
done

# Sparkle ships adhoc-signed (TeamIdentifier=not set), which notarization rejects. Re-sign its nested
# code inside-out with our Developer ID + hardened runtime: XPC services (keep their own entitlements),
# then Autoupdate + Updater.app, then the framework itself — before sealing the app bundle below.
SPARKLE="$APP/Contents/Frameworks/Sparkle.framework"
if [ -d "$SPARKLE" ]; then
  echo "     re-signing Sparkle.framework nested code with Developer ID"
  for xpc in "$SPARKLE"/Versions/*/XPCServices/*.xpc; do
    [ -e "$xpc" ] && codesign --force --timestamp --options runtime --preserve-metadata=entitlements -s "$SIGN_ID" "$xpc"
  done
  for bin in "$SPARKLE"/Versions/*/Autoupdate "$SPARKLE"/Versions/*/Updater.app; do
    [ -e "$bin" ] && codesign --force --timestamp --options runtime -s "$SIGN_ID" "$bin"
  done
  codesign --force --timestamp --options runtime -s "$SIGN_ID" "$SPARKLE"
fi

codesign --force --timestamp --options runtime -s "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP"

echo "[6/6] package .dmg (UDBZ)"
rm -rf "$DIST/dmg-stage" "$DMG"; mkdir -p "$DIST/dmg-stage"
ditto "$APP" "$DIST/dmg-stage/ConnectOnionStudio.app"
ln -s /Applications "$DIST/dmg-stage/Applications"
hdiutil create -volname "ConnectOnion Studio" -srcfolder "$DIST/dmg-stage" -ov -format UDBZ "$DMG" >/dev/null
rm -rf "$DIST/dmg-stage"

echo ""
echo "DONE -> $DMG"
if [[ "$SOURCE_BUILD" == "1" ]]; then
  echo "SOURCE PREVIEW ONLY: not a published PyPI build. Do not add this artifact to the production appcast."
fi
echo "Notarize (needs a one-time keychain profile: xcrun notarytool store-credentials co-studio-notary --apple-id <you> --team-id 7V6USF6B96 --password <app-specific-pw>):"
echo "  xcrun notarytool submit \"$DMG\" --keychain-profile co-studio-notary --wait"
echo "  xcrun stapler staple \"$DMG\""
