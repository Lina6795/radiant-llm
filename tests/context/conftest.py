import pytest

from context.selector import EvidenceItem


@pytest.fixture
def evidence_items():
    """Small synthetic candidate set with mixed pages / authorities."""

    def make(eid, content, page, score, authority="primary", pinned=False,
             valid_from=None):
        return EvidenceItem(evidence_id=eid, content=content, page=page,
                            score=score, authority_level=authority,
                            pinned=pinned, valid_from=valid_from,
                            document_id="docA", chunk_id=f"docA:p{page}:c0")

    return [
        make("ev-gold", "The Transformer consists of an encoder and a decoder stack.", 1, 0.9, pinned=True),
        make("ev-hi", "attention mechanisms compute weighted sums of values", 2, 0.8),
        make("ev-hi2", "scaled dot product attention uses queries and keys", 2, 0.75),
        make("ev-hi3", "multi head attention runs heads in parallel", 2, 0.7),
        make("ev-mid", "positional encodings inject sequence order information", 3, 0.5, authority="secondary"),
        make("ev-lo", "unrelated filler text about training hardware budgets", 4, 0.1),
    ]


@pytest.fixture
def stub_summarizer():
    """Deterministic LLM-stub: keeps the first half of the text."""
    calls = []

    def summarize(text: str, target_tokens: int) -> str:
        calls.append({"len": len(text), "target": target_tokens})
        return text[: max(1, len(text) // 2)]

    summarize.calls = calls
    return summarize
