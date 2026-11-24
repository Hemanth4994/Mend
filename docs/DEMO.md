# Mend: a 2-minute demo, and the questions to expect

## Before you press record

- Docker Desktop is running and Kestra is open (http://localhost:8080, or the port in `MEND_KESTRA_PORT`). Sign in beforehand.
- Run the **bootstrap** flow once so the pipeline starts healthy with 5 days. Do this off camera; it takes about a minute.
- Close other tabs and hide the bookmarks bar. Never show `orchestration/.env` or any key.
- The model is Gemini, so its wording changes from run to run. If it proposes something other than `coalesce(amount, total_amount)`, read it aloud and
  say what you check: the gate line must show all five checks passing. If it escalates instead, that is also a valid demo (see step 6).

## The script (about 2 minutes)

| Time | You do | You say |
|---|---|---|
| 0:00 | Show the README diagram | "Pipelines break when a source changes shape. Mend notices, has an agent propose a fix to the dbt model, tests it in a sandbox, and a person approves it." |
| 0:15 | Flows, **source_change**, pick `rename_amount`, Execute | "I'm playing the source team: they rename `amount` to `total_amount` without telling anyone." |
| 0:30 | Flows, **daily**, Execute. Open its logs | "The daily run loads the new day. dbt's not-null check fails, so the marts keep the last good numbers instead of publishing the bad day. It starts the repair flow." |
| 0:50 | Executions, open the **repair** run (state: Paused). Open the `propose` log | "The agent read the failing check and the sample rows and proposed this change. The gate line says the SQL parses, it runs, history is unchanged, the column fills, and dbt passes. Nothing has been changed yet." |
| 1:10 | **Resume**, choose `approve`, type a comment, confirm | "Every proposal waits for a person. In my evaluation no kind of change earned autonomy." |
| 1:25 | Show the `apply` log: "Applied. The pipeline is healthy; the marts have 6 days" | "Before applying it re-checks the patch on today's data, rebuilds, and would put the old model back if the build were not healthy." |
| 1:40 | Back to the README results table | "On 30 frozen test cases the agent made the right decision 26 times with evidence, against 20 with the schema only. The silent failures were all timestamps, so those always go to a person." |
| 1:55 | Stop | |

Optional extra (30 seconds): bootstrap again, run `source_change` with `drop_city`, run `daily`. The `repair` run ends **red**: the agent
escalates, because no patch can bring a dropped column back. A red run is on purpose, so an escalation cannot go unnoticed.

Say-only line about AWS (do not show the bucket on screen: its name contains your AWS account number): "Every proposal and every approval is also copied to a
versioned S3 bucket, by an IAM user that can only add files, so the record cannot be rewritten by the program that wrote it."

Recording on Windows: **Win + Alt + R** starts and stops the Xbox Game Bar recorder for the focused window (files land in `Videos\Captures`).
OBS Studio works too. Keep it under 2 minutes and upload it unlisted to YouTube, or attach it to a LinkedIn post, then link it from the README.

## Questions to expect, with honest answers

**Why does everything wait for a human?** I measured it. To earn autonomy, a category's 95% upper bound on error has to stay under 5%. With 5 to 7
cases per category, even zero errors leaves the bound far above that; about 52 clean decisions are needed. Separately, a patch on a timestamp column
should wait whatever the statistics say.

**What is a silent corruption?** A repair the gate accepts that makes the numbers wrong. There were 5 across the six runs, all about time: IST local time
with no offset, and day-first dates. No check that runs in production can see them, because the build stays green.

**How do I know the evaluation was not tuned to the test set?** The prompt was fixed on the dev split only. The code was frozen and tagged `frozen-v1`
before the test split was touched, and the runner refuses to run the test split unless that tag exists and the frozen files match it. One run per
configuration. One case (`v04`) was re-run after a Windows-only infrastructure fault, and that is declared in PLAN.md.

**What does the gate check, and does it see the answer key?** It never sees the answer key. It checks that the SQL parses and keeps the column
contract, that it runs, that the history is unchanged, that the fixed column actually fills, and that dbt's tests pass.

**What are the limits?** Synthetic data written by the same person who wrote the answer key, one model (Gemini 3.5 Flash-Lite), 30 test cases, one run per
configuration, and one change at a time on a fresh history. Several changes in a row on one live pipeline were not part of the evaluation.

**What happens if applying a patch goes wrong?** The apply step re-checks the patch on today's data and refuses if it no longer passes. If it
applies and the rebuild is not healthy, it puts the old model back and fails the run.

**What did the Kestra run actually show?** Two cases on my laptop: `rename_amount` (patched, approved, the marts recovered, next day healthy) and
`drop_city` (escalated, red run, nothing changed). I had to fix one design flaw along the way: the approval form had an on/off switch that started off, so a
bare Resume silently rejected the proposal. It is now a required approve/reject choice with no default.

**What does the AWS part do?** Terraform builds a private, encrypted, versioned S3 bucket, an IAM user that can only put and get files under one folder, and a budget
alarm. After a run, Mend copies the marts and every proposal and decision there. I applied it to a free-plan AWS account and a Kestra run uploaded the files; a
second run uploaded only what had changed. It is an audit archive of a few kilobytes a day, not a data lake.

**Have you seen real schema drift?** I read the schema of 213 monthly NYC taxi files (2009 to 2026): 30 months changed it, 23 of them only INTEGER/DOUBLE flips in
fee columns that were empty at the time, and two were rename waves in 2010 and 2011. In 2025 `total_amount` started to include a new fee, which fails no build. I did not run
the agent on it: the agent and the gate are written around the orders model, so that needs a generalised contract and prompt. Details: [docs/NYC.md](NYC.md).

**What would you do next?** Run the agent itself on real schema changes (the NYC files above, with a generalised contract and prompt), measure several changes in a row, show a
column-by-column summary of the patch instead of a raw diff (the model tends to rewrite the whole file on one line), and leave the schedule on for a week.
