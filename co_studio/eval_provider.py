"""Evaluation-only provider transport bounds; never patch a running Agent."""
from __future__ import annotations

UNRECOVERABLE_ERROR_TYPES = frozenset({
    "InsufficientCreditsError", "LLMAuthenticationError", "PaidModelRequiredError",
    "AuthenticationError", "PermissionDeniedError",
})


def is_unrecoverable(error_type):
    """Safe exception-type classification, never inspect/record secret messages."""
    return isinstance(error_type, str) and error_type in UNRECOVERABLE_ERROR_TYPES


def create_evaluation_llm(model):
    from connectonion.core.llm import create_llm
    provider = create_llm(model)
    client = getattr(provider, "client", None)
    # The pinned managed provider uses an OpenAI-compatible client. Disable its
    # normal automatic retries for a controlled experiment, on this instance
    # only. Other SDKs remain explicit unknowns, not a claimed billing cap.
    if callable(getattr(client, "with_options", None)):
        provider.client = client.with_options(timeout=45.0, max_retries=0)
    return provider
