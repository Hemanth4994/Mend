# Mend

Self-healing data pipelines. A daily dlt + dbt pipeline loads an orders feed into DuckDB. When the source changes shape without
warning, an agent proposes a fix to the dbt staging model, tests it in a sandbox, and applies it or escalates to a human.

Status: **work in progress.** The feed, the 60-case benchmark, the answer key, the sandbox scorer and the baseline run are done.
The agent, the evaluation and the Kestra flows are not built yet. See [PLAN.md](PLAN.md) for the design and what is done.

## What exists today

- `src/mend/feed.py`: the orders feed, deterministic.
- `src/mend/drifts.py`: 60 source changes (renames, nesting, units, formats, recoded values, dropped columns, harmless additions).
- `eval/cases.json`: 60 cases, 30 dev and 30 test.
- `src/mend/gold.py`: a hand-written correct repair for each of the 36 repairable cases (the agent never sees it).
- `src/mend/sandbox.py`: builds a case with real dlt, runs a staging model, compares with the answer key.
- `dbt/mend_dbt/`: staging and mart models with data tests.
- `scripts/baseline_report.py`: runs every case through the unrepaired pipeline. Result: 34 break loudly, 11 pass every test with wrong
  numbers but show a schema change, 1 is invisible to every check, and the 14 harmless ones are all absorbed by dlt.

## Run it

```bash
pip install -c constraints.txt -e ".[dev]"
python -m pytest                       # 143 tests, about a minute
python scripts/baseline_report.py      # all 60 cases through dlt and dbt, about two minutes
```

The versions in `constraints.txt` matter: dlt's handling of schema changes differs between versions.
