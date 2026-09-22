"""Per-partition budget model: quota validation, margins, over-allocation."""

import pytest

from context.budgets import (DEFAULT_QUOTAS, DEFAULT_RESPONSE_RESERVE,
                             DEFAULT_TOTAL_TOKENS, PARTITIONS, BudgetConfig,
                             BudgetUsage)


def test_default_layout_sums_within_128k():
    cfg = BudgetConfig()
    assert cfg.total_tokens == DEFAULT_TOTAL_TOKENS == 128 * 1024
    assert sum(cfg.quotas.values()) + cfg.response_reserve <= cfg.total_tokens
    assert set(cfg.quotas) == set(PARTITIONS)


def test_quota_sum_over_total_rejected():
    bad = {name: DEFAULT_TOTAL_TOKENS for name in PARTITIONS}
    with pytest.raises(ValueError):
        BudgetConfig(quotas=bad)


def test_unknown_partition_rejected():
    with pytest.raises(ValueError):
        BudgetConfig(quotas={"system": 10, "mystery": 5})


def test_over_partition_detection_and_margin():
    cfg = BudgetConfig(total_tokens=1000, response_reserve=100,
                       quotas={"system": 100, "active_turn": 100,
                               "memory": 100, "evidence": 300,
                               "artifact": 100, "tool_result": 100})
    usage = BudgetUsage(config=cfg)
    usage.set("evidence", 250)
    assert not usage.is_over("evidence")
    assert usage.margin("evidence") == 50
    usage.set("evidence", 350)
    assert usage.is_over("evidence")
    assert usage.margin("evidence") == -50
    assert usage.over_partitions() == {"evidence": 50}


def test_total_overflow_uses_reserve_correctly():
    cfg = BudgetConfig(total_tokens=1000, response_reserve=200,
                       quotas={"system": 200, "active_turn": 200,
                               "memory": 200, "evidence": 200,
                               "artifact": 0, "tool_result": 0})
    usage = BudgetUsage(config=cfg)
    usage.set("system", 500)
    usage.set("memory", 250)
    assert usage.total_used == 750
    assert not usage.is_over_total          # 750 <= 800 usable
    usage.set("active_turn", 100)
    assert usage.is_over_total              # 850 > 800 usable
    assert usage.overall_margin == -50


def test_scale_derives_proportional_quotas():
    cfg = BudgetConfig.scale(8192)
    assert sum(cfg.quotas.values()) + cfg.response_reserve <= 8192
    assert cfg.quotas["evidence"] > cfg.quotas["system"]
