"""Evaluator and metric-schema versioning (S11-A).

A cross-run metric comparison is only meaningful when both runs used the
same metric schema, the same evaluator code, and the same frozen inputs
(corpus + datasets + pipeline config). These constants are bumped
deliberately; the release gate refuses regression comparisons between
reports whose versions or fingerprints disagree and reports the pair as
``baseline_incompatible`` instead (fail-closed).

Version policy:
* ``METRIC_SCHEMA_VERSION`` -- bump when any metric's definition changes
  (e.g. S11-C split detected vs final-committed unsupported rates).
* ``EVALUATOR_VERSION`` -- bump when the harness aggregation logic changes.
* ``LAYER_EVALUATOR_VERSIONS`` -- bump the single layer whose executor
  logic changed.
* Source/config digests in ``fingerprint.py`` catch code drift that
  nobody remembered to version-bump; they are the fail-closed net.
"""

METRIC_SCHEMA_VERSION = "radiant-eval-metrics/v2"
REPORT_SCHEMA_VERSION = "radiant-eval-report/v2"
EVALUATOR_VERSION = "eval-harness/v2"

LAYER_EVALUATOR_VERSIONS = {
    "control": "control-exec/v1",
    "durable": "durable-exec/v1",
    "retrieval": "retrieval-exec/v2",
    "context": "context-exec/v1",
    "memory": "memory-exec/v1",
    "verification": "verification-exec/v2",
    "agent_e2e": "agent-e2e-exec/v2",
}

# The production retrieval preset used by the eval layer (M4 ablation
# ladder, app/retrieval/pipeline.py). Pinned so a preset swap is a
# deliberate, versioned event.
RETRIEVAL_PRESET = "A4_gates"

# Deterministic lanes/gates/fusion need no seed; the live-LLM draft in the
# verification answer arm is the only nondeterministic input and is
# recorded (not seeded) in the fingerprint.
RANDOM_SEED = 0
