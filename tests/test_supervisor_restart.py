"""Offline lifecycle tests: fake processes, mocked health, temporary files only."""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
import socket
from types import SimpleNamespace

import pytest

from co_studio import ports, registry
from co_studio import supervisor as module


REAL_IS_FREE = ports.is_free


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    directory = tmp_path / "demo"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    meta = registry.AgentMeta(
        slug="demo", name="Synthetic", address="0xsynthetic", port=8001,
        model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z",
    )
    manager = module.Supervisor()
    calls, spawned, saved, killed = [], [], [], []
    monkeypatch.setattr(registry, "agent_dir", lambda _: directory)
    monkeypatch.setattr(registry, "locked", nullcontext)
    monkeypatch.setattr(registry, "load_all", lambda: [meta])
    monkeypatch.setattr(registry, "save", lambda value: saved.append(deepcopy(value)))
    monkeypatch.setattr(module.workspaces, "ensure", lambda *_a, **_k: workspace)
    monkeypatch.setattr(ports, "is_free", lambda _: True)

    def probe(port, address):
        calls.append(("probe", port))
        assert address == meta.address
        return "down"

    def spawn(args, **kwargs):
        calls.append(("spawn", int(kwargs["env"]["CO_STUDIO_PORT"])))
        process = Process(20000 + len(spawned))
        spawned.append((process, args, kwargs))
        return process

    def terminate(record):
        calls.append(("terminate", record.pid))
        killed.append(record.pid)
        record.popen.returncode = -15

    monkeypatch.setattr(manager, "_probe", probe)
    monkeypatch.setattr(manager, "_terminate", terminate)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(manager, "_killpg", lambda *_: pytest.fail("Must not signal a real process"))

    def running():
        process = Process(10000)
        manager._records[meta.slug] = module.ProcRecord(pid=process.pid, popen=process, state="online")
        (directory / module.config.PIDFILE_NAME).write_text(str(process.pid), encoding="utf-8")
        return process

    return SimpleNamespace(manager=manager, meta=meta, directory=directory, calls=calls,
                           spawned=spawned, saved=saved, killed=killed, running=running)


