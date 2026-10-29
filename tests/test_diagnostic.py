from unittest.mock import Mock
import pytest

from co_studio.diagnostic import DiagnosticDraft, generate_v1 as generate
from co_studio.knowledge import KnowledgeError


def store():
    value = Mock()
    value.search.return_value = {"hits": [{"chunk_id": "a" * 64, "content": "Check the configured listening port.", "version": "v1", "source_uri": "studio://docs/port"}]}
    value.validate_citations.return_value = {"valid": True}
    return value


def draft():
    return {"status": "hypotheses", "hypotheses": [{"possible_cause": "The client may use an outdated port.", "evidence": [{"chunk_id": "a" * 64, "quote": "Check the configured listening port."}], "verification_step": "Compare configured addresses without changing them."}], "limitation": "A hypothesis, not a verified root cause."}


def test_scripted_structured_diagnosis_uses_schema_and_cites_current_source():
    db, complete = store(), Mock(return_value=draft())
    result = generate(db, "Cannot connect", model="scripted-offline", complete=complete)
    assert result["model_called"] is True and result["usage"] is None
    assert complete.call_args.kwargs["output"] is DiagnosticDraft
    assert "UNTRUSTED DATA" in complete.call_args.kwargs["system_prompt"]
    assert "untrusted_evidence" in complete.call_args.args[0]
    db.validate_citations.assert_called_once()


def test_no_evidence_does_not_call_model():
    db, complete = store(), Mock()
    db.search.return_value = {"hits": []}
    assert generate(db, "Unknown", model="scripted", complete=complete)["model_called"] is False
    complete.assert_not_called()


@pytest.mark.parametrize("bad", [{"status": "invented"}, {**draft(), "unexpected": "field"}, {**draft(), "hypotheses": []}])
def test_bad_schema_or_empty_hypotheses_withheld(bad):
    with pytest.raises(KnowledgeError):
        generate(store(), "issue", model="scripted", complete=lambda *a, **k: bad)


def test_stale_or_invented_citations_withheld():
    db = store()
    db.validate_citations.return_value = {"valid": False}
    with pytest.raises(KnowledgeError, match="citation"):
        generate(db, "issue", model="scripted", complete=lambda *a, **k: draft())


def test_conflict_never_returns_speculative_hypotheses():
    value = {**draft(), "status": "conflicting_evidence"}
    result = generate(store(), "issue", model="scripted", complete=lambda *a, **k: value)
    assert result["hypotheses"] == []


def test_chinese_instructions_keep_quotes_verbatim_and_bound_output():
    db, complete = store(), Mock(return_value=draft())
    generate(db, "无法连接", model="scripted", mode="bm25", collection="private", complete=complete)
    db.search.assert_called_once_with("无法连接", collection="private", mode="bm25", k=5)
    assert "Simplified Chinese" in complete.call_args.kwargs["system_prompt"]
    assert "quotes verbatim" in complete.call_args.kwargs["system_prompt"]
    assert complete.call_args.kwargs["max_tokens"] == 2048


@pytest.mark.parametrize("status", ["conflicting_evidence", "insufficient_evidence"])
def test_even_nonhypothesis_evidence_is_rechecked_after_provider(status):
    db = store()
    db.validate_citations.return_value = {"valid": False}
    with pytest.raises(KnowledgeError, match="citation"):
        generate(db, "issue", model="scripted", complete=lambda *a, **k: {**draft(), "status": status})


def test_provider_exception_does_not_leak_credentials():
    complete = Mock(side_effect=RuntimeError("API_KEY=supersecret"))
    with pytest.raises(KnowledgeError) as failure:
        generate(store(), "issue", model="scripted", complete=complete)
    assert "supersecret" not in str(failure.value)
    assert failure.value.provider_error_type == "RuntimeError"
    assert failure.value.failure_phase == "provider_call"
    assert failure.value.validation_errors == []


