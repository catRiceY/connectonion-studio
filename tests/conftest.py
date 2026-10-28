"""Keep offline regression tests away from real Studio data and credentials."""

from contextlib import ExitStack
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch


def _no_network(*args, **kwargs):
    raise RuntimeError("Outbound networking is disabled in the offline test suite")


def pytest_configure(config):
    # Run before test collection: several modules resolve their paths at import.
    stack = ExitStack()
    config._studio_test_stack = stack
    test_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="co-studio-tests-")))
    stack.enter_context(patch.object(Path, "home", return_value=test_root))
    stack.enter_context(patch.dict("os.environ", {"AGENT_CONFIG_PATH": str(test_root / ".co")}))
    stack.enter_context(patch.object(socket.socket, "connect", _no_network))
    stack.enter_context(patch.object(socket.socket, "connect_ex", _no_network))
    stack.enter_context(patch.object(socket, "create_connection", _no_network))


def pytest_unconfigure(config):
    stack = getattr(config, "_studio_test_stack", None)
    if stack is not None:
        stack.close()
