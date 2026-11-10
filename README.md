# Mend

Self-healing data pipelines. A daily dlt + dbt pipeline loads an orders feed into DuckDB. When the source changes shape without
warning, an agent proposes a fix to the dbt staging model, tests it in a sandbox, and applies it or escalates to a human.

Status: **the evaluation is finished; the Kestra flows, Docker Compose and CI are not built yet.** See [PLAN.md](PLAN.md) for the design and what is done.

## Result

60 source changes (30 dev, 30 test) run through real dlt and dbt, scored against an answer key, one run per configuration on code frozen before the
test split was touched (tag `frozen-v1`). Gemini 3.5 Flash-Lite, 2 attempts per case, a production gate with no access to the answer key.

| The agent is shown | Test: correct (95% interval) | Test: silent corruption |
|---|---|---|
| table schema only | 20 of 30 (49%-81%) | 1 |
| + failing checks, sample rows, schema signals | 26 of 30 (70%-95%) | 2 |
| + the source's release note | 27 of 30 (74%-97%) | 1 |

- Showing the evidence is what makes repair possible (dev and test together: 65% with schema only, 93% with evidence).
- All the silent corruptions are about time: IST local time with no offset, day-first dates. No check that runs in production can see them.
- No category earns autonomy: with 5-7 cases per category, even zero errors leaves an upper bound far above the 5% limit (52 clean decisions are needed).
  So everything waits for approval, and a patch on a timestamp column should wait whatever the statistics say.
- Limits: synthetic data written by the same author as the answer key, one run per configuration, one small model, 30 cases. Details and the failure
  analysis are in [PLAN.md](PLAN.md); one case (`v04`) had to be re-run after a Windows-only infrastructure fault, declared there.

## What exists today

- `src/mend/feed.py`: the orders feed, deterministic.
- `src/mend/drifts.py`: 60 source changes (renames, nesting, units, formats, recoded values, dropped columns, harmless additions).
- `eval/cases.json`: 60 cases, 30 dev and 30 test.
- `src/mend/gold.py`: a hand-written correct repair for each of the 36 repairable cases (the agent never sees it).
- `src/mend/sandbox.py`: builds a case with real dlt, runs a staging model, compares with the answer key.
- `dbt/mend_dbt/`: staging and mart models with data tests.
- `src/mend/evidence.py`, `src/mend/validate.py`: what the agent is shown, and the production gate that judges its patch (no answer key).
- `src/mend/scoring.py`, `scripts/run_eval.py`: scores every decision against the answer key (silent corruption is the headline number) and runs a split,
  resuming where it stopped. The test split is refused unless the tag `frozen-v1` exists and the frozen folders match it.
- `results/{dev,test}_{schema,evidence,notes}_gemini.jsonl`: one run per configuration and split with Gemini, one row per case. `results/infra/` holds two rows set aside for an infrastructure fault; `seed/` the database that replaced the broken one.
- `scripts/standins.py`: an oracle, a lazy and an always-escalate stand-in, to measure the gate without an API key (`results/dev_evidence_*.jsonl`).
- `src/mend/agent.py`: the LangGraph repair agent (gather, propose, gate, one repair, then patch, no_change or escalate). The model is an injected
  function, so the graph is tested with a scripted stand-in and no API key (`tests/test_agent.py`).
- `scripts/baseline_report.py`: runs every case through the unrepaired pipeline. Result: 34 break loudly, 11 pass every test with wrong
  numbers but show a schema change, 1 is invisible to every check, and the 14 harmless ones are all absorbed by dlt.

## Run it

```bash
pip install -c constraints.txt -e ".[dev]"
python -m pytest                       # 218 tests, about two minutes
python scripts/baseline_report.py      # all 60 cases through dlt and dbt, about two minutes
```

The versions in `constraints.txt` matter: dlt's handling of schema changes differs between versions.