def test_quick_restart_preserves_port_when_stopped_connections_are_in_time_wait(lifecycle, monkeypatch):
    case = lifecycle
    old = case.running()
    # Controlled socket models TIME_WAIT; the suite forbids TCP connections.
    class TimeWaitSocket:
        reusable = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, level, option, value):
            assert (level, option, value) == (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.reusable = True

        def bind(self, address):
            assert address == ("", 8001)
            if not self.reusable:
                raise OSError("TIME_WAIT cannot be bound without reuse")

    # The fixture's availability stub is replaced with the real probe function.
    monkeypatch.setattr(ports, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ports, "socket", SimpleNamespace(
        socket=lambda *_: TimeWaitSocket(), AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR,
    ))
    monkeypatch.setattr(ports, "is_free", REAL_IS_FREE)
    monkeypatch.setattr(ports, "allocate", lambda _: pytest.fail("Restart must not allocate a different port"))
    assert asyncio.run(case.manager.restart(case.meta)) == "starting"
    assert old.returncode == -15 and case.meta.port == 8001 and case.saved == []
    assert case.calls == [("terminate", 10000), ("probe", 8001), ("spawn", 8001)]
    assert (case.directory / module.config.PIDFILE_NAME).read_text() == "20000"


@pytest.mark.parametrize("verdict", ["down", "mismatch", "online"])
def test_restart_refuses_a_real_owner_without_moving_endpoint_or_adopting_it(lifecycle, monkeypatch, verdict):
    case = lifecycle
    case.running()
    monkeypatch.setattr(case.manager, "_probe", lambda *_: verdict)
    monkeypatch.setattr(ports, "is_free", lambda _: False)
    monkeypatch.setattr(ports, "allocate", lambda _: pytest.fail("Must not migrate the endpoint"))
    assert asyncio.run(case.manager.restart(case.meta)) == "crashed"
    assert case.meta.port == 8001 and case.saved == case.spawned == []
    assert case.killed == [10000]  # Only our previously tracked process was stopped.
    assert case.manager.state_of("demo") == "crashed"
    assert "port 8001 is in use" in case.manager.last_error_of("demo")
    assert "endpoint was kept unchanged" in case.manager.last_error_of("demo")
    assert not (case.directory / module.config.PIDFILE_NAME).exists()


def test_restart_of_untracked_agent_does_not_signal_an_unrelated_port_owner(lifecycle, monkeypatch):
    case = lifecycle
    monkeypatch.setattr(ports, "is_free", lambda _: False)
    assert asyncio.run(case.manager.restart(case.meta)) == "crashed"
    assert case.killed == case.saved == case.spawned == [] and case.meta.port == 8001


def test_restart_failure_is_exposed_in_the_existing_api_state_and_detail(lifecycle, monkeypatch):
    from co_studio.api import agents

    case = lifecycle
    case.running()
    monkeypatch.setattr(agents, "SUPERVISOR", case.manager)
    monkeypatch.setattr(registry, "load", lambda slug: case.meta if slug == "demo" else None)
    monkeypatch.setattr(ports, "is_free", lambda _: False)
    assert asyncio.run(agents.restart_agent("demo")) == {"state": "crashed"}
    detail = agents.detail(case.meta)
    assert detail["state"] == "crashed" and detail["port"] == 8001
    assert "port 8001 is in use" in detail["last_error"]
    assert case.saved == case.spawned == []


def test_ordinary_start_keeps_existing_busy_port_reallocation_behavior(lifecycle, monkeypatch):
    case = lifecycle
    monkeypatch.setattr(ports, "is_free", lambda _: False)
    monkeypatch.setattr(ports, "allocate", lambda reserved: 8002)
    assert asyncio.run(case.manager.start(case.meta)) == "starting"
    assert case.meta.port == 8002 and [item.port for item in case.saved] == [8002]
    assert case.spawned[0][2]["env"]["CO_STUDIO_PORT"] == "8002"
    assert case.killed == []


def test_ordinary_start_still_adopts_an_existing_matching_agent(lifecycle, monkeypatch):
    case = lifecycle
    monkeypatch.setattr(case.manager, "_probe", lambda *_: "online")
    monkeypatch.setattr(case.manager, "_read_pid", lambda _: 12345)
    assert asyncio.run(case.manager.start(case.meta)) == "online"
    assert case.manager._records["demo"].pid == 12345
    assert case.spawned == case.saved == case.killed == []


def test_restart_holds_one_lock_across_stop_and_start_without_deadlocking(lifecycle, monkeypatch):
    case = lifecycle
    case.running()

    async def scenario():
        stopped, resume = asyncio.Event(), asyncio.Event()
        original_stop = case.manager._stop_locked

        async def pause_after_stop(slug):
            result = await original_stop(slug)
            stopped.set()
            await resume.wait()
            return result

        monkeypatch.setattr(case.manager, "_stop_locked", pause_after_stop)
        restarting = asyncio.create_task(case.manager.restart(case.meta))
        await asyncio.wait_for(stopped.wait(), timeout=2)
        competing_start = asyncio.create_task(case.manager.start(case.meta))
        await asyncio.sleep(0)
        assert not competing_start.done()
        assert case.manager._lock_for("demo").locked()
        assert case.spawned == []
        resume.set()
        results = await asyncio.wait_for(asyncio.gather(restarting, competing_start), timeout=2)
        assert results == ["starting", "starting"]

    asyncio.run(scenario())
    assert len(case.spawned) == 1 and case.killed == [10000]


def test_two_restarts_are_serialized_and_each_stops_only_its_previous_process(lifecycle):
    case = lifecycle
    case.running()

    async def scenario():
        return await asyncio.wait_for(asyncio.gather(
            case.manager.restart(case.meta), case.manager.restart(case.meta),
        ), timeout=2)

    assert asyncio.run(scenario()) == ["starting", "starting"]
    assert case.killed == [10000, 20000] and len(case.spawned) == 2
    assert case.meta.port == 8001 and case.saved == []
    assert case.manager._records["demo"].pid == 20001


def test_stop_while_restart_is_between_phases_runs_after_the_new_start(lifecycle, monkeypatch):
    case = lifecycle
    case.running()

    async def scenario():
        stopped, resume = asyncio.Event(), asyncio.Event()
        original_stop = case.manager._stop_locked

        async def pause_once(slug):
            result = await original_stop(slug)
            if not stopped.is_set():
                stopped.set()
                await resume.wait()
            return result

        monkeypatch.setattr(case.manager, "_stop_locked", pause_once)
        restarting = asyncio.create_task(case.manager.restart(case.meta))
        await asyncio.wait_for(stopped.wait(), timeout=2)
        stopping = asyncio.create_task(case.manager.stop(case.meta.slug))
        await asyncio.sleep(0)
        assert not stopping.done()
        resume.set()
        return await asyncio.wait_for(asyncio.gather(restarting, stopping), timeout=2)

    assert asyncio.run(scenario()) == ["starting", "stopped"]
    assert case.killed == [10000, 20000] and len(case.spawned) == 1
    assert case.manager.state_of("demo") == "stopped"
