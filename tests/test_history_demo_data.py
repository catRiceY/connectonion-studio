"""The human-readable demo must not introduce accidental adapter warnings."""

from pathlib import Path
import runpy

from co_studio.execution_adapter import convert_snapshot


def test_readable_history_demo_has_valid_records_and_distinct_scenarios():
    rows = runpy.run_path(str(Path(__file__).parent / "manual/history_demo_data.py"))["demo_rows"]()
    assert len(rows) == len({row["session_id"] for row in rows}) == 25
    converted = {row["session_id"]: convert_snapshot(row, agent_slug="recorded") for row in rows}
    assert all(not result.issues for result in converted.values())
    normal = converted["demo-01-two-turn-diagnosis"]
    assert len(normal.turns) == 2
    assert normal.turns[0].result is None  # Do not invent earlier answers.
    assert "模拟回答" in normal.turns[-1].result
    timeout = converted["demo-02-tool-timeout"]
    assert any(event.payload.get("status") == "error" for event in timeout.events)
    paged = converted["demo-03-pagination-23-turns"]
    assert len(paged.turns) == 23 and len(paged.events) > 50
    assert paged.events[0].turn_no is None
    missing = converted["demo-25-missing-fields"]
    assert missing.session.updated_at is None
    assert all(event.duration_ms is None and "usage" not in event.payload for event in missing.events)
