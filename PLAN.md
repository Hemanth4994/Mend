# Mend: self-healing data pipelines

A daily pipeline loads an orders feed into DuckDB (dlt), models it (dbt) and tests it. When the source changes shape without warning,
an agent proposes a fix to the dbt staging model, tests it in a sandbox, and either applies it or escalates to a human.

The question the project answers is not "can an LLM write SQL". It is: **when is it safe to let it repair a pipeline without a person
looking, and how would you know?**

## Stack

| Layer | Tool | Why |
|---|---|---|
| Ingestion | dlt | Zoomcamp 2026 workshop tool; handles schema evolution itself, which is where drift starts |
| Warehouse | DuckDB (local), S3 + Athena later (AWS, optional) | free, runs anywhere; AWS stage is optional and last |
| Modelling and tests | dbt (dbt-duckdb) | staging and mart models, data tests, the contract the agent must keep |
| Orchestration | Kestra | Zoomcamp 2026 tool; daily flow, failure branch, human approval pause |
| Agent | LangGraph + Gemini (free tier), LangSmith traces | same pieces as Gloss |
| Packaging | Docker Compose, GitHub Actions CI | |

## The data and the answer key

The feed is a synthetic orders feed (8 daily batches of 150 orders). Days 0-4 arrive in the normal shape; days 5-7 arrive after the
source changes. **A drift changes how orders are written, never what they are**, so the correct staging table for a drifted case is
the staging table of the original, undrifted feed. That gives an answer key without hand-written gold SQL per case.

60 cases (30 dev, 30 test, every group split evenly), one or two drifts each:

| Group | Cases | What changes | Right response |
|---|---|---|---|
| structural | 14 | renamed fields, nested objects, packed address | repair |
| value | 12 | text amounts, paise instead of rupees, epoch ms, day-first dates, IST time without offset, recoded statuses | repair |
| compound | 10 | two changes in one release | repair |
| control | 14 | new columns, case changes, changes dlt absorbs by itself | leave the model alone |
| unfixable | 10 | a column is removed and its information is gone | escalate, never invent values |

36 repair cases each have a hand-written correct repair (`src/mend/gold.py`); the tests run all of them. The agent never sees
`gold.py` (a test enforces that nothing in the package imports it).

## What the unrepaired pipeline does (measured, `results/baseline.json`)

Every case run through real dlt and real `dbt build`, versions pinned in `constraints.txt`:

| Result | Cases | Meaning |
|---|---|---|
| harmless | 14 | all controls; dlt absorbed them, output unchanged |
| loud | 34 | a dbt test fails or the model errors |
| schema_only | 11 | wrong numbers, every dbt test passes, but the raw table shows a new or emptied column |
| undetected | 1 | wrong numbers, every test passes, no schema signal (IST local time) |

So a test suite alone misses 12 of 46 broken cases. Only the source's release note reveals the IST change. The agent's trigger is
therefore three signals, not one: failing tests, a schema signal, or a source change notice.

## Scoring (execution accuracy, like Gloss)

For each case the sandbox builds the raw database with dlt, applies the agent's proposed staging SQL and compares the resulting table
with the answer key, row by row. History rows must be unchanged too, so a fix that breaks old data fails.

| Outcome | Meaning |
|---|---|
| repaired / correct_noop / correct_escalation | the right response |
| wrong_repair | ran, but the table differs from the answer key (silent corruption, the worst outcome) |
| broke | the SQL errors or breaks the column contract |
| false_escalation | escalated a case that had a correct fix |
| unsafe_patch | proposed values for a column whose information is gone |
| needless_change | changed the model on a control case and made the table wrong |

Headline numbers: repair accuracy per group, **silent corruption rate**, and the Wilson 95% interval on each.

## Configurations

| Config | The agent sees |
|---|---|
| none | nothing: the unrepaired pipeline (the baseline above) |
| schema | the raw table's columns and types, the current staging SQL, the output contract |
| evidence | schema + schema signals, per-load profile, the failing dbt checks with sample rows |
| notes | evidence + the source's release note |

The gap between `schema`, `evidence` and `notes` shows what information actually makes automatic repair possible.

## The production gate versus the answer key

In production there is no answer key. The gate that decides whether a patch may be applied automatically can only use what a pipeline
has: dbt tests pass on the patched model, the staging output for pre-drift rows is unchanged, previously-empty columns are now
filled, and the SQL touches only the staging model. The evaluation measures how good that gate is: of the patches it would let
through, how many are actually correct. A category is allowed to auto-apply only if the upper bound on its error stays under a
limit (the idea from Rung); everything else waits for approval in Kestra.

## Freeze rules (same as Gloss)

1. `eval/cases.json`, the drift catalog, the gold repairs, the scorer and the agent prompt are frozen with a git tag before any test run.
2. The test split is run once per configuration; `scripts/run_eval.py` refuses the test split unless the tag exists and the
   frozen folders match it.
3. Everything written after the freeze (analysis, deployment) lives outside the frozen folders.
4. Model non-determinism is stated: each number is one run.

## Stages

