"""S6: answer.draft / answer.verify tools -- bounded Draft -> Claim -> Verify
-> Revise workflow.

* answer.draft REQUIRES a ContextPackage (S4 red line: no model call without
  its budget trace) and produces the draft plus deterministic atomic claims.
* answer.verify checks every claim against the real evidence records
  (supported / unsupported / conflict / out-of-scope), and on
  RETRIEVE_MORE performs exactly ONE bounded LLM revise before re-verifying.
  The verdicts and every decision are returned in the tool output (trace).

The LLM entry point is the module-level ``_chat`` (injectable in tests);
production resolves it lazily from the DeepSeek-compatible endpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from app.control.models import ToolError, ToolMetrics, ToolResult, ToolStatus
from app.verification.claims import Claim, content_tokens, split_claims
from app.verification.claim_map import map_claims
from app.verification.verifier import VerificationAction, Verifier

TOOL_VERSION_DRAFT = "answer.draft/1"
TOOL_VERSION_VERIFY = "answer.verify/1"

_OUT_OF_SCOPE_OVERLAP_FLOOR = 0.05

_chat = None  # injectable in tests; lazily resolved in production


def _default_chat():
    global _chat
    if _chat is not None:
        return _chat

    def deepseek(messages, *, temperature: float = 0.0, timeout: float = 120.0):
        import urllib.request

        base = (os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set; answer LLM unavailable")
        model = (os.getenv("RADIANT_DEFAULT_MODEL") or "deepseek-chat").strip()
        body = json.dumps({
            "model": model, "messages": messages,
            "temperature": temperature, "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        usage = payload.get("usage") or {}
        return {
            "content": payload["choices"][0]["message"]["content"],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }

    _chat = deepseek
    return _chat


def _model_name() -> str:
    return (os.getenv("RADIANT_DEFAULT_MODEL") or "deepseek-chat").strip()


def _terminal(code: str, message: str, started: float, provenance) -> ToolResult:
    return ToolResult(
        status=ToolStatus.TERMINAL_ERROR,
        error=ToolError(code=code, message=message, retryable=False),
        metrics=ToolMetrics(latency_ms=int((time.monotonic() - started) * 1000), token_count=0),
        provenance=provenance,
    )


def _llm_call(messages, started: float, provenance) -> tuple[Optional[Dict[str, Any]], Optional[ToolResult]]:
    try:
        chat = _chat or _default_chat()
        return chat(messages), None
    except Exception as exc:  # noqa: BLE001 - typed terminal error, never crash the run
        return None, _terminal("answer.llm_error", f"{type(exc).__name__}: {exc}", started, provenance)


def answer_draft_handler(arguments: Dict[str, Any], ctx) -> ToolResult:
    started = time.monotonic()
    package = arguments.get("context_package")
    if not isinstance(package, dict) or not package.get("package_fingerprint"):
        return _terminal(
            "answer.context_package_missing",
            "answer.draft requires a ContextPackage with budget trace (S4 red line)",
            started, ctx.provenance,
        )
    question = (arguments.get("question") or "").strip()
    if not question:
        return _terminal("answer.question_missing", "question is required", started, ctx.provenance)

    context_text = package.get("text") or ""
    messages = [
        {"role": "system", "content": (
            "Answer ONLY from the evidence in the context below. Cite evidence_ids "
            "in square brackets after each claim. For claims about figures, tables "
            "or diagrams, cite the evidence record marked (…, visual) — a text "
            "mention of a figure is not visual proof. If the evidence is "
            "insufficient, say so."
        )},
        {"role": "user", "content": f"{context_text}\n\nQuestion: {question}"},
    ]
    call, err = _llm_call(messages, started, ctx.provenance)
    if err is not None:
        return err

    draft = call["content"]
    claims = split_claims(draft).claims
    fingerprint = hashlib.sha256(
        json.dumps({"context": context_text[:4000], "question": question}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output={
            "draft": draft,
            "claims": [c.model_dump() for c in claims],
            "model": _model_name(),
            "prompt_fingerprint": fingerprint,
            "usage": call,
            "mock": False,
        },
        metrics=ToolMetrics(latency_ms=int((time.monotonic() - started) * 1000),
                            token_count=(call.get("completion_tokens") or 0)),
        provenance=ctx.provenance,
    )


def _cited_ids(text: str) -> List[str]:
    """Evidence ids the draft explicitly cites in [brackets] (comma/semicolon
    separated, optional ', page N' tail stripped)."""
    out: List[str] = []
    for bracket in re.findall(r"\[([^\]]+)\]", text or ""):
        for token in re.split(r"[,;]", bracket):
            token = token.strip()
            match = re.match(r"(ev-[0-9a-f]+)", token)
            if match:
                out.append(match.group(1))
    return out


def _label_claims(claims: List[Claim], claim_map, evidence_by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    labelled = []
    bindings = {b.claim_id: b for b in claim_map.bindings}
    for claim in claims:
        binding = bindings.get(claim.claim_id)
        verdict = "unsupported"
        evidence_ids: List[str] = []
        page = None
        span = None
        if binding is not None:
            evidence_ids = ([binding.evidence_id] if binding.evidence_id else []) or list(binding.candidate_ids)
            verdict = binding.status.value  # supported | unsupported | conflicted
            if binding.page is not None:
                page = binding.page
            if evidence_ids:
                src = evidence_by_id.get(evidence_ids[0]) or {}
                span = src.get("source_span")
        # citation-aware evidence: an explicit [ev-...] citation present in the
        # evidence set takes precedence for verdict/gate evaluation (the
        # mapper's best-overlap pick may differ from what the draft cited)
        cited = [eid for eid in _cited_ids(claim.text) if eid in evidence_by_id]
        if cited:
            evidence_ids = cited
            if page is None:
                page = evidence_by_id[cited[0]].get("page")
            src = evidence_by_id[cited[0]]
            span = src.get("source_span")
        if verdict == "unsupported":
            best = 0.0
            ctoks = content_tokens(claim.text)
            for rec in evidence_by_id.values():
                if not ctoks:
                    break
                overlap = len(ctoks & content_tokens(rec.get("content") or "")) / len(ctoks)
                best = max(best, overlap)
            if best < _OUT_OF_SCOPE_OVERLAP_FLOOR:
                verdict = "out_of_scope"
        # S7-5 Visual Fact Gate: a VISUAL claim only counts as supported when
        # its bound evidence is a real visual record -- a text mention of a
        # figure never constitutes visual proof.
        if (
            claim.claim_type.value == "visual"
            and verdict == "supported"
            and evidence_ids
            and (evidence_by_id.get(evidence_ids[0]) or {}).get("modality") != "visual"
        ):
            verdict = "unsupported"
        labelled.append({
            "claim_id": claim.claim_id,
            "text": claim.text,
            "claim_type": claim.claim_type.value,
            "numbers": claim.numbers,
            "units": claim.units,
            "verdict": verdict,
            "evidence_ids": evidence_ids,
            "page": page,
            "span": span,
        })
    return labelled


def _verify_once(draft: str, evidence_records: List[Dict[str, Any]], case_risk: str):
    claims = split_claims(draft).claims
    # Noise hygiene: LLM drafts often carry header lines like "Revised draft:"
    # that are not factual assertions; a claim with zero content tokens and no
    # numbers, or a short label line ending with a bare colon, must never drag
    # the verdict to review.
    def _is_noise(c) -> bool:
        if not content_tokens(c.text) and not c.numbers:
            return True
        stripped = c.text.strip()
        return len(stripped) <= 40 and stripped.endswith(":")

    noise = [c for c in claims if _is_noise(c)]
    claims = [c for c in claims if not _is_noise(c)]
    cmap = map_claims(claims, evidence_records)
    decision = Verifier().verify(
        claims=claims, claim_map=cmap, evidence_items=evidence_records, case_risk=case_risk
    )
    evidence_by_id = {r["evidence_id"]: r for r in evidence_records if r.get("evidence_id")}
    labelled = _label_claims(claims, cmap, evidence_by_id)
    return claims, cmap, decision, labelled, len(noise)


_ACTION_MAP = {
    VerificationAction.COMMIT: "accept",
    VerificationAction.RETRIEVE_MORE: "revise",
    VerificationAction.CLARIFY: "clarify",
    VerificationAction.HUMAN_REVIEW: "review",
    VerificationAction.ABSTAIN: "abstain",
}


def answer_verify_handler(arguments: Dict[str, Any], ctx) -> ToolResult:
    started = time.monotonic()
    draft = (arguments.get("draft") or "").strip()
    records = arguments.get("evidence_records")
    if not draft:
        return _terminal("answer.draft_missing", "draft is required", started, ctx.provenance)
    if not isinstance(records, list):
        return _terminal("answer.evidence_missing", "evidence_records must be a list", started, ctx.provenance)
    allow_revise = bool(arguments.get("allow_revise", True))

    _claims, _cmap, decision, labelled, noise_filtered = _verify_once(draft, records, "low")
    action = _ACTION_MAP[decision.action]
    if action == "revise" and not allow_revise:
        # insufficient evidence and revise disabled: do not answer
        action = "abstain"
    revise_used = False
    final_answer = draft if action == "accept" else None
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0}

    def _gate_blocks_accept() -> bool:
        # label-level gates (Visual Fact Gate etc.) must be able to veto a
        # map-level COMMIT -- a supported-looking decision with a gate-failed
        # claim is not acceptable.
        return any(c["verdict"] != "supported" for c in labelled)

    if action == "accept" and _gate_blocks_accept():
        action = "revise" if allow_revise else "review"
        final_answer = None

    if action == "revise" and allow_revise:
        # ONE bounded revise: critique in, revised draft out, re-verify once.
        revise_used = True
        gaps = {
            "unsupported": [c["text"] for c in labelled if c["verdict"] != "supported"],
            "citation_gaps": decision.citation_gaps,
        }
        messages = [
            {"role": "system", "content": (
                "Revise the draft so every claim is supported by the evidence. "
                "Drop or hedge anything unsupported; keep [evidence_id] citations."
            )},
            {"role": "user", "content": json.dumps(
                {"draft": draft, "problems": gaps,
                 "evidence": [{"evidence_id": r.get("evidence_id"), "page": r.get("page"),
                               "content": (r.get("content") or "")[:500]} for r in records]},
                ensure_ascii=False)},
        ]
        call, err = _llm_call(messages, started, ctx.provenance)
        if err is not None:
            return err
        usage = call
        revised = call["content"]
        _claims, _cmap, decision2, labelled, _nf2 = _verify_once(revised, records, "low")
        noise_filtered += _nf2
        action = _ACTION_MAP[decision2.action]
        if action == "revise":
            action = "review"  # second failure is NOT retried -- bounded loop ends here
        decision = decision2
        if action == "accept" and _gate_blocks_accept():
            action = "review"  # label-level gate veto after the single revise
        if action == "accept":
            final_answer = revised
        draft = revised

    return ToolResult(
        status=ToolStatus.SUCCESS,
        output={
            "verify_action": action,
            "answer": final_answer,
            "claims": labelled,
            "coverage": decision.coverage,
            "numeric_accuracy": decision.numeric_accuracy,
            "reason_codes": decision.reason_codes,
            "conflicts": decision.conflicts,
            "revise_used": revise_used,
            "noise_claims_filtered": noise_filtered,
            "model": _model_name() if revise_used else None,
            "usage": usage,
            "mock": False,
        },
        metrics=ToolMetrics(latency_ms=int((time.monotonic() - started) * 1000),
                            token_count=(usage.get("completion_tokens") or 0)),
        provenance=ctx.provenance,
    )
