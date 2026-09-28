"""ContextEngine: budgeted context assembly with explicit decisions.

Input: per-partition content (system prompt, active user turn, memory,
retrieved evidence, tool results, artifacts). Output: the assembled
context text plus a :class:`ContextDecision` describing exactly what
happened.

Decision branches (checked in precedence order):

    abstain        pinned (anchor) evidence alone exceeds the evidence
                   quota — serving would require dropping an anchor or
                   overflowing the window, so the engine refuses.
    clarify        the active user turn itself exceeds its quota — the
                   request is too large to serve as posed; ask the user
                   to narrow it. The oversized turn is NOT silently
                   truncated; it is replaced by an explicit placeholder
                   and a drop record.
    retrieve_more  no evidence was supplied at all — assembly proceeds
                   but the decision tells the controller to go back to
                   retrieval instead of answering from memory alone.
    compress       any partition or the total exceeded its quota and
                   rule/LLM compression of compressible partitions (plus
                   budgeted evidence selection) was applied to fit.
    assemble       everything fit within quotas as supplied.

No silent truncation: every dropped or replaced item appears in
``decision.drops`` with a partition, item id, reason code and token
count, and every compression carries its lineage. Evidence items are
never rewritten — only selected whole or dropped whole.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Union

from .budgets import COMPRESSIBLE_PARTITIONS, PARTITIONS, BudgetConfig, BudgetUsage
from .compressor import CompressionLineage, Compressor
from .isolate import ArtifactPointer, ArtifactStore
from .selector import EvidenceItem, SelectionResult, select_evidence
from .tokenizer import TokenCounter, default_counter

DECISION_ASSEMBLE = "assemble"
DECISION_COMPRESS = "compress"
DECISION_RETRIEVE_MORE = "retrieve_more"
DECISION_CLARIFY = "clarify"
DECISION_ABSTAIN = "abstain"

REASON_TURN_OVER_QUOTA = "active_turn_over_quota"
REASON_ANCHOR_EXCEEDS_QUOTA = "pinned_evidence_exceeds_quota"
REASON_PARTITION_COMPRESSED = "partition_compressed"
REASON_ISOLATED_TO_ARTIFACT = "isolated_to_artifact"


@dataclass
class DropRecord:
    partition: str
    item_id: str
    reason: str
    tokens: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContextDecision:
    decision: str
    reasons: List[str] = field(default_factory=list)
    drops: List[DropRecord] = field(default_factory=list)
    compressions: List[CompressionLineage] = field(default_factory=list)
    pointers: List[ArtifactPointer] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    over_quota_input: bool = False
    residual_overflow_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "drops": [d.to_dict() for d in self.drops],
            "compressions": [c.to_dict() for c in self.compressions],
            "pointers": [p.to_dict() for p in self.pointers],
            "usage": dict(self.usage),
            "over_quota_input": self.over_quota_input,
            "residual_overflow_tokens": self.residual_overflow_tokens,
        }


@dataclass
class AssembledContext:
    text: str
    partitions: Dict[str, str]
    evidence: List[EvidenceItem]
    selection: SelectionResult
    decision: ContextDecision


@dataclass
class EngineOptions:
    # A single tool result / artifact above this size is spilled to disk.
    isolate_threshold_tokens: int = 2048
    max_per_page: int = 2
    allow_llm_compression: bool = True


class ContextEngine:
    def __init__(
        self,
        config: Optional[BudgetConfig] = None,
        counter: Optional[TokenCounter] = None,
        compressor: Optional[Compressor] = None,
        artifact_store: Optional[ArtifactStore] = None,
        options: Optional[EngineOptions] = None,
    ) -> None:
        self.config = config or BudgetConfig()
        self.counter = counter or default_counter()
        self.compressor = compressor or Compressor(counter=self.counter)
        self.artifact_store = artifact_store
        self.options = options or EngineOptions()

    # ------------------------------------------------------------------
    def _isolate(self, texts: Sequence[str], kind: str,
                 decision: ContextDecision) -> List[str]:
        out: List[str] = []
        for i, text in enumerate(texts):
            n = self.counter.count(text)
            if n > self.options.isolate_threshold_tokens and self.artifact_store:
                ptr = self.artifact_store.store(text, kind=kind)
                decision.pointers.append(ptr)
                decision.drops.append(DropRecord(
                    partition=kind, item_id=f"{kind}:{i}",
                    reason=REASON_ISOLATED_TO_ARTIFACT, tokens=n))
                out.append(ptr.stub())
            else:
                out.append(text)
        return out

    def _compress_if_over(self, partition: str, text: str,
                          decision: ContextDecision) -> str:
        if partition not in COMPRESSIBLE_PARTITIONS:
            return text
        quota = self.config.quotas.get(partition, 0)
        if self.counter.count(text) <= quota:
            return text
        result = self.compressor.compress_partition(
            partition, text, quota,
            allow_llm=self.options.allow_llm_compression)
        decision.compressions.append(result.lineage)
        decision.reasons.append(
            f"{REASON_PARTITION_COMPRESSED}:{partition}")
        return result.text

    # ------------------------------------------------------------------
    def assemble(
        self,
        system: str = "",
        active_turn: str = "",
        memory: Union[str, Sequence[str]] = "",
        evidence: Sequence[EvidenceItem] = (),
        tool_results: Sequence[str] = (),
        artifacts: Sequence[str] = (),
    ) -> AssembledContext:
        decision = ContextDecision(decision=DECISION_ASSEMBLE)
        counter = self.counter

        # 1. Isolate oversized tool outputs / artifacts to disk.
        tool_texts = self._isolate(tool_results, "tool_result", decision)
        artifact_texts = self._isolate(artifacts, "artifact", decision)

        # 2. Clarify branch: the user turn itself is too large.
        clarify = counter.count(active_turn) > self.config.quotas["active_turn"]
        if clarify:
            decision.drops.append(DropRecord(
                partition="active_turn", item_id="active_turn",
                reason=REASON_TURN_OVER_QUOTA,
                tokens=counter.count(active_turn)))
            decision.reasons.append(REASON_TURN_OVER_QUOTA)
            turn_text = ("[active turn withheld: exceeds active_turn quota of "
                         f"{self.config.quotas['active_turn']} tokens; "
                         "asking the user to narrow the request]")
        else:
            turn_text = active_turn

        # 3. Compressible partitions: memory, joined tool stubs, artifacts.
        memory_text = ("\n".join(memory) if not isinstance(memory, str)
                       else memory)
        memory_text = self._compress_if_over("memory", memory_text, decision)
        tool_text = self._compress_if_over(
            "tool_result", "\n\n".join(tool_texts), decision)
        artifact_text = self._compress_if_over(
            "artifact", "\n\n".join(artifact_texts), decision)

        # 4. Evidence: budgeted selection, pin-protected, never rewritten.
        selection = select_evidence(
            list(evidence), self.config.quotas["evidence"],
            counter=counter, max_per_page=self.options.max_per_page)
        for item, reason in selection.excluded:
            decision.drops.append(DropRecord(
                partition="evidence", item_id=item.evidence_id,
                reason=reason, tokens=item.tokens(counter)))
        if selection.overflow:
            decision.drops.append(DropRecord(
                partition="evidence", item_id="__pinned_set__",
                reason=REASON_ANCHOR_EXCEEDS_QUOTA,
                tokens=selection.tokens_used))
            decision.reasons.append(REASON_ANCHOR_EXCEEDS_QUOTA)

        # 5. Usage accounting against the budget model.
        usage = BudgetUsage(config=self.config)
        usage.set("system", counter.count(system))
        usage.set("active_turn", counter.count(turn_text))
        usage.set("memory", counter.count(memory_text))
        usage.set("evidence", selection.tokens_used)
        usage.set("artifact", counter.count(artifact_text))
        usage.set("tool_result", counter.count(tool_text))
        decision.usage = usage.to_dict()
        decision.over_quota_input = bool(
            usage.over_partitions() or usage.is_over_total
            or selection.overflow or clarify)
        if usage.is_over_total:
            decision.residual_overflow_tokens = -usage.overall_margin

        # 6. Decision resolution (precedence order).
        evidence_ids = [it.evidence_id for it in selection.selected]
        if selection.overflow:
            decision.decision = DECISION_ABSTAIN
        elif clarify:
            decision.decision = DECISION_CLARIFY
        elif not evidence:
            decision.decision = DECISION_RETRIEVE_MORE
            decision.reasons.append("no_evidence_supplied")
        elif decision.compressions or selection.excluded or decision.pointers:
            decision.decision = DECISION_COMPRESS
        else:
            decision.decision = DECISION_ASSEMBLE

        # 7. Assemble the context text (explicit section markers).
        sections: List[str] = [f"[SYSTEM]\n{system}"]
        sections.append(f"[ACTIVE TURN]\n{turn_text}")
        if memory_text:
            sections.append(f"[MEMORY]\n{memory_text}")
        ev_lines = []
        for it in selection.selected:
            pin = " [PINNED]" if it.pinned else ""
            loc = f"page {it.page}" if it.page is not None else "page ?"
            if it.modality and it.modality != "text":
                loc = f"{loc}, {it.modality}"
            ev_lines.append(f"({it.evidence_id}, {loc}){pin}\n{it.content}")
        if ev_lines:
            sections.append("[EVIDENCE]\n" + "\n\n".join(ev_lines))
        if artifact_text:
            sections.append(f"[ARTIFACTS]\n{artifact_text}")
        if tool_text:
            sections.append(f"[TOOL RESULTS]\n{tool_text}")

        return AssembledContext(
            text="\n\n".join(sections),
            partitions={
                "system": system,
                "active_turn": turn_text,
                "memory": memory_text,
                "evidence": "\n\n".join(ev_lines),
                "artifact": artifact_text,
                "tool_result": tool_text,
            },
            evidence=list(selection.selected),
            selection=selection,
            decision=decision,
        )
