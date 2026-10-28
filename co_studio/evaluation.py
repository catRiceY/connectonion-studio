"""Versioned offline contracts and isolated PostgreSQL synthetic capacity CLI.

Never reads Studio's production database environment or personal history. A
capacity run accepts only a bootstrap-created loopback benchmark database with
a matching identity marker. No DROP/DELETE, Agent host or paid model commands.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import time
from uuid import uuid4

from .context_eval import context_cases, run_context_evaluation
from .evaluation_cases import CASES_VERSION, engineering_cases, evaluate_engineering_case
from .execution_adapter import read_execution_stream
from .history_archive import PostgresArchive, prepare_capture

FORMAT_VERSION = 1
CAPACITY_VERSION = "synthetic-history-capacity-v1"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
_DB_NAME = re.compile(r"studio_benchmark_[a-z0-9_]{8,40}\Z")
_IDENTITY = re.compile(r"[a-f0-9]{32}\Z")
_SCENARIOS = ("read-success", "tool-error", "approval-declined", "missing-result", "interrupted",
              "max-iterations", "unicode-evidence", "long-tool-result", "llm-retry", "no-tool")


class EvaluationError(RuntimeError):
    """Safe CLI errors without credentials, SQL, configuration or user content."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _write_json(path: Path, value) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _new_output(path: Path) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path:
        raise EvaluationError("Linked evaluation output paths are not supported")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except OSError:
        raise EvaluationError("Evaluation output must be a new directory") from None
    return path


def _read_config(path: Path) -> dict:
    path = Path(path).absolute()
    try:
        if path.resolve() != path:
            raise EvaluationError("Linked benchmark configuration is not supported")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise EvaluationError("Invalid benchmark configuration file")
            data = json.loads(handle.read(16385))
        if not isinstance(data, dict):
            raise EvaluationError("Invalid benchmark configuration")
        return data
    except (OSError, ValueError, UnicodeError):
        raise EvaluationError("Benchmark configuration is unavailable") from None