@pytest.mark.parametrize("budget", [256, 4096, 8192])
def test_explicit_output_budget_is_passed_once_without_automatic_retry(budget):
    complete = Mock(return_value=draft())
    generate(store(), "issue", model="scripted", complete=complete, max_output_tokens=budget)
    assert complete.call_count == 1
    assert complete.call_args.kwargs["max_tokens"] == budget


@pytest.mark.parametrize("budget", [0, 255, 8193, True, 2048.0, "2048", None])
def test_invalid_output_budget_rejected_before_retrieval_and_model(budget):
    db, complete = store(), Mock()
    with pytest.raises(KnowledgeError, match="budget"):
        generate(db, "issue", model="scripted", complete=complete, max_output_tokens=budget)
    db.search.assert_not_called()
    complete.assert_not_called()


@pytest.mark.parametrize("inside_sdk", [False, True])
def test_schema_failure_only_exposes_safe_error_codes_and_field_paths(inside_sdk):
    import json
    value = draft()
    value["hypotheses"][0]["evidence"][0]["chunk_id"] = "SECRET-VALUE"
    value["SECRET-FIELD"] = "SECRET-INPUT"
    def complete(*a, **kw):
        return DiagnosticDraft.model_validate(value) if inside_sdk else value
    with pytest.raises(KnowledgeError) as failure:
        generate(store(), "issue", model="scripted", complete=complete)
    assert failure.value.failure_phase == "schema_validation"
    assert failure.value.provider_error_type == "ValidationError"
    details = failure.value.validation_errors
    assert {"type": "string_pattern_mismatch", "loc": ["hypotheses", 0, "evidence", 0, "chunk_id"]} in details
    assert {"type": "extra_forbidden", "loc": ["<field>"]} in details
    assert "SECRET" not in json.dumps(details) + str(failure.value)


def test_citation_and_answer_failures_are_not_misclassified_as_provider_errors():
    db = store()
    db.validate_citations.return_value = {"valid": False}
    with pytest.raises(KnowledgeError) as failure:
        generate(db, "issue", model="scripted", complete=lambda *a, **k: draft())
    assert failure.value.failure_phase == "citation_validation"
    assert failure.value.provider_error_type is None
    with pytest.raises(KnowledgeError) as failure:
        generate(store(), "issue", model="scripted",
                 complete=lambda *a, **k: {**draft(), "hypotheses": []})
    assert failure.value.failure_phase == "answer_validation"
    assert failure.value.provider_error_type is None


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_pinned_managed_sdk_schema_contract_with_offline_http_transport(finish_reason):
    """Exercise CO + SDK parsing without credentials, external requests or charges."""
    import json
    import httpx
    import openai
    from connectonion.core.llm import OpenOnionLLM
    requests = []
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["max_tokens"] == 2048
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert payload["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
        return httpx.Response(200, json={"id": "offline", "created": 0, "model": "offline",
            "object": "chat.completion", "choices": [{"index": 0, "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": json.dumps(draft())}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2048, "total_tokens": 2078}})
    # Bypass provider initialization: this dummy client has only the in-process
    # transport and cannot touch the user's managed credentials or endpoint.
    provider = object.__new__(OpenOnionLLM)
    provider.model = "offline"
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        provider.client = openai.OpenAI(api_key="offline-not-a-key", base_url="http://offline.invalid/v1",
                                       http_client=http, max_retries=0)
        def complete(text, **kwargs):
            return provider.structured_complete(
                [{"role": "system", "content": kwargs["system_prompt"]}, {"role": "user", "content": text}],
                kwargs["output"], max_tokens=kwargs["max_tokens"])
        if finish_reason == "stop":
            result = generate(store(), "issue", model="offline", complete=complete)
            assert result["hypotheses"] == draft()["hypotheses"]
        else:
            with pytest.raises(KnowledgeError) as failure:
                generate(store(), "issue", model="offline", complete=complete)
            assert failure.value.provider_error_type == "LengthFinishReasonError"
            assert failure.value.failure_phase == "provider_call"
    assert len(requests) == 1
