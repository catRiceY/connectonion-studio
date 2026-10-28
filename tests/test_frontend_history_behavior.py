"""Run the offline browser-controller tests without adding frontend dependencies."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_history_frontend_behaviors():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the dependency-free frontend behavior tests")
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "tests/frontend").glob("*.test.mjs"))
    assert files, "No frontend regression tests discovered"
    result = subprocess.run(
        [node, "--test", *(str(path) for path in files)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
