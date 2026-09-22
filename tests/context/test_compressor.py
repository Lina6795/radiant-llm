"""Compressor: two levels, lineage completeness, evidence red line."""

import pytest

from context.compressor import (METHOD_LLM, METHOD_RULES, Compressor,
                                EvidenceCompressionError)
from context.tokenizer import TokenCounter

COUNTER = TokenCounter()

BOILERPLATE_TEXT = "\n".join([
    "Page 3 of 10",
    "====================",
    "Memory note: the user prefers concise answers with citations.",
    "",
    "",
    "",
    "42",
    "Another note: retrieval runs A4 gates by default.",
    "----------------",
])

LONG_TEXT = (
    "The experiment measured latency across 42 configurations in March 2026. "
    + "Intermediate observation sentences with some detail. " * 30
    + "Final conclusion: the A4 gate configuration wins on anchor hit rate."
)


def test_rule_compression_reduces_tokens():
    comp = Compressor(counter=COUNTER)
    target = COUNTER.count(BOILERPLATE_TEXT) // 2
    result = comp.compress(BOILERPLATE_TEXT, target)
    assert result.lineage.tokens_after < result.lineage.tokens_before
    assert "drop_boilerplate_lines" in result.lineage.steps


def test_sentence_window_keeps_first_and_last():
    comp = Compressor(counter=COUNTER)
    result = comp.compress(LONG_TEXT, 60)
    assert result.text.startswith("The experiment measured latency")
    assert "Final conclusion" in result.text


def test_lineage_is_complete_and_traceable():
    comp = Compressor(counter=COUNTER)
    result = comp.compress(LONG_TEXT, 50)
    lin = result.lineage
    assert lin.input_hash and lin.input_hash != lin.output_hash
    assert lin.method == METHOD_RULES
    assert lin.model is None
    assert lin.tokens_before > lin.tokens_after
    assert lin.target_tokens == 50
    assert lin.steps


def test_llm_recursion_uses_injected_callable(stub_summarizer):
    # First+last sentences are individually huge, so the rule-level
    # sentence window cannot reach the target and the LLM level must
    # take over.
    long_sentences = (
        "First section opens with " + "detailed elaboration tokens " * 60 + ". "
        + "Middle filler sentences keep coming. " * 20
        + "Final section closes with " + "detailed concluding tokens " * 60 + "."
    )
    comp = Compressor(counter=COUNTER, llm_summarizer=stub_summarizer,
                      model_name="stub-model")
    target = COUNTER.count(long_sentences) // 8
    result = comp.compress(long_sentences, target, allow_llm=True)
    assert stub_summarizer.calls                       # callable was invoked
    assert result.lineage.method == METHOD_LLM
    assert result.lineage.model == "stub-model"
    assert any(s.startswith("llm_summarize_depth_")
               for s in result.lineage.steps)


def test_llm_not_called_when_rules_suffice(stub_summarizer):
    comp = Compressor(counter=COUNTER, llm_summarizer=stub_summarizer)
    comp.compress(BOILERPLATE_TEXT, COUNTER.count(BOILERPLATE_TEXT) + 10)
    assert stub_summarizer.calls == []


def test_evidence_partition_refuses_compression():
    comp = Compressor(counter=COUNTER)
    with pytest.raises(EvidenceCompressionError):
        comp.compress_partition("evidence", "citation text", 5)


def test_noncompressible_partition_rejected():
    comp = Compressor(counter=COUNTER)
    with pytest.raises(ValueError):
        comp.compress_partition("system", "prompt", 5)


def test_memory_partition_compresses():
    comp = Compressor(counter=COUNTER)
    result = comp.compress_partition("memory", LONG_TEXT, 60)
    assert result.lineage.tokens_after <= result.lineage.tokens_before
