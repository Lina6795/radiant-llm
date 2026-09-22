"""Scenario (f): similar-topic bleed across sessions in the same workspace is
filtered by the Read Gate's task-relevance scoring. Also covers Recall@K
ranking and read-only namespace readability."""

from __future__ import annotations

from app.memory import ReadQuery, attempt_write
from app.memory.metrics import recall_at_k
from app.memory.models import MemoryCategory

from conftest import make_candidate


def _write(store, gate, subject, value, session):
    cand = make_candidate(
        category=MemoryCategory.SESSION,
        subject=subject,
        value=value,
        source_session_id=session,
        user_confirmed=False,
    )
    decision = attempt_write(store, gate, cand)
    assert decision.allowed, decision.reasons
    return decision.record


def test_cross_session_topic_bleed_filtered(store, gate, read_gate):
    _write(store, gate, "部署端口配置", "服务端口 8080，Nginx 反向代理", "sess-1")
    _write(store, gate, "季度总结报告大纲", "Q3 营收与用户增长", "sess-2")

    # task of the current session: quarterly report -- the deployment note
    # from sess-1 is a different topic and must be filtered out.
    results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="写季度总结报告"))
    assert all("部署" not in r.subject for r in results)
    assert any("季度" in r.subject for r in results)

    # and the reverse direction: deployment task does not pull the report note
    results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="部署端口是多少"))
    assert all("季度" not in r.subject for r in results)


def test_unrelated_query_recalls_nothing(store, gate, read_gate):
    _write(store, gate, "部署端口配置", "服务端口 8080", "sess-1")
    results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="周末爬山计划"))
    assert results == []


def test_recall_at_k_ranks_relevant_first(store, gate, read_gate):
    relevant = _write(store, gate, "数据库选型决策记录", "选用 SQLite 作为嵌入式库", "sess-1")
    _write(store, gate, "前端配色方案", "主色深蓝", "sess-2")
    _write(store, gate, "周报模板", "本周进展/下周计划", "sess-3")

    results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="数据库选型", top_k=3))
    assert results and results[0].memory_id == relevant.memory_id
    assert recall_at_k([r.memory_id for r in results], [relevant.memory_id]) == 1.0


def test_readonly_namespace_is_readable(store, gate, read_gate):
    """Read-only namespaces are write-protected but readable as policy context."""
    from app.memory.models import MemoryRecord, Provenance, Origin

    record = MemoryRecord(
        category=MemoryCategory.SESSION,
        subject="技能库使用规范",
        value="技能库内容只读，禁止写入",
        namespace="skills",
        workspace="ws-a",
        provenance=Provenance(origin=Origin.SYSTEM, source_run_id="bootstrap"),
        write_reason="内置策略上下文",
        confidence=1.0,
        sensitivity="internal",
        created_at=1.0,
    )
    store.put(record, actor="memory.bootstrap")
    results = read_gate.recall(store, ReadQuery(workspace="ws-a", namespace="skills"))
    assert [r.memory_id for r in results] == [record.memory_id]


def test_top_k_limit(store, gate, read_gate):
    for i in range(8):
        _write(store, gate, f"部署相关事项 {i}", f"部署笔记 {i}", f"sess-{i}")
    results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="部署", top_k=5))
    assert len(results) == 5
