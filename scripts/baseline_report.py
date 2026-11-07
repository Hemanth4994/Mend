"""Run every case through the unrepaired pipeline and record what happens. Never calls a model.

For each case it records whether the baseline staging model still produces the right table, which dbt checks fail, and which
schema signals the raw table shows. From those it classifies the case:

    harmless           the unrepaired model still gives the right answer (nothing to fix)
    loud               the answer is wrong and a dbt check fails or the model errors
    schema_only        the answer is wrong, every dbt check passes, but the raw table shows a new or emptied column
    undetected         the answer is wrong and nothing at all notices

    python scripts/baseline_report.py [--cache DIR] [--out results/baseline.json]
"""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from mend import sandbox, signals
from mend.cases import load_cases


def classify(check: sandbox.Check, failed: list[str], sig: dict) -> str:
    if check.ok:
        return "harmless"
    if check.error or failed:
        return "loud"
    if sig["added"] or sig["went_null"]:
        return "schema_only"
    return "undetected"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache")
    ap.add_argument("--out", default="results/baseline.json")
    args = ap.parse_args()
    cache = Path(args.cache).resolve()

    gold = sandbox.build_gold(cache)
    baseline = sandbox.baseline_sql()
    records, started = [], time.time()
    for case in load_cases():
        db = sandbox.build_case(case, cache)
        check = sandbox.check_model(db, baseline, gold)
        results = signals.dbt_build(db, cache / "dbt_work" / case.id)
        failed = signals.failed_checks(results)
        sig = signals.schema_signals(db)
        records.append({"id": case.id, "split": case.split, "group": case.group, "drift": list(case.drifts), "expected": case.expected,
                        "baseline_ok": check.ok, "error": check.error, "columns_differ": (check.diff or {}).get("columns_differ"),
                        "failed_checks": failed, "signals": sig, "class": classify(check, failed, sig)})
        print(f"{case.id} {case.drifts[0]:<22} {records[-1]['class']:<12} failed={failed} signals={sig}", flush=True)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(records, indent=1), encoding="utf-8")

    print(f"\n{len(records)} cases in {time.time() - started:.0f}s\n")
    by_group = defaultdict(Counter)
    for r in records:
        by_group[r["group"]][r["class"]] += 1
    for group, counts in by_group.items():
        print(f"{group:<11}", dict(counts))
    # what the design says should happen
    problems = [r["id"] for r in records if (r["expected"] == "noop") != (r["class"] == "harmless")]
    print("\ncases whose baseline result contradicts their group:", problems or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
