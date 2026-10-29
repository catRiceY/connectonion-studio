#!/usr/bin/env python3
"""Install the checkout's locked runtime into its owned standalone Python bundle.

No signing, publishing, model downloads or Agent startup. Runtime dependencies
are hash-locked; setuptools' isolated wheel-build environment is separate from
that runtime lock. The resulting source fingerprint must match this checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


REPO = Path(__file__).resolve().parents[2]
EXTRAS = ("history", "mcp", "knowledge")
REQUIRED_MODULES = (
    "psycopg", "mcp", "jsonschema", "fastembed", "co_studio.history_archive",
    "co_studio.knowledge", "co_studio.diagnostic_web", "co_studio.model_eval",
    "co_studio.context_failure", "co_studio.mcp_readonly",
)
SOURCE_SUFFIXES = {".py", ".html", ".css", ".js", ".tmpl", ".md", ".png"}


def source_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and "__pycache__" not in path.parts
    }


def validate_target(python: Path, repo: Path = REPO) -> Path:
    dist = (repo / "macos/dist").absolute()
    root = python.absolute().parent.parent
    # Preview runs own a fresh, non-overwriting child. Never accept a symlink
    # that redirects synchronization into an unrelated interpreter/environment.
    relative = root.relative_to(dist) if root.is_relative_to(dist) else None
    preview = relative is not None and len(relative.parts) == 3 and relative.parts[0] == "previews" and relative.parts[-1] == "python"
    if (root != dist / "python" and not preview) or root.resolve() != root:
        raise ValueError("Target must be this checkout's owned macos/dist Python bundle")
    resolved = python.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Target must be this checkout's macos/dist/python interpreter")
    observed = subprocess.check_output(
        [str(python), "-I", "-c", "import sys; print(sys.prefix)"], text=True,
    ).strip()
    if Path(observed).resolve() != root:
        raise ValueError("Target interpreter prefix is outside the owned bundle")
    return root


def validate_version(mode: str, requested: str | None, repo: Path = REPO) -> str:
    expected = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
    if mode == "pypi" and not requested:
        raise ValueError("PyPI mode requires an explicit STUDIO_VERSION; no default release is assumed")
    if requested is not None and requested != expected:
        raise ValueError("STUDIO_VERSION must match this checkout's project version")
    return expected


def verify(manifest_path: Path, repo: Path = REPO) -> None:
    """Runs via the target interpreter with -I, never the checkout on sys.path."""
    manifest = json.loads(manifest_path.read_text())
    if importlib.metadata.version("connectonion-studio") != manifest["studio_version"]:
        raise RuntimeError("Bundled Studio version mismatch")
    if importlib.metadata.version("connectonion") != manifest["connectonion_version"]:
        raise RuntimeError("Bundled framework does not match uv.lock")
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("Missing bundled features: " + ", ".join(missing))
    import co_studio
    actual = source_hashes(Path(co_studio.__file__).parent)
    if actual != manifest["source_files"]:
        raise RuntimeError("Installed package differs from the checkout; refusing to label old code as this build")
    if source_hashes(repo / "co_studio") != manifest["source_files"]:
        raise RuntimeError("Checkout changed during packaging; rerun from a stable source snapshot")
    print("Verified bundled source, framework and history/mcp/knowledge features")


def install(python: Path, mode: str, version: str | None, repo: Path = REPO) -> None:
    expected = validate_version(mode, version, repo)
    root = validate_target(python, repo)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Install uv before building a locked Studio bundle")
    # A prior successful build must not remain labelled current if this attempt
    # fails after synchronizing or replacing packages in the same owned bundle.
    provenance = root / "studio-bundle-provenance.json"
    provenance.unlink(missing_ok=True)
    lock_bytes = (repo / "uv.lock").read_bytes()
    packages = tomllib.loads(lock_bytes.decode())["package"]
    framework = next(item["version"] for item in packages if item["name"] == "connectonion")
    manifest = {
        "schema_version": 1, "mode": mode, "studio_version": expected,
        "connectonion_version": framework, "extras": list(EXTRAS),
        "uv_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "source_files": source_hashes(repo / "co_studio"),
        "note": "Runtime dependencies are hash-locked; isolated wheel build tools are not in the runtime lock. Not a signed or published release.",
    }
    with tempfile.TemporaryDirectory(prefix="studio-bundle-") as temporary:
        temp = Path(temporary)
        requirements = temp / "requirements.txt"
        command = [uv, "export", "--locked", "--no-dev", "--no-emit-project", "--quiet", "--format", "requirements.txt"]
        for extra in EXTRAS:
            command += ["--extra", extra]
        subprocess.run(command + ["--output-file", str(requirements)], cwd=repo, check=True)
        wheel = None
        if mode == "source":
            subprocess.run([uv, "build", "--wheel", "--no-build-logs", "--python", str(python), "--out-dir", str(temp / "wheels"), str(repo)], cwd=repo, check=True)
            wheels = list((temp / "wheels").glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("Expected exactly one source wheel")
            wheel = wheels[0]
        # Exact sync removes stale optional dependencies, not just overlays code.
        subprocess.run([uv, "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)], check=True)
        package = ["--no-index", str(wheel)] if wheel else [f"connectonion-studio=={expected}"]
        subprocess.run([uv, "pip", "install", "--python", str(python), "--no-deps", "--reinstall", *package], check=True)
        subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)
        manifest_path = temp / "studio-bundle-provenance.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        subprocess.run([str(python), "-I", str(repo / "macos/scripts/bundle_runtime.py"), "verify", str(manifest_path)], check=True)
        if (repo / "uv.lock").read_bytes() != lock_bytes:
            raise RuntimeError("Lockfile changed during packaging")
        shutil.copyfile(manifest_path, provenance)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("install")
    build.add_argument("python", type=Path)
    build.add_argument("--mode", choices=("source", "pypi"), required=True)
    build.add_argument("--version")
    check = sub.add_parser("verify")
    check.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.action == "install":
        install(args.python, args.mode, args.version)
    else:
        verify(args.manifest)


if __name__ == "__main__":
    main()
