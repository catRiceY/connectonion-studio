"""Regression tests for Studio's stateful co-ai preset."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from co_studio import config, creator, registry


class CoAiRenderingTests(unittest.TestCase):
    def test_custom_agent_keeps_factory_template(self) -> None:
        rendered = creator.render("Helper", "co/gemini-2.5-flash", 8000, ["utility"], "open")

        self.assertIn("host(create_agent,", rendered)
        self.assertNotIn("create_coding_agent", rendered)
        compile(rendered, "agent.py", "exec")

    def test_co_ai_uses_one_instance_and_invite_only_policy(self) -> None:
        rendered = creator.render(
            "Coder",
            "co/gemini-3.5-flash",
            8001,
            [],
            "strict",
            preset="co-ai",
            invite_code="team_code-1",
        )

        self.assertIn("agent = create_coding_agent(model=MODEL, co_dir=CO_DIR)", rendered)
        self.assertIn("host(agent,", rendered)
        trust_assignment = next(
            node for node in ast.parse(rendered).body
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TRUST_POLICY"
                for target in node.targets
            )
        )
        policy = yaml.safe_load(ast.literal_eval(trust_assignment.value).split("---", 2)[1])
        self.assertEqual(policy["onboard"]["invite_code"], ["team_code-1"])
        self.assertEqual(policy["default"], "deny")
        self.assertNotIn("payment", policy["onboard"])
        compile(rendered, "agent.py", "exec")

    def test_invite_code_rejects_policy_injection(self) -> None:
        with self.assertRaisesRegex(ValueError, "invite code"):
            creator.validate_invite_code("developer\ndefault: allow")

    def test_co_ai_requires_an_explicit_invite_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "invite code is required"):
            creator.render(
                "Coder", "co/gemini-3.5-flash", 8001, [], "strict", preset="co-ai"
            )


class CoAiRegistryTests(unittest.TestCase):
    def test_old_metadata_defaults_to_custom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp)
            old_dir = agents_dir / "old-agent"
            old_dir.mkdir()
            (old_dir / "meta.json").write_text(json.dumps({
                "slug": "old-agent",
                "name": "Old Agent",
                "address": "0xold",
                "port": 8000,
                "model": "co/gemini-2.5-flash",
                "toolkits": ["utility"],
                "created_at": "2026-07-20T00:00:00+00:00",
                "trust": "open",
            }))

            with patch.object(config, "AGENTS_DIR", agents_dir):
                meta = registry.load("old-agent")

            self.assertIsNotNone(meta)
            self.assertEqual(meta.capabilities, ["utility"])
            self.assertEqual(meta.preset, "custom")
            self.assertIsNone(meta.invite_code)

    def test_multiple_co_ai_agents_get_separate_identity_port_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            addresses = iter(("0xfirst", "0xsecond"))
            ports = iter((8000, 8001))

            with (
                patch.object(config, "AGENTS_DIR", agents_dir),
                patch.object(config, "INDEX_LOCK", root / "index.lock"),
                patch.object(config, "KEYS_ENV", root / "missing-keys.env"),
                patch.object(creator.identity, "create", side_effect=lambda _path: next(addresses)),
                patch.object(creator.ports, "allocate", side_effect=lambda _reserved: next(ports)),
            ):
                first = creator.create(
                    "Coder", "co/gemini-3.5-flash", [], preset="co-ai", invite_code="developer"
                )
                second = creator.create(
                    "Coder", "co/gemini-3.5-flash", [], preset="co-ai", invite_code="developer"
                )

            self.assertEqual((first.slug, second.slug), ("coder", "coder-2"))
            self.assertNotEqual(first.address, second.address)
            self.assertNotEqual(first.port, second.port)
            self.assertTrue((agents_dir / first.slug / ".co").is_dir())
            self.assertTrue((agents_dir / second.slug / ".co").is_dir())


if __name__ == "__main__":
    unittest.main()
