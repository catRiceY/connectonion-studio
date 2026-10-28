"""Offline bundle contracts; never download, install, sign or start native apps."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("studio_bundle_runtime", ROOT / "macos/scripts/bundle_runtime.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def checkout(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.8"\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "connectonion"\nversion = "1.9.2"\n')
    (tmp_path / "co_studio").mkdir()
    (tmp_path / "co_studio/__init__.py").write_text('# synthetic source\n')
    bundle = tmp_path / "macos/dist/python"
    (bundle / "bin").mkdir(parents=True)
    python = bundle / "bin/python3"
    python.write_text("not an executable; subprocess is stubbed")
    return bundle, python


@pytest.mark.parametrize("mode,version", [("pypi", None), ("pypi", "0.1.7"), ("source", "0.1.7")])
def test_version_mismatch_stops_before_install_or_target_execution(tmp_path, monkeypatch, mode, version):
    value = module()
    _, python = checkout(tmp_path)
    monkeypatch.setattr(value.subprocess, "check_output", lambda *a, **k: pytest.fail("must not run target"))
    monkeypatch.setattr(value.subprocess, "run", lambda *a, **k: pytest.fail("must not install"))
    with pytest.raises(ValueError):
        value.install(python, mode, version, tmp_path)


def test_refuses_interpreter_outside_owned_bundle(tmp_path, monkeypatch):
    value = module()
    checkout(tmp_path)
    foreign = tmp_path / "user-python"
    foreign.write_text("synthetic")
    monkeypatch.setattr(value.subprocess, "check_output", lambda *a, **k: pytest.fail("must not execute outside interpreter"))
    with pytest.raises(ValueError, match="checkout"):
        value.validate_target(foreign, tmp_path)


def test_refuses_owned_path_resolving_to_foreign_prefix(tmp_path, monkeypatch):
    value = module()
    _, python = checkout(tmp_path)
    monkeypatch.setattr(value.subprocess, "check_output", lambda *a, **k: "/outside\n")
    with pytest.raises(ValueError, match="prefix"):
        value.validate_target(python, tmp_path)


@pytest.mark.parametrize("mode", ["source", "pypi"])
def test_exact_runtime_sync_and_source_verification_are_required(tmp_path, monkeypatch, mode):
    value = module()
    bundle, python = checkout(tmp_path)
    calls = []
    monkeypatch.setattr(value.subprocess, "check_output", lambda *a, **k: str(bundle) + "\n")
    monkeypatch.setattr(value.shutil, "which", lambda _: "/fake/uv")

    def run(command, **kwargs):
        calls.append(command)
        if "export" in command:
            Path(command[command.index("--output-file") + 1]).write_text("synthetic==1 --hash=sha256:fake\n")
        if "build" in command:
            output = Path(command[command.index("--out-dir") + 1])
            output.mkdir()
            (output / "synthetic-0.1.8-py3-none-any.whl").write_bytes(b"synthetic wheel")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(value.subprocess, "run", run)
    value.install(python, mode, "0.1.8" if mode == "pypi" else None, tmp_path)
    exported = calls[0]
    assert "--locked" in exported and "--no-dev" in exported and "--no-emit-project" in exported
    assert [exported[i + 1] for i, token in enumerate(exported) if token == "--extra"] == ["history", "mcp", "knowledge"]
    sync = next(call for call in calls if call[1:3] == ["pip", "sync"])
    assert "--require-hashes" in sync and str(python) in sync
    installed = next(call for call in calls if call[1:3] == ["pip", "install"])
    assert calls.index(sync) < calls.index(installed)
    assert "--no-deps" in installed and "--reinstall" in installed
    assert ("--no-index" in installed) is (mode == "source")
    assert calls[-1][0:2] == [str(python), "-I"] and calls[-1][-2] == "verify"
    provenance = json.loads((bundle / "studio-bundle-provenance.json").read_text())
    assert provenance["mode"] == mode and provenance["extras"] == ["history", "mcp", "knowledge"]
    assert provenance["source_files"] == value.source_hashes(tmp_path / "co_studio")
    assert str(tmp_path) not in json.dumps(provenance)


def test_no_success_provenance_if_verification_fails(tmp_path, monkeypatch):
    value = module()
    bundle, python = checkout(tmp_path)
    (bundle / "studio-bundle-provenance.json").write_text('{"previous": "successful build"}')
    monkeypatch.setattr(value.subprocess, "check_output", lambda *a, **k: str(bundle) + "\n")
    monkeypatch.setattr(value.shutil, "which", lambda _: "/fake/uv")

    def run(command, **kwargs):
        if command[-2] == "verify":
            raise value.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(value.subprocess, "run", run)
    with pytest.raises(value.subprocess.CalledProcessError):
        value.install(python, "pypi", "0.1.8", tmp_path)
    assert not (bundle / "studio-bundle-provenance.json").exists()


def test_verify_rejects_same_version_with_old_source(tmp_path, monkeypatch):
    value = module()
    checkout(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"studio_version": "0.1.8", "connectonion_version": "1.9.2", "source_files": {"__init__.py": "old"}}))
    monkeypatch.setattr(value.importlib.metadata, "version", lambda name: "0.1.8" if name == "connectonion-studio" else "1.9.2")
    monkeypatch.setattr(value.importlib.util, "find_spec", lambda _: object())
    with pytest.raises(RuntimeError, match="differs"):
        value.verify(manifest, tmp_path)


def test_native_scripts_use_verified_helper_and_explicit_release_selection():
    local = (ROOT / "macos/scripts/build_local.sh").read_text()
    release = (ROOT / "macos/scripts/build_release.sh").read_text()
    assert 'bundle_runtime.py" install "$DIST/python/bin/python3" --mode source' in local
    assert "syncUpdate" not in local
    assert '${STUDIO_VERSION:-0.1.8}' not in release
    assert release.index('Set STUDIO_VERSION') < release.index('curl --fail')
    assert 'bundle_runtime.py" install "$DIST/python/bin/python3" --mode pypi --version "$STUDIO_VERSION"' in release
    assert 'SOURCE_BUILD="${SOURCE_BUILD:-0}"' in release
    assert "source-preview" in release and "Do not add this artifact to the production appcast" in release
