#!/usr/bin/env python3
"""Build an isolated, ad-hoc signed local preview; never publish or replace apps.

Apple Silicon only. Downloads official CPython with a pinned release SHA-256,
installs this checkout plus locked extras, compiles the native shell, and seals a
uniquely named .app. PostgreSQL and embedding weights are NOT bundled. No Apple
account/keychain, Developer ID, notarization, DMG, or production updater is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import uuid

import bundle_runtime

REPO = Path(__file__).resolve().parents[2]
PREVIEWS = REPO / "macos/dist/previews"
PYTHON_ASSET = "cpython-3.12.13%2B20260718-aarch64-apple-darwin-install_only.tar.gz"
PYTHON_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20260718/" + PYTHON_ASSET
# Official GitHub release asset digest; pinned, never learned from the download itself.
PYTHON_SHA256 = "62aeee6161d57303a71a138b75fd5cc6fb8c89c4b1d9c7f0a052d89fa0b6652b"
PYTHON_RELEASE = "https://github.com/astral-sh/python-build-standalone/releases/tag/20260718"
PROJECT = REPO / "macos/ConnectOnionStudio/ConnectOnionStudio.xcodeproj"
LOCK = PROJECT / "project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
XCODE = "/Applications/Xcode.app/Contents/Developer"


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_workspace(path: Path) -> Path:
    path = path.absolute()
    if path.parent != PREVIEWS or path.resolve() != path or not path.is_dir():
        raise ValueError("Preview workspace must be a real, immediate child of macos/dist/previews")
    marker = json.loads((path / "preview-workspace.json").read_text())
    if marker != {"schema_version": 1, "python_sha256": PYTHON_SHA256}:
        raise ValueError("Unrecognized preview workspace; refusing to modify it")
    return path


def extract_python(archive: Path, workspace: Path) -> None:
    if sha256(archive) != PYTHON_SHA256:
        raise ValueError("Official Python SHA-256 mismatch; nothing will be extracted/executed")
    target = workspace / "python"
    if target.exists() or target.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing Python runtime")
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "python" or ".." in parts:
                raise ValueError("Unexpected path in Python archive")
        source.extractall(workspace, filter="data")


def prepare_workspace() -> Path:
    name = datetime.now(timezone.utc).strftime("preview-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    workspace = PREVIEWS / name
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "preview-workspace.json").write_text(json.dumps({"schema_version": 1, "python_sha256": PYTHON_SHA256}) + "\n")
    print(f"Preview workspace: {workspace}", flush=True)
    archive = workspace / "python.tar.gz"
    subprocess.run(["curl", "--fail", "--location", "--retry", "2", "--connect-timeout", "20", "--max-time", "300", PYTHON_URL, "-o", str(archive)], check=True)
    extract_python(archive, workspace)
    (workspace / "python-source.json").write_text(json.dumps({"release": PYTHON_RELEASE, "url": PYTHON_URL, "sha256": PYTHON_SHA256}, indent=2) + "\n")
    return workspace


def configure_preview(info: dict, identifier: str) -> dict:
    result = dict(info)
    result["CFBundleIdentifier"] = identifier
    result["CFBundleDisplayName"] = "ConnectOnion Studio Preview"
    result["COStudioIsolatedPreview"] = True
    result["SUEnableAutomaticChecks"] = False
    result["SUAutomaticallyUpdate"] = False
    result.pop("SUFeedURL", None)
    result.pop("SUPublicEDKey", None)
    return result


def sign_preview(app: Path) -> None:
    """Seal inside-out with the '-' identity: ad-hoc, never a keychain identity."""
    runtime = app / "Contents/Resources/python"
    macho = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf"}
    for file in sorted(runtime.rglob("*")):
        if file.is_file() and not file.is_symlink():
            with file.open("rb") as source:
                native = source.read(4) in macho
            if native:
                subprocess.run(["codesign", "--force", "--sign", "-", str(file)], check=True, stdout=subprocess.DEVNULL)
    # The Sparkle binary package ships ad-hoc signatures; re-seal nested bundles
    # after the native unsigned build, before sealing the top-level app.
    framework = app / "Contents/Frameworks/Sparkle.framework"
    if framework.exists():
        nested = [p for p in framework.rglob("*") if not p.is_symlink() and p.suffix in {".xpc", ".app"}]
        for item in sorted(nested, key=lambda p: len(p.parts), reverse=True):
            subprocess.run(["codesign", "--force", "--sign", "-", str(item)], check=True)
        for item in framework.glob("Versions/*/Autoupdate"):
            if not item.parent.is_symlink():
                subprocess.run(["codesign", "--force", "--sign", "-", str(item)], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(framework)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def build(workspace: Path, package_cache: Path | None) -> Path:
    workspace = validate_workspace(workspace)
    app = workspace / "ConnectOnion Studio Preview.app"
    if app.exists() or app.is_symlink():
        raise FileExistsError("Preview app already exists; create a fresh workspace instead")
    python = workspace / "python/bin/python3"
    print("[1/4] Install source and hash-locked runtime extras", flush=True)
    bundle_runtime.install(python, "source", None)
    source_provenance = json.loads((workspace / "python/studio-bundle-provenance.json").read_text())
    swift_hashes = {p.relative_to(PROJECT.parent).as_posix(): sha256(p) for p in PROJECT.parent.rglob("*") if p.is_file() and p.suffix in {".swift", ".pbxproj", ".plist", ".resolved"}}
    lock_digest = sha256(LOCK)
    print("[2/4] Compile unsigned macOS native shell", flush=True)
    derived = workspace / "DerivedData"
    cache = package_cache or workspace / "SourcePackages"
    identifier = "com.evanyifanyang.ConnectOnionStudio.preview." + workspace.name
    command = ["xcodebuild", "-project", str(PROJECT), "-scheme", "ConnectOnionStudio", "-configuration", "Debug", "-destination", "platform=macOS", "-derivedDataPath", str(derived), "-clonedSourcePackagesDirPath", str(cache), "-disableAutomaticPackageResolution", "-onlyUsePackageVersionsFromResolvedFile", "-skipPackageUpdates", "-scmProvider", "system", "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "PRODUCT_BUNDLE_IDENTIFIER=" + identifier, "build"]
    import os
    environment = dict(os.environ)
    environment["DEVELOPER_DIR"] = XCODE
    with (workspace / "xcodebuild.log").open("w") as log:
        subprocess.run(command, cwd=REPO, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)
    if sha256(LOCK) != lock_digest:
        raise RuntimeError("Swift lock changed during build")
    if any(sha256(PROJECT.parent / relative) != digest for relative, digest in swift_hashes.items()):
        raise RuntimeError("Native source changed during build; use a stable source snapshot")
    print("[3/4] Assemble isolated app and verify relocated interpreter", flush=True)
    subprocess.run(["ditto", str(derived / "Build/Products/Debug/ConnectOnionStudio.app"), str(app)], check=True)
    subprocess.run(["ditto", str(workspace / "python"), str(app / "Contents/Resources/python")], check=True)
    info = app / "Contents/Info.plist"
    info.write_bytes(plistlib.dumps(configure_preview(plistlib.loads(info.read_bytes()), identifier)))
    embedded = app / "Contents/Resources/python/bin/python3"
    manifest = app / "Contents/Resources/python/studio-bundle-provenance.json"
    subprocess.run([str(embedded), "-I", str(REPO / "macos/scripts/bundle_runtime.py"), "verify", str(manifest)], check=True)
    # Actual imports catch missing native dylibs that find_spec alone cannot.
    subprocess.run([str(embedded), "-I", "-c", "import co_studio, psycopg, mcp, fastembed, onnxruntime; print('Relocated imports passed')"], check=True)
    print("[4/4] Ad-hoc seal; no Developer ID / notarization / publication", flush=True)
    sign_preview(app)
    provenance = {"schema_version": 1, "kind": "isolated-local-preview", "python_url": PYTHON_URL, "python_sha256": PYTHON_SHA256, "studio": source_provenance, "swift_files": swift_hashes, "swift_lock_sha256": lock_digest, "bundle_identifier": identifier, "app_name": app.name, "signing": "ad-hoc", "database_bundled": False, "embedding_weights_bundled": False, "gui_acceptance": "not_yet_run"}
    (workspace / "preview-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"BUILT (not published): {app}", flush=True)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="Download/hash-check Python only; no native build")
    parser.add_argument("--resume", type=Path, help="Resume this script's previously prepared workspace")
    parser.add_argument("--package-cache", type=Path, help="Reuse an existing Xcode Swift package cache; lock still enforced")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("This pinned preview currently supports Apple Silicon macOS only")
    if sys.version_info < (3, 12):
        parser.error("Run this script with Python 3.12 or newer")
    for required in ("uv", "curl", "ditto", "codesign", "xcodebuild"):
        if shutil.which(required) is None:
            parser.error(f"Missing build prerequisite: {required}")
    workspace = validate_workspace(args.resume) if args.resume else prepare_workspace()
    if not args.prepare_only:
        build(workspace, args.package_cache)


if __name__ == "__main__":
    main()
