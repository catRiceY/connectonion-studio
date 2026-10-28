"""Agent process lifecycle: spawn, health-poll, stop, and orphan adoption."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import config, logs, ports, registry, workspaces
from .registry import AgentMeta

# Spawn each agent in its OWN process group so we can tear it (and any children) down as a
# unit: POSIX uses setsid()+killpg; Windows uses a new process group + psutil tree-kill.
if os.name == "nt":
    _SPAWN_KWARGS = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    _SPAWN_KWARGS = {"start_new_session": True}


def _http_json(url: str, timeout: float = 2.0) -> dict:
    """GET a local URL and parse the JSON body."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — localhost only
        return json.loads(response.read().decode("utf-8"))


def fetch_info(port: int) -> dict | None:
    """GET /info from a local agent, or None if unreachable."""
    try:
        return _http_json(f"http://127.0.0.1:{port}/info")
    except Exception:  # noqa: BLE001 — any failure means "not reachable"
        return None


@dataclass
class ProcRecord:
    """Live bookkeeping for one agent process."""

    pid: int | None = None
    popen: subprocess.Popen | None = None
    state: str = "stopped"
    started_at: float = 0.0
    stop_requested: bool = False
    last_error: str | None = None
    log_path: Path | None = None   # this run's stdout capture (runs/<timestamp>.log)


