"""Offline contracts for the ConnectOnion 1.9.2 sandbox adapters."""

from contextlib import chdir
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from connectonion.core.agent import Agent
from connectonion.core.tool_executor import execute_single_tool
from connectonion.core.tool_result import ToolFailure

from co_studio.sandbox import (
    SandboxedBackgroundTasks,
    SandboxedBrowserPaths,
    SandboxedFileTools,
    SandboxedShell,
    sandbox_coding_agent,
)


class FrameworkSandboxCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="studio-sandbox-compat-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()

    def test_expected_denials_are_string_compatible_tool_failures(self):
        files = SandboxedFileTools(self.work)
        browser = SandboxedBrowserPaths(SimpleNamespace(), self.work)
        shell = SandboxedShell(self.work, self.root / "runtime")
        background = SandboxedBackgroundTasks(shell)

        failures = [
            files.read_file("../missing.txt"),
            files.write("../outside.txt", "must not be written"),
            files.glob("../*"),
            files.grep("anything", file_pattern="../*"),
            browser.go_to("file:///outside"),
            browser.newtab("file:///outside"),
            browser.save_state("../outside.json"),
            shell.bash("not executed", "reject cwd", cwd=".."),
            background.task_output("missing"),
            background.kill_task("missing"),
        ]
        for result in failures:
            with self.subTest(result=result):
                self.assertIsInstance(result, ToolFailure)
                self.assertIsInstance(result, str)
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertNotIsInstance(files.write("inside.txt", "safe"), ToolFailure)
        self.assertIn("safe", files.read_file("inside.txt"))

    def test_shell_failures_preserve_outcome_without_starting_processes(self):
        shell = SandboxedShell(self.work, self.root / "runtime")
        for error in (subprocess.TimeoutExpired("unused", 1), OSError("unavailable")):
            with self.subTest(error=type(error).__name__):
                with patch.object(shell, "_launch", side_effect=error):
                    self.assertIsInstance(shell.bash("unused", "test"), ToolFailure)
        failed = subprocess.CompletedProcess("unused", 2, "", "invalid command")
        passed = subprocess.CompletedProcess("unused", 0, "Error: just file contents", "")
        self.assertIsInstance(shell._format(failed), ToolFailure)
        # Do not classify arbitrary output by its text prefix.
        self.assertNotIsInstance(shell._format(passed), ToolFailure)
        background = SandboxedBackgroundTasks(shell)
        with patch("co_studio.sandbox.sys.platform", "linux"):
            self.assertIsInstance(background.run_background("unused"), ToolFailure)

    def test_framework_records_workspace_denial_as_failed_execution(self):
        files = SandboxedFileTools(self.work)
        agent = Agent(
            "sandbox-trace", llm=SimpleNamespace(model="offline"),
            tools=[files.read_file], log=False, quiet=True,
        )
        agent.current_session = {"trace": [], "messages": [], "iteration": 1}
        result = execute_single_tool(
            "read_file", {"path": "../outside.txt"}, "test-call",
            agent.tools, agent, agent.logger,
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "ToolFailure")
        self.assertIn("Workspace access denied", result["result"])

    def test_real_co_ai_factory_keeps_supported_tools_and_drops_unknown_tools(self):
        from connectonion.cli.co_ai import agent as factory

        def offline_agent(**kwargs):
            return Agent(**kwargs, llm=SimpleNamespace(model="offline"), log=False, quiet=True)

        def future_unreviewed_tool() -> str:
            """Simulate a tool added by a later upstream release."""
            raise AssertionError("Unreviewed tool must never execute")

        with chdir(self.work), patch.object(factory, "Agent", offline_agent), patch.object(
            factory, "load_project_context", return_value=""
        ):
            agent = factory.create_coding_agent(co_dir=self.root / ".co")
            before = set(agent.list_tools())
            self.assertTrue({"codex", "claude_code", "watch_task", "watch_every"} <= before)
            agent.add_tool(future_unreviewed_tool)
            sandbox_coding_agent(agent, self.work, self.root / "runtime")

        supported = {
            "read_file", "glob", "grep", "edit", "multi_edit", "write",
            "bash", "run_background", "task_output", "kill_task",
            "add", "start", "complete", "remove", "list", "update", "clear",
            "skill", "load_guide", "ask_user",
        }
        self.assertEqual(set(agent.list_tools()), supported)
        self.assertIsInstance(agent.tools.read_file("../outside.txt"), ToolFailure)
        self.assertIn("Successfully wrote", agent.tools.write("inside.txt", "safe"))
        self.assertIn("safe", agent.tools.read_file("inside.txt"))
        # A stale model request cannot call a removed upstream capability.
        agent.current_session = {"trace": [], "messages": []}
        result = execute_single_tool(
            "future_unreviewed_tool", {}, "future-call", agent.tools, agent, agent.logger,
        )
        self.assertEqual(result["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
