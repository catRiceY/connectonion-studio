# ConnectOnion Studio — macOS app

A thin native shell: a `WKWebView` showing the same web UI (`co_studio/`), in a window with a
hidden title bar so the traffic-light buttons sit over the content — the look pywebview couldn't do.
The Swift shell talks to the studio server over `http://127.0.0.1`; it does **not** import the Python package.

**Status:** the native shell is implemented; each checkout still requires a fresh
`xcodebuild` result. Swift syntax checks alone are not a successful native build. Sources live
in `ConnectOnionStudio/ConnectOnionStudio/`:
- `ConnectOnionStudioApp.swift` — `@main` App + `ContentView`; `.windowStyle(.hiddenTitleBar)`
- `WebView.swift` — `WKWebView` wrapper
- `StudioServer.swift` — launches `co-studio` as a child process, waits for it, hands over the URL

## Run it (dev)

1. **Signing & Capabilities → remove "App Sandbox"** (dev launches a subprocess + hits localhost;
   the sandbox blocks both).
2. Point the shell at your local server — **Product → Scheme → Edit Scheme → Run → Arguments →
   Environment Variables**, add `CO_STUDIO_BIN` = `/absolute/path/to/connectonion-studio/.venv/bin/co-studio`
   (or leave it unset if `co-studio` is on your PATH).
3. **⌘R.** The app launches the server, waits until it answers, then shows the studio in a
   hidden-title-bar window.

## Build from the command line

The machine's `xcode-select` points at Command Line Tools, so pass Xcode explicitly:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project macos/ConnectOnionStudio/ConnectOnionStudio.xcodeproj \
  -scheme ConnectOnionStudio -configuration Debug -destination 'platform=macOS' \
  CODE_SIGNING_ALLOWED=NO build
```

## Bundle current source or a published package

### Isolated local preview (recommended for acceptance testing)

On Apple Silicon macOS, with Xcode, `uv`, and Python 3.12+ installed:

```bash
.venv/bin/python macos/scripts/build_preview.py
```

This creates a fresh `macos/dist/previews/preview-<timestamp>-<id>/` directory,
verifies the pinned official standalone Python archive SHA-256, installs the
checkout with locked runtime extras, builds the native shell, and produces
`ConnectOnion Studio Preview.app` with an **ad-hoc** signature. It does not read
Apple signing credentials, notarize, publish, create a DMG, replace an installed
app, or alter the production appcast. It refuses to overwrite an existing preview.
The Python interpreter and current source are included; `preview-provenance.json`
records their hashes and the native build inputs.

Preview builds have a unique bundle identifier and isolated state under
`~/Library/Application Support/ConnectOnionStudioPreviews/<bundle-id>/`. They
pass explicit `STUDIO_DATA_DIR` and `AGENT_CONFIG_PATH`, disable automatic
authentication and Sparkle updates, and do not inherit model credentials or
database configuration. The user's home is unchanged. The backend chooses a
free loopback port; existing Studio/Agent ports and data are not reused. WebKit
uses a non-persistent data store. Server logs are under
`~/Library/Caches/ConnectOnionStudioPreviews/<bundle-id>/server.log`.

The preview is **self-contained for the Studio/Python application**, not a
bundled PostgreSQL service or embedding-model download. Database-backed features
require a separately configured PostgreSQL/pgvector service; missing configuration
must show an unavailable state, not silently use the user's production database.
Successful packaging is not GUI acceptance: open the new app, verify readiness,
empty isolated data, and quit/relaunch behavior before marking that step complete.

Downloads and compilation can be separated without discarding the runtime:

```bash
.venv/bin/python macos/scripts/build_preview.py --prepare-only
.venv/bin/python macos/scripts/build_preview.py --resume macos/dist/previews/<printed-id>
```

`--package-cache <path>` may reuse an Xcode Swift package cache while still
requiring the checked-in `Package.resolved`. Network proxies, when necessary,
should be supplied per command rather than changing global Git configuration.

### Existing Developer ID packaging scripts

`scripts/build_local.sh` requires an existing standalone CPython under
`macos/dist/python` (not a symlink to a developer's environment). It now installs
the current checkout's wheel plus **history, MCP and knowledge** extras, using
hash-checked runtime dependencies exported from `uv.lock`. It no longer overlays
new source onto an unchecked old environment. The helper verifies source hashes,
framework version and optional modules, then writes a credential-free
`studio-bundle-provenance.json` inside that Python bundle.

The release script no longer silently downloads Studio 0.1.8 from PyPI:

```bash
# Explicit published version, which must match both this checkout's version and source.
STUDIO_VERSION=<published-checkout-version> ./macos/scripts/build_release.sh
# Explicit current-source preview; uses distinct source-preview app/DMG names.
SOURCE_BUILD=1 ./macos/scripts/build_release.sh
```

Both scripts perform Developer ID signing when actually executed. These commands
are packaging actions, not harmless build checks. Source previews must not be
added to the production appcast. No project version is bumped by these changes.
The standalone Python download, signing/notarization, PostgreSQL service and
embedding model are separate prerequisites: installing extras does not bundle or
start a database or download the embedding weights. Runtime dependencies are
locked; the isolated setuptools wheel-build environment is not part of that lock.

## Distribution prerequisites

Turning this into a downloadable `.dmg` needs a **self-contained server** inside the `.app`:

- ⚠️ The studio **spawns Python agent subprocesses** (`sys.executable …`), so a plain PyInstaller
  freeze breaks agent launch — the frozen binary isn't a Python interpreter. Options: bundle a
  relocatable Python (python-build-standalone) + the package, or a PyInstaller build that re-invokes
  itself to run the runner. **Tackle this as its own step.**
- Add entitlements (`com.apple.security.network.client`), code-sign + notarize, package a `.dmg`,
  attach it to a GitHub Release.
- The native shell already uses `--free-port --port-file` to avoid a fixed-port conflict.
