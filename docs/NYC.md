# Real schema drift: the NYC yellow taxi files, 2009 to 2026

Mend is evaluated on a synthetic feed whose drifts I wrote. This page is about the other question: what does real drift look like? The New York City
Taxi and Limousine Commission (TLC) publishes one Parquet file per month of yellow taxi trips, back to 2009, and its schema has changed over time.

**What I did.** I read the footer of every monthly file (213 files, 2009-01 to 2026-09, 1,860,540,041 trips in all) to get its column names and types,
and compared each month with the one before ([`explore/nyc_schema_timeline.py`](../explore/nyc_schema_timeline.py), output in
[`results/nyc/yellow_schema_timeline.json`](../results/nyc/yellow_schema_timeline.json)). Then I saved the first 1,000 rows of the 12 months around six of the
changes ([`explore/nyc_sample.py`](../explore/nyc_sample.py), [`results/nyc/samples/`](../results/nyc/samples/)). The counts below come from
[`explore/nyc_summarize.py`](../explore/nyc_summarize.py); `python explore/nyc_summarize.py --md` prints the full list of events.

**What I did not do.** I did not run Mend's agent or gate on this data (see "Why the agent was not run" below).

## What changed

30 of the 212 month-to-month steps changed the schema:

| Kind | Events | What they were |
|---|---|---|
| type-only | 23 | 22 only flip `congestion_surcharge`, `airport_fee` or `improvement_surcharge` between INTEGER and DOUBLE (2011 to 2020); one (2018-07) widens `passenger_count` and `RatecodeID` from BIGINT to DOUBLE |
| restructure | 2 | 2010-01: 12 columns replaced by snake_case names (`Fare_Amt` to `fare_amount`, `Trip_Pickup_DateTime` to `pickup_datetime`, ...) and 4 more changed only in case. 2011-01: the `tpep_` names, a real `TIMESTAMP` for the pickup time, location IDs instead of coordinates, new fee columns |
| additive | 2 | 2025-01: `cbd_congestion_fee`. 2026-06: `request_source` |
| artifact | 2 | the pandas index column `__index_level_0__` appears in 2010-02 and is gone by 2010-04 |
| case-only | 1 | 2023-02: `airport_fee` became `Airport_fee` (with some integer-width changes) |

## What the 12,000 sampled rows show

The sample is the first 1,000 rows of each file, which cover only the last day of the previous month and the first day of the month. It tells us about column
types and presence, not about statistics.

- **The type flips are in empty columns.** `congestion_surcharge` and `airport_fee` have no values at all (0 of 1,000) in the 2011-01, 2018-06 and 2018-07
  samples. The `congestion_surcharge` flips end in 2019-01 (`airport_fee`'s in 2020-11, `improvement_surcharge`'s in 2014-09), and the 2023 samples hold real
  values in both. This fits a file writer typing an empty column differently from month to month. Only the samples were checked, not whole files.
- **The pickup time changed from text to `TIMESTAMP` in 2011-01** (together with a new column name, so a build would have failed loudly). All 1,000 text values in each
  of the three text samples (2009-12, 2010-01, 2010-12) parse as `YYYY-MM-DD HH:MM:SS`, so a plain cast works. There is no day-first date in these samples.
- **`total_amount` changed meaning in 2025.** In the 2026-05 sample, `total_amount` equals the sum of its listed parts to the cent for 80.4% of rows when
  `cbd_congestion_fee` is included and for 12.0% when it is not (2026-06: 82.2% against 25.7%). A model that rebuilds the total from the component columns
  would silently undercount; one that reads `total_amount` stays right. The fee column is all zero in the 2025-01 sample (31 Dec 2024 and 1 Jan 2025), which matches the
  TLC data dictionary: it applies from 5 January 2025.
- **`request_source` (2026-06)** is empty in the sample, and I do not know what it will hold.

## What this means for Mend

- For a model that reads the money, time and passenger columns by name and casts them, my reading is that 28 of the 30 events needed no change to the model. The
  synthetic benchmark has 14 harmless cases in 60. In these files the harmless change is the common one, so an agent that edits the model on every change would be
  wrong most of the time.
- The changes that did matter are rare and old. Since 2011 no event renamed a column that a revenue model reads. The only name change was case-only, which
  DuckDB ignores.
- The most dangerous real change here is not a rename but the 2025 change in what `total_amount` contains. It fails no build and no test, which is the same
  family as the benchmark's silent corruptions: the build stays green and the numbers are wrong.

## Why the agent was not run

Mend's agent, gate and sandbox are written around the orders model: `stg_orders`, `raw.orders`, a fixed ten-column contract and a prompt about orders, all in
`src/`, which the tag `frozen-v1` freezes. Running them on taxi data would need a new contract, prompt and staging model, and either a change to frozen code or a
trimmed copy of the agent. The result would be six decisions with an answer key that I wrote after reading the schemas, so it would be a demonstration, not a
measurement. I chose to publish the timeline and these checks, which are real data, and not to build that harness.

## Limits

- One dataset, one kind of source (a government file archive that re-publishes old months), and 1,000 rows per sample.
- The kinds are assigned by fixed rules from column lists (the rules are at the top of `nyc_summarize.py`); which pairs are renames, and which events a revenue
  model can ignore, is my judgement.
- The TLC may have re-exported old months, so these files may not show how the original feed looked.