def validated_connection(config: dict, *, admin: bool = False) -> tuple[str, dict]:
    """No service files, alternate addresses or implicit production DSN fallback."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    try:
        data = conninfo_to_dict(config.get("database_url", ""))
    except Exception:
        raise EvaluationError("Invalid benchmark database configuration") from None
    allowed = {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "connect_timeout", "application_name"}
    if (not data or set(data) - allowed or data.get("host") != "127.0.0.1"
            or data.get("hostaddr", "127.0.0.1") != "127.0.0.1" or data.get("port") not in {"55432", "55433"}
            or not data.get("dbname") or not data.get("user")):
        raise EvaluationError("Benchmark database must be explicitly configured on approved loopback ports")
    if not admin and (not _DB_NAME.fullmatch(data["dbname"]) or config.get("purpose") != CAPACITY_VERSION
                      or not isinstance(config.get("benchmark_id"), str) or not _IDENTITY.fullmatch(config["benchmark_id"])):
        raise EvaluationError("Capacity runs require a bootstrap-created benchmark database")
    # Explicit hostaddr and settings override libpq environment routing. Only
    # credential/SSL values from the passed JSON connection string are retained.
    dsn = make_conninfo(**data, **({"hostaddr": "127.0.0.1"} if "hostaddr" not in data else {}))
    return dsn, data


def bootstrap_database(admin_config: Path, output_config: Path) -> dict:
    """Create a fresh named benchmark DB; never reuse/drop an existing DB."""
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.sql import SQL, Identifier
    config_path = Path(output_config).absolute()
    if config_path.resolve() != config_path or config_path.exists() or not config_path.parent.is_dir():
        raise EvaluationError("Benchmark config output must be a new file in an existing directory")
    dsn, settings = validated_connection(_read_config(admin_config), admin=True)
    identity = uuid4().hex
    name = "studio_benchmark_" + datetime.now(timezone.utc).strftime("%Y%m%d") + "_" + identity[:12]
    target = make_conninfo(dsn, dbname=name, hostaddr="127.0.0.1")
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5, application_name="studio-benchmark-bootstrap") as connection:
            connection.execute(SQL("CREATE DATABASE {}").format(Identifier(name)))
        with psycopg.connect(target, connect_timeout=5, application_name="studio-benchmark-bootstrap") as connection:
            connection.execute("CREATE SCHEMA studio_benchmark")
            connection.execute("CREATE TABLE studio_benchmark.identity (singleton boolean PRIMARY KEY CHECK(singleton), identity text NOT NULL, purpose text NOT NULL)")
            connection.execute("INSERT INTO studio_benchmark.identity VALUES (TRUE,%s,%s)", (identity, CAPACITY_VERSION))
        _write_json(config_path, {"database_url": target, "benchmark_id": identity, "purpose": CAPACITY_VERSION})
    except (psycopg.Error, OSError):
        raise EvaluationError("Benchmark bootstrap failed; any new isolated database was preserved for inspection") from None
    return {"database_name": name, "port": int(settings["port"]), "purpose": CAPACITY_VERSION}


def _open_benchmark(config_path: Path):
    import psycopg
    config = _read_config(config_path)
    dsn, settings = validated_connection(config)
    try:
        with psycopg.connect(dsn, connect_timeout=5, application_name="studio-benchmark-verify") as connection:
            identity = connection.execute("SELECT identity,purpose FROM studio_benchmark.identity WHERE singleton").fetchone()
            if identity != (config["benchmark_id"], CAPACITY_VERSION):
                raise EvaluationError("Benchmark identity does not match the configuration")
            exists = connection.execute("SELECT to_regclass('studio_history.captures')").fetchone()[0]
            if exists is not None and connection.execute("SELECT count(*) FROM studio_history.captures").fetchone()[0]:
                raise EvaluationError("Capacity baseline requires a fresh empty benchmark database")
    except psycopg.Error:
        raise EvaluationError("Cannot verify the isolated benchmark database") from None
    return PostgresArchive(dsn), dsn, {"database_name": settings["dbname"], "port": int(settings["port"])}


def _metadata():
    root = Path(__file__).resolve().parent.parent
    files = ["evaluation.py", "evaluation_cases.py", "context_eval.py", "context_policy.py", "context_runtime.py",
             "context_input.py", "context_report.py", "context_failure.py",
             "execution_adapter.py", "execution_models.py", "execution_privacy.py", "history_archive.py",
             "mcp_readonly.py", "mcp_retry.py", "mcp_agent.py", "approvals.py"]
    hashes = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in files}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"format_version": FORMAT_VERSION, "run_id": uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(), "framework_version": version("connectonion"),
            "python_version": platform.python_version(), "code_commit": commit, "code_files_sha256": hashes,
            "machine": {"platform": platform.platform(), "logical_cpu_count": os.cpu_count()},
            "provider_input_tokens": None, "provider_output_tokens": None, "provider_total_tokens": None,
            "model_quality_evaluated": False}


def run_contract_evaluation(output: Path) -> dict:
    destination = _new_output(output)
    cases = engineering_cases()
    manifest = [asdict(case) for case in cases]
    context = run_context_evaluation()
    for case in context_cases():
        manifest.append({"case_id": "context/" + case.case_id, "category": case.category,
                         "inputs": {"messages": case.messages, "budget": case.budget}, "expected": list(case.expected),
                         "scoring_rule": "All exact retained fields plus pair validity plus approximate budget; not semantic quality"})
    rows = []
    for case in cases:
        started = time.perf_counter()
        actual, error = None, None
        try:
            actual = evaluate_engineering_case(case)
        except Exception as exc:
            error = type(exc).__name__
        rows.append({"case_id": case.case_id, "category": case.category, "strategy": "implementation-contract",
                     "expected": case.expected, "actual": actual, "passed": error is None and actual == case.expected,
                     "error": error, "elapsed_ms": (time.perf_counter() - started) * 1000,
                     "provider_input_tokens": None, "provider_output_tokens": None, "provider_total_tokens": None})
    # Same frozen case and unchanged expectation; retain the raw-CO failure and
    # separately measure the Studio intervention, not another independent case.
    gate_case = next(case for case in cases if case.case_id == "runtime-tool-invalid-arguments")
    started = time.perf_counter()
    gate_actual, gate_error = None, None
    try:
        gate_actual = evaluate_engineering_case(gate_case, studio_gate=True)
    except Exception as exc:
        gate_error = type(exc).__name__
    gate_passed = gate_error is None and gate_actual == gate_case.expected
    rows.append({"case_id": gate_case.case_id, "category": gate_case.category, "strategy": "studio-schema-gate",
                 "expected": gate_case.expected, "actual": gate_actual, "passed": gate_passed, "error": gate_error,
                 "elapsed_ms": (time.perf_counter() - started) * 1000,
                 "provider_input_tokens": None, "provider_output_tokens": None, "provider_total_tokens": None})
    for row in context["results"]:
        rows.append({**row, "case_id": "context/" + row["case_id"], "passed": row["scripted_task_pass"]})
    aggregate = {**_metadata(), "dataset_version": CASES_VERSION, "dataset_sha256": _digest(manifest),
                 "origin": "synthetic_offline_engineering_contracts", "independent_case_count": len(manifest),
                 "run_result_count": len(rows), "engineering_case_count": len(cases),
                 "engineering_passed": sum(row["passed"] for row in rows if row["strategy"] == "implementation-contract"),
                 "engineering_failed": sum(not row["passed"] for row in rows if row["strategy"] == "implementation-contract"),
                 "studio_schema_gate_comparison": {"case_id": gate_case.case_id, "passed": gate_passed,
                                                   "independent_additional_cases": 0},
                 "review_status": "agent-authored engineering fixtures; not a human-reviewed external holdout",
                 "context_case_count": context["case_count"], "context_by_strategy": context["summary"],
                 "context_policy_version": context["policy_version"], "context_estimator": context["estimator"],
                 "scoring_scope": "Engineering contracts and scripted context assertions; not production tasks or real-LLM quality"}
    _write_json(destination / "manifest.json", {"version": CASES_VERSION, "cases": manifest})
    _write_json(destination / "aggregate.json", aggregate)
    with (destination / "results.jsonl").open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(_json({"dataset_version": CASES_VERSION, "run_id": aggregate["run_id"], **row}) + "\n")
    return aggregate


def _turn_trace(task, turn, scenario):
    stamp = 1_700_000_000.0 + task
    prompt = f"[SYNTHETIC] task-{task:05d} scenario={scenario}: verify the recorded operation"
    trace = [{"type": "user_input", "id": f"u-{task}", "turn": turn, "content": prompt, "ts": stamp},
             {"type": "llm_call", "id": f"llm-{task}", "model": "synthetic-no-provider", "ts": stamp + .1}]
    reason, status, result = "natural", "done", f"[SYNTHETIC] observed task-{task:05d}"
    if scenario == "missing-result":
        return prompt, trace, "running", None, None
    llm_status = "interrupted" if scenario == "interrupted" else "error" if scenario == "llm-retry" else "success"
    trace.append({"type": "llm_result", "id": f"llm-{task}", "status": llm_status, "model": "synthetic-no-provider", "ts": stamp + .2})
    if scenario == "llm-retry":
        trace.extend([{"type": "llm_call", "id": f"retry-{task}", "model": "synthetic-no-provider"},
                      {"type": "llm_result", "id": f"retry-{task}", "status": "success", "model": "synthetic-no-provider"}])
    if scenario not in {"interrupted", "no-tool"}:
        tool_result = "[SYNTHETIC] read-only fixture evidence"
        if scenario == "long-tool-result":
            tool_result += " measured fixture detail" * 45
        elif scenario == "unicode-evidence":
            tool_result = "[SYNTHETIC] 合成检查：文件未修改；引用证据 Δ-42"
        trace.extend([{"type": "tool_call", "id": f"tc-{task}", "tool_id": f"tool-{task}", "name": "synthetic_inspect", "args": {"task": task}},
                      {"type": "tool_result", "id": f"tr-{task}", "tool_id": f"tool-{task}", "name": "synthetic_inspect",
                       "status": "error" if scenario in {"tool-error", "approval-declined"} else "success",
                       "result": None if scenario in {"tool-error", "approval-declined"} else tool_result,
                       **({"error": "Synthetic inspection failed", "error_type": "FixtureError"} if scenario == "tool-error" else {}),
                       **({"declined": True, "error": "Synthetic human denial"} if scenario == "approval-declined" else {})}])
    if scenario == "interrupted":
        reason, result = "interrupted", "[SYNTHETIC] interrupted, no success claim"
    elif scenario == "max-iterations":
        reason, result = "max_iterations", "[SYNTHETIC] incomplete iteration budget"
    trace.append({"type": "turn_result", "id": f"end-{task}", "turn": turn, "reason": reason, "ts": stamp + .9})
    return prompt, trace, status, result, None  # Missing actual duration/usage stays unknown.


def generate_capacity_sources(output: Path, *, turns: int = 10_000, turns_per_session: int = 10,
                              sessions_per_shard: int = 100) -> dict:
    if (type(turns) is not int or not 1 <= turns <= 100_000 or type(turns_per_session) is not int
            or not 1 <= turns_per_session <= 50 or type(sessions_per_shard) is not int or not 1 <= sessions_per_shard <= 100):
        raise EvaluationError("Invalid synthetic capacity dimensions")
    root = Path(output)
    root.mkdir(mode=0o700, exist_ok=False)
    total_sessions = math.ceil(turns / turns_per_session)
    counts, files, task = Counter(), [], 0
    for shard in range(math.ceil(total_sessions / sessions_per_shard)):
        slug = f"synthetic-benchmark-{shard:03d}"
        path = root / f"{slug}.jsonl"
        with path.open("x", encoding="utf-8") as handle:
            for local in range(sessions_per_shard):
                session_index = shard * sessions_per_shard + local
                if session_index >= total_sessions:
                    break
                identity, trace = f"synthetic-session-{session_index:05d}", []
                for turn in range(1, min(turns_per_session, turns - task) + 1):
                    scenario = _SCENARIOS[task % len(_SCENARIOS)]
                    counts[scenario] += 1
                    prompt, entries, status, result, duration = _turn_trace(task, turn, scenario)
                    trace.extend(entries)
                    snapshot = {"session_id": identity, "status": status, "prompt": prompt, "result": result,
                                "created": 1_700_000_000.0 + session_index, "updated_at": 1_700_000_000.0 + task,
                                "revision": turn, "duration_ms": duration,
                                "session": {"session_id": identity, "turn": turn, "trace": trace}}
                    handle.write(_json(snapshot) + "\n")
                    task += 1
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise EvaluationError("Synthetic shard exceeded the supported 64 MiB source limit")
        files.append({"agent_slug": slug, "file": path.name, "bytes": size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"dataset_version": CAPACITY_VERSION, "origin": "synthetic_generated_not_model_runs", "task_count": task,
            "session_count": total_sessions, "turns_per_session": turns_per_session, "source_snapshot_count": task,
            "scenario_templates": dict(counts), "files": files, "total_source_bytes": sum(item["bytes"] for item in files),
            "dataset_sha256": _digest(files), "real_model_task_count": 0, "provider_total_tokens": None}


def percentiles(values) -> dict:
    """Nearest-rank percentiles over all measured samples, including slow ones."""
    ordered = sorted(values)
    return {"samples": len(ordered), "method": "nearest-rank", "p50_ms": ordered[math.ceil(.5 * len(ordered)) - 1] if ordered else None,
            "p95_ms": ordered[math.ceil(.95 * len(ordered)) - 1] if ordered else None}


def run_capacity_evaluation(config: Path, output: Path, *, turns: int = 10_000) -> dict:
    store, dsn, database = _open_benchmark(config)  # Validate identity before output/schema changes.
    destination = _new_output(output)
    metadata = _metadata()
    (destination / "results.jsonl").touch(mode=0o600, exist_ok=False)
    try:
        return _run_capacity_evaluation(store, dsn, database, destination, metadata, turns=turns)
    except Exception as exc:
        failed = {**metadata, "dataset_version": CAPACITY_VERSION, "origin": "synthetic_generated_not_model_runs",
                  "run_status": "failed", "failed_operations": 1, "error": type(exc).__name__, "database": database,
                  "unique_synthetic_task_count": None,
                  "denominator_note": "Run incomplete; completed operations are retained in results.jsonl, no capacity success claimed"}
        _append_jsonl(destination / "results.jsonl", {"run_id": metadata["run_id"], "operation": "run_failed", "passed": False, "error": type(exc).__name__})
        if not (destination / "aggregate.json").exists():
            _write_json(destination / "aggregate.json", failed)
        raise


def _append_jsonl(path: Path, row: dict):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_json({"dataset_version": CAPACITY_VERSION, "provider_total_tokens": None, **row}) + "\n")


def _run_capacity_evaluation(store, dsn, database, destination, metadata, *, turns):
    import psycopg
    def record(row):
        _append_jsonl(destination / "results.jsonl", {"run_id": metadata["run_id"], **row})

    manifest = generate_capacity_sources(destination / "sources", turns=turns)
    _write_json(destination / "manifest.json", manifest)
    store.initialize()
    captures, save_times, repeat_times, page_times, detail_times = [], [], [], [], []
    started = time.perf_counter()
    for shard in manifest["files"]:
        with (destination / "sources" / shard["file"]).open("rb") as handle:
            history = read_execution_stream(handle, agent_slug=shard["agent_slug"], max_bytes=MAX_SOURCE_BYTES)
        if history.issues:
            raise EvaluationError("Synthetic source produced unexpected reader issues")
        for session in history.sessions:
            capture = prepare_capture(session)
            before = time.perf_counter()
            inserted = store.save(capture)
            elapsed = (time.perf_counter() - before) * 1000
            save_times.append(elapsed)
            if not inserted:
                raise EvaluationError("Initial synthetic import was not fresh")
            captures.append(capture)
            record({"operation": "initial_capture_import", "agent_slug": capture.session.agent_slug,
                         "session_id": capture.session.session_id, "capture_id": capture.capture_id,
                         "turn_count": len(capture.turns), "event_count": len(capture.events), "elapsed_ms": elapsed, "passed": True})
    ingest_seconds = time.perf_counter() - started
    if sum(len(capture.turns) for capture in captures) != turns:
        raise EvaluationError("Normalized synthetic task count does not match the manifest")
    for capture in captures:
        before = time.perf_counter()
        inserted = store.save(capture)
        elapsed = (time.perf_counter() - before) * 1000
        repeat_times.append(elapsed)
        record({"operation": "duplicate_capture_import", "capture_id": capture.capture_id,
                "created": inserted, "elapsed_ms": elapsed, "passed": not inserted})
        if inserted:
            raise EvaluationError("Duplicate synthetic import created another capture")
    # Verify every session across actual DB-backed pages, without concurrent writes.
    for shard in manifest["files"]:
        expected = {item.session.session_id for item in captures if item.session.agent_slug == shard["agent_slug"]}
        seen, offset = set(), 0
        while True:
            before = time.perf_counter()
            total, page = store.session_page(shard["agent_slug"], offset=offset, limit=23)
            elapsed = (time.perf_counter() - before) * 1000
            page_times.append(elapsed)
            identities = [item["session"].session_id for item in page]
            if total != len(expected) or len(set(identities)) != len(identities) or seen.intersection(identities):
                raise EvaluationError("Synthetic session pagination is inconsistent")
            seen.update(identities)
            record({"operation": "session_page", "agent_slug": shard["agent_slug"], "offset": offset,
                         "returned_sessions": len(page), "total_sessions": total, "elapsed_ms": elapsed, "passed": True})
            if not page:
                break
            offset += len(page)
        if seen != expected:
            raise EvaluationError("Synthetic session pagination lost sessions")
    # Read all captures back through production validation: all 10k task records,
    # not only sampled success examples. This is warm single-client store latency.
    for capture in captures:
        before = time.perf_counter()
        loaded = store.get_owned(capture.session.agent_slug, capture.capture_id)
        elapsed = (time.perf_counter() - before) * 1000
        detail_times.append(elapsed)
        if loaded is None or loaded["capture"] != capture:
            raise EvaluationError("Synthetic archived content did not round-trip")
        record({"operation": "capture_detail", "capture_id": capture.capture_id,
                     "checked_tasks": len(capture.turns), "elapsed_ms": elapsed, "passed": True})
    # Insert another observation of the SAME tasks between event pages. It is a
    # capture version, explicitly not an extra independently executed task.
    original = captures[0]
    first_page = [event.event_id for event in store.get_owned(original.session.agent_slug, original.capture_id)["capture"].events[:7]]
    changed = replace(original,
                      session=original.session.model_copy(update={"source_revision": (original.session.source_revision or 0) + 1,
                                                                    "updated_at": (original.session.updated_at or 0) + 999}),
                      turns=(*original.turns[:-1], original.turns[-1].model_copy(update={"result": "[SYNTHETIC] later observation, same task"})))
    from .execution_adapter import ConvertedSession
    revised = prepare_capture(ConvertedSession(changed.session, changed.turns, changed.events))
    if not store.save(revised):
        raise EvaluationError("Synthetic version test did not create a new capture")
    pinned = store.get_owned(original.session.agent_slug, original.capture_id)["capture"]
    pinned_ids = first_page + [event.event_id for event in pinned.events[7:]]
    version_count, _ = store.versions_page(original.session.agent_slug, original.session.session_id)
    if pinned != original or pinned_ids != [event.event_id for event in original.events] or version_count != 2:
        raise EvaluationError("Pinned synthetic capture changed across pagination")
    if store.get_owned("synthetic-wrong-owner", original.capture_id) is not None:
        raise EvaluationError("Synthetic archive ownership isolation failed")
    record({"operation": "fixed_capture_across_new_version", "capture_id": original.capture_id,
            "new_capture_id": revised.capture_id, "version_count": version_count, "passed": True})
    with psycopg.connect(dsn, connect_timeout=5, application_name="studio-benchmark-count") as connection:
        count = connection.execute("SELECT count(*) FROM (SELECT DISTINCT c.agent_slug,c.session_id,t.turn_no FROM studio_history.captures c JOIN studio_history.turns t USING(capture_id)) tasks").fetchone()[0]
        physical_turns = connection.execute("SELECT count(*) FROM studio_history.turns").fetchone()[0]
        physical_events = connection.execute("SELECT count(*) FROM studio_history.events").fetchone()[0]
        physical_captures = connection.execute("SELECT count(*) FROM studio_history.captures").fetchone()[0]
        database_bytes = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        postgres_version = connection.execute("SHOW server_version").fetchone()[0]
    if count != turns or physical_captures != len(captures) + 1:
        raise EvaluationError("Synthetic SQL task counts do not match the manifest")
    aggregate = {**metadata, "dataset_version": CAPACITY_VERSION, "dataset_sha256": manifest["dataset_sha256"],
                 "origin": manifest["origin"], "database": database, "postgres_version": postgres_version,
                 "unique_synthetic_task_count": count, "unique_session_count": len(captures),
                 "capture_count": physical_captures, "physical_turn_rows": physical_turns, "event_rows": physical_events,
                 "source_snapshot_count": manifest["source_snapshot_count"], "source_file_count": len(manifest["files"]),
                 "source_bytes": manifest["total_source_bytes"], "database_bytes": database_bytes,
                 "ingest_seconds": ingest_seconds, "ingest_tasks_per_second": count / ingest_seconds,
                 "duplicate_imports_attempted": len(captures), "duplicate_captures_created": 0,
                 "roundtrip_tasks_checked": count, "session_pagination_complete": True,
                 "fixed_capture_pagination": True, "cross_agent_isolation": True,
                 "failed_operations": 0, "run_status": "complete", "query_concurrency": 1,
                 "query_cache_condition": "warm after ingestion; no cold-cache or concurrent-load claim",
                 "latency": {"initial_capture_save": percentiles(save_times), "duplicate_capture_save": percentiles(repeat_times),
                             "session_page_23": percentiles(page_times), "validated_capture_detail": percentiles(detail_times)},
                 "metric_scope": "Production archive store methods, not HTTP end-to-end or real Agent/model runs"}
    _write_json(destination / "aggregate.json", aggregate)
    return aggregate


def merge_reports(reports: list[Path], output: Path) -> dict:
    """Keep component denominators separate; never sum unlike datasets as N."""
    destination = _new_output(output)
    components, unique_cases = [], set()
    for path in reports:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("dataset_version", value.get("dataset_id")), str):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            raise EvaluationError("Merge requires versioned JSON aggregate reports") from None
        # Credentials/configuration are never legitimate evaluation metrics.
        def has_connection_config(item):
            if isinstance(item, dict):
                return any(key.lower() in {"database_url", "dsn", "connection_string", "password"}
                           or has_connection_config(child) for key, child in item.items())
            return isinstance(item, list) and any(has_connection_config(child) for child in item)
        if has_connection_config(value):
            raise EvaluationError("Aggregate reports must not contain connection configuration")
        verified = []
        if "independent_case_count" in value:
            try:
                manifest = json.loads(Path(path).with_name("manifest.json").read_text(encoding="utf-8"))
                catalog = manifest["cases"]
                verified = [case["case_id"] for case in catalog]
                if len(set(verified)) != value["independent_case_count"] or _digest(catalog) != value["dataset_sha256"]:
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise EvaluationError("Independent case count requires a matching versioned case manifest") from None
        elif "independent_cases" in value:
            try:
                verified = sorted({row["case_id"] for row in value["results"]})
                if len(verified) != value["independent_cases"]:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise EvaluationError("Retrieval case count does not match its result identities") from None
        if any(not isinstance(identity, str) or not identity for identity in verified):
            raise EvaluationError("Invalid evaluation case identifier")
        unique_cases.update(verified)
        from .execution_privacy import redact_value
        components.append({"report_sha256": _digest(value), "verified_case_count": len(verified), "report": redact_value(value)})
    combined = {**_metadata(), "dataset_version": "studio-component-report-v1", "components": components,
                "independent_case_count": len(unique_cases), "case_ids": sorted(unique_cases),
                "denominator_policy": "Union verified unique case IDs only; repeated strategy/request rows and capacity tasks are excluded. Keep component metric denominators separate.",
                "model_quality_evaluated": any(item["report"].get("model_quality_evaluated") is True for item in components)}
    _write_json(destination / "aggregate.json", combined)
    return combined


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--admin-config", type=Path, required=True)
    bootstrap.add_argument("--config", type=Path, required=True)
    contracts = sub.add_parser("contracts")
    contracts.add_argument("--output", type=Path, required=True)
    capacity = sub.add_parser("capacity")
    capacity.add_argument("--config", type=Path, required=True)
    capacity.add_argument("--output", type=Path, required=True)
    capacity.add_argument("--turns", type=int, default=10_000)
    merge = sub.add_parser("merge")
    merge.add_argument("--report", action="append", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = bootstrap_database(args.admin_config, args.config)
        elif args.command == "contracts":
            result = run_contract_evaluation(args.output)
        elif args.command == "capacity":
            result = run_capacity_evaluation(args.config, args.output, turns=args.turns)
        else:
            result = merge_reports(args.report, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Never print original config/SQL/provider exceptions or DSNs.
        message = str(exc) if isinstance(exc, EvaluationError) else "Evaluation failed; existing benchmark outputs and database were preserved"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
