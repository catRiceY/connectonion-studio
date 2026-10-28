"""Evaluation-only transport configuration; never creates a network client."""
from types import SimpleNamespace

from co_studio.eval_provider import create_evaluation_llm, is_unrecoverable


def test_transport_settings_only_replace_evaluation_instance_client(monkeypatch):
    seen = []
    bounded = object()
    class Client:
        def with_options(self, **kwargs):
            seen.append(kwargs)
            return bounded
    original = Client()
    evaluation = SimpleNamespace(client=original)
    live_agent = SimpleNamespace(client=original)
    monkeypatch.setattr("connectonion.core.llm.create_llm", lambda model: evaluation)
    assert create_evaluation_llm("explicit-model") is evaluation
    assert evaluation.client is bounded and live_agent.client is original
    assert seen == [{"timeout":45.0,"max_retries":0}]


def test_unknown_transport_remains_explicitly_unbounded(monkeypatch):
    provider = SimpleNamespace(client=object())
    monkeypatch.setattr("connectonion.core.llm.create_llm", lambda model: provider)
    assert create_evaluation_llm("other-sdk") is provider


def test_provider_without_client_does_not_get_fake_transport_settings(monkeypatch):
    provider = SimpleNamespace(model="other")
    monkeypatch.setattr("connectonion.core.llm.create_llm", lambda model: provider)
    assert create_evaluation_llm("other") is provider
    assert not hasattr(provider, "client")


def test_fatal_classification_uses_safe_type_allowlist_not_provider_messages():
    assert is_unrecoverable("InsufficientCreditsError")
    assert is_unrecoverable("LLMAuthenticationError")
    assert not is_unrecoverable("LLMRateLimitError")
    assert not is_unrecoverable("ProviderServiceError")
    assert not is_unrecoverable("some error message saying insufficient credits")
    assert not is_unrecoverable({"error":"InsufficientCreditsError"})
