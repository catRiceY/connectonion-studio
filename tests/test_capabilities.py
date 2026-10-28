"""Risk matrix, trust enforcement, metadata compatibility, and generated-agent tests."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from co_studio import config, creator, registry
from co_studio.api.agents import CreateAgentBody
from co_studio.toolkits import required_trust, resolve, validate


class CapabilityMatrixTests(unittest.TestCase):
    def test_risk_tiers_force_open_careful_and_strict(self) -> None:
        self.assertEqual(required_trust(["utility", "web", "image"]), "open")
        self.assertEqual(required_trust(["utility", "files"]), "careful")
        self.assertEqual(required_trust(["utility", "browser"]), "strict")
        self.assertEqual(required_trust(["utility", "shell"]), "strict")
        self.assertEqual(required_trust(["utility", "file-write"]), "strict")

    def test_file_editing_supersedes_read_only_file_bundle(self) -> None:
        self.assertEqual(validate(["files", "utility", "file-write"]), ["utility", "file-write"])

    def test_unknown_capability_uses_public_product_term(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown capabilities"):
            validate(["teleport"])

    def test_file_bundles_and_approval_plugins_match_the_risk_model(self) -> None:
        from connectonion import useful_plugins

        read_tools, read_plugins = resolve(["files"])
        write_tools, write_plugins = resolve(["file-write", "shell"])
        self.assertEqual(read_tools[0]._permission, "read")
        self.assertEqual(read_plugins, [])
        self.assertEqual(write_tools[0]._permission, "write")
        self.assertIn(useful_plugins.prefer_write_tool, write_plugins)
        self.assertIn(useful_plugins.tool_approval, write_plugins)
        self.assertIn(useful_plugins.runtime_input, write_plugins)
        self.assertEqual(sum(plugin is useful_plugins.tool_approval for plugin in write_plugins), 1)


class CapabilityPolicyTests(unittest.TestCase):
    def _create(self, root: Path, capabilities: list[str], invite_code: str | None, trust: str = "open"):
        agents_dir = root / "agents"
        with (
            patch.object(config, "AGENTS_DIR", agents_dir),
            patch.object(config, "INDEX_LOCK", root / "index.lock"),
            patch.object(config, "KEYS_ENV", root / "missing-keys.env"),
            patch.object(creator.identity, "create", return_value="0xtest"),
            patch.object(creator.ports, "allocate", return_value=8000),
        ):
            return creator.create(
                "Capability Test",
                creator.DEFAULT_MODEL,
                capabilities,
                trust,
                invite_code=invite_code,
            )

    def test_standard_capabilities_allow_open_or_invite_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            open_meta = self._create(root, ["utility", "web"], None)
            invite_meta = self._create(root, ["utility", "image"], "standard-team", trust="careful")
        self.assertEqual(open_meta.trust, "open")
        self.assertIsNone(open_meta.invite_code)
        self.assertEqual(invite_meta.trust, "careful")
        self.assertEqual(invite_meta.invite_code, "standard-team")
        selection, trust, code = creator.normalize_custom_policy(
            ["utility", "web"], "code-means-invite"
        )
        self.assertEqual(selection, ["utility", "web"])
        self.assertEqual((trust, code), ("careful", "code-means-invite"))

    def test_reading_requires_code_and_forces_careful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "invite code"):
                self._create(Path(tmp), ["utility", "files"], "")
            meta = self._create(Path(tmp), ["utility", "files"], "read-team")
        self.assertEqual(meta.trust, "careful")
        self.assertEqual(meta.invite_code, "read-team")

    def test_dangerous_capabilities_force_strict_deny_by_default_policy(self) -> None:
        rendered = creator.render(
            "Builder",
            creator.DEFAULT_MODEL,
            8000,
            ["utility", "file-write", "shell", "browser"],
            "open",
            invite_code="build-team",
        )
        trust_assignment = next(
            node for node in ast.parse(rendered).body
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TRUST"
                for target in node.targets
            )
        )
        policy = yaml.safe_load(ast.literal_eval(trust_assignment.value).split("---", 2)[1])
        self.assertEqual(policy["onboard"]["invite_code"], ["build-team"])
        self.assertEqual(policy["default"], "deny")
        self.assertIn("agent.tools.remove(\"wait_for_manual_login\")", rendered)
        self.assertIn("Side-effecting commands require explicit approval", rendered)
        self.assertIn("All file operations are confined to this Agent's workspace", rendered)
        self.assertIn("work_dir=WORK_DIR, runtime_dir=RUNTIME_DIR", rendered)
        compile(rendered, "agent.py", "exec")


class CapabilityCompatibilityTests(unittest.TestCase):
    def test_old_toolkits_metadata_loads_as_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp)
            agent_dir = agents_dir / "legacy"
            agent_dir.mkdir()
            (agent_dir / "meta.json").write_text(json.dumps({
                "slug": "legacy",
                "name": "Legacy",
                "address": "0xlegacy",
                "port": 8000,
                "model": creator.DEFAULT_MODEL,
                "toolkits": ["utility", "web"],
                "created_at": "2026-07-20T00:00:00+00:00",
            }))
            with patch.object(config, "AGENTS_DIR", agents_dir):
                meta = registry.load("legacy")

        self.assertIsNotNone(meta)
        self.assertEqual(meta.capabilities, ["utility", "web"])
        self.assertEqual(meta.toolkits, meta.capabilities)

    def test_api_accepts_new_field_and_keeps_legacy_request_alias(self) -> None:
        modern = CreateAgentBody(name="Modern", capabilities=["utility", "files"])
        legacy = CreateAgentBody(name="Legacy", toolkits=["utility", "web"])
        self.assertEqual(modern.capabilities, ["utility", "files"])
        self.assertEqual(legacy.toolkits, ["utility", "web"])

    def test_sensitive_selection_never_gets_a_default_invite_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "invite code is required"):
            creator.normalize_custom_policy(["utility", "files"], None)


if __name__ == "__main__":
    unittest.main()
