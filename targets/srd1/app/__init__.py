"""SRD-1 minimal: the parts of the reference system that one procedure (TP-SEC-001) needs.

Built here: C-01 customer channel, C-02 orchestrator, C-04 model gateway, C-15 trace store (a hash chain).
Not built here: C-03 and C-05 to C-14, C-16 to C-20, and every document, data set and role of the specification
(`targets/srd1/SPEC.md`). In particular C-10 (guardrails) is not built, so a result from this system is not a
result for the full SRD-1.

All data is synthetic. The only outside call is the model call that the gateway makes.
"""
