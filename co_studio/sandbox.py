"""Workspace-confined file and command tools used by generated Studio agents.

File operations are canonical-path checked on every call. On macOS, shell commands
also run under Seatbelt (``sandbox-exec``), so a command cannot bypass the boundary
with an absolute path, a subshell, another interpreter, or a symlink. Other platforms
fail closed until an equivalent OS sandbox is available.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from connectonion.core.tool_result import ToolFailure


_DENIED = "Error: Workspace access denied"


def _display(root: Path) -> str:
    return str(root)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(
    root: Path,
    raw: str | Path,
    *,
    must_exist: bool,
) -> Path:
    value = Path(raw).expanduser()
    candidate = value if value.is_absolute() else root / value
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise ValueError(f"path does not exist: {raw}") from exc
    if not _inside(root, resolved):
        raise ValueError(f"path is outside workspace {_display(root)}: {raw}")
    return resolved


def _safe_pattern(pattern: str) -> bool:
    path = Path(pattern)
    return not path.is_absolute() and ".." not in path.parts


class SandboxedFileTools:
    """The framework FileTools API with canonical workspace path enforcement."""

    def __init__(self, work_dir: str | Path, permission: Literal["write", "read"] = "write"):
        from connectonion import useful_tools

        self.work_dir = Path(work_dir).expanduser().resolve(strict=True)
        self._permission = permission
        self._inner = useful_tools.FileTools(permission=permission)

    def _path(self, raw: str, *, must_exist: bool) -> str:
        return str(_resolve(self.work_dir, raw, must_exist=must_exist))

    @staticmethod
    def _error(exc: ValueError) -> str:
        return ToolFailure(f"{_DENIED} - {exc}")

    def read_file(self, path: str, offset: Optional[int] = None, limit: Optional[int] = None) -> str:
        """Read a file inside this Agent's workspace."""
        try:
            safe = self._path(path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self._inner.read_file(safe, offset, limit)

    def glob(self, pattern: str, path: Optional[str] = None) -> str:
        """Find files inside the workspace without accepting traversal patterns."""
        if not _safe_pattern(pattern):
            return ToolFailure(f"{_DENIED} - glob pattern must stay inside workspace: {pattern}")
        try:
            base = self._path(path or ".", must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self._inner.glob(pattern, base)

    def grep(
        self,
        pattern: str,
        path: Optional[str] = None,
        file_pattern: Optional[str] = None,
        output_mode: Literal["files", "content", "count"] = "files",
        context_lines: int = 0,
        ignore_case: bool = False,
        max_results: int = 50,
    ) -> str:
        """Search file contents inside the workspace."""
        if file_pattern and not _safe_pattern(file_pattern):
            return ToolFailure(f"{_DENIED} - file pattern must stay inside workspace: {file_pattern}")
        try:
            base = self._path(path or ".", must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self._inner.grep(
            pattern, base, file_pattern, output_mode, context_lines, ignore_case, max_results
        )

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """Edit a previously read file inside the workspace."""
        try:
            safe = self._path(file_path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self._inner.edit(safe, old_string, new_string, replace_all)

    def multi_edit(self, file_path: str, edits: list[dict[str, Any]]) -> str:
        """Apply multiple edits atomically to a file inside the workspace."""
        try:
            safe = self._path(file_path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self._inner.multi_edit(safe, edits)

    def write(self, path: str, content: str) -> str:
        """Create or overwrite a file inside the workspace."""
        try:
            safe = self._path(path, must_exist=False)
        except ValueError as exc:
            return self._error(exc)
        return self._inner.write(safe, content)


def _sbpl_string(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


class SandboxedShell:
    """A ``bash`` tool confined to one workspace by the host OS."""

    def __init__(self, work_dir: str | Path, runtime_dir: str | Path):
        self.work_dir = Path(work_dir).expanduser().resolve(strict=True)
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.home_dir = self.runtime_dir / "home"
        self.temp_dir = self.runtime_dir / "tmp"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def _cwd(self, raw: str) -> Path:
        return _resolve(self.work_dir, raw, must_exist=True)

    def _profile(self) -> str:
        workspace = _sbpl_string(self.work_dir)
        runtime = _sbpl_string(self.runtime_dir)
        # system.sb provides only the OS resources required to start ordinary command-line
        # programs. Additional read-only paths cover common Apple/Homebrew developer tools.
        return f'''(version 1)
(deny default)
(import "system.sb")
(allow process*)
(allow network*)
(allow file-read*
  (subpath "/bin")
  (subpath "/sbin")
  (subpath "/usr")
  (subpath "/System")
  (subpath "/Library/Apple")
  (subpath "/Library/Developer")
  (subpath "/Applications/Xcode.app")
  (subpath "/opt/homebrew")
  (subpath "/usr/local")
  (subpath "/private/etc")
  (subpath "/dev")
  (subpath "{workspace}")
  (subpath "{runtime}"))
(allow file-write* (subpath "{workspace}") (subpath "{runtime}"))'''

    def _launch(self, command: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise RuntimeError("Shell workspace sandbox is currently available only on macOS")
        env = {
            **os.environ,
            "HOME": str(self.home_dir),
            "TMPDIR": str(self.temp_dir),
            "CO_STUDIO_WORK_DIR": str(self.work_dir),
        }
        return subprocess.run(
            ["sandbox-exec", "-p", self._profile(), "/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=min(max(timeout, 1), 600),
        )

    @staticmethod
    def _format(result: subprocess.CompletedProcess[str]) -> str:
        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"STDERR:\n{result.stderr.rstrip()}")
        if result.returncode != 0:
            parts.append(f"\nExit code: {result.returncode}")
        output = "\n".join(parts) if parts else "(no output)"
        formatted = output[:10000] + (f"\n... (truncated, {len(output):,} total chars)" if len(output) > 10000 else "")
        return ToolFailure(formatted) if result.returncode != 0 else formatted

    def bash(self, command: str, description: str, cwd: str = ".", timeout: int = 120) -> str:
        """Run a shell command inside this Agent's workspace."""
        try:
            safe_cwd = self._cwd(cwd)
            result = self._launch(command, safe_cwd, timeout)
        except ValueError as exc:
            return ToolFailure(f"{_DENIED} - {exc}")
        except subprocess.TimeoutExpired:
            return ToolFailure(f"Error: Command timed out after {min(max(timeout, 1), 600)} seconds")
        except (OSError, RuntimeError) as exc:
            return ToolFailure(f"Error: {exc}")
        return self._format(result)


@dataclass
class _BackgroundTask:
    id: str
    command: str
    process: subprocess.Popen[str]
    output: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None


class SandboxedBackgroundTasks:
    """co-ai background commands using the same OS workspace sandbox as ``bash``."""

    def __init__(self, shell: SandboxedShell):
        self.shell = shell
        self._tasks: dict[str, _BackgroundTask] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def _reader(self, task: _BackgroundTask) -> None:
        assert task.process.stdout is not None
        for line in iter(task.process.stdout.readline, ""):
            if not line:
                break
            task.output.append(line.rstrip())
        task.process.wait()
        task.ended_at = time.time()

    def run_background(self, command: str, description: str = "") -> str:
        """Run a long command in the workspace and return a task id."""
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            return ToolFailure("Error: Shell workspace sandbox is currently available only on macOS")
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter}"
        env = {
            **os.environ,
            "HOME": str(self.shell.home_dir),
            "TMPDIR": str(self.shell.temp_dir),
            "CO_STUDIO_WORK_DIR": str(self.shell.work_dir),
        }
        try:
            process = subprocess.Popen(
                ["sandbox-exec", "-p", self.shell._profile(), "/bin/bash", "-lc", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=self.shell.work_dir,
                env=env,
            )
        except OSError as exc:
            return ToolFailure(f"Error: Could not start background command: {exc}")
        task = _BackgroundTask(task_id, command, process)
        with self._lock:
            self._tasks[task_id] = task
        threading.Thread(target=self._reader, args=(task,), daemon=True).start()
        suffix = f" ({description})" if description else ""
        return f"Task {task_id} started{suffix}: {command}"

    def task_output(self, task_id: str, tail: int = 50) -> str:
        """Return recent output and status for a background task."""
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return ToolFailure(f"Task '{task_id}' not found.")
        ended = task.ended_at or time.time()
        state = "running" if task.process.poll() is None else ("completed" if task.process.returncode == 0 else "failed")
        lines = task.output[-max(1, min(tail, 500)):]
        output = "\n".join(lines) if lines else "(no output yet)"
        return f"Task {task.id}: {state} ({ended - task.started_at:.1f}s)\nCommand: {task.command}\n\nOutput:\n{output}"

    def kill_task(self, task_id: str) -> str:
        """Terminate a running background task."""
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return ToolFailure(f"Task '{task_id}' not found.")
        if task.process.poll() is not None:
            return ToolFailure(f"Task '{task_id}' is not running.")
        task.process.send_signal(signal.SIGTERM)
        return f"Task '{task_id}' terminated."


def configure_process_environment(work_dir: str | Path, runtime_dir: str | Path) -> None:
    """Give framework helpers an agent-local HOME/TMP before they discover user state."""
    work = Path(work_dir).expanduser().resolve(strict=True)
    runtime = Path(runtime_dir).expanduser().resolve()
    home = runtime / "home"
    temp = runtime / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["TMPDIR"] = str(temp)
    os.environ["CO_STUDIO_WORK_DIR"] = str(work)
    os.chdir(work)


def _add_methods(agent: Any, instance: Any, names: tuple[str, ...]) -> None:
    for name in names:
        agent.remove_tool(name)
        agent.add_tool(getattr(instance, name))


class SandboxedBrowserPaths:
    """Guard BrowserAutomation APIs that can touch local filesystem paths."""

    def __init__(self, browser: Any, work_dir: str | Path):
        self.browser = browser
        self.work_dir = Path(work_dir).expanduser().resolve(strict=True)

    def _path(self, raw: str, *, must_exist: bool) -> str:
        return str(_resolve(self.work_dir, raw, must_exist=must_exist))

    @staticmethod
    def _error(exc: ValueError) -> str:
        return ToolFailure(f"{_DENIED} - {exc}")

    def go_to(self, url: str, purpose: str = "", who: str = "", hours: float = 0.0) -> str:
        """Navigate to a web URL; local file URLs are unavailable."""
        if not url_is_workspace_safe(url):
            return ToolFailure(f"{_DENIED} - browser file URLs are unavailable")
        return self.browser.go_to(url, purpose, who, hours)

    def newtab(self, url: str = "", purpose: str = "", who: str = "", hours: float = 0.0) -> str:
        """Open a web URL in a new tab; local file URLs are unavailable."""
        if url and not url_is_workspace_safe(url):
            return ToolFailure(f"{_DENIED} - browser file URLs are unavailable")
        return self.browser.newtab(url, purpose, who, hours)

    def run_page_script(self, script_path: str, args_json: str = "{}") -> str:
        """Run a script loaded from inside the workspace."""
        try:
            safe = self._path(script_path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self.browser.run_page_script(safe, args_json)

    def run_frame_script(
        self,
        script_path: str,
        args_json: str = "{}",
        frame_url_contains: str = "",
        frame_name: str = "",
        first_ok: bool = True,
    ) -> str:
        """Run a frame script loaded from inside the workspace."""
        try:
            safe = self._path(script_path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self.browser.run_frame_script(
            safe, args_json, frame_url_contains, frame_name, first_ok
        )

    def save_state(self, path: str) -> str:
        """Save browser state inside the workspace."""
        try:
            safe = self._path(path, must_exist=False)
        except ValueError as exc:
            return self._error(exc)
        return self.browser.save_state(safe)

    def take_screenshot(self, path: str | None = None, full_page: bool = False) -> str:
        """Save a screenshot inside the workspace."""
        try:
            safe = self._path(path or ".tmp/screenshot.png", must_exist=False)
        except ValueError as exc:
            return self._error(exc)
        return self.browser.take_screenshot(safe, full_page)

    def upload_file_by_selector(
        self,
        selector: str,
        file_path: str,
        index: int = 0,
        frame_url_contains: str = "",
        frame_name: str = "",
    ) -> str:
        """Upload a file from inside the workspace."""
        try:
            safe = self._path(file_path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self.browser.upload_file_by_selector(
            selector, safe, index, frame_url_contains, frame_name
        )

    def upload_file_after_click_by_selector(
        self,
        click_selector: str,
        file_path: str,
        index: int = 0,
        text: str = "",
        frame_url_contains: str = "",
        frame_name: str = "",
        timeout_ms: int = 5000,
    ) -> str:
        """Upload a file from inside the workspace after clicking an element."""
        try:
            safe = self._path(file_path, must_exist=True)
        except ValueError as exc:
            return self._error(exc)
        return self.browser.upload_file_after_click_by_selector(
            click_selector, safe, index, text, frame_url_contains, frame_name, timeout_ms
        )


_BROWSER_PATH_METHODS = (
    "go_to",
    "newtab",
    "run_page_script",
    "run_frame_script",
    "save_state",
    "take_screenshot",
    "upload_file_by_selector",
    "upload_file_after_click_by_selector",
)


def sandbox_browser_agent_tools(agent: Any, work_dir: str | Path) -> None:
    """Replace path-bearing BrowserAutomation tools while preserving session binding."""
    browser = agent.tools.get_instance("browserautomation")
    if browser is None:
        return
    _add_methods(agent, SandboxedBrowserPaths(browser, work_dir), _BROWSER_PATH_METHODS)


def create_sandboxed_browser(work_dir: str | Path, *, headless: bool = False) -> Any:
    """Create a BrowserAutomation whose local-path entry points are workspace guarded."""
    from connectonion.useful_tools.browser_tools import BrowserAutomation as FrameworkBrowser

    root = Path(work_dir).expanduser().resolve(strict=True)

    class BrowserAutomation(FrameworkBrowser):
        def _safe_path(self, raw: str, *, must_exist: bool) -> str:
            return str(_resolve(root, raw, must_exist=must_exist))

        def go_to(self, url: str, purpose: str = "", who: str = "", hours: float = 0.0) -> str:
            """Navigate to a web URL; local file URLs are unavailable."""
            if not url_is_workspace_safe(url):
                return ToolFailure(f"{_DENIED} - browser file URLs are unavailable")
            return super().go_to(url, purpose, who, hours)

        def newtab(self, url: str = "", purpose: str = "", who: str = "", hours: float = 0.0) -> str:
            """Open a web URL in a new tab; local file URLs are unavailable."""
            if url and not url_is_workspace_safe(url):
                return ToolFailure(f"{_DENIED} - browser file URLs are unavailable")
            return super().newtab(url, purpose, who, hours)

        def run_page_script(self, script_path: str, args_json: str = "{}") -> str:
            """Run a script loaded from inside the workspace."""
            try:
                safe = self._safe_path(script_path, must_exist=True)
            except ValueError as exc:
                return ToolFailure(f"{_DENIED} - {exc}")
            return super().run_page_script(safe, args_json)

        def run_frame_script(
            self,
            script_path: str,
            args_json: str = "{}",
            frame_url_contains: str = "",
            frame_name: str = "",
            first_ok: bool = True,
        ) -> str:
            """Run a frame script loaded from inside the workspace."""
            try:
                safe = self._safe_path(script_path, must_exist=True)
            except ValueError as exc:
                return ToolFailure(f"{_DENIED} - {exc}")
            return super().run_frame_script(safe, args_json, frame_url_contains, frame_name, first_ok)

        def save_state(self, path: str) -> str:
            """Save browser state inside the workspace."""
            try:
                safe = self._safe_path(path, must_exist=False)
            except ValueError as exc:
                return ToolFailure(f"{_DENIED} - {exc}")
            return super().save_state(safe)

        def take_screenshot(self, path: str | None = None, full_page: bool = False) -> str:
            """Save a screenshot inside the workspace."""
            try:
                safe = self._safe_path(path or ".tmp/screenshot.png", must_exist=False)
            except ValueError as exc:
                return ToolFailure(f"{_DENIED} - {exc}")
            return super().take_screenshot(safe, full_page)

        def upload_file_by_selector(
            self,
            selector: str,
            file_path: str,
            index: int = 0,
            frame_url_contains: str = "",
            frame_name: str = "",
        ) -> str:
            """Upload a file from inside the workspace."""
            try:
                safe = self._safe_path(file_path, must_exist=True)
            except ValueError as exc:
                return ToolFailure(f"{_DENIED} - {exc}")
            return super().upload_file_by_selector(selector, safe, index, frame_url_contains, frame_name)

        def upload_file_after_click_by_selector(
            self,
            click_selector: str,
            file_path: str,
            index: int = 0,
            text: str = "",
            frame_url_contains: str = "",
            frame_name: str = "",
            timeout_ms: int = 5000,
        ) -> str:
            """Upload a file from inside the workspace after clicking an element."""
            try:
                safe = self._safe_path(file_path, must_exist=True)
            except ValueError as exc:
                return ToolFailure(f"{_DENIED} - {exc}")
            return super().upload_file_after_click_by_selector(
                click_selector, safe, index, text, frame_url_contains, frame_name, timeout_ms
            )

    return BrowserAutomation(headless=headless)


# Explicitly reviewed co-ai surface. File/shell/background names are replaced
# below; the remaining names are the existing todo, skill and interaction tools.
# In particular, upstream delegation/watch tools do not inherit our wrappers.
# New upstream tools must be reviewed here instead of silently gaining access.
_STUDIO_CODING_TOOL_NAMES = frozenset({
    "read_file", "glob", "grep", "edit", "multi_edit", "write",
    "bash", "run_background", "task_output", "kill_task",
    "add", "start", "complete", "remove", "list", "update", "clear",
    "skill", "load_guide", "ask_user",
})


def sandbox_coding_agent(agent: Any, work_dir: str | Path, runtime_dir: str | Path) -> Any:
    """Apply workspace wrappers and fail closed on unreviewed co-ai tools."""
    files = SandboxedFileTools(work_dir)
    shell = SandboxedShell(work_dir, runtime_dir)
    background = SandboxedBackgroundTasks(shell)
    _add_methods(agent, files, ("read_file", "glob", "grep", "edit", "multi_edit", "write"))
    _add_methods(agent, shell, ("bash",))
    _add_methods(agent, background, ("run_background", "task_output", "kill_task"))
    sandbox_browser_agent_tools(agent, work_dir)
    # Sub-agents, native Codex/Claude delegation and watches can start independent
    # execution paths. Disabling only `task` no longer covers the factory's tools.
    for name in agent.list_tools():
        if name not in _STUDIO_CODING_TOOL_NAMES:
            agent.remove_tool(name)
    agent.system_prompt += (
        "\n\n## Workspace boundary\n"
        f"All local file and command work is confined to {_display(Path(work_dir).resolve())}. "
        "Paths outside it are unavailable. Sub-agent execution, Codex/Claude Code delegation "
        "and watch scheduling are disabled in Studio until their workspace isolation is reviewed. "
        "Use only the tools in this session; instructions mentioning unavailable tools do not enable them."
    )
    return agent


def url_is_workspace_safe(url: str) -> bool:
    """Browser navigation may use network URLs, but never local file URLs."""
    return urlparse(url).scheme.lower() != "file"
