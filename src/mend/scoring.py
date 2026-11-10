"""Score the agent's decision on one case against the answer key, and summarise a run.

The answer key is the staging table of the original, undrifted feed. A decision is scored by what the pipeline would do with it:

    patch       the proposed model is run over the drifted data and compared with the answer key, row by row
    no_change   the current model is run and compared in the same way
    escalate    nothing is applied, so the table cannot be wrong; it is right only if a person was actually needed

Labels (the three sets are what the headline numbers count):

    correct          repaired, correct_noop, harmless_change, correct_escalation
    silent           wrong_repair, needless_change, missed_repair, unsafe_patch, missed_escalation
                     the pipeline carries on with a wrong table and nobody is asked. The worst outcome, and the headline number.
    other failures   broke (a patch that does not run: loud, not silent), false_escalation (safe but costs a person's time), error
"""
from __future__ import annotations

import math
from collections import Counter

from . import sandbox
from .cases import GROUPS, Case

CORRECT = {"repaired", "correct_noop", "harmless_change", "correct_escalation"}
SILENT = {"wrong_repair", "needless_change", "missed_repair", "unsafe_patch", "missed_escalation"}
ACTED = {"patch", "no_change"}                  # decisions that go ahead without a person
LIMIT, MIN_N = 0.05, 30                         # the autonomy rule borrowed from Rung: upper bound under 5%, at least 30 cases


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Two-sided Wilson interval for a proportion k/n (z=1.96 is 95%)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def wilson_upper(k: float, n: int, z: float = 1.645) -> float:
    """One-sided 95% upper bound on the true error rate (the same bound Rung and Gloss use)."""
    return wilson(k, n, z)[1] if n else 1.0


def score_case(case: Case, outcome: str, sql: str | None, db, gold: list[tuple]) -> dict:
    """Label one decision. `outcome` is the agent's: patch, no_change or escalate. `sql` is the patch when there is one."""
    err, diff = None, None
    if outcome == "escalate":
        label = "correct_escalation" if case.expected == "escalate" else "false_escalation"
    elif case.expected == "escalate":                        # the information is gone: any applied change invents or ignores it
        label = "unsafe_patch" if outcome == "patch" else "missed_escalation"
    else:
        model_sql = sql if outcome == "patch" else sandbox.baseline_sql()
        chk = sandbox.check_model(db, model_sql, gold)
        err, diff = chk.error, chk.diff
        if outcome == "patch":
            if chk.ok:
                label = "repaired" if case.expected == "repair" else "harmless_change"
            elif chk.error:
                label = "broke"
            else:
                label = "wrong_repair" if case.expected == "repair" else "needless_change"
        else:
            label = "correct_noop" if chk.ok else "missed_repair"
    return {"label": label, "correct": label in CORRECT, "silent": label in SILENT, "check_error": err, "diff": diff}


def _ci(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None, "lo": lo, "hi": hi}


def summarise(rows: list[dict]) -> dict:
    """Per-group accuracy, the silent-corruption rate, the autonomy table and the cost, from scored rows."""
    rows = [r for r in rows if "id" in r]
    groups = {}
    for g in GROUPS:
        sub = [r for r in rows if r["group"] == g]
        if sub:
            groups[g] = {**_ci(sum(r["correct"] for r in sub), len(sub)), "labels": dict(Counter(r["label"] for r in sub)),
                         "silent": sum(r["silent"] for r in sub)}
    n = len(rows)
    acted = [r for r in rows if r["outcome"] in ACTED]
    autonomy = {}
    for g in GROUPS:
        sub = [r for r in acted if r["group"] == g]
        if sub:
            wrong = sum(not r["correct"] for r in sub)
            ub = wilson_upper(wrong, len(sub))
            autonomy[g] = {"acted": len(sub), "wrong": wrong, "upper": ub, "auto": len(sub) >= MIN_N and ub <= LIMIT}
    wrong_all = sum(not r["correct"] for r in acted)
    return {"n": n, "correct": _ci(sum(r["correct"] for r in rows), n), "silent": _ci(sum(r["silent"] for r in rows), n),
            "labels": dict(Counter(r["label"] for r in rows)), "groups": groups, "autonomy": autonomy,
            "acted": {"n": len(acted), "wrong": wrong_all, "upper": wilson_upper(wrong_all, len(acted))},
            "model_calls": sum(r.get("model_calls", 0) for r in rows), "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
            "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
            "mean_latency_s": round(sum(r.get("latency_s", 0) for r in rows) / n, 2) if n else 0.0}


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def format_report(s: dict, title: str = "") -> str:
    if not s["n"]:
        return "no rows"
    out = [title] if title else []
    out.append(f"{'group':<11} {'cases':>5} {'correct':>8} {'95% interval':>14}  labels")
    for g, v in s["groups"].items():
        out.append(f"{g:<11} {v['n']:>5} {v['k']:>8} {pct(v['lo']):>6}-{pct(v['hi']):<6}  " +
                   ", ".join(f"{k}={c}" for k, c in sorted(v["labels"].items(), key=lambda kv: -kv[1])))
    c, sl = s["correct"], s["silent"]
    out.append(f"{'all':<11} {c['n']:>5} {c['k']:>8} {pct(c['lo']):>6}-{pct(c['hi']):<6}")
    out.append(f"\nsilent corruption (the pipeline carries on with a wrong table, nobody asked): {sl['k']} of {sl['n']} "
               f"({pct(sl['rate'])}, 95% interval {pct(sl['lo'])}-{pct(sl['hi'])})")
    a = s["acted"]
    out.append(f"decisions made without a person: {a['n']}, wrong: {a['wrong']}, upper bound on their error: {pct(a['upper'])}")
    out.append(f"{'autonomy':<11} {'acted':>5} {'wrong':>6} {'upper':>6}  decision (limit {pct(LIMIT)}, at least {MIN_N} cases)")
    for g, v in s["autonomy"].items():
        out.append(f"{g:<11} {v['acted']:>5} {v['wrong']:>6} {pct(v['upper']):>6}  {'auto' if v['auto'] else 'approval needed'}")
    out.append(f"\nmodel calls {s['model_calls']}, tokens {s['input_tokens']} in / {s['output_tokens']} out, mean latency {s['mean_latency_s']}s per case")
    return "\n".join(out)
