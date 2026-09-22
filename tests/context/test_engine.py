"""ContextEngine: branch decisions and the no-silent-truncation contract."""

from context.budgets import BudgetConfig
from context.engine import (DECISION_ABSTAIN, DECISION_ASSEMBLE,
                            DECISION_CLARIFY, DECISION_COMPRESS,
                            DECISION_RETRIEVE_MORE, ContextEngine,
                            EngineOptions)
from context.isolate import ArtifactStore
from context.selector import EvidenceItem
from context.tokenizer import TokenCounter

COUNTER = TokenCounter()


def small_config() -> BudgetConfig:
    return BudgetConfig(
        total_tokens=2000, response_reserve=200,
        quotas={"system": 200, "active_turn": 200, "memory": 300,
                "evidence": 500, "artifact": 200, "tool_result": 200})


def make_engine(tmp_path=None, **kwargs) -> ContextEngine:
    store = ArtifactStore(root=tmp_path) if tmp_path else None
    return ContextEngine(config=small_config(), counter=COUNTER,
                         artifact_store=store,
                         options=EngineOptions(isolate_threshold_tokens=64),
                         **kwargs)


def ev(eid, words, page, score, pinned=False):
    return EvidenceItem(evidence_id=eid, content=" ".join([words] * 8),
                        page=page, score=score, pinned=pinned)


# ---------------------------------------------------------------------------
def test_assemble_branch_when_everything_fits():
    engine = make_engine()
    result = engine.assemble(
        system="You are concise.", active_turn="What is attention?",
        memory="short note", evidence=[ev("e1", "attention", 1, 0.9)])
    assert result.decision.decision == DECISION_ASSEMBLE
    assert "[EVIDENCE]" in result.text
    assert result.decision.drops == []


def test_compress_branch_on_oversized_memory():
    engine = make_engine()
    big_memory = " ".join(f"note {i} about experiments" for i in range(300))
    result = engine.assemble(
        system="sys", active_turn="q?", memory=big_memory,
        evidence=[ev("e1", "attention", 1, 0.9)])
    assert result.decision.decision == DECISION_COMPRESS
    assert result.decision.compressions
    lin = result.decision.compressions[0]
    assert lin.tokens_after <= lin.tokens_before
    assert COUNTER.count(result.partitions["memory"]) <= 300


def test_retrieve_more_branch_when_no_evidence():
    engine = make_engine()
    result = engine.assemble(system="sys", active_turn="q?", memory="m")
    assert result.decision.decision == DECISION_RETRIEVE_MORE
    assert "no_evidence_supplied" in result.decision.reasons


def test_clarify_branch_on_oversized_turn():
    engine = make_engine()
    huge_turn = "please analyze " + "everything " * 500
    result = engine.assemble(system="sys", active_turn=huge_turn,
                             evidence=[ev("e1", "attention", 1, 0.9)])
    assert result.decision.decision == DECISION_CLARIFY
    assert "withheld" in result.partitions["active_turn"]
    drop = result.decision.drops[0]
    assert drop.partition == "active_turn" and drop.reason


def test_abstain_branch_when_pinned_exceeds_quota():
    engine = make_engine()
    pinned = EvidenceItem(evidence_id="gold", content="gold anchor evidence " * 300,
                          page=1, score=1.0, pinned=True)
    result = engine.assemble(system="sys", active_turn="q?",
                             evidence=[pinned])
    assert result.decision.decision == DECISION_ABSTAIN
    # The anchor itself is still present — abstain never sheds the pin.
    assert "gold" in result.partitions["evidence"]
    reasons = [d.reason for d in result.decision.drops]
    assert "pinned_evidence_exceeds_quota" in reasons


def test_oversized_tool_result_isolated_to_disk(tmp_path):
    engine = make_engine(tmp_path=tmp_path)
    big_tool = "data row with many tokens " * 200
    result = engine.assemble(system="sys", active_turn="q?",
                             evidence=[ev("e1", "attention", 1, 0.9)],
                             tool_results=[big_tool])
    assert result.decision.pointers
    ptr = result.decision.pointers[0]
    assert ptr.uri in result.partitions["tool_result"]
    assert big_tool not in result.text
    drop = [d for d in result.decision.drops
            if d.reason == "isolated_to_artifact"]
    assert drop and drop[0].tokens > 0
    # The spilled content is recoverable byte-for-byte.
    assert engine.artifact_store.resolve(ptr) == big_tool


def test_no_silent_truncation_every_drop_has_reason():
    engine = make_engine()
    evidence = [ev(f"e{i}", f"topic{i}", page=i, score=1.0 - i * 0.05)
                for i in range(20)]  # far more than the 500-token quota
    result = engine.assemble(system="sys", active_turn="q?",
                             memory="m", evidence=evidence)
    selected_ids = {it.evidence_id for it in result.evidence}
    dropped_ids = {d.item_id for d in result.decision.drops
                   if d.partition == "evidence"}
    all_ids = {it.evidence_id for it in evidence}
    # Every candidate is accounted for: selected, or dropped WITH a reason.
    assert selected_ids | dropped_ids == all_ids
    assert all(d.reason and d.tokens > 0 for d in result.decision.drops)


def test_evidence_text_never_rewritten():
    engine = make_engine()
    content = "Precise citation sentence that must survive verbatim."
    result = engine.assemble(system="sys", active_turn="q?",
                             memory="x " * 500,  # force compression branch
                             evidence=[EvidenceItem(
                                 evidence_id="cite", content=content,
                                 page=1, score=0.9)])
    assert content in result.text
