"""RADIANT-Control M8: observability底座.

* :mod:`app.observability.trace` -- unified trace schema plus adapters
  mapping the M4 retrieval trace, the M3 durable event stream and the
  M5 context decision record into it.
* :mod:`app.observability.bad_cases` -- the bad-case registry feeding
  the data flywheel (发现 -> 归因 -> 修复 -> regression case).
"""