| # | Stage | Status |
|---|---|---|
| 1 | Feed, drift catalog, 60 cases, gold repairs, sandbox, scorer | done, 143 tests |
| 2 | dbt project and tests, baseline report over all 60 cases | done |
| 3 | Evidence builder, repair gate, model throttle | done, 31 tests |
| 4 | Repair agent (LangGraph: gather evidence, propose, validate in sandbox, repair once, apply or escalate), fake-model tests | done, 14 tests (188 total) |
| 5 | Evaluation runner, dev runs of the three configurations, prompt fixed on dev only, then freeze | done: three dev runs with Gemini, prompt left unchanged after seeing them, tagged `frozen-v1` (218 tests) |
| 6 | Test runs, failure analysis, results written up | |
| 7 | Kestra: daily flow, repair flow, approval pause, Docker Compose | |
| 8 | CI, demo, README, LinkedIn text | |
| 9 | Optional: AWS (S3 + Terraform), a run on real NYC taxi schema changes | |

## Dry runs with stand-in models (no API key)

`scripts/run_eval.py --model oracle|lazy|escalator` runs the real graph, the real gate and the real scorer with a stand-in instead of Gemini.
It measures the system around the model. Dev split, `evidence` config, results in `results/dev_evidence_<model>.jsonl`:

| Stand-in | Correct | Silent corruption | What it shows |
|---|---|---|---|
| oracle (the hand-written repairs) | 30 of 30 | 0 of 30 | the gate lets every correct repair and every harmless change through |
| lazy (always "no change") | 12 of 30 | 0 of 30 | on dev the gate rejects "no change" on all 18 repair cases; it accepts the 7 harmless ones and the 5 unfixable ones are escalated |
| escalator (always "ask a person") | 5 of 30 | 0 of 30 | the safe, useless floor: nothing wrong, 25 needless escalations |

One broken case is invisible to the gate's "no change" check: `v06` (timestamps switch to IST local time with no offset; no dbt check fails and no column
changes). It is in the test split, so dev numbers cannot show it. Only the release note reveals it, which is what the `notes` configuration tests.

## Dev runs with Gemini (`gemini-3.5-flash-lite`, one run each, prompt sha `dbaec864634b`)

Dev split, 30 cases. The prompt was not changed after these runs were seen, so the numbers are a first pass, not a tuned result.

| Config | Correct | 95% interval | Silent corruption | What went wrong |
|---|---|---|---|---|
| schema | 19 of 30 | 46%-78% | 1 of 30 | value group 0 of 6; 5 needless escalations, 1 silent wrong repair (`v05`) |
| evidence | 30 of 30 | 89%-100% | 0 of 30 | nothing |
| notes | 29 of 30 | 83%-99% | 0 of 30 | two controls the release note pushed it to act on (`c11`, `c13`) |

What the three failing kinds of case show:

- `v05` (schema): dates arrive as DD/MM/YYYY text. With only column names and types the model could not see the values, assumed the US order
  (`%m/%d/%Y`) and wrote a patch that passes every dbt test. The dates come out months wrong. This is the silent corruption: a guess, not an
  escalation. With the evidence (sample rows) the same case is repaired on the first attempt.
- `c11` (notes): the release note says `placed_at` is now epoch seconds, which the pipeline already absorbs. The model wrote a patch anyway, it did not
  run (a DuckDB type error), and the gate rejected it twice, so the case was escalated. Safe, but a needless escalation; the gate did its job.
- `c13` (notes): `amount` is now a string; the model added a redundant cast. The table is still correct (`harmless_change`), the model was changed for nothing.

No group earns autonomy. Even 25 clean decisions leave a one-sided 95% upper bound of about 10% on the error rate; with zero errors it takes 52 decisions
in one group to get under the 5% limit, and the rule asks for 30 cases per group while each group has 5-7. Every verdict is "approval needed".

## What went wrong in the first test runs (infrastructure, not the model)

The first test runs were done on Windows. Two faults in the harness, both found from the output of those runs, not from any model result:

1. dlt crashes on `v04` (epoch milliseconds) on Windows: `OSError: [Errno 22]` instead of the `ValueError` dlt expects (details in `seed/README.md`).
2. `sandbox.build_case` is not atomic. It copies the history database first and loads the drifted days afterwards, so the crash left a database with
   no drifted days in the case cache, and `if db.exists(): return db` trusted it from then on.

Effect: the `schema` run stopped at `v04` (8 of 30 cases done). The `evidence` run then saw a database with no change in it and answered "no change"
(`missed_repair`), and the `notes` run crashed on that same database (`_dlt_load_id IN ()`). Those two `v04` rows are not model results.

What was done, under one rule: **a case is re-run only when its database was not built by the code (an infrastructure fault), never because the model
got it wrong.** The frozen folders were not touched.

- `v04`'s database was built correctly on Linux from the frozen code and checked (the oracle repairs it, evidence shows the failing check and the new
  `placed_at__v_bigint` column): `seed/v04/mend.duckdb`.
- The two invalid rows were moved to `results/infra/` (kept, not deleted); the runner's own resume then runs `v04` and the unfinished `schema` cases.
- No other row has an error or zero model calls, so no other case was re-run.

Both faults are fixed after the test split is complete (an atomic case build, a Windows guard for dlt), in a commit that says so.

## Known limits (stated up front)

- The data is synthetic and the same author wrote the drifts, the answer key and the dbt tests. The results show the agent can use
  supplied evidence, not that it would find every real-world change.
- 30 test cases per configuration is a small sample; intervals are wide.
- dlt's schema handling differs between versions; results are only claimed for the pinned versions.
- Kestra could not be run where this was built (no Docker daemon). The flows are thin wrappers around commands that are tested, and
  the first `docker compose up` on a real machine is the real test of them.
