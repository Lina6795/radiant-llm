"""Tokenizer consistency: same interface across backends, deterministic,
heuristic within documented tolerance of tiktoken on prose."""

import pytest

from context.tokenizer import (BACKEND_HEURISTIC, BACKEND_TIKTOKEN,
                               TokenCounter, count_tokens)

SAMPLE_PROSE = (
    "The dominant sequence transduction models are based on complex "
    "recurrent or convolutional neural networks that include an encoder "
    "and a decoder. The best performing models connect the encoder and "
    "decoder through an attention mechanism. " * 4
)


def test_same_text_same_count():
    counter = TokenCounter()
    assert counter.count(SAMPLE_PROSE) == counter.count(SAMPLE_PROSE)


def test_monotonic_in_length():
    counter = TokenCounter()
    base = counter.count("the encoder and the decoder")
    doubled = counter.count("the encoder and the decoder " * 2)
    assert doubled > base


def test_empty_and_none_safe():
    counter = TokenCounter()
    assert counter.count("") == 0
    assert count_tokens("") == 0


def test_heuristic_within_tolerance_on_prose():
    tk = TokenCounter("tiktoken")
    heur = TokenCounter("heuristic")
    exact, approx = tk.count(SAMPLE_PROSE), heur.count(SAMPLE_PROSE)
    # Documented error band for English technical prose: ±15%.
    assert abs(approx - exact) / exact < 0.15


def test_heuristic_flags_approximate():
    assert TokenCounter("heuristic").is_approximate
    assert not TokenCounter("tiktoken").is_approximate


def test_backend_names():
    assert TokenCounter("tiktoken").backend == BACKEND_TIKTOKEN
    assert TokenCounter("heuristic").backend == BACKEND_HEURISTIC


def test_unknown_backend_rejected():
    with pytest.raises(ValueError):
        TokenCounter("bogus")


def test_truncate_to_tokens_respects_budget():
    counter = TokenCounter()
    cut = counter.truncate_to_tokens(SAMPLE_PROSE, 20)
    assert counter.count(cut) <= 20
