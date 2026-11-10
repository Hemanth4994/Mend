# Seed database for test case v04

`v04/mend.duckdb` is the raw database for case `v04` (`placed_at` becomes epoch milliseconds), built by `sandbox.build_case` from the code tagged
`frozen-v1` (commit 942a64d) on Linux with duckdb 1.5.6. SHA-256: `b2433500e45f0b0eb58ff690a8c4a49184eff104fe6cf225e55419a6db64aaef`.

Why it exists: on Windows, dlt crashes while loading this one case. dlt expects `ValueError` when an epoch-milliseconds integer cannot become a
timestamp and then files the value in a side column (`placed_at__v_bigint`), which is what happens on Linux. Python on Windows raises `OSError`
instead, dlt does not catch it, and the load fails halfway. The case builder then left a half-built database in the case cache, and the next runs
trusted it (see PLAN.md, "What went wrong in the first test runs").

To use it on Windows, replace the case in the cache: `.mend_cache/cases/v04/mend.duckdb`. Nothing under the frozen folders changes.
