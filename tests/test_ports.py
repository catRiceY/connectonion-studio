"""Port probes are local bind-only checks; no connection or Agent is started."""

import errno
import socket
from types import SimpleNamespace

import pytest

from co_studio import ports


class BindProbe:
    def __init__(self, *, time_wait=False, listening=False):
        self.time_wait = time_wait
        self.listening = listening
        self.options = []
        self.binds = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def setsockopt(self, *option):
        assert not self.binds, "Reuse must be configured before probing"
        self.options.append(option)

    def bind(self, address):
        self.binds.append(address)
        reused = (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in self.options
        if self.listening or self.time_wait and not reused:
            raise OSError(errno.EADDRINUSE, "Synthetic occupied port")


def install_probe(monkeypatch, probe, platform):
    def create(family, kind):
        assert (family, kind) == (socket.AF_INET, socket.SOCK_STREAM)
        return probe

    monkeypatch.setattr(ports, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(ports, "socket", SimpleNamespace(
        socket=create, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR,
    ))


def test_posix_time_wait_is_reusable_like_the_actual_host(monkeypatch):
    probe = BindProbe(time_wait=True)
    install_probe(monkeypatch, probe, "posix")
    assert ports.is_free(8001)
    assert probe.options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]
    assert probe.binds == [("", 8001)] and probe.closed


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_an_active_listener_is_not_reused_or_taken(monkeypatch, platform):
    probe = BindProbe(listening=True)
    install_probe(monkeypatch, probe, platform)
    assert not ports.is_free(8001)
    assert probe.binds == [("", 8001)] and probe.closed
    if platform == "nt":
        assert probe.options == []  # Windows REUSEADDR can allow competing binds.


def test_windows_keeps_default_non_reusing_bind_semantics(monkeypatch):
    probe = BindProbe()
    install_probe(monkeypatch, probe, "nt")
    assert ports.is_free(8001)
    assert probe.options == [] and probe.closed


def test_real_active_listener_remains_bound_and_probe_does_not_steal_it():
    # Binding/listening is allowed by the offline suite; no connect/send occurs.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert not ports.is_free(port)
        assert listener.getsockname()[1] == port
        listener.listen(1)  # Still usable; SO_ACCEPTCONN isn't supported on every OS.
        assert not ports.is_free(port)
    assert ports.is_free(port)


def test_allocate_skips_reserved_and_truly_occupied_ports(monkeypatch):
    checked = []
    monkeypatch.setattr(ports.config, "AGENT_PORT_RANGE", range(8000, 8004))

    def available(port):
        checked.append(port)
        return port != 8001

    monkeypatch.setattr(ports, "is_free", available)
    assert ports.allocate({8000}) == 8002
    assert checked == [8001, 8002]


def test_allocate_reports_exhaustion_without_claiming_a_busy_port(monkeypatch):
    monkeypatch.setattr(ports.config, "AGENT_PORT_RANGE", range(8000, 8002))
    monkeypatch.setattr(ports, "is_free", lambda _: False)
    with pytest.raises(RuntimeError, match="no free agent port"):
        ports.allocate(set())
