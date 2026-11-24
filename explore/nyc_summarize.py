"""Sort the schema changes found by nyc_schema_timeline.py into kinds and count them.

    python explore/nyc_summarize.py                 # reads results/nyc/yellow_schema_timeline.json, prints the counts and one line per event
    python explore/nyc_summarize.py --md            # the same as a Markdown table

The kinds are decided by fixed rules from the column lists alone (no judgement about meaning):
    restructure  three or more columns removed (a rename wave or a new layout)
    artifact     only the pandas index column `__index_level_0__` appeared or went away (plus any type wobble that came with it)
    case-only    a column differs only in letter case (airport_fee -> Airport_fee), plus any type changes
    additive     only new columns
    type-only    columns kept their names and changed type
Exploration only: not part of the evaluated code or the test suite.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

TIMELINE = Path(__file__).resolve().parent.parent / "results" / "nyc" / "yellow_schema_timeline.json"
FEE_COLUMNS = {"congestion_surcharge", "airport_fee", "improvement_surcharge"}
ARTIFACT = "__index_level_0__"


def kind(c: dict) -> str:
    """The kind of one change event (a dict with added, removed, case_renamed, retyped)."""
    added, removed = set(c["added"]), set(c["removed"])
    if len(removed) >= 3:
        return "restructure"
    changed = added | removed
    if changed and changed <= {ARTIFACT}:
        return "artifact"
    if c["case_renamed"]:
        return "case-only"
    if added and not removed and not c["retyped"]:
        return "additive"
    if c["retyped"] and not added and not removed:
        return "type-only"
    return "mixed"


def summarize(data: dict) -> dict:
    months = data["months"]
    read = [m for m, d in months.items() if "error" not in d]
    kinds = Counter(kind(c) for c in data["changes"])
    type_only = [c for c in data["changes"] if kind(c) == "type-only"]
    only_fees = [c for c in type_only if {col.lower() for col, _, _ in c["retyped"]} <= FEE_COLUMNS]
    return {
        "months_read": len(read), "first": read[0], "last": read[-1],
        "unreadable": len(months) - len(read),
        "rows_total": sum(months[m]["rows"] for m in read),
        "events": len(data["changes"]), "kinds": dict(kinds),
        "type_only": len(type_only), "type_only_fee_columns_only": len(only_fees),
        "type_only_other": [f"{c['from']} -> {c['to']}" for c in type_only if c not in only_fees],
    }


def describe_event(c: dict) -> str:
    parts = []
    if c["added"]:
        parts.append(f"{len(c['added'])} added" + (f" ({', '.join(c['added'])})" if len(c["added"]) <= 3 else ""))
    if c["removed"]:
        parts.append(f"{len(c['removed'])} removed" + (f" ({', '.join(c['removed'])})" if len(c["removed"]) <= 3 else ""))
    if c["case_renamed"]:
        parts.append("case: " + ", ".join(f"{o}->{n}" for o, n in c["case_renamed"]))
    if c["retyped"]:
        parts.append("retyped: " + ", ".join(f"{col} {old}->{new}" for col, old, new in c["retyped"]))
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--timeline", default=str(TIMELINE))
    ap.add_argument("--md", action="store_true", help="print the event list as a Markdown table")
    args = ap.parse_args(argv)
    data = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    s = summarize(data)
    print(f"{s['months_read']} monthly files read ({s['first']} .. {s['last']}), {s['unreadable']} unreadable, {s['rows_total']:,} trips in all")
    print(f"{s['events']} months differ in schema from the one before: " + ", ".join(f"{k} {n}" for k, n in sorted(s["kinds"].items(), key=lambda kv: -kv[1])))
    print(f"type-only events: {s['type_only']}, of which {s['type_only_fee_columns_only']} touch only {', '.join(sorted(FEE_COLUMNS))}; others: {', '.join(s['type_only_other']) or 'none'}")
    if args.md:
        print("\n| Month | Kind | What changed |\n|---|---|---|")
    for c in data["changes"]:
        line = (f"| {c['to']} | {kind(c)} | {describe_event(c)} |" if args.md else f"{c['from']} -> {c['to']}  {kind(c):<11} {describe_event(c)}")
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
