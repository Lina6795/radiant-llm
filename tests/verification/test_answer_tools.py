"""S6: answer.draft / answer.verify -- bounded Draft->Claim->Verify->Revise
workflow with budget trace and structured verdicts. LLM is injected for
offline tests; production uses the real DeepSeek endpoint.
"""

from __future__ import annotations

import pytest

from app.control.models import ToolStatus
from app.control.registry import build_default_registry

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)

PKG = {
    "package_fingerprint": "fp1",
    "token_counter": "heuristic",
    "budgets": {"evidence": 48000, "active_turn": 16000, "memory": 24000,
                "system": 8000, "artifact": 12000, "tool_result": 12000},
    "usage": {"evidence": 100},
    "items": [],
    "decision": "assemble",
    "text": "[EVIDENCE]\n(ev-1, page 1)\nThe Transformer consists of an encoder and a decoder stack.",
}

RECORDS = [
    {"evidence_id": "ev-1", "document_id": "doc1", "page": 1, "modality": "text",
     "content": "The Transformer architecture consists of an encoder and a decoder stack.",
     "source_span": {"chunk_id": "doc1:p1:c0", "page": 1}},
]

GOOD_DRAFT = "The Transformer consists of an encoder and a decoder stack [ev-1]."
QUALIFIER_DRAFT = "The Transformer consists of two main components: an encoder and a decoder [ev-1]."
BAD_DRAFT = "The GPU shortage delayed many research projects in 2019."


class StubLLM:
    def __init__(self, reply="stub draft"):
        self.reply = reply
        self.calls = []

    def __call__(self, messages, **kw):
        self.calls.append(messages)
        return {"content": self.reply, "latency_ms": 1, "prompt_tokens": 10, "completion_tokens": 5}


def _invoke(tool, arguments, kb_env, workspace="default"):
    registry = build_default_registry()
    return registry.invoke(tool, arguments, run_id="s6", workspace=workspace)


def test_draft_requires_context_package(kb_env) -> None:
    result = _invoke("answer.draft", {"question": "q"}, kb_env)
    assert result.status == ToolStatus.TERMINAL_ERROR
    assert result.error.code == "answer.context_package_missing"


def test_draft_calls_llm_with_budget_traced_context(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    stub = StubLLM(reply="draft text")
    monkeypatch.setattr(answer_tools, "_chat", stub)
    result = _invoke("answer.draft", {"question": "q", "context_package": PKG}, kb_env)
    assert result.status == ToolStatus.SUCCESS, result.error
    out = result.output
    assert out["draft"] == "draft text"
    assert out["mock"] is False
    assert out["model"]
    assert out["usage"]["prompt_tokens"] == 10
    assert stub.calls and "encoder" in str(stub.calls[0])


def test_verify_supported_claims(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    monkeypatch.setattr(answer_tools, "_chat", StubLLM())
    result = _invoke(
        "answer.verify",
        {"draft": GOOD_DRAFT, "evidence_records": RECORDS, "question": "q", "allow_revise": False},
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    out = result.output
    assert out["verify_action"] == "accept"
    assert out["claims"]
    assert all(c["verdict"] == "supported" for c in out["claims"])
    assert out["claims"][0]["evidence_ids"] == ["ev-1"]
    assert out["answer"] == GOOD_DRAFT
    assert out["revise_used"] is False


def test_word_number_qualifier_not_digit_checked(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    monkeypatch.setattr(answer_tools, "_chat", StubLLM())
    result = _invoke(
        "answer.verify",
        {"draft": QUALIFIER_DRAFT, "evidence_records": RECORDS, "question": "q", "allow_revise": False},
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    # 设计语义：确定性验证覆盖数字（digit）与单位；词形数字（"two"）不做
    # 语义计数，encoder+decoder 证据足够支撑该 claim → accept。
    # 若未来加入词形数字归一，TEACH-T01 会被误送 review（corpus 未字面写 two）。
    assert result.output["verify_action"] == "accept"


def test_verify_unsupported_triggers_single_bounded_revise(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    stub = StubLLM(reply=GOOD_DRAFT)
    monkeypatch.setattr(answer_tools, "_chat", stub)
    result = _invoke(
        "answer.verify",
        {"draft": BAD_DRAFT, "evidence_records": RECORDS, "question": "q", "allow_revise": True},
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    out = result.output
    assert out["revise_used"] is True
    assert len(stub.calls) == 1  # exactly ONE revise call, bounded
    assert out["claims"]
    assert all(c["verdict"] == "supported" for c in out["claims"])
    assert out["usage"]["completion_tokens"] >= 5


def test_verify_out_of_scope_when_claim_outside_evidence(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    monkeypatch.setattr(answer_tools, "_chat", StubLLM(reply=GOOD_DRAFT))
    draft = "The moon landing was filmed in a studio. The Transformer consists of an encoder and a decoder stack [ev-1]."
    result = _invoke(
        "answer.verify",
        {"draft": draft, "evidence_records": RECORDS, "question": "q", "allow_revise": False},
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    out = result.output
    verdicts = {c["verdict"] for c in out["claims"]}
    assert "out_of_scope" in verdicts or "unsupported" in verdicts
    assert out["verify_action"] in ("accept", "review", "abstain", "clarify")


def test_verify_high_risk_numeric_claim_marks_review(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    monkeypatch.setattr(answer_tools, "_chat", StubLLM(reply=GOOD_DRAFT))
    risky = "The Transformer uses exactly 6 encoder layers and 6 decoder layers [ev-1]."
    result = _invoke(
        "answer.verify",
        {"draft": risky, "evidence_records": RECORDS, "question": "q", "allow_revise": False},
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    out = result.output
    # numeric claims not backed by evidence must not be silently accepted
    assert out["verify_action"] in ("review", "abstain") or any(
        c["verdict"] in ("unsupported", "conflict") for c in out["claims"]
    )


def test_header_noise_claims_filtered(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    monkeypatch.setattr(answer_tools, "_chat", StubLLM())
    draft = "Revised draft:\nThe Transformer consists of an encoder and a decoder stack [ev-1]."
    result = _invoke(
        "answer.verify",
        {"draft": draft, "evidence_records": RECORDS, "question": "q", "allow_revise": False},
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    out = result.output
    assert out["verify_action"] == "accept"
    assert out["noise_claims_filtered"] >= 1
    assert all(c["verdict"] == "supported" for c in out["claims"])
