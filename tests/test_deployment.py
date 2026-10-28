"""Offline deployment contract checks. Docker acceptance is an explicit probe."""
import importlib.util
import os
from pathlib import Path
import signal
from types import SimpleNamespace
from urllib.parse import urlsplit, unquote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_serve():
    spec = importlib.util.spec_from_file_location("studio_deploy_serve", ROOT / "deploy/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_only_publishes_loopback_app_and_never_mounts_host_home():
    spec = yaml.safe_load((ROOT / "compose.studio.yml").read_text())
    assert spec["services"]["app"]["ports"] == ["127.0.0.1:9904:8080"]
    assert "ports" not in spec["services"]["db"]
    assert spec["services"]["app"]["volumes"] == ["studio-home:/home/studio/.co-studio"]
    assert spec["services"]["db"]["volumes"] == ["studio-db:/var/lib/postgresql/data"]
    assert spec["services"]["app"]["environment"]["STUDIO_AUTO_AUTH"] == "0"
    assert spec["services"]["app"]["cap_drop"] == ["ALL"]
    assert spec["services"]["app"]["security_opt"] == ["no-new-privileges:true"]
    assert "@sha256:" in spec["services"]["db"]["image"]


def test_optional_model_mount_is_explicit_read_only_and_cannot_create_host_path():
    spec = yaml.safe_load((ROOT / "deploy/compose.model-cache.yml").read_text())
    mount, = spec["services"]["app"]["volumes"]
    assert mount["read_only"] is True and mount["bind"]["create_host_path"] is False
    assert mount["target"] == "/home/studio/.co-studio/embedding-cache"
    assert "${STUDIO_EMBEDDING_CACHE:?" in mount["source"]


def test_build_is_locked_nonroot_and_uses_whitelist_context():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "@sha256:" in dockerfile.splitlines()[0]
    assert "uv sync --frozen --no-dev" in dockerfile
    assert dockerfile.index("--no-install-project") < dockerfile.index("COPY co_studio")
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/uv" in dockerfile
    assert "USER studio" in dockerfile and "--uid 10001" in dockerfile
    assert "COPY . ." not in dockerfile
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ignored[0] == "*"
    assert set(line for line in ignored if line.startswith("!")) == {
        "!pyproject.toml", "!uv.lock", "!README.md", "!co_studio/", "!co_studio/**", "!deploy/", "!deploy/**"}
    assert "**/.env" in ignored and "**/keys.env" in ignored


def test_proxy_preserves_origin_and_host_and_overwrites_untrusted_forwarded_peer():
    nginx = (ROOT / "deploy/nginx.conf").read_text()
    assert "proxy_pass http://127.0.0.1:9900" in nginx
    assert "proxy_set_header Host $http_host" in nginx
    assert "proxy_set_header X-Forwarded-For 127.0.0.1" in nginx
    assert "proxy_set_header Origin" not in nginx
    assert 'if ($studio_origin != "$scheme://$http_host") { return 403; }' in nginx
    assert "if ($studio_fetch_allowed = 0) { return 403; }" in nginx
    assert "map $http_sec_fetch_site $studio_fetch_allowed" in nginx
    assert "proxy_buffering off" in nginx and "return 403" in nginx
    for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        assert f"{kind}_temp_path /tmp/" in nginx


@pytest.mark.parametrize("password", ["", "short"])
def test_missing_password_stops_before_database_or_process(monkeypatch, password):
    serve = load_serve()
    monkeypatch.setenv("STUDIO_DATABASE_PASSWORD", password)
    monkeypatch.setattr(serve, "PostgresArchive", lambda _: pytest.fail("must not open database"))
    with pytest.raises(SystemExit, match="at least 16 characters"):
        serve.main()


def test_database_error_never_discloses_password_or_raw_exception(monkeypatch):
    serve = load_serve()
    secret = "synthetic-secret-for-test"
    monkeypatch.setenv("STUDIO_DATABASE_PASSWORD", secret)
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "unused")
    monkeypatch.setenv("STUDIO_KNOWLEDGE_DATABASE_URL", "unused")
    def fail(_):
        raise ValueError(secret)
    monkeypatch.setattr(serve, "PostgresArchive", fail)
    with pytest.raises(SystemExit) as failure:
        serve.main()
    assert secret not in str(failure.value)
    assert str(failure.value) == "Container database initialization failed; credentials are not logged"


def test_supervisor_uses_fixed_database_and_bounded_graceful_shutdown(monkeypatch):
    serve = load_serve()
    password = "synthetic@:/?#password"
    monkeypatch.setenv("STUDIO_DATABASE_PASSWORD", password)
    # Keep main's exported DSNs scoped to this test, too.
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "unused")
    monkeypatch.setenv("STUDIO_KNOWLEDGE_DATABASE_URL", "unused")
    initialized, commands, children, handlers = [], [], [], {}
    def store(dsn):
        initialized.append(dsn)
        return SimpleNamespace(initialize=lambda: None)
    class Child:
        returncode = None
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = -signal.SIGTERM
        def wait(self, timeout):
            assert timeout == 10
            return self.returncode
    def launch(command):
        commands.append(command)
        child = Child()
        children.append(child)
        return child
    monkeypatch.setattr(serve, "PostgresArchive", store)
    monkeypatch.setattr(serve, "KnowledgeStore", store)
    monkeypatch.setattr(serve.subprocess, "Popen", launch)
    monkeypatch.setattr(serve.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    monkeypatch.setattr(serve.time, "sleep", lambda _: handlers[signal.SIGTERM]())
    assert serve.main() == 0
    assert initialized[0] == initialized[1] == os.environ["STUDIO_HISTORY_DATABASE_URL"]
    parsed = urlsplit(initialized[0])
    assert parsed.hostname == "db" and parsed.port == 5432 and parsed.path == "/studio"
    assert unquote(parsed.password) == password
    assert commands[0][-2:] == ["--timeout-graceful-shutdown", "5"]
    assert commands[0][commands[0].index("--host") + 1] == "127.0.0.1"
    assert commands[0][commands[0].index("--forwarded-allow-ips") + 1] == "127.0.0.1"
    assert len(children) == 2 and all(child.returncode == -signal.SIGTERM for child in children)


def test_synthetic_probe_refuses_desktop_execution(monkeypatch):
    spec = importlib.util.spec_from_file_location("studio_deploy_smoke", ROOT / "deploy/smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, "getuid", lambda: 501)
    with pytest.raises(RuntimeError, match="isolated non-root"):
        module.isolated_archive()
