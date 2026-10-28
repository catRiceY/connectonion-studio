"""Relay SOCKS dependency smoke tests; never open a proxy/model connection."""
import asyncio
from types import SimpleNamespace

import pytest

from co_studio import setup_check


def test_installed_websocket_transport_uses_socks_implementation(monkeypatch):
    from websockets.asyncio import client
    from websockets.proxy import parse_proxy
    from websockets.uri import parse_uri

    observed = []
    sentinel = object()

    class Proxy:
        def __init__(self, *args):
            observed.append(args)

        async def connect(self, host, port, **kwargs):
            observed.append((host, port, kwargs))
            return sentinel

    assert callable(client.SocksProxy)  # Missing dependency previously installed a raising stub.
    monkeypatch.setattr(client, "SocksProxy", Proxy)
    result = asyncio.run(client.connect_socks_proxy(
        parse_proxy("socks5h://127.0.0.1:9999"), parse_uri("wss://relay.invalid/ws")))
    assert result is sentinel
    assert observed[0][-1] is True  # hostname is resolved at proxy
    assert observed[1][:2] == ("relay.invalid", 443)


@pytest.mark.parametrize("available", [True, False])
def test_doctor_distinguishes_dependency_from_relay_connectivity(monkeypatch, available):
    real_import = setup_check.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "websockets.asyncio.client":
            return SimpleNamespace(**({"SocksProxy": lambda: None} if available else {}))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(setup_check.importlib, "import_module", fake_import)
    result = next(item for item in setup_check.run_doctor()
                  if item["check"] == setup_check._RELAY_DEPS_CHECK)
    assert result["ok"] is available
    assert ("connectivity checked separately" if available else "restart") in result["detail"]
