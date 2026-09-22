"""RADIANT-Control M2: rule-based Router skeleton.

The router is a plain callable `(query) -> RouterDecision` so an LLM-backed
implementation can be injected later without changing the pipeline contract.
Top-level actions are restricted to respond | tool_call | clarify | abstain;
confidence below the threshold is forced to clarify; prompt-injection shaped
input abstains. Every decision carries stable reason codes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from app.control.models import (
    Action,
    Intent,
    ReasonCode,
    ResponseContract,
    ResponseFormat,
    RouterDecision,
)

DEFAULT_CONFIDENCE_THRESHOLD = 0.6

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)",
    r"bypass\s+(the\s+)?(policy|guard|schema|validation|authorization|approval)",
    r"without\s+(any\s+)?(approval|authorization|confirmation|validation)",
    r"skip\s+(the\s+)?(policy|guard|schema|validation|authorization|approval)",
    r"do\s+not\s+ask\s+for\s+(approval|confirmation|authorization)",
    r"(reveal|show|print|disclose)\s+(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions?)",
    r"(skill|workflow)\s+(says|tells|instructs)\s+(you\s+)?to\s+(ignore|bypass|skip)",
    r"run\s+\w*tool\w*\s+without\s+(approval|authorization)",
    r"绕过|无视.{0,8}(策略|校验|审批)|无需审批|跳过(校验|策略|审批)",
]

_INTENT_PATTERNS: list[tuple[Intent, str]] = [
    (Intent.COMPARE, r"\bcompare\b|\bversus\b|\bvs\.?\b|difference\s+between"),
    (Intent.INGEST, r"\bingest\b|\bupload\b|\bimport\b.{0,20}\b(document|file|pdf)\b"),
    (Intent.AUDIT, r"\baudit\b|\btrace\b|\bdecision\s+log\b"),
    (Intent.VISUAL_QA, r"\bimage\b|\bfigure\b|\bdiagram\b|\bchart\b|\bplot\b|\bpicture\b|\bgraph\b"),
]

_TOOL_CALL_PATTERN = r"\bsearch\b|\bretrieve\b|\bexport\b|\bingest\b|\bfind\b.{0,30}\b(document|paper|evidence|report)\b|\breport\b"

_VAGUE_PATTERN = r"\bstuff\b|\bthing\b|\bsomething\b|\bwhatever\b|\banything\b"


@dataclass(frozen=True)
class RuleRouter:
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    def __call__(self, query: str) -> RouterDecision:
        text = (query or "").strip()
        if not text:
            return RouterDecision(
                intent=Intent.KNOWLEDGE_QA,
                action=Action.CLARIFY,
                confidence=0.0,
                reason_codes=[ReasonCode.ROUTER_EMPTY_INPUT.value],
                response_contract=ResponseContract(),
            )

        lowered = text.lower()

        for pattern in _INJECTION_PATTERNS:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return RouterDecision(
                    intent=Intent.AUDIT,
                    action=Action.ABSTAIN,
                    confidence=0.95,
                    working_subject=None,
                    reason_codes=[ReasonCode.ROUTER_INJECTION_SUSPECTED.value],
                    response_contract=ResponseContract(),
                )

        intent = Intent.KNOWLEDGE_QA
        reasons: list[str] = []
        matched_intent = False
        for candidate, pattern in _INTENT_PATTERNS:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                intent = candidate
                matched_intent = True
                reasons.append(ReasonCode.ROUTER_KEYWORD_MATCH.value)
                break
        if not matched_intent:
            reasons.append(ReasonCode.ROUTER_DEFAULT_INTENT.value)

        wants_tools = bool(re.search(_TOOL_CALL_PATTERN, lowered, flags=re.IGNORECASE))
        vague = bool(re.search(_VAGUE_PATTERN, lowered, flags=re.IGNORECASE))

        if matched_intent and wants_tools:
            confidence = 0.9
        elif matched_intent or wants_tools:
            confidence = 0.85
        elif vague or len(text.split()) < 3:
            confidence = 0.35
        else:
            confidence = 0.75

        if confidence < self.confidence_threshold:
            action = Action.CLARIFY
            reasons.append(ReasonCode.ROUTER_LOW_CONFIDENCE.value)
        else:
            action = Action.TOOL_CALL if wants_tools else Action.RESPOND

        contract = ResponseContract(
            format=ResponseFormat.REPORT if re.search(r"\breport\b", lowered) else ResponseFormat.ANSWER,
            citation_required=bool(re.search(r"\bsearch\b|\bevidence\b|\breport\b|\bcite\b", lowered)),
        )

        return RouterDecision(
            intent=intent,
            action=action,
            confidence=confidence,
            working_subject=None,
            reason_codes=reasons,
            response_contract=contract,
        )


# Type alias for dependency injection: any callable with this signature can
# replace the rule router (e.g. an LLM-backed router) without changing the
# control plane.
RouterCallable = Callable[[str], RouterDecision]
