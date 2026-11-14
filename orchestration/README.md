# Mend, orchestrated

Stage 7: the pipeline runs on a schedule under [Kestra](https://kestra.io), and a person stays in the loop.

```
 daily flow ──► ingest ──► build (dbt) ──► healthy? ──yes──► done
                                              │
                                              no (a check fails, or the source added / emptied a column)
                                              ▼
                                        repair flow ──► propose (agent + production gate; changes nothing)
                                                           │
                              patch / no change ───────────┤──────────── escalate
                                    ▼                                          ▼
                         PAUSE: a person reads the diff               execution fails on purpose (red)
                                    │
                         approve ───┴─── reject
                            ▼               ▼
                 apply: re-check on today's data,      recorded; pipeline
                 change the model, rebuild;            stays in drift, marts
                 undo if not healthy                   keep last good numbers
```

The agent and the production gate are the frozen code in `src/mend/`, used unchanged. Everything here decides *when* to call them.
Every proposal waits for a person, because the evaluation found no kind of change that earns autonomy (see the main README).

## What was and was not tested

Tested here (Linux, no Docker): `mend_ops.py` end to end with real dlt, dbt and DuckDB and a scripted model, 40+ tests in
`orchestration/tests/`: detection, proposal, approval, stale proposals, undo, reject, escalation, locking, the output lines.
The Python layer was also run from a clean install laid out like the image (pinned dependencies, no git binary).

**Not tested: Docker and Kestra themselves.** The sandbox this was built in cannot pull images (Docker Hub is blocked), so
the image has not been built and no flow has been run. What stands in for that run:

- Every task type, property and input type in `flows/` was checked against the source of Kestra **v2.0.10**, the version
  pinned in `kestra/Dockerfile` (`core`, `plugin-scripts`, and the Docker runner). `tests/test_flows.py` keeps the flows inside
  that list, and checks that every output a flow reads (`outputs.<task>.vars.<key>`) is a key the command really prints.
- The compose file follows Kestra's own `docker-compose.yml` for that version, with the changes below.

So the first `docker compose up` on your machine is the real test. If something fails, it is most likely one of the points under
"If the first run fails".

## Run it (Windows, Docker Desktop with the WSL 2 backend)

From the repository root, in PowerShell:

```powershell
python orchestration/make_env.py                  # asks for a Kestra login and, optionally, your Gemini key (not echoed)
docker compose -f orchestration/docker-compose.yml --env-file orchestration/.env up -d --build
```

The first build takes a few minutes: it installs the Python dependencies for the Mend image, and builds a slim Kestra image (downloading the shell plugin from Maven Central, so it needs internet). Then open http://localhost:8080 and sign in with the
login you gave `make_env.py`. The flows are in the namespace `mend`; they are loaded from `orchestration/flows/` when Kestra starts
(restart Kestra after editing one: `docker compose -f orchestration/docker-compose.yml --env-file orchestration/.env restart kestra`).

`.env` holds your login and your Gemini key (as a base64 Kestra secret). It is ignored by git; never paste it anywhere.
Without a Gemini key everything works except the model call in the `repair` flow, which stops with a message saying so.

## A demo, in this order

1. **bootstrap**: wipes the pipeline, loads 5 healthy days, builds. The marts have 5 days.
2. **source_change**: pick `rename_amount` and run it. You are now the team that owns the source.
3. **daily**: loads day 6, which arrives with `total_amount` instead of `amount`. The build reports drift (the `not_null` check on
   `amount` fails, a new column appeared, one went empty). The marts still say day 5: dbt did not publish the bad day.
   The flow starts `repair` and finishes.
4. **repair** (the new execution, state *Paused*): open the log of `propose`. It has the diff to `stg_orders.sql` and the result of
   each gate check. Click *Resume*, tick `approve`, add a comment, confirm. `apply` re-checks the patch on today's data, changes
   the live model, rebuilds. The marts now have 6 days.
5. Try the others from `source_change` (the flow's description says what each one does to the build and what the right answer is).
   What the model answers is up to the model; the right answers are `add_coupon` = "no change needed" (approve it and the alarm
   stops) and `drop_city` = escalate (the execution ends red: nothing can bring the column back). `ts_ist_naive` is different:
   the build stays *healthy while the numbers are wrong*. That is the honest limit, the build cannot see it, which is why the main
   README says timestamp patches should always wait for a person.

Re-run **bootstrap** any time to start over.

## Without Docker (the same commands, from a terminal)

```powershell
$env:MEND_HOME = ".mend_home"                         # optional; this is the default
python orchestration/mend_ops.py bootstrap
python orchestration/mend_ops.py source-change rename_amount
python orchestration/mend_ops.py ingest
python orchestration/mend_ops.py build                # drift
python orchestration/mend_ops.py repair               # needs GOOGLE_API_KEY in your terminal; or --proposal-file answer.json
python orchestration/mend_ops.py apply r0001 --by you --comment "looks right"
python orchestration/mend_ops.py status
```

`source-change --list` shows the changes you can simulate. On Windows, outside Docker, `ts_epoch_ms` hits the dlt `OSError` described in
PLAN.md; inside the container (Linux) it does not.

## Design notes

- **One folder holds the state** (`MEND_HOME`, the Docker volume `mend_home`): the warehouse, dlt's state, a live copy of the dbt project,
  `state.json`, one folder per repair (`proposal.json`, `decision.json`, the gate's work), the previous model versions, an event log.
- **dbt publishes nothing it cannot test.** A failed check skips the marts, so they keep the last good numbers while the staging table
  holds the bad days. The agent is shown the evidence; the marts are never fed the bad day.
- **Approval is for a patch the gate passed, not for a file.** `apply` refuses (and marks the proposal stale) if the live model changed after
  the proposal, or if the patch no longer passes the gate on today's data. After applying, it rebuilds; if the pipeline is not healthy it puts
  the old model back and fails.
- **Changes a person approved are not reported again.** After `amount` was retired, the frozen detector keeps calling it "went empty" for as
  long as old loads outnumber new ones, so a *second*, different change would make the agent escalate for the wrong reason (this was observed,
  and is a test). The ops layer hides acknowledged columns from the evidence while the agent and gate run. The agent was evaluated on one change
  per fresh history; several changes in a row on one live pipeline are outside what was measured.
- **One command at a time.** DuckDB has one writer, and a repair must not copy a warehouse mid-load, so commands take a lock file in the home
  (stale after 2 hours, in case a run crashed). The `daily` flow also limits itself to one execution at a time.
- **A waiting proposal is not asked for again.** The daily flow starts `repair` only when nothing is open. A rejected or escalated one stays open
  (so the model is not called every morning) until someone runs `repair` with `force`.
- **The schedule is defined and switched off** (`daily.yml`, `disabled: true`) so a demo laptop does not change state overnight.
- **Secrets**: the Gemini key reaches only the `propose` step, as `secret('GOOGLE_API_KEY')`. The Kestra UI is bound to 127.0.0.1 and needs the
  login from `make_env.py`; port 8081 (management, unauthenticated) is not published. Kestra needs the Docker socket and root to start the step
  containers, which is fine on a laptop and is the reason for those two settings.

## Where this differs from Kestra's own compose file

- **A slim Kestra image, built locally** (`kestra/Dockerfile`): `kestra/kestra:v2.0.10-slim` (pinned, not `latest`) plus the one plugin the flows need,
  `plugin-script-shell`, installed at build time. The stock image bundles every plugin and is a ~4.7 GB download; on a slow connection that pull
  stalled. Everything else the flows use is part of Kestra itself (checked in `tests/test_flows.py`). Open-source Kestra installs the latest plugin
  version compatible with the image, so the plugin version is not pinned.
  Sources: Kestra's guides [Build a Custom Kestra Image with Only the Plugins You Need](https://kestra.io/docs/how-to-guides/slim-image-plugins) and
  [Install Only Selected Plugins](https://kestra.io/docs/how-to-guides/selected-plugin-installation).
- The UI on `127.0.0.1` only; basic auth from `.env`; `--flow-path /mend_flows`; `plugins.configurations` enables `volume-enabled` for the Docker
  runner (without it a flow cannot mount `mend_home`); the `mend-image` service that builds `mend-ops:local` and exits, which Kestra waits for.
- Kestra 2.0 removed `pluginDefaults`, so each task repeats its small Docker block.

## If the first run fails

| Symptom | Likely cause and where to look |
|---|---|
| `docker compose` stops at "run python orchestration/make_env.py first" | `.env` is missing; run it, and keep `--env-file orchestration/.env` in the command |
| No flows under namespace `mend` | `docker compose ... logs kestra` and look for `Unable to create flow`: it names the file and the property Kestra rejected |
| A step fails with "volumes are disabled" | the `plugins.configurations` block did not reach Kestra; check the `KESTRA_CONFIGURATION` indentation in `docker-compose.yml` |
| A step fails with "image mend-ops:local not found" | the build did not finish: `docker compose ... build mend-image`, then `docker image ls mend-ops` |
| `ports are not available ... 127.0.0.1:8080 ... Only one usage of each socket address` | another program on your machine already uses port 8080. Find it with `netstat -ano \| findstr :8080` (the last column is the process id; `tasklist /FI "PID eq <id>"` names it), or add `MEND_KESTRA_PORT=8088` to `orchestration/.env`, run the `up` command again, and open http://localhost:8088 |
| A flow is missing, and the Kestra log says the type `io.kestra.plugin.scripts.shell.Commands` is unknown | the shell plugin is not in the image: `docker compose -f orchestration/docker-compose.yml --env-file orchestration/.env build --no-cache kestra`, then `docker run --rm --entrypoint /app/kestra mend-kestra:local plugins list --plugins /app/plugins` should list it |
| The Kestra image build fails at `plugins install` | it downloads from Maven Central: check your internet connection or proxy, then build again |
| `propose` fails with GOOGLE_API_KEY | run `make_env.py` again with your key, then `restart kestra` |
| A step fails with a Docker connection error | Kestra starts the step containers through `/var/run/docker.sock` (as in Kestra's own compose file); Docker Desktop must be running and be the engine `docker compose` talks to |
| Anything in `mend_ops` | `docker run --rm -v mend_home:/mend_home mend-ops:local mend-ops status` shows the pipeline from outside Kestra |

## Files

- `mend_ops.py`: the commands (`init`, `bootstrap`, `source-change`, `ingest`, `build`, `repair`, `apply`, `reject`, `status`).
- `flows/`: `bootstrap`, `daily`, `repair`, `source_change`.
- `Dockerfile`, `docker-compose.yml`, `make_env.py`, `.env.example`.
- `tests/`: the ops tests and the flow checks (`pytest` from the repository root runs them with the rest).
