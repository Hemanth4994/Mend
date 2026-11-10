"""Run the Mend repair agent over one split in one configuration and score every decision against the answer key.

    python scripts/run_eval.py --split dev --config evidence --model oracle     # a stand-in: no API key, measures the gate
    python scripts/run_eval.py --split dev --config evidence --model gemini     # the real model, needs GOOGLE_API_KEY
    python scripts/run_eval.py --split test --config evidence --model gemini --final   # only after `git tag frozen-v1`, once per configuration
    python scripts/run_eval.py --report results/dev_evidence_oracle.jsonl       # print the summary of a finished run

Results go to results/<split>_<config>_<model>.jsonl, one line per case, so a run that stops (daily quota, closed laptop) resumes where
it stopped. Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY to see every case as a trace.

The test split is guarded for every model, stand-ins included: it refuses to run unless the tag frozen-v1 exists and the frozen folders are
exactly as tagged. Anything learned from the test split after that would be tuning on it.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))          # for `standins` when this file is imported by the tests

import standins                                                    # noqa: E402

from mend import agent as agent_mod, sandbox, scoring              # noqa: E402
from mend.cases import CASES_FILE, load_cases                      # noqa: E402
from mend.throttle import DailyQuotaExceeded, Throttle             # noqa: E402

TAG = "frozen-v1"
GUARDED = ("src", "eval", "scripts", "tests", "dbt")
MODELS = ("gemini",) + standins.NAMES


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, cwd=ROOT).stdout.strip()


def check_freeze() -> None:
    """Fail closed: the test split may only be run on exactly the code and cases that were tagged."""
    try:
        tag_commit = git("rev-list", "-n", "1", TAG)
        head = git("rev-parse", "HEAD")
        git("merge-base", "--is-ancestor", tag_commit, head)
        changed = git("diff", "--name-only", TAG, "--", *GUARDED) + git("status", "--porcelain", "--", *GUARDED)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.exit(f"Cannot confirm the freeze ({exc}). Create the tag with `git tag {TAG}` first.")
    if changed.strip():
        sys.exit(f"Files under {GUARDED} differ from tag {TAG}; the test split must run on the frozen code:\n{changed}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def header(split: str, config: str, model: str, model_id: str) -> dict:
    try:
        head = git("rev-parse", "--short", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        head = "unknown"
    return {"run": {"split": split, "config": config, "model": model, "model_id": model_id, "max_attempts": agent_mod.MAX_ATTEMPTS,
                    "prompt_sha": sha(agent_mod.SYSTEM_PROMPT), "cases_sha": sha(CASES_FILE.read_text(encoding="utf-8")),
                    "git_head": head, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}}


def run_split(split: str, config: str, model: str, cache: Path, out: Path, limit: int | None = None, ids: list[str] | None = None,
              proposer=None, log=print) -> list[dict]:
    """Run the agent on each unanswered case of a split, appending one scored row per case. Returns every row in the file."""
    cases = [c for c in load_cases(split) if ids is None or c.id in ids]
    done = {r["id"] for r in read_rows(out) if "id" in r}
    todo = [c for c in cases if c.id not in done][:limit]
    out.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    shared = proposer
    if model == "gemini" and shared is None:
        if not os.environ.get("GOOGLE_API_KEY"):
            sys.exit("GOOGLE_API_KEY is not set. Set it in your own terminal (never paste it into a chat or a file), or use a stand-in model.")
        shared = agent_mod.gemini_proposer(throttle=Throttle())
    model_id = getattr(shared, "model", "") or ("stand-in" if model in standins.NAMES else model)
    gold = sandbox.build_gold(cache)
    new_file = not out.exists()
    with open(out, "a", encoding="utf-8", newline="\n") as f:
        if new_file:
            f.write(json.dumps(header(split, config, model, model_id)) + "\n")
        for case in todo:
            db = sandbox.build_case(case, cache)
            prop = shared or standins.make(model, case)
            # dbt keeps dbt.log open until the process ends, and Windows will not delete an open file: tolerate that when cleaning up
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
                try:
                    final = agent_mod.run_case(agent_mod.build_agent(prop), db, work, config, case.notes, case.id, case.split, model_id)
                except DailyQuotaExceeded:
                    log(f"daily quota reached after {len(done)} cases; run the same command again tomorrow and it resumes.")
                    break
                except Exception as exc:                                  # an unexplained failure is a result, not a reason to stop the run
                    row = {"id": case.id, "split": case.split, "group": case.group, "expected": case.expected, "drifts": list(case.drifts),
                           "config": config, "outcome": "error", "label": "error", "correct": False, "silent": False, "error": repr(exc)[:300]}
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    log(f"{case.id} ERROR {exc!r}"[:200])
                    continue
            s = scoring.score_case(case, final["outcome"], final["sql"], db, gold)
            row = {"id": case.id, "split": case.split, "group": case.group, "expected": case.expected, "drifts": list(case.drifts),
                   "config": config, "outcome": final["outcome"], "sql": final["sql"], "proposed_sql": final.get("proposed_sql"),
                   "reason": final["reason"], "attempts": final["attempts"], "model_calls": final["model_calls"],
                   "input_tokens": final["input_tokens"], "output_tokens": final["output_tokens"], "latency_s": final["latency_s"],
                   "models": final["models"], "rejected": [r["problems"] for r in final["rejected"]],
                   "failing_checks": sorted(final["evidence"].failing), "schema_signals": final["evidence"].signals, **s}
            f.write(json.dumps(row, default=str) + "\n")
            f.flush()
            log(f"{case.id} {case.group:<10} {final['outcome']:<10} -> {s['label']:<18} attempts={final['attempts']}")
    return read_rows(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"])
    ap.add_argument("--config", choices=agent_mod.CONFIGS)
    ap.add_argument("--model", choices=MODELS, default="gemini")
    ap.add_argument("--cache", default=".mend_cache")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None, help="run only the first N unanswered cases (smoke test)")
    ap.add_argument("--final", action="store_true", help="required for the test split")
    ap.add_argument("--report", nargs="+", metavar="FILE", help="print the summary of finished runs and exit")
    args = ap.parse_args()

    if args.report:
        for path in args.report:
            print(scoring.format_report(scoring.summarise(read_rows(Path(path))), title=f"== {path}") + "\n")
        return 0
    if not (args.split and args.config):
        ap.error("--split and --config are required (or use --report)")
    if args.split == "test":
        if not args.final:
            sys.exit("The test split is frozen. Use --final, once per configuration, after tagging.")
        check_freeze()
    out = Path(args.out) if args.out else ROOT / "results" / f"{args.split}_{args.config}_{args.model}.jsonl"
    rows = run_split(args.split, args.config, args.model, Path(args.cache).resolve(), out, args.limit)
    print("\n" + scoring.format_report(scoring.summarise(rows), title=f"== {out.name}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
