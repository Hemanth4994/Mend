# Mend

Self-healing data pipelines. A daily dlt + dbt pipeline loads an orders feed into DuckDB. When the source changes shape without
warning, an agent proposes a fix to the dbt staging model, tests it in a sandbox, and applies it or escalates to a human.

Status: **work in progress.** The feed, the 60-case benchmark, the answer key, the sandbox scorer, the baseline run, the evidence builder,
the production gate and the repair agent are done. The evaluation runs and the Kestra flows are not built yet. See [PLAN.md](PLAN.md) for the design and what is done.

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
- `scripts/standins.py`: an oracle, a lazy and an always-escalate stand-in, to measure the gate without an API key (`results/dev_evidence_*.jsonl`).
- `src/mend/agent.py`: the LangGraph repair agent (gather, propose, gate, one repair, then patch, no_change or escalate). The model is an injected
  function, so the graph is tested with a scripted stand-in and no API key (`tests/test_agent.py`).
- `scripts/baseline_report.py`: runs every case through the unrepaired pipeline. Result: 34 break loudly, 11 pass every test with wrong
  numbers but show a schema change, 1 is invisible to every check, and the 14 harmless ones are all absorbed by dlt.

## Run it

```bash
pip install -c constraints.txt -e ".[dev]"
python -m pytest                       # 217 tests, about two minutes
python scripts/baseline_report.py      # all 60 cases through dlt and dbt, about two minutes
```

The versions in `constraints.txt` matter: dlt's handling of schema changes differs between versions.
