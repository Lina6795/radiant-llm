"""M5 stress experiment: context budget pressure vs anchor preservation.

One-command reproduction:

    cd /mnt/lina/radiant-llm && \\
      PYTHONPATH=app \\
      RADIANT_EVIDENCE_DB=artifacts/baseline/m0-20260922/evidence.db \\
      RADIANT_ARTIFACT_DIR=artifacts/context/m5-20260922/artifact_store \\
      ./runtime/bin/python3.12 -m context.experiment \\
        --output-dir artifacts/context/m5-20260922

Methodology (pseudo-sources — READ BEFORE CITING NUMBERS)
---------------------------------------------------------
The frozen real corpus has exactly ONE document (119 text chunks of
attention_is_all_you_need_1706.03762.pdf). Scaling to 10/25/50/100 real
PDF sources is infeasible here: Nougat parsing runs ~10 min/page on CPU,
so 100 PDFs would take ~17 CPU-hours. We therefore simulate additional
sources with a seeded generator:

* near-synonym distractors: real chunks rewritten via a fixed
  substitution map plus sentence shuffling (they compete with gold
  chunks for relevance and budget);
* filler distractors: template-generated sentences disjoint from the
  query topic (they consume budget only).

Retrieval scores are also simulated (gold ~ U(0.65, 0.95), near-syn ~
U(0.45, 0.90), filler ~ U(0.02, 0.35), seeded): the components under
test are budgeting / selection / compression / assembly, NOT the M4
retrieval stack. Everything downstream of the candidate list — token
counting, selection, pin protection, compression, isolation, decision —
is the real ``app/context`` code running on real chunk text.

Limitations: distractor score distributions and chunk lengths are
chosen, not measured; real 100-PDF corpora will have heavier tails,
cross-document near-duplicates and OCR-degraded chunks. Absolute
failure onsets reported here are lower bounds on corpus difficulty.

Gold anchor: RET-T01 from benchmarks/retrieval_cases.jsonl (page 1 of
the attention paper, 5 chunks; expected facts "encoder", "decoder").
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context.budgets import BudgetConfig  # noqa: E402
from context.engine import ContextEngine, EngineOptions  # noqa: E402
from context.isolate import ArtifactStore  # noqa: E402
from context.selector import EvidenceItem  # noqa: E402
from context.tokenizer import TokenCounter  # noqa: E402
from evidence.store import get_evidence_store  # noqa: E402

SEED = 20260922
GOLD_PAGE = 1
GOLD_DOCUMENT = "attention_is_all_you_need_1706.03762.pdf"
EXPECTED_FACTS = ["encoder", "decoder"]
DEFAULT_SOURCES = [1, 10, 25, 50, 100]
CHUNKS_PER_PSEUDO_SOURCE = 6   # 4 near-synonym + 2 filler
NEAR_SYN_PER_SOURCE = 4

# Fixed substitution map for near-synonym distractors (applied with a
# seeded per-chunk coin flip so rewrites differ across chunks).
SUBSTITUTIONS = {
    "attention": "focus weighting",
    "encoder": "encoding stack",
    "decoder": "decoding stack",
    "transformer": "sequence transducer",
    "layer": "block",
    "training": "optimization",
    "sequence": "series",
    "model": "architecture",
    "network": "system",
    "output": "result",
    "input": "signal",
    "translation": "conversion",
}

FILLER_TEMPLATES = [
    "The {org} committee reviewed the {topic} proposal on {day}.",
    "Preliminary {topic} figures were tabled at the {org} meeting.",
    "A {org} spokesperson declined to comment on the {topic} report.",
    "Minutes of the {day} session mention the {topic} budget twice.",
    "The {topic} working group adjourned without a resolution on {day}.",
]
FILLER_ORGS = ["regional", "oversight", "planning", "steering", "advisory"]
FILLER_TOPICS = ["catering", "parking", "lighting", "scheduling", "venue"]
FILLER_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

MEMORY_NOTES = (
    "User prefers concise answers with explicit citations. "
    "Previous session covered retrieval ablations A0 through A4. "
    "The A4 gate configuration was selected as the default. "
) * 6

BIG_TOOL_RESULT = (
    "batch_job_id,status,latency_ms,rows\n"
    + "\n".join(f"job-{i:04d},done,{120 + i % 37},{1000 + i}"
                for i in range(600))
)


def load_real_items(db_path: str) -> List[Dict[str, Any]]:
    store = get_evidence_store(db_path)
    try:
        result = store.query_evidence(modality="text", limit=1000)
        return [it for it in result["items"] if it.get("content")]
    finally:
        store.close()


def gold_ids(items: List[Dict[str, Any]]) -> List[str]:
    return [it["evidence_id"] for it in items
            if GOLD_DOCUMENT in (it.get("artifact_uri") or "")
            and it.get("page") == GOLD_PAGE]


# ---------------------------------------------------------------------------
# Pseudo-source generation (seeded, deterministic)
# ---------------------------------------------------------------------------

def rewrite_near_synonym(text: str, rng: random.Random) -> str:
    out = text
    for src, dst in SUBSTITUTIONS.items():
        if src in out.lower() and rng.random() < 0.8:
            out = out.replace(src, dst).replace(src.capitalize(),
                                                dst.capitalize())
    sentences = [s for s in out.replace("\n", " ").split(". ") if s.strip()]
    rng.shuffle(sentences)
    return ". ".join(sentences)


def make_filler(rng: random.Random) -> str:
    return " ".join(
        rng.choice(FILLER_TEMPLATES).format(org=rng.choice(FILLER_ORGS),
                                            topic=rng.choice(FILLER_TOPICS),
                                            day=rng.choice(FILLER_DAYS))
        for _ in range(3))


def pseudo_source_chunks(idx: int, real_texts: List[str],
                         rng: random.Random) -> List[Dict[str, Any]]:
    doc = f"pseudo-src-{idx:03d}"
    chunks = []
    for c in range(NEAR_SYN_PER_SOURCE):
        chunks.append({
            "document_id": doc, "page": c + 1,
            "content": rewrite_near_synonym(rng.choice(real_texts), rng),
            "kind": "near_syn",
        })
    for c in range(CHUNKS_PER_PSEUDO_SOURCE - NEAR_SYN_PER_SOURCE):
        chunks.append({
            "document_id": doc,
            "page": NEAR_SYN_PER_SOURCE + c + 1,
            "content": make_filler(rng),
            "kind": "filler",
        })
    return chunks


def build_candidates(items: List[Dict[str, Any]], n_sources: int,
                     rep: int, pin: bool, seed: int,
                     gold: List[str]) -> List[EvidenceItem]:
    """Real chunks (source 1) + (n_sources - 1) pseudo-sources, scored."""
    rng = random.Random(seed + rep * 1009 + n_sources * 9176 + int(pin))
    candidates: List[EvidenceItem] = []
    gold_set = set(gold)
    for it in items:
        is_gold = it["evidence_id"] in gold_set
        score = rng.uniform(0.65, 0.95) if is_gold else rng.uniform(0.10, 0.55)
        candidates.append(EvidenceItem(
            evidence_id=it["evidence_id"], content=it["content"],
            page=it.get("page"), score=score,
            authority_level=it.get("authority_level", "primary"),
            pinned=is_gold and pin,
            document_id=it.get("document_id"),
            chunk_id=(it.get("source_span") or {}).get("chunk_id")))
    real_texts = [it["content"] for it in items]
    for src in range(2, n_sources + 1):
        for j, chunk in enumerate(pseudo_source_chunks(src, real_texts, rng)):
            score = (rng.uniform(0.45, 0.90) if chunk["kind"] == "near_syn"
                     else rng.uniform(0.02, 0.35))
            candidates.append(EvidenceItem(
                evidence_id=f"{chunk['document_id']}:p{chunk['page']}",
                content=chunk["content"], page=chunk["page"], score=score,
                authority_level="secondary",
                document_id=chunk["document_id"]))
    rng.shuffle(candidates)
    return candidates


# ---------------------------------------------------------------------------
# Run matrix
# ---------------------------------------------------------------------------

def experiment_budget() -> BudgetConfig:
    # Deliberately tight 16k window so pressure starts at small source
    # counts and grows monotonically with the pseudo-source ladder.
    return BudgetConfig(
        total_tokens=16 * 1024, response_reserve=2048,
        quotas={"system": 1024, "active_turn": 1024, "memory": 2048,
                "evidence": 8192, "artifact": 1024, "tool_result": 1024})


def run_once(engine: ContextEngine, counter: TokenCounter,
             candidates: List[EvidenceItem], gold: List[str]) -> Dict[str, Any]:
    input_tokens = (counter.count(MEMORY_NOTES)
                    + counter.count(BIG_TOOL_RESULT)
                    + sum(c.tokens(counter) for c in candidates))
    t0 = time.perf_counter()
    result = engine.assemble(
        system="You are RADIANT-LLM. Answer with citations.",
        active_turn="What two main components does the Transformer "
                    "architecture consist of?",
        memory=MEMORY_NOTES,
        evidence=candidates,
        tool_results=[BIG_TOOL_RESULT])
    latency_ms = (time.perf_counter() - t0) * 1000

    selected_ids = {it.evidence_id for it in result.evidence}
    gold_set = set(gold)
    evidence_text = result.partitions["evidence"].lower()
    facts = {f: (f in evidence_text) for f in EXPECTED_FACTS}
    final_tokens = counter.count(result.text)
    return {
        "n_candidates": len(candidates),
        "input_tokens": input_tokens,
        "final_tokens": final_tokens,
        "token_savings_rate": round(1 - final_tokens / max(1, input_tokens), 4),
        "overflow_input": input_tokens > engine.config.usable_tokens,
        "gold_chunks_retained": round(
            len(gold_set & selected_ids) / max(1, len(gold_set)), 4),
        "facts_present": facts,
        "answer_proxy": round(sum(facts.values()) / len(EXPECTED_FACTS), 4),
        "decision": result.decision.decision,
        "n_drops": len(result.decision.drops),
        "drops_without_reason": sum(
            1 for d in result.decision.drops if not d.reason),
        "n_compressions": len(result.decision.compressions),
        "n_pointers": len(result.decision.pointers),
        "assemble_latency_ms": round(latency_ms, 2),
        "residual_overflow_tokens": result.decision.residual_overflow_tokens,
    }


def aggregate(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    def ms(key):
        vals = [r[key] for r in runs]
        return {"mean": round(statistics.fmean(vals), 4),
                "pstdev": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0}

    return {
        "reps": len(runs),
        "input_tokens": ms("input_tokens"),
        "final_tokens": ms("final_tokens"),
        "token_savings_rate": ms("token_savings_rate"),
        "gold_chunks_retained": ms("gold_chunks_retained"),
        "answer_proxy": ms("answer_proxy"),
        "assemble_latency_ms": ms("assemble_latency_ms"),
        "overflow_runs": sum(1 for r in runs if r["overflow_input"]),
        "decisions": sorted({r["decision"] for r in runs}),
        "drops_without_reason": sum(r["drops_without_reason"] for r in runs),
        "n_compressions": ms("n_compressions"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", default="artifacts/context/m5-20260922")
    parser.add_argument("--evidence-db",
                        default=os.getenv("RADIANT_EVIDENCE_DB"))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--sources", type=int, nargs="+",
                        default=DEFAULT_SOURCES)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(os.getenv("RADIANT_ARTIFACT_DIR",
                                  out_dir / "artifact_store"))

    counter = TokenCounter()
    items = load_real_items(args.evidence_db)
    gold = gold_ids(items)
    if not gold:
        raise SystemExit("gold anchor chunks not found — wrong evidence DB?")

    engine = ContextEngine(
        config=experiment_budget(), counter=counter,
        artifact_store=ArtifactStore(root=artifact_dir, counter=counter),
        options=EngineOptions(isolate_threshold_tokens=1024))

    results: Dict[str, Any] = {
        "milestone": "M5-context-budget",
        "seed": args.seed,
        "reps": args.reps,
        "tokenizer_backend": counter.backend,
        "budget": {"total_tokens": engine.config.total_tokens,
                   "quotas": engine.config.quotas,
                   "response_reserve": engine.config.response_reserve},
        "gold_anchor": {"case": "RET-T01", "document": GOLD_DOCUMENT,
                        "page": GOLD_PAGE, "chunk_ids": gold,
                        "expected_facts": EXPECTED_FACTS},
        "methodology": ("pseudo-sources: 1 real document (119 chunks) + "
                        "seeded near-synonym/filler distractors; simulated "
                        "retrieval scores; see module docstring"),
        "cells": {},
    }

    for n_sources in args.sources:
        for pin in (True, False):
            runs = []
            for rep in range(args.reps):
                candidates = build_candidates(items, n_sources, rep, pin,
                                              args.seed, gold)
                runs.append(run_once(engine, counter, candidates, gold))
            key = f"sources={n_sources}|pin={'on' if pin else 'off'}"
            results["cells"][key] = {"runs": runs, "aggregate": aggregate(runs)}
            agg = results["cells"][key]["aggregate"]
            print(f"{key}: savings={agg['token_savings_rate']['mean']:.3f} "
                  f"gold={agg['gold_chunks_retained']['mean']:.3f} "
                  f"facts={agg['answer_proxy']['mean']:.3f} "
                  f"decisions={agg['decisions']}")

    with open(out_dir / "metrics.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    write_report(results, out_dir / "REPORT.md")
    print(f"wrote {out_dir / 'metrics.json'} and {out_dir / 'REPORT.md'}")


def write_report(results: Dict[str, Any], path: Path) -> None:
    lines: List[str] = []
    lines.append("# M5 stress experiment: context budget vs anchor preservation")
    lines.append("")
    lines.append(f"- seed `{results['seed']}`, {results['reps']} reps/cell, "
                 f"tokenizer `{results['tokenizer_backend']}`")
    b = results["budget"]
    lines.append(f"- budget: total {b['total_tokens']}, quotas {b['quotas']}, "
                 f"reserve {b['response_reserve']}")
    g = results["gold_anchor"]
    lines.append(f"- gold anchor: {g['case']} ({g['document']} page {g['page']}, "
                 f"{len(g['chunk_ids'])} chunks, facts {g['expected_facts']})")
    lines.append("")
    lines.append("| sources | pin | savings rate | gold retained | "
                 "facts present | latency ms | decisions |")
    lines.append("|---|---|---|---|---|---|---|")
    for key, cell in results["cells"].items():
        sources, pin = key.split("|")
        a = cell["aggregate"]
        lines.append(
            f"| {sources.split('=')[1]} | {pin.split('=')[1]} "
            f"| {a['token_savings_rate']['mean']:.3f}±{a['token_savings_rate']['pstdev']:.3f} "
            f"| {a['gold_chunks_retained']['mean']:.3f}±{a['gold_chunks_retained']['pstdev']:.3f} "
            f"| {a['answer_proxy']['mean']:.3f}±{a['answer_proxy']['pstdev']:.3f} "
            f"| {a['assemble_latency_ms']['mean']:.1f}±{a['assemble_latency_ms']['pstdev']:.1f} "
            f"| {','.join(a['decisions'])} |")
    lines.append("")
    # Failure onset: first source count where pin-off gold/fact retention
    # degrades below 1.0 in ANY rep (worst-case, not best-case, reporting).
    onset = None
    for key, cell in results["cells"].items():
        if not key.endswith("pin=off"):
            continue
        worst = min(r["answer_proxy"] for r in cell["runs"])
        if worst < 1.0:
            onset = (key, worst)
            break
    lines.append("## Failure onset (pin off)")
    lines.append("")
    if onset:
        lines.append(f"- first degraded cell: `{onset[0]}` "
                     f"(worst-rep answer proxy {onset[1]:.3f} < 1.0)")
    else:
        lines.append("- no pin-off degradation observed in the tested range")
    lines.append("")
    lines.append("All cells are mean±pstdev over all reps; every run is "
                 "kept verbatim in metrics.json (no best-run cherry-picking).")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
