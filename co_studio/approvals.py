"""Local owner decisions, bound to one running Agent's exact tool call.

This is an additional gate, not a replacement for CO trust, native policy, or
the workspace sandbox. SQLite transactions coordinate Studio and Agent processes.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from filelock import FileLock
import psutil

from .execution_privacy import redact_value

_HEX = re.compile(r"^[a-f0-9]{32}$")
_ACTIVE = ("pending", "approved", "executing")
_DEFAULT = {"revision": 0, "mcp_enabled": False, "approvals_enabled": False,
            "approval_timeout_seconds": 120, "context_budget": None}
_UNSET = object()


class ToolsError(RuntimeError):
    pass


class ToolsInvalid(ToolsError):
    pass


class ToolsConflict(ToolsError):
    pass


class ToolsNotFound(ToolsError):
    pass


def _safe(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path:
        raise ToolsInvalid("Linked tool configuration/storage paths are not supported")
    if path.exists() and not (path.is_dir() or path.is_file()):
        raise ToolsInvalid("Invalid tool configuration/storage path")
    return path


def _read_json(path: Path, maximum: int = 16384) -> dict:
    _safe(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise ToolsInvalid("Tool configuration exceeds size limit")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise ToolsInvalid("Tool configuration is unreadable") from None


def _root(agent_dir: Path, create=False) -> Path:
    directory = _safe(Path(agent_dir))
    root = _safe(directory / ".studio-tools")
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    return root


def check_custom(agent_dir: Path) -> None:
    metadata = _read_json(Path(agent_dir) / "meta.json")
    if metadata.get("preset", "custom") != "custom":
        raise ToolsInvalid("Studio tool integration supports generated custom Agents only")
    from .prompt_versions import PromptError, _baseline
    try:
        _baseline(Path(agent_dir) / "agent.py")
    except PromptError:
        raise ToolsInvalid("Studio tool integration supports generated custom Agents only") from None


def tool_config(agent_dir: Path) -> dict:
    root = _root(agent_dir)
    path = _safe(root / "config.json")
    value = _read_json(path) if path.exists() else dict(_DEFAULT)
    # Earlier saved configurations predate the optional context setting.
    value.setdefault("context_budget", None)
    if (set(value) != set(_DEFAULT) or type(value["revision"]) is not int
            or value["revision"] < 0 or type(value["mcp_enabled"]) is not bool
            or type(value["approvals_enabled"]) is not bool
            or type(value["approval_timeout_seconds"]) is not int
            or not 30 <= value["approval_timeout_seconds"] <= 300):
        raise ToolsInvalid("Invalid tool configuration")
    budget = value["context_budget"]
    if budget is not None and (type(budget) is not int or not 128 <= budget <= 2_000_000):
        raise ToolsInvalid("Invalid context budget")
    return value


def config_view(agent_dir: Path) -> dict:
    check_custom(agent_dir)
    from .mcp_docs_server import document_tool_definitions
    return {**tool_config(agent_dir), "applies_on_next_start": True,
            "available_tools": [{"name": item["name"], "description": item["description"]}
                                for item in document_tool_definitions()] + [
                {"name": "studio_search_knowledge", "description": "Native read-only public + this Agent's indexed history RAG search; requires a configured index."}]}


def save_tool_config(agent_dir: Path, expected_revision: int, *, mcp_enabled: bool,
                     approvals_enabled: bool, approval_timeout_seconds: int, context_budget=_UNSET) -> dict:
    check_custom(agent_dir)
    if (type(expected_revision) is not int or expected_revision < 0
            or type(mcp_enabled) is not bool or type(approvals_enabled) is not bool
            or type(approval_timeout_seconds) is not int or not 30 <= approval_timeout_seconds <= 300):
        raise ToolsInvalid("Invalid tool configuration values")
    root = _root(agent_dir, create=True)
    with FileLock(str(_safe(root / "config.lock")), timeout=5):
        current = tool_config(agent_dir)
        if current["revision"] != expected_revision:
            raise ToolsConflict("Tool configuration changed; reload before saving")
        budget = current["context_budget"] if context_budget is _UNSET else context_budget
        if budget is not None and (type(budget) is not int or not 128 <= budget <= 2_000_000):
            raise ToolsInvalid("Invalid context budget")
        value = {"revision": expected_revision + 1, "mcp_enabled": mcp_enabled,
                 "approvals_enabled": approvals_enabled, "approval_timeout_seconds": approval_timeout_seconds,
                 "context_budget": budget}
        path = _safe(root / "config.json")
        temporary = root / f"config-{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return config_view(agent_dir)


def canonical(value) -> bytes:
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ToolsInvalid("Tool call must contain finite JSON data") from None
    if len(data) > 16384:
        raise ToolsInvalid("Tool call exceeds 16 KiB approval limit")
    return data


def call_binding(agent_slug, run_id, session_id, turn_no, call_id, tool_name, arguments) -> str:
    return hashlib.sha256(canonical({"agent_slug": agent_slug, "run_id": run_id,
                                   "session_id": session_id, "turn_no": turn_no, "call_id": call_id,
                                   "tool_name": tool_name, "arguments": arguments})).hexdigest()


class ApprovalStore:
    def __init__(self, agent_dir: Path, *, clock=time.time):
        self.agent_dir = _safe(Path(agent_dir))
        self.agent_slug = self.agent_dir.name
        self.root = _root(self.agent_dir)
        self.path = _safe(self.root / "approvals.sqlite3")
        self.clock = clock

    @contextmanager
    def _db(self):
        _root(self.agent_dir, create=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = _safe(Path(str(self.path) + suffix))
            if candidate.exists() and not candidate.is_file():
                raise ToolsInvalid("Approval database path is not a regular file")
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise ToolsInvalid("Approval storage permissions must be owner-only")
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, binding TEXT UNIQUE NOT NULL, "
                       "session_id TEXT NOT NULL, turn_no INTEGER NOT NULL, call_id TEXT NOT NULL, "
                       "tool_name TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL, "
                       "created_at REAL NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL, "
                       "decision TEXT, reason TEXT, owner_pid INTEGER NOT NULL, owner_created REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _expire(self, db):
        now = self.clock()
        for row in db.execute("SELECT id,status,expires_at,owner_pid,owner_created FROM approvals "
                              "WHERE status IN ('pending','approved','executing')").fetchall():
            try:
                alive = abs(psutil.Process(row["owner_pid"]).create_time() - row["owner_created"]) < 0.01
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                alive = False
            if not alive or row["status"] != "executing" and now >= row["expires_at"]:
                status = "unknown" if row["status"] == "executing" else "expired"
                reason = "Owner process ended; execution outcome is unknown" if status == "unknown" else "Approval expired"
                db.execute("UPDATE approvals SET status=?,reason=?,updated_at=? WHERE id=?",
                           (status, reason, now, row["id"]))

    def _view(self, row) -> dict:
        data = dict(row)
        data.pop("owner_pid")
        data.pop("owner_created")
        data["arguments"] = json.loads(data["arguments"])
        data["agent_slug"] = self.agent_slug
        return data

    def request(self, *, run_id: str, session_id: str, turn_no: int, call_id: str,
                tool_name: str, arguments: dict, ttl: int = 120) -> dict:
        if (not all(isinstance(value, str) and 0 < len(value) <= 512
                    for value in (run_id, session_id, call_id, tool_name))
                or type(turn_no) is not int or turn_no < 1 or type(arguments) is not dict
                or type(ttl) is not int or not 1 <= ttl <= 300):
            raise ToolsInvalid("Invalid approval call identity")
        binding = call_binding(self.agent_slug, run_id, session_id, turn_no, call_id, tool_name, arguments)
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE binding=?", (binding,)).fetchone()
            if row is None:
                count = db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
                active = db.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','approved','executing')").fetchone()[0]
                if count >= 10000 or active >= 64:
                    raise ToolsError("Approval storage capacity reached")
                now = self.clock()
                record_id = uuid.uuid4().hex
                db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (record_id, binding, session_id, turn_no, call_id, tool_name,
                            json.dumps(redact_value(json.loads(canonical(arguments)))), "pending", now,
                            now + ttl, now, None, None, os.getpid(), psutil.Process().create_time()))
                row = db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone()
            return self._view(row)

    def get(self, record_id: str) -> dict:
        if not isinstance(record_id, str) or not _HEX.fullmatch(record_id) or not self.path.exists():
            raise ToolsNotFound("Approval not found")
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise ToolsNotFound("Approval not found")
            return self._view(row)

    def list(self, *, status: str = "pending", limit: int = 50) -> dict:
        if status not in {"pending", "all"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ToolsInvalid("Invalid approval list filter")
        if not self.path.exists():
            return {"items": [], "total": 0}
        with self._db() as db:
            where = " WHERE status='pending'" if status == "pending" else ""
            total = db.execute("SELECT COUNT(*) FROM approvals" + where).fetchone()[0]
            rows = db.execute("SELECT * FROM approvals" + where + " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return {"items": [self._view(row) for row in rows], "total": total}

    def decide(self, record_id: str, *, binding: str, decision: str) -> dict:
        if decision not in {"approve", "reject"}:
            raise ToolsInvalid("Decision must be approve or reject")
        if not isinstance(record_id, str) or not _HEX.fullmatch(record_id) or not self.path.exists():
            raise ToolsNotFound("Approval not found")
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise ToolsNotFound("Approval not found")
            if binding != row["binding"]:
                raise ToolsConflict("Approval no longer matches the displayed call")
            if row["decision"] == decision:
                return self._view(row)  # Same action is idempotent, never re-executes.
            if row["status"] != "pending":
                raise ToolsConflict("Approval has already been decided or expired")
            status = "approved" if decision == "approve" else "rejected"
            db.execute("UPDATE approvals SET status=?,decision=?,updated_at=? WHERE id=?",
                       (status, decision, self.clock(), record_id))
            return self._view(db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone())

    def consume(self, record_id: str, binding: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone()
            if row is None or row["binding"] != binding or row["status"] != "approved":
                raise ToolsConflict("Approval is unavailable or has already been consumed")
            db.execute("UPDATE approvals SET status='executing',updated_at=? WHERE id=?", (self.clock(), record_id))
            return self._view(db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone())

    def finish(self, record_id: str, status: str, reason: str | None = None) -> dict:
        if status not in {"cancelled", "succeeded", "failed", "unknown"}:
            raise ToolsInvalid("Invalid terminal approval status")
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise ToolsNotFound("Approval not found")
            if row["status"] in _ACTIVE:
                db.execute("UPDATE approvals SET status=?,reason=?,updated_at=? WHERE id=?",
                           (status, reason[:300] if reason else None, self.clock(), record_id))
            return self._view(db.execute("SELECT * FROM approvals WHERE id=?", (record_id,)).fetchone())
