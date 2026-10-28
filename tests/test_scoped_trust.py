"""Regression tests for Studio's per-agent trust compatibility layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from connectonion.network.trust.trust_agent import TrustAgent

from co_studio import creator
from co_studio.runner import co_studio_runner
from co_studio.scoped_trust import ScopedTrustAgent, scope_host


INVITE_POLICY = """---
allow: [whitelisted, contact]
deny: [blocked]
onboard:
  invite_code: [agent-code]
default: deny
---
Only invited clients may connect.
"""


class ScopedTrustAgentTests(unittest.TestCase):
    def test_global_framework_contact_does_not_authorize_scoped_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_dir = root / ".co"
            global_dir.mkdir()
            (global_dir / "contacts.txt").write_text("0xclient\n")

            with patch.object(Path, "home", return_value=root):
                trust = ScopedTrustAgent(INVITE_POLICY, co_dir=root / "agent" / ".co")
                decision = trust.should_allow("0xclient")

            self.assertFalse(decision.allow)
            self.assertEqual(trust.get_level("0xclient"), "stranger")

    def test_invite_promotion_is_visible_only_to_the_target_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ScopedTrustAgent(INVITE_POLICY, co_dir=root / "first" / ".co")
            second = ScopedTrustAgent(INVITE_POLICY, co_dir=root / "second" / ".co")

            self.assertTrue(first.verify_invite("0xclient", "agent-code"))

            self.assertTrue(first.should_allow("0xclient").allow)
            self.assertFalse(second.should_allow("0xclient").allow)
            self.assertEqual((root / "first" / ".co" / "contacts.txt").read_text(), "0xclient\n")
            self.assertFalse((root / "second" / ".co" / "contacts.txt").exists())

    def test_direct_invite_request_and_blocklist_stay_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trust = ScopedTrustAgent(INVITE_POLICY, co_dir=root / "first" / ".co")
            second = ScopedTrustAgent(INVITE_POLICY, co_dir=root / "second" / ".co")

            admitted = trust.should_allow("0xclient", {"invite_code": "agent-code"})
            second.verify_invite("0xclient", "agent-code")
            trust.block("0xclient", "test")

            self.assertTrue(admitted.allow)
            self.assertFalse(trust.should_allow("0xclient").allow)
            self.assertEqual(trust.get_level("0xclient"), "blocked")
            self.assertTrue(second.should_allow("0xclient").allow)
            self.assertFalse(second.is_blocked("0xclient"))

    def test_whitelist_and_admin_changes_stay_scoped(self) -> None:
        policy = INVITE_POLICY.replace("[whitelisted, contact]", "[admin, whitelisted, contact]")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ScopedTrustAgent(policy, co_dir=root / "first" / ".co")
            second = ScopedTrustAgent(policy, co_dir=root / "second" / ".co")

            first.promote_to_whitelist("0xtrusted")
            first.add_admin("0xadmin")

            self.assertTrue(first.should_allow("0xtrusted").allow)
            self.assertFalse(second.should_allow("0xtrusted").allow)
            self.assertTrue(first.is_admin("0xadmin"))
            self.assertTrue(first.should_allow("0xadmin").allow)
            self.assertFalse(second.is_admin("0xadmin"))
            self.assertFalse(second.should_allow("0xadmin").allow)

            first.remove_admin("0xadmin")
            self.assertFalse(first.is_admin("0xadmin"))

    def test_unset_environment_invite_cannot_be_used_as_a_literal_code(self) -> None:
        placeholder = "$CO_STUDIO_TEST_INVITE"
        policy = INVITE_POLICY.replace("agent-code", placeholder)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"CO_STUDIO_TEST_INVITE": ""}
        ):
            trust = ScopedTrustAgent(policy, co_dir=Path(tmp) / ".co")

            self.assertFalse(trust.verify_invite("0xclient", placeholder))
            self.assertFalse(trust.should_allow("0xclient", {"invite_code": placeholder}).allow)
            self.assertFalse(trust.is_contact("0xclient"))

    def test_environment_invite_uses_resolved_value(self) -> None:
        policy = INVITE_POLICY.replace("agent-code", "$CO_STUDIO_TEST_INVITE")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"CO_STUDIO_TEST_INVITE": "resolved-code"}
        ):
            trust = ScopedTrustAgent(policy, co_dir=Path(tmp) / ".co")
            self.assertTrue(trust.verify_invite("0xclient", "resolved-code"))
            self.assertTrue(trust.is_contact("0xclient"))

    def test_self_reported_payment_does_not_authorize_or_promote(self) -> None:
        policy = INVITE_POLICY.replace("invite_code: [agent-code]", "payment: 10")
        with tempfile.TemporaryDirectory() as tmp:
            trust = ScopedTrustAgent(policy, co_dir=Path(tmp) / ".co")
            with patch.object(trust, "_verify_transfer_via_api") as verify:
                decision = trust.should_allow("0xclient", {"payment": 1000})

            self.assertFalse(decision.allow)
            self.assertFalse(trust.is_contact("0xclient"))
            verify.assert_not_called()

    def test_adapter_inherits_native_policy_and_storage_methods(self) -> None:
        for name in ("should_allow", "verify_invite", "promote_to_contact", "block", "is_admin"):
            with self.subTest(method=name):
                self.assertIs(getattr(ScopedTrustAgent, name), getattr(TrustAgent, name))

    def test_open_policy_keeps_allowing_unknown_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trust = ScopedTrustAgent("open", co_dir=Path(tmp) / ".co")
            self.assertTrue(trust.should_allow("0xstranger").allow)


class ScopedHostTests(unittest.TestCase):
    def test_wrapper_replaces_string_policy_with_framework_compatible_instance(self) -> None:
        captured = {}

        def fake_host(agent, *args, **kwargs):
            captured.update(agent=agent, args=args, kwargs=kwargs)
            return "hosted"

        with tempfile.TemporaryDirectory() as tmp:
            co_dir = Path(tmp) / ".co"
            result = scope_host(fake_host)(
                "agent", port=8000, trust=INVITE_POLICY, co_dir=co_dir
            )

        scoped = captured["kwargs"]["trust"]
        self.assertEqual(result, "hosted")
        self.assertIsInstance(scoped, ScopedTrustAgent)
        self.assertIsInstance(scoped, TrustAgent)
        self.assertEqual(scoped.co_dir, co_dir.resolve())

    def test_wrapper_preserves_explicit_custom_trust_agent(self) -> None:
        captured = {}

        def fake_host(_agent, **kwargs):
            captured.update(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            custom = TrustAgent("open", co_dir=Path(tmp) / "custom" / ".co")
            scope_host(fake_host)("agent", trust=custom, co_dir=Path(tmp) / "host" / ".co")

        self.assertIs(captured["trust"], custom)

    def test_wrapper_is_idempotent(self) -> None:
        def fake_host(_agent, **_kwargs):
            return None

        wrapped = scope_host(fake_host)
        self.assertIs(scope_host(wrapped), wrapped)

    def test_runner_path_covers_both_generated_templates(self) -> None:
        custom = creator.render("Custom", creator.DEFAULT_MODEL, 8000, [], "open")
        co_ai = creator.render(
            "Coder", creator.DEFAULT_MODEL, 8001, [], "strict", preset="co-ai", invite_code="code"
        )

        self.assertIn("from connectonion import Agent, host", custom)
        self.assertIn("from connectonion import host", co_ai)
        self.assertIn("host(create_agent,", custom)
        self.assertIn("host(agent,", co_ai)

    def test_runner_installs_wrapper_on_connectonion_public_host(self) -> None:
        import connectonion

        def fake_host(_agent, **_kwargs):
            return None

        with patch.object(connectonion, "host", fake_host):
            co_studio_runner._patch_scoped_trust()
            self.assertTrue(connectonion.host._co_studio_scoped_trust)


if __name__ == "__main__":
    unittest.main()
