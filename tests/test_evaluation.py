"""Evaluation runner safety and counting contracts; no real database/network."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from co_studio import evaluation as evaluation
from co_studio.evaluation_cases import engineering_cases, evaluate_engineering_case
from co_studio.execution_adapter import read_execution_stream


def config(**changes):
    from psycopg.conninfo import make_conninfo
    values = {"host": "127.0.0.1", "port": "55433", "dbname": "studio_benchmark_test_12345678",
              "user": "synthetic", "password": "synthetic-not-a-secret"}
    values.update(changes)
    return {"database_url": make_conninfo(**values), "benchmark_id": "a" * 32, "purpose": evaluation.CAPACITY_VERSION}


@pytest.mark.parametrize("case", engineering_cases(), ids=lambda case: case.case_id)
def test_versioned_engineering_scenarios_execute_real_implementations(case):
    actual = evaluate_engineering_case(case)
    if case.case_id == "runtime-tool-invalid-arguments":
        # This upstream limitation remains an explicit failed safety assertion
        # in the report. A Python type hint is not input validation.
        assert actual["executions"] == 1
        assert actual != case.expected
    else:
        assert actual == case.expected


def test_contract_catalog_has_unique_scenarios_and_separate_result_denominators(tmp_path):
    report = evaluation.run_contract_evaluation(tmp_path / "contracts")
    manifest = json.loads((tmp_path / "contracts" / "manifest.json").read_text())
    rows = [json.loads(line) for line in (tmp_path / "contracts" / "results.jsonl").read_text().splitlines()]
    assert len({case["case_id"] for case in manifest["cases"]}) == 40
    assert report["independent_case_count"] == 40
    assert report["engineering_case_count"] == 34
    assert report["context_case_count"] == 6
    assert report["run_result_count"] == len(rows) == 59
    assert report["engineering_failed"] == 1
    assert report["engineering_passed"] == 33
    gate = next(row for row in rows if row["strategy"] == "studio-schema-gate")
    baseline = next(row for row in rows if row["strategy"] == "implementation-contract" and row["case_id"] == gate["case_id"])
    assert gate["expected"] == baseline["expected"]
    assert gate["passed"] and not baseline["passed"]
    assert gate["actual"]["executions"] == 0 and baseline["actual"]["executions"] == 1
    assert all(row["provider_total_tokens"] is None for row in rows)
    assert report["model_quality_evaluated"] is False
    assert report["code_files_sha256"]["history_archive.py"]
    assert len(report["dataset_sha256"]) == 64
    assert all(case.get("scoring_rule") and "expected" in case for case in manifest["cases"])


def test_capacity_generator_counts_tasks_not_events_snapshots_or_versions(tmp_path):
    manifest = evaluation.generate_capacity_sources(tmp_path / "sources", turns=121, sessions_per_shard=4)
    keys, snapshots, events = set(), 0, 0
    for item in manifest["files"]:
        path = tmp_path / "sources" / item["file"]
        snapshots += len(path.read_text().splitlines())
        assert item["bytes"] == path.stat().st_size < evaluation.MAX_SOURCE_BYTES
        with path.open("rb") as stream:
            result = read_execution_stream(stream, agent_slug=item["agent_slug"], max_bytes=evaluation.MAX_SOURCE_BYTES)
        assert result.issues == ()
        for session in result.sessions:
            for turn in session.turns:
                key = (turn.agent_slug, turn.session_id, turn.turn_no)
                assert key not in keys
                keys.add(key)
                assert turn.prompt.startswith("[SYNTHETIC]")
                assert turn.duration_ms is None
            events += len(session.events)
    assert len(keys) == manifest["task_count"] == snapshots == manifest["source_snapshot_count"] == 121
    assert events > 121
    assert manifest["session_count"] == 13
    assert len(manifest["files"]) == 4
    assert len(manifest["scenario_templates"]) == 10
    assert sum(manifest["scenario_templates"].values()) == 121
    assert manifest["real_model_task_count"] == 0
    assert manifest["provider_total_tokens"] is None


def test_synthetic_dataset_is_deterministic_across_output_directories(tmp_path):
    first = evaluation.generate_capacity_sources(tmp_path / "one", turns=23)
    second = evaluation.generate_capacity_sources(tmp_path / "two", turns=23)
    assert first == second


@pytest.mark.parametrize("changes", [{"turns": True}, {"turns": 0}, {"turns": 100001},
                                      {"turns_per_session": 51}, {"sessions_per_shard": 101}])
def test_capacity_rejects_invalid_dimensions_before_writing(tmp_path, changes):
    with pytest.raises(evaluation.EvaluationError):
        evaluation.generate_capacity_sources(tmp_path / "sources", **changes)
    assert not (tmp_path / "sources").exists()


def test_percentiles_are_nearest_rank_and_unknown_for_no_samples():
    assert evaluation.percentiles([9, 1, 3, 2, 7]) == {"samples": 5, "method": "nearest-rank", "p50_ms": 3, "p95_ms": 9}
    assert evaluation.percentiles([])["p95_ms"] is None


def test_capacity_connection_has_explicit_loopback_routing():
    from psycopg.conninfo import conninfo_to_dict
    dsn, _ = evaluation.validated_connection(config())
    assert conninfo_to_dict(dsn)["hostaddr"] == "127.0.0.1"


@pytest.mark.parametrize("changes", [
    {"host": "example.com"}, {"host": "localhost"}, {"host": "127.0.0.1,example.com"},
    {"hostaddr": "8.8.8.8"}, {"port": "5432"}, {"dbname": "studio_history"},
    {"dbname": "studio_benchmark_x;DROP DATABASE y"}, {"service": "personal"}, {"user": ""},
])
def test_production_remote_or_ambiguous_database_routes_rejected(changes):
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validated_connection(config(**changes))


def test_identity_marker_is_required_and_admin_exception_only_allows_bootstrap():
    value = config(dbname="knowledge")
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validated_connection(value)
    assert evaluation.validated_connection(value, admin=True)
    value = config()
    value["benchmark_id"] = "wrong"
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validated_connection(value)


def test_no_environment_dsn_fallback(monkeypatch):
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://synthetic:private@example.com/production")
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validated_connection({})


def test_configuration_refuses_symlinks_and_large_files(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config()))
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(evaluation.EvaluationError):
        evaluation._read_config(link)
    path.write_text(" " * 20000)
    with pytest.raises(evaluation.EvaluationError):
        evaluation._read_config(path)


def test_bootstrap_creates_only_new_named_database_and_private_config(tmp_path, monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    admin = tmp_path / "admin.json"
    admin.write_text(json.dumps(config(dbname="knowledge")))
    connections, statements = [], []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, sql, params=None):
            statements.append(str(sql))
    def connect(dsn, **kwargs):
        connections.append(conninfo_to_dict(dsn)["dbname"])
        return Connection()
    monkeypatch.setattr(psycopg, "connect", connect)
    output = tmp_path / "benchmark.json"
    result = evaluation.bootstrap_database(admin, output)
    assert result["database_name"].startswith("studio_benchmark_")
    assert connections == ["knowledge", result["database_name"]]
    assert not any("DROP" in statement or "DELETE" in statement for statement in statements)
    assert output.stat().st_mode & 0o077 == 0
    assert "synthetic-not-a-secret" not in json.dumps(result)
    assert evaluation.validated_connection(json.loads(output.read_text()))
    with pytest.raises(evaluation.EvaluationError):
        evaluation.bootstrap_database(admin, output)
    assert len(connections) == 2


def test_failed_capacity_run_preserves_safe_failure_artifact(tmp_path, monkeypatch, capsys):
    def fail():
        raise RuntimeError("postgresql://synthetic:private-secret@127.0.0.1/never-log")
    monkeypatch.setattr(evaluation, "_open_benchmark", lambda path: (SimpleNamespace(initialize=fail), "not-used",
                                                                  {"database_name": "studio_benchmark_test_12345678", "port": 55433}))
    output = tmp_path / "failed"
    assert evaluation.main(["capacity", "--config", "not-read.json", "--output", str(output), "--turns", "10"]) == 1
    aggregate = json.loads((output / "aggregate.json").read_text())
    assert aggregate["run_status"] == "failed"
    assert aggregate["unique_synthetic_task_count"] is None
    assert aggregate["failed_operations"] == 1
    assert "private-secret" not in (output / "aggregate.json").read_text()
    assert "private-secret" not in capsys.readouterr().err


def test_existing_output_never_overwritten(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    file = existing / "aggregate.json"
    file.write_text("keep me")
    with pytest.raises(evaluation.EvaluationError):
        evaluation.run_contract_evaluation(existing)
    assert file.read_text() == "keep me"


def test_merge_keeps_metrics_separate_and_rejects_connection_config(tmp_path):
    one, two = tmp_path / "a.json", tmp_path / "b.json"
    one.write_text(json.dumps({"dataset_version": "history-v1", "unique_synthetic_task_count": 10000}))
    two.write_text(json.dumps({"dataset_version": "retrieval-v1", "case_count": 12, "recall_at_5": .75}))
    result = evaluation.merge_reports([one, two], tmp_path / "merged")
    assert len(result["components"]) == 2
    assert "case_count" not in result and "unique_synthetic_task_count" not in result
    two.write_text(json.dumps({"dataset_version": "unsafe", "database_url": "postgresql://private:secret@localhost/db"}))
    with pytest.raises(evaluation.EvaluationError):
        evaluation.merge_reports([two], tmp_path / "rejected")


def test_merge_unions_verified_cases_not_strategy_rows_or_capacity_tasks(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    catalog = [{"case_id": f"engineering-{number}", "expected": True} for number in range(40)]
    (contracts / "manifest.json").write_text(json.dumps({"version": "engineering-v1", "cases": catalog}))
    aggregate = contracts / "aggregate.json"
    aggregate.write_text(json.dumps({"dataset_version": "engineering-v1", "independent_case_count": 40,
                                    "dataset_sha256": evaluation._digest(catalog)}))
    retrieval = tmp_path / "retrieval.json"
    retrieval.write_text(json.dumps({"dataset_id": "retrieval-v1", "independent_cases": 32, "request_count": 96,
                                     "results": [{"case_id": f"retrieval-{number}", "strategy": mode}
                                                 for number in range(32) for mode in ("bm25", "vector", "hybrid")]}))
    capacity = tmp_path / "capacity.json"
    capacity.write_text(json.dumps({"dataset_version": "capacity-v1", "unique_synthetic_task_count": 10000}))
    result = evaluation.merge_reports([aggregate, retrieval, capacity, aggregate], tmp_path / "combined")
    assert result["independent_case_count"] == 72
    assert [part["verified_case_count"] for part in result["components"]] == [40, 32, 0, 40]
    assert len(result["case_ids"]) == 72


def test_merge_refuses_forged_case_count_and_manifest_fingerprint(tmp_path):
    path = tmp_path / "aggregate.json"
    catalog = [{"case_id": "only-case"}]
    (tmp_path / "manifest.json").write_text(json.dumps({"cases": catalog}))
    path.write_text(json.dumps({"dataset_version": "bad-v1", "independent_case_count": 100,
                               "dataset_sha256": evaluation._digest(catalog)}))
    with pytest.raises(evaluation.EvaluationError):
        evaluation.merge_reports([path], tmp_path / "rejected-count")
    path.write_text(json.dumps({"dataset_version": "bad-v1", "independent_case_count": 1,
                               "dataset_sha256": "0" * 64}))
    with pytest.raises(evaluation.EvaluationError):
        evaluation.merge_reports([path], tmp_path / "rejected-hash")
