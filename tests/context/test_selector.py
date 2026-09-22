"""Evidence selection: pin protection, diversity cap, ordering, reasons."""

from context.selector import (REASON_BUDGET_EXHAUSTED,
                              REASON_DIVERSITY_PAGE_CAP, EvidenceItem,
                              composite_scores, select_evidence)
from context.tokenizer import TokenCounter

COUNTER = TokenCounter()


def test_pinned_never_dropped_even_over_budget(evidence_items):
    tiny = 1  # one token: cannot fit the pinned gold chunk at all
    result = select_evidence(evidence_items, tiny, counter=COUNTER)
    ids = [it.evidence_id for it in result.selected]
    assert "ev-gold" in ids                # pin survives
    assert result.overflow                 # ...and the overflow is explicit
    assert all(it.pinned or r for it, r in result.excluded)


def test_higher_relevance_wins_budget(evidence_items):
    gold = evidence_items[0]
    budget = gold.tokens(COUNTER) + evidence_items[1].tokens(COUNTER) + 10
    result = select_evidence(evidence_items, budget, counter=COUNTER)
    ids = [it.evidence_id for it in result.selected]
    assert ids[0] == "ev-gold"             # pinned first
    assert "ev-hi" in ids                  # strongest unpinned admitted
    assert "ev-lo" not in ids              # weakest dropped
    drop = dict((it.evidence_id, r) for it, r in result.excluded)
    assert drop["ev-lo"] == REASON_BUDGET_EXHAUSTED


def test_diversity_caps_same_page_duplicates(evidence_items):
    # Page 2 has 3 candidates; cap is 2 per page. Give ample budget so the
    # only reason the third page-2 item can be excluded is the diversity cap.
    budget = sum(it.tokens(COUNTER) for it in evidence_items) + 100
    result = select_evidence(evidence_items, budget, counter=COUNTER,
                             max_per_page=2)
    page2 = [it.evidence_id for it in result.selected if it.page == 2]
    assert len(page2) == 2
    drop = dict((it.evidence_id, r) for it, r in result.excluded)
    assert drop.get("ev-hi3") == REASON_DIVERSITY_PAGE_CAP


def test_authority_and_freshness_affect_composite():
    now = 1_800_000_000.0
    a = EvidenceItem("a", "x", score=0.5, authority_level="primary",
                     valid_from=now - 3600)
    b = EvidenceItem("b", "x", score=0.5, authority_level="tertiary",
                     valid_from=now - 10 * 365 * 24 * 3600)
    scores = composite_scores([a, b], now=now)
    assert scores["a"] > scores["b"]


def test_every_exclusion_carries_a_reason(evidence_items):
    result = select_evidence(evidence_items, 5, counter=COUNTER)
    assert result.excluded
    assert all(reason for _, reason in result.excluded)
