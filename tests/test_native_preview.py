"""Offline preview safety contracts: no builds, downloads, signing or model calls."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "macos/scripts"))
    spec = importlib.util.spec_from_file_location("preview", ROOT / "macos/scripts/build_preview.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_bad_official_hash_never_extracts(tmp_path, monkeypatch):
    value = load(monkeypatch)
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(b"untrusted archive")
    monkeypatch.setattr(value.tarfile, "open", lambda *a, **k: pytest.fail("must verify before parsing"))
    with pytest.raises(ValueError, match="SHA-256"):
        value.extract_python(archive, tmp_path)
    assert not (tmp_path / "python").exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "python/../../outside", "other/file"])
def test_archive_rejects_unexpected_paths(tmp_path, monkeypatch, name):
    value = load(monkeypatch)
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo(name)
        info.size = 1
        output.addfile(info, io.BytesIO(b"x"))
    monkeypatch.setattr(value, "PYTHON_SHA256", value.sha256(archive))
    with pytest.raises(ValueError, match="Unexpected path"):
        value.extract_python(archive, tmp_path)


def test_archive_rejects_escape_symlinks(tmp_path, monkeypatch):
    value = load(monkeypatch)
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("python/escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        output.addfile(info)
    monkeypatch.setattr(value, "PYTHON_SHA256", value.sha256(archive))
    with pytest.raises(tarfile.LinkOutsideDestinationError):
        value.extract_python(archive, tmp_path)


def test_preview_plist_is_separate_and_cannot_auto_update(monkeypatch):
    value = load(monkeypatch)
    original = {"SUFeedURL": "https://production", "SUPublicEDKey": "production-key", "SUEnableAutomaticChecks": True, "CFBundleIdentifier": "production.id"}
    info = value.configure_preview(original, "isolated.preview.id")
    assert info["COStudioIsolatedPreview"] is True
    assert info["CFBundleIdentifier"] == "isolated.preview.id"
    assert not info["SUEnableAutomaticChecks"] and not info["SUAutomaticallyUpdate"]
    assert "SUFeedURL" not in info and "SUPublicEDKey" not in info
    assert original["SUEnableAutomaticChecks"] is True


def test_workspace_requires_owned_marker_and_real_child(tmp_path, monkeypatch):
    value = load(monkeypatch)
    monkeypatch.setattr(value, "PREVIEWS", tmp_path)
    owned = tmp_path / "preview-test"
    owned.mkdir()
    (owned / "preview-workspace.json").write_text(json.dumps({"schema_version": 1, "python_sha256": value.PYTHON_SHA256}))
    assert value.validate_workspace(owned) == owned
    linked = tmp_path / "linked"
    linked.symlink_to(owned, target_is_directory=True)
    with pytest.raises(ValueError, match="real"):
        value.validate_workspace(linked)
    (owned / "preview-workspace.json").write_text("{}")
    with pytest.raises(ValueError, match="Unrecognized"):
        value.validate_workspace(owned)


def test_runtime_accepts_owned_preview_but_not_symlinked_root(tmp_path, monkeypatch):
    value = load(monkeypatch).bundle_runtime
    runtime = tmp_path / "macos/dist/previews/preview-test/python"
    (runtime / "bin").mkdir(parents=True)
    executable = runtime / "bin/python3"
    executable.write_text("fake; never run")
    monkeypatch.setattr(value.subprocess, "check_output", lambda *a, **k: str(runtime))
    assert value.validate_target(executable, tmp_path) == runtime
    alias = runtime.parent.parent / "preview-alias"
    alias.symlink_to(runtime.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="owned"):
        value.validate_target(alias / "python/bin/python3", tmp_path)


def test_preview_launch_has_allowlist_isolation_and_no_production_updater():
    server = (ROOT / "macos/ConnectOnionStudio/ConnectOnionStudio/StudioServer.swift").read_text()
    web = (ROOT / "macos/ConnectOnionStudio/ConnectOnionStudio/WebView.swift").read_text()
    assert 'environment.filter { allowed.contains($0.key) }' in server
    assert 'environment["STUDIO_DATA_DIR"]' in server
    assert 'environment["AGENT_CONFIG_PATH"]' in server
    assert 'environment["STUDIO_AUTO_AUTH"] = "0"' in server
    assert 'environment["HOME"] =' not in server
    assert 'proc.currentDirectoryURL = directory' in server
    assert 'guard !StudioServer.isPreview else { return }' in web
    assert 'configuration.websiteDataStore = .nonPersistent()' in web


def load_config(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    spec = importlib.util.spec_from_file_location("isolated_config", ROOT / "co_studio/config.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_explicit_config_isolates_studio_and_framework(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "preview/studio"))
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(tmp_path / "preview/framework"))
    value = load_config(monkeypatch, tmp_path)
    assert value.STUDIO_HOME == tmp_path / "preview/studio"
    assert value.AGENTS_DIR == tmp_path / "preview/studio/agents"
    assert value.KEYS_ENV == tmp_path / "preview/framework/keys.env"
    assert not value.STUDIO_HOME.exists()


@pytest.mark.parametrize("key", ["STUDIO_DATA_DIR", "AGENT_CONFIG_PATH"])
@pytest.mark.parametrize("bad", ["", "relative", "~/data"])
def test_config_rejects_ambiguous_relative_directory(monkeypatch, tmp_path, key, bad):
    monkeypatch.delenv("STUDIO_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    monkeypatch.setenv(key, bad)
    with pytest.raises(ValueError, match="absolute"):
        load_config(monkeypatch, tmp_path)


def test_default_config_paths_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("STUDIO_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    value = load_config(monkeypatch, tmp_path)
    assert value.STUDIO_HOME == tmp_path / ".co-studio"
    assert value.MAIN_CO_DIR == tmp_path / ".co"