class Supervisor:
    """Owns agent subprocesses and their observed states (creating→starting→online→...)."""

    def __init__(self) -> None:
        self._records: dict[str, ProcRecord] = {}
        self._subscribers: list[asyncio.Queue[bool]] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, slug: str) -> asyncio.Lock:
        """Per-slug lock serialising start/stop/restart so two starts can't double-spawn."""
        lock = self._locks.get(slug)
        if lock is None:
            lock = self._locks[slug] = asyncio.Lock()
        return lock

    # ── events ───────────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue[bool]:
        """Register a queue that receives a token on every state change."""
        queue: asyncio.Queue[bool] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bool]) -> None:
        """Remove a previously subscribed queue."""
        with suppress(ValueError):
            self._subscribers.remove(queue)

    def _notify(self) -> None:
        """Wake every status subscriber."""
        for queue in list(self._subscribers):
            queue.put_nowait(True)

    # ── state queries ────────────────────────────────────────────────────
    def state_of(self, slug: str) -> str:
        """Current observed state for a slug ("stopped" when untracked)."""
        record = self._records.get(slug)
        return record.state if record else "stopped"

    def last_error_of(self, slug: str) -> str | None:
        """Last recorded error for a slug, if any."""
        record = self._records.get(slug)
        return record.last_error if record else None

    def current_log_path(self, slug: str) -> Path | None:
        """This run's stdout file (or the most-recent run on disk when untracked)."""
        record = self._records.get(slug)
        if record and record.log_path:
            return record.log_path
        return logs.latest_run_log(registry.agent_dir(slug))

    def started_at_of(self, slug: str) -> float | None:
        """Epoch seconds when this run started, or None when not running."""
        record = self._records.get(slug)
        return record.started_at if record and record.started_at else None

    def forget(self, slug: str) -> None:
        """Drop tracking for a deleted agent."""
        if self._records.pop(slug, None) is not None:
            self._notify()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self, meta: AgentMeta) -> str:
        """Spawn the agent via the runner shim; reallocates the port if it went busy."""
        async with self._lock_for(meta.slug):  # two concurrent starts must not double-spawn
            return await self._start_locked(meta)

    async def _start_locked(self, meta: AgentMeta, *, preserve_port: bool = False) -> str:
        """Start under the caller's slug lock; Restart must retain its endpoint."""
        record = self._records.get(meta.slug)
        if record and record.state in ("starting", "online", "offline") and self._alive(record):
            return record.state
        verdict = await asyncio.to_thread(self._probe, meta.port, meta.address)
        if verdict == "online" and not preserve_port:  # existing ordinary-start adoption
            self._records[meta.slug] = ProcRecord(
                pid=self._read_pid(meta.slug), state="online", started_at=time.time(),
                log_path=logs.latest_run_log(registry.agent_dir(meta.slug)),
            )
            self._notify()
            return "online"
        if (preserve_port and verdict != "down") or not ports.is_free(meta.port):
            if preserve_port:
                # Never adopt a surviving process as a successful restart, or
                # move a phone's saved endpoint because another service owns it.
                self._records[meta.slug] = ProcRecord(
                    state="crashed", last_error=(
                        f"Cannot restart: port {meta.port} is in use. The endpoint was kept unchanged; "
                        "free the port and try Restart again."
                    ),
                )
                self._notify()
                return "crashed"
            with registry.locked():
                reserved = {m.port for m in registry.load_all() if m.slug != meta.slug}
                meta.port = ports.allocate(reserved)
                registry.save(meta)
        agent_dir = registry.agent_dir(meta.slug)
        try:
            work_dir = workspaces.ensure(
                meta,
                require_write=meta.preset == "co-ai" or any(
                    capability in {"file-write", "shell", "browser"}
                    for capability in meta.capabilities
                ),
            )
        except ValueError as exc:
            self._records[meta.slug] = ProcRecord(
                state="crashed", last_error=str(exc)
            )
            self._notify()
            return "crashed"
        # One fresh log file per start (runs/<timestamp>.log) so the live view shows ONLY this
        # run and every start→stop is kept for the Finder history. Second-granularity is fine:
        # a stop→start within the same second is the only collision and just shares one file.
        runs_dir = agent_dir / config.RUNS_DIR_NAME
        runs_dir.mkdir(exist_ok=True)
        log_path = runs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.log"
        # Reuse (not a new name) so this override MASKS any CO_STUDIO_PORT the user exported for the
        # studio's own port — each agent always gets its own port here, never the studio's.
        env = {
            **os.environ,
            "CO_STUDIO_PORT": str(meta.port),
            "CO_STUDIO_WORK_DIR": str(work_dir),
        }
        with open(log_path, "ab") as stdout:
            popen = subprocess.Popen(  # noqa: S603 — our own generated script
                # config.agent_python() == sys.executable everywhere except a frozen build; under
                # the bundled relocatable interpreter it IS the embedded python, so agents spawn
                # against a real interpreter that can import connectonion — no re-exec needed.
                [config.agent_python(), str(config.RUNNER_PATH), str(agent_dir / "agent.py")],
                cwd=work_dir,  # project context and relative tool paths are workspace-relative
                env=env,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                **_SPAWN_KWARGS,  # own process group for group teardown (POSIX + Windows)
            )
        (agent_dir / config.PIDFILE_NAME).write_text(str(popen.pid))
        self._records[meta.slug] = ProcRecord(
            pid=popen.pid, popen=popen, state="starting", started_at=time.time(), log_path=log_path
        )
        self._notify()
        return "starting"

    async def stop(self, slug: str) -> str:
        """SIGTERM the process group, 5s grace, then SIGKILL."""
        async with self._lock_for(slug):
            return await self._stop_locked(slug)

    async def _stop_locked(self, slug: str) -> str:
        """Stop under the caller's slug lock, also used by atomic Restart."""
        record = self._records.get(slug)
        if record is None or not self._alive(record):
            self._records[slug] = ProcRecord(state="stopped")
        else:
            record.stop_requested = True
            await asyncio.to_thread(self._terminate, record)
            record.state, record.popen, record.pid = "stopped", None, None
        self._remove_pidfile(slug)
        self._notify()
        return "stopped"

    async def restart(self, meta: AgentMeta) -> str:
        """Stop and start atomically without changing the Agent's endpoint."""
        async with self._lock_for(meta.slug):
            await self._stop_locked(meta.slug)
            return await self._start_locked(meta, preserve_port=True)

    def adopt_orphans(self) -> None:
        """Reattach to agent processes left running by a previous studio instance."""
        for meta in registry.load_all():
            pid = self._read_pid(meta.slug)
            if pid is None:
                continue
            if self._pid_alive(pid):
                self._records[meta.slug] = ProcRecord(
                    pid=pid, state="starting", started_at=time.time(),
                    log_path=logs.latest_run_log(registry.agent_dir(meta.slug)),
                )
            else:
                self._remove_pidfile(meta.slug)

    # ── health polling ───────────────────────────────────────────────────
    async def run(self, interval: float = config.HEALTH_INTERVAL) -> None:
        """Poll process liveness and /health + /info every `interval` seconds, forever."""
        while True:
            await self._poll_once()
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        for meta in registry.load_all():
            record = self._records.get(meta.slug)
            if record is None or record.state in ("stopped", "crashed"):
                continue
            if not self._alive(record):
                if record.stop_requested:
                    new_state = "stopped"
                else:
                    new_state = "crashed"
                    record.last_error = self._crash_reason(meta.slug, record)
                self._remove_pidfile(meta.slug)
            else:
                verdict = await asyncio.to_thread(self._probe, meta.port, meta.address)
                if verdict == "online":
                    new_state, record.last_error = "online", None
                elif verdict == "mismatch":  # PID-reuse guard: someone else answers on our port
                    new_state = "offline"
                    record.last_error = f"port {meta.port} is answered by a different agent"
                elif record.state == "starting" and time.time() - record.started_at < config.START_GRACE:
                    new_state = "starting"
                else:
                    new_state = "offline"
            if new_state != record.state:
                record.state = new_state
                self._notify()

    def _crash_reason(self, slug: str, record: ProcRecord) -> str:
        """Best crash explanation: last traceback from stdout, else the exit code."""
        tail = logs.read_tail(self.current_log_path(slug), 400)
        traceback = logs.find_last_traceback(tail)
        if traceback:
            return traceback
        code = record.popen.returncode if record.popen else "unknown"
        return f"process exited unexpectedly (code {code})"

    # ── low-level helpers ────────────────────────────────────────────────
    @staticmethod
    def _probe(port: int, expect_address: str) -> str:
        """Return online|mismatch|down from /health plus the /info address cross-check."""
        try:
            health = _http_json(f"http://127.0.0.1:{port}/health")
            info = _http_json(f"http://127.0.0.1:{port}/info")
        except Exception:  # noqa: BLE001 — connection refused/timeout/bad JSON all mean down
            return "down"
        if health.get("status") != "healthy":
            return "down"
        return "online" if info.get("address") == expect_address else "mismatch"

    @classmethod
    def _alive(cls, record: ProcRecord) -> bool:
        """Is the tracked process still running?"""
        if record.popen is not None:
            return record.popen.poll() is None
        return record.pid is not None and cls._pid_alive(record.pid)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Liveness probe. POSIX uses signal-0; Windows can't (os.kill would *terminate*)."""
        if os.name != "posix":
            import psutil  # Windows-only path; declared in pyproject
            return psutil.pid_exists(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate(self, record: ProcRecord) -> None:
        """Blocking group terminate: graceful → STOP_GRACE → force (runs in a thread)."""
        pid = record.pid or (record.popen.pid if record.popen else None)
        if pid is None:
            return
        if os.name == "posix":
            self._killpg(pid, signal.SIGTERM)
            deadline = time.time() + config.STOP_GRACE
            while time.time() < deadline:
                if not self._alive(record):
                    break
                time.sleep(0.2)
            else:
                self._killpg(pid, signal.SIGKILL)
        else:
            self._terminate_tree_windows(pid)
        if record.popen is not None:
            with suppress(subprocess.TimeoutExpired):
                record.popen.wait(timeout=2)  # reap the zombie

    @staticmethod
    def _killpg(pid: int, sig: int) -> None:
        """Signal the whole process group (start_new_session=True ⇒ pgid == pid)."""
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)

    @staticmethod
    def _terminate_tree_windows(pid: int) -> None:
        """Windows: terminate the agent and its child processes, force-kill any survivors."""
        import psutil  # Windows-only path; declared in pyproject

        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        procs = parent.children(recursive=True) + [parent]
        for proc in procs:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.terminate()
        _, alive = psutil.wait_procs(procs, timeout=config.STOP_GRACE)
        for proc in alive:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()

    @staticmethod
    def _read_pid(slug: str) -> int | None:
        """Read the agent's pidfile, if present and sane."""
        path = registry.agent_dir(slug) / config.PIDFILE_NAME
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _remove_pidfile(slug: str) -> None:
        """Delete a stale pidfile."""
        (registry.agent_dir(slug) / config.PIDFILE_NAME).unlink(missing_ok=True)


SUPERVISOR = Supervisor()
