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
