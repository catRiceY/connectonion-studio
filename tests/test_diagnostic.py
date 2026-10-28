from unittest.mock import Mock
import pytest

from co_studio.diagnostic import DiagnosticDraft, generate
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
