"""RADIANT-Control M8: eval harness.

Unified layered evaluation over the frozen benchmark sets:

* :mod:`app.eval.registry` -- dataset registry + discovery over
  ``benchmarks/*.jsonl``.
* :mod:`app.eval.runner` -- single-command layered eval producing a
  machine-readable ``report.json`` and a human-readable ``report.md``.
* :mod:`app.eval.metrics` -- the five paper metrics (CoP/CiP/CiH/HR/ViR)
  plus the injectable-judge protocol with mandatory provenance.
* :mod:`app.eval.release_gate` -- release gate comparing a candidate
  report against a baseline report.
* :mod:`app.eval.fingerprint` -- configuration fingerprint collection
  (git, models, env, data versions, dependency versions).

Layer executors live in :mod:`app.eval.adapters`. Missing datasets or
missing optional packages (e.g. the M7 verification layer) are recorded
as ``skipped`` with a reason -- never as an error and never as a pass.
"""
