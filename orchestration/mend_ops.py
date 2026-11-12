"""Mend operations: the commands a scheduler runs against a live Mend pipeline.

Everything works on one folder, the home: the warehouse (raw.orders loaded by dlt, analytics.* built by dbt), the dlt state,
a live copy of the dbt project, a small state file, and one folder per repair. The commands are small and each does one job,
so an orchestrator (Kestra, see flows/) can schedule them, branch on their outputs and pause for a person between them.

    ingest        load the next daily batch of the source feed into raw.orders
    build         dbt build on the live warehouse, then judge: healthy, or drift (a check fails, or the source changed shape)
    repair        run the agent (evaluated in eval/) on the drift and write a proposal; nothing is changed
    apply         a person approved the proposal: re-check it on today's data, change the live model, rebuild; undo if it fails
    reject        a person declined the proposal
    source-change simulate the source team changing the feed (demo only)

The policy is the finding of the evaluation: no kind of change earned autonomy, so every proposal waits for a person.
The agent and the production gate are the frozen code in src/mend/, used unchanged; this file only decides when to call them.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from mend import drifts as drift_mod
from mend import feed, sandbox, signals

os.environ.setdefault("DBT_USE_COLORS", "false")     # plain text in the scheduler's log, not terminal colour codes
os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")

DEFAULT_HOME = ".mend_home"
BOOTSTRAP_DAYS = 5                      # healthy history before the source first changes (the evaluation used 5 as well)
STAGING_REL = Path("models") / "staging" / "stg_orders.sql"
REPO_DBT = signals.DBT_DIR              # the pristine project in the repository; each home gets its own copy
_ORIGINAL_SCHEMA_SIGNALS = signals.schema_signals
_DBT_ENTRY = "import sys; sys.argv[0] = 'dbt'; from dbt.cli.main import cli; cli()"


class OpsError(RuntimeError):
    """An expected operational failure (stale proposal, no history, a build that did not recover). The CLI prints it and exits 1."""


# ---- the home ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Home:
    root: Path

    @classmethod
    def at(cls, path: str | Path | None = None) -> "Home":
        return cls(Path(path or os.environ.get("MEND_HOME") or DEFAULT_HOME).resolve())

    warehouse = property(lambda s: s.root / "warehouse.duckdb")
    dlt_dir = property(lambda s: s.root / "dlt")
    project = property(lambda s: s.root / "project")
    dbt_out = property(lambda s: s.root / "dbt_out")
    state_file = property(lambda s: s.root / "state.json")
    repairs = property(lambda s: s.root / "repairs")
    model_history = property(lambda s: s.root / "model_history")
    events = property(lambda s: s.root / "events.jsonl")
    lock = property(lambda s: s.root / ".lock")
    model = property(lambda s: s.project / STAGING_REL)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _new_state() -> dict:
    return {"schema": 1, "next_day": 0, "loads": [], "source_drift": [], "release_note": "", "last_good_load": None,
            "acknowledged": {"added": [], "went_null": []}, "open_repair": None, "repair_counter": 0, "model_version": 0,
            "last_build": None}


def load_state(home: Home) -> dict:
    if not home.state_file.exists():
        raise OpsError(f"nothing at {home.root}: run `init` (or `bootstrap`) first")
    return json.loads(home.state_file.read_text(encoding="utf-8"))


def save_state(home: Home, state: dict) -> None:
    _write_atomic(home.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")


def log_event(home: Home, kind: str, **data) -> None:
    with home.events.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), "kind": kind, **data}, sort_keys=True) + "\n")


@contextlib.contextmanager
def locked(home: Home, wait: float = 900.0, stale: float = 7200.0):
    """One command at a time per home: DuckDB allows a single writer, and a repair must not read a warehouse mid-load.
    A lock file older than `stale` seconds is taken to be left by a crashed run."""
    home.root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            fd = os.open(home.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - home.lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale:
                home.lock.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise OpsError(f"another Mend command has held {home.lock} for {int(age)}s; try again when it finishes")
            time.sleep(0.5)
    try:
        yield
    finally:
        home.lock.unlink(missing_ok=True)


# ---- the live project ----------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def live_project(home: Home):
    """Point the frozen modules at this home's project while the agent and the gate run.

    sandbox.baseline_sql() and signals.dbt_build() read module-level paths to the repository's dbt project. For a live pipeline the
    model to improve is the live one (it may already carry an earlier repair), so those two paths are swapped for the duration.
    signals.schema_signals is wrapped so that changes a person already approved are not reported to the agent again; the
    frozen code reports 'went empty' for a retired column for as long as it dominates the history."""
    old_dir, old_model, old_sig = signals.DBT_DIR, sandbox.STAGING_MODEL, signals.schema_signals
    state = load_state(home)

    def filtered(db):
        return _without_acknowledged(_ORIGINAL_SCHEMA_SIGNALS(db), state["acknowledged"])

    signals.DBT_DIR, sandbox.STAGING_MODEL, signals.schema_signals = home.project, home.model, filtered
    try:
        yield
    finally:
        signals.DBT_DIR, sandbox.STAGING_MODEL, signals.schema_signals = old_dir, old_model, old_sig


def _without_acknowledged(sig: dict, ack: dict) -> dict:
    return {"added": [c for c in sig["added"] if c not in ack["added"]],
            "went_null": [c for c in sig["went_null"] if c not in ack["went_null"]]}


# ---- init / source / ingest ----------------------------------------------------------------------------------------------

def init(home: Home, reset: bool = False) -> dict:
    with locked(home):
        if reset and home.root.exists():
            for child in home.root.iterdir():
                if child != home.lock:
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        if home.state_file.exists():
            return {"initialised": False, "home": str(home.root)}
        for d in (home.dlt_dir, home.repairs, home.model_history, home.dbt_out):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO_DBT, home.project, ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"))
        shutil.copy(home.model, home.model_history / "stg_orders.v0.sql")
        save_state(home, _new_state())
        log_event(home, "init", model_sha=_sha(home.model.read_text(encoding="utf-8")))
        return {"initialised": True, "home": str(home.root)}


def source_change(home: Home, drift_ids: list[str], clear: bool = False) -> dict:
    """Demo helper: from the next ingest on, the feed arrives changed the way the named drifts describe."""
    unknown = [d for d in drift_ids if d not in drift_mod.DRIFTS]
    if unknown:
        raise OpsError(f"unknown drift(s) {', '.join(unknown)}; run `source-change --list`")
    if not clear and not drift_ids:
        raise OpsError("name at least one drift, or use --clear")
    with locked(home):
        state = load_state(home)
        state["source_drift"] = [] if clear else list(drift_ids)
        state["release_note"] = "" if clear else drift_mod.notes_for(drift_ids)
        save_state(home, state)
        log_event(home, "source_change", drifts=state["source_drift"])
    return {"source_drift": ",".join(state["source_drift"]) or "none", "release_note": state["release_note"]}


def ingest(home: Home, days: int = 1) -> dict:
    from mend.pipeline import load_batch, make_pipeline

    if days < 1:
        raise OpsError("days must be at least 1")
    with locked(home):
        state = load_state(home)
        pipe = make_pipeline(home.warehouse, home.dlt_dir)
        done = []
        for _ in range(days):
            day = state["next_day"]
            rows = feed.day_batch(day)
            if state["source_drift"]:
                rows = drift_mod.apply_drifts(state["source_drift"], rows)
            load_id = load_batch(pipe, rows)
            state["loads"].append({"day": day, "load_id": load_id, "rows": len(rows), "source_drift": list(state["source_drift"])})
            state["next_day"] = day + 1
            save_state(home, state)                 # after every load, so a crash never repeats a day that did land
            log_event(home, "ingest", day=day, load_id=load_id, rows=len(rows))
            done.append(day)
        return {"days_loaded": len(done), "latest_day": done[-1], "latest_load": state["loads"][-1]["load_id"],
                "rows_loaded": sum(l["rows"] for l in state["loads"][-days:])}


# ---- build ---------------------------------------------------------------------------------------------------------------

def _dbt_build_live(home: Home) -> tuple[dict[str, str], dict[str, int], dict[str, str]]:
    """dbt build on the live warehouse, in its own process so that it has let go of the DuckDB file when it returns."""
    out = home.dbt_out
    shutil.rmtree(out / "target", ignore_errors=True)
    env = {**os.environ, "MEND_DB": str(home.warehouse), "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    cmd = [sys.executable, "-c", _DBT_ENTRY, "--quiet", "build", "--project-dir", str(home.project), "--profiles-dir", str(home.project),
           "--target-path", str(out / "target"), "--log-path", str(out / "logs")]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    results = out / "target" / "run_results.json"
    if not results.exists():
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise OpsError(f"dbt could not run (exit {proc.returncode}):\n{tail}")
    data = json.loads(results.read_text(encoding="utf-8"))
    names = lambda r: r["unique_id"].split(".")[2]                      # noqa: E731
    statuses = {names(r): r["status"] for r in data["results"]}
    messages = {names(r): (r.get("message") or "")[:300] for r in data["results"] if r["status"] in ("fail", "error")}
    return statuses, signals.dbt_failures(out), messages


def _mart_summary(home: Home) -> dict:
    try:
        con = duckdb.connect(str(home.warehouse), read_only=True)
        try:
            rows, latest = con.execute("select count(*), max(order_date) from analytics.fct_daily_revenue").fetchone()
        finally:
            con.close()
        return {"mart_days": int(rows), "mart_latest_day": str(latest) if latest else ""}
    except duckdb.Error:
        return {"mart_days": 0, "mart_latest_day": ""}


def _build_locked(home: Home) -> dict:
    state = load_state(home)
    if not state["loads"]:
        raise OpsError("nothing has been ingested yet")
    statuses, failing, messages = _dbt_build_live(home)
    sig = _without_acknowledged(_ORIGINAL_SCHEMA_SIGNALS(home.warehouse), state["acknowledged"])
    reasons = ([f"dbt: {n} ({k} failing rows)" if k else f"dbt: {n} did not build" for n, k in sorted(failing.items())]
               + ([f"new columns: {', '.join(sig['added'])}"] if sig["added"] else [])
               + ([f"columns that went empty: {', '.join(sig['went_null'])}"] if sig["went_null"] else []))
    status = "drift" if reasons else "healthy"
    latest = state["loads"][-1]["load_id"]
    if status == "healthy":
        state["last_good_load"] = latest
    handled = bool(state["open_repair"])
    state["last_build"] = {"at": _now(), "status": status, "reasons": reasons}
    save_state(home, state)
    log_event(home, "build", status=status, reasons=reasons)
    return {"status": status, "reasons": reasons, "failing": failing, "dbt_messages": messages, "new_columns": sig["added"],
            "went_empty": sig["went_null"], "latest_load": latest, "last_good_load": state["last_good_load"] or "",
            "open_repair": (state["open_repair"] or {}).get("id", ""), "open_repair_status": (state["open_repair"] or {}).get("status", ""),
            "drift_handled": status == "drift" and handled, "dbt_nodes": len(statuses), **_mart_summary(home)}


def build(home: Home) -> dict:
    with locked(home):
        return _build_locked(home)


# ---- repair --------------------------------------------------------------------------------------------------------------

def _history_loads(state: dict) -> int:
    ids = [l["load_id"] for l in state["loads"]]
    good = state["last_good_load"]
    if good is None or good not in ids:
        raise OpsError("no healthy build has completed yet, so there is no known-good history to test a patch against")
    n = ids.index(good) + 1
    if n >= len(ids):
        raise OpsError("no load arrived after the last healthy build; there is nothing new to repair")
    return n


def _diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "stg_orders.sql (live)", "stg_orders.sql (proposed)", lineterm=""))


def replay_proposer(path: str | Path):
    """A proposer that answers from a JSON file {action, sql, reason}: for running the flows without a model key, and for tests."""
    from mend.agent import Proposal, ProposeResult

    data = json.loads(Path(path).read_text(encoding="utf-8"))

    def proposer(messages):
        return ProposeResult(proposal=Proposal(action=data["action"], sql=data.get("sql", ""), reason=data.get("reason", "")), model="replay")
    return proposer


def repair(home: Home, config: str = "evidence", notes: str | None = None, proposer=None, force: bool = False, model: str = "") -> dict:
    """Run the agent on the current drift and write a proposal. The live pipeline is not changed."""
    from mend import agent as agent_mod

    with locked(home):
        state = load_state(home)
        open_ = state["open_repair"]
        if open_ and open_["status"] == "awaiting_approval" and not force:
            return {"outcome": "pending", "repair_id": open_["id"], "needs_approval": False,
                    "reason": f"{open_['id']} is already waiting for a decision in another run; to replace it, ask again with force"}
        verdict = _build_locked(home)
        if verdict["status"] == "healthy":
            return {"outcome": "none", "repair_id": "", "needs_approval": False, "reason": "the pipeline is healthy; nothing to repair"}
        history_loads = _history_loads(load_state(home))
        if proposer is None:
            if not os.environ.get("GOOGLE_API_KEY"):
                raise OpsError("GOOGLE_API_KEY is not set, so the model cannot be called. Put it in orchestration/.env "
                               "(see orchestration/README.md) or pass --proposal-file to replay a saved answer.")
            from mend.throttle import Throttle
            proposer = agent_mod.gemini_proposer(model or None, throttle=Throttle())
        model_name = getattr(proposer, "model", model or "")
        state = load_state(home)
        state["repair_counter"] += 1
        repair_id = f"r{state['repair_counter']:04d}"
        folder = home.repairs / repair_id
        folder.mkdir(parents=True)
        if open_ and open_["status"] == "awaiting_approval":
            log_event(home, "superseded", repair_id=open_["id"], by=repair_id)
        live_model = home.model.read_text(encoding="utf-8")
        release_note = state["release_note"] if notes is None else notes
        with live_project(home):
            final = agent_mod.run_case(agent_mod.build_agent(proposer), home.warehouse, folder / "work", config=config, notes=release_note,
                                       case_id=f"live-{repair_id}", model=model_name, history_loads=history_loads)
        outcome = final["outcome"]
        new_sql = final.get("sql")
        proposal = {
            "repair_id": repair_id, "created_at": _now(), "outcome": outcome, "reason": final["reason"], "config": config,
            "release_note_used": release_note if config == "notes" else "", "base_model_sha": _sha(live_model),
            "history_loads": history_loads, "last_good_load": state["last_good_load"], "latest_load": verdict["latest_load"],
            "drift_reasons": verdict["reasons"], "sql": new_sql, "rejected_attempt_sql": final.get("proposed_sql"),
            "diff": _diff(live_model, new_sql) if new_sql else "", "checks": final.get("checks", {}), "attempts": final["attempts"],
            "models": final.get("models", []), "input_tokens": final["input_tokens"], "output_tokens": final["output_tokens"],
            "latency_s": final["latency_s"], "steps": final["steps"],
        }
        _write_atomic(folder / "proposal.json", json.dumps(proposal, indent=2, default=str) + "\n")
        needs_approval = outcome in ("patch", "no_change")
        state["open_repair"] = {"id": repair_id, "status": "awaiting_approval" if needs_approval else "escalated", "outcome": outcome}
        save_state(home, state)
        log_event(home, "repair", repair_id=repair_id, outcome=outcome, config=config, attempts=final["attempts"])
        return {"outcome": outcome, "repair_id": repair_id, "needs_approval": needs_approval, "reason": final["reason"],
                "attempts": final["attempts"], "diff": proposal["diff"], "drift": "; ".join(verdict["reasons"]), "checks": proposal["checks"]}


# ---- apply / reject ------------------------------------------------------------------------------------------------------

def _open_proposal(home: Home, state: dict, repair_id: str) -> dict:
    open_ = state["open_repair"]
    if not open_ or open_["id"] != repair_id:
        current = open_["id"] if open_ else "none"
        raise OpsError(f"{repair_id} is not the open repair (the open one is {current}); a newer proposal replaced it or it is closed")
    if open_["status"] != "awaiting_approval":
        raise OpsError(f"{repair_id} is {open_['status']}, so there is nothing to approve")
    return json.loads((home.repairs / repair_id / "proposal.json").read_text(encoding="utf-8"))


def _set_open(state: dict, status: str) -> None:
    state["open_repair"] = {**state["open_repair"], "status": status}


def _acknowledge_current(home: Home) -> dict:
    raw = _ORIGINAL_SCHEMA_SIGNALS(home.warehouse)
    return {"added": sorted(raw["added"]), "went_null": sorted(raw["went_null"])}


def _decision(home: Home, repair_id: str, **data) -> None:
    _write_atomic(home.repairs / repair_id / "decision.json", json.dumps({"at": _now(), **data}, indent=2) + "\n")


def apply(home: Home, repair_id: str, by: str = "", comment: str = "") -> dict:
    """A person approved the proposal. Re-check it against today's data, change the live model, rebuild; put the old model back if the
    rebuild does not come out healthy. An approval is for a patch the gate has passed, not for whatever the file holds later."""
    from mend import validate

    by = by or getpass.getuser()
    with locked(home):
        state = load_state(home)
        proposal = _open_proposal(home, state, repair_id)
        live_model = home.model.read_text(encoding="utf-8")
        outcome = proposal["outcome"]
        if outcome == "patch":
            if _sha(live_model) != proposal["base_model_sha"]:
                _set_open(state, "stale")
                save_state(home, state)
                raise OpsError(f"the live model changed after {repair_id} was proposed; ask for a new proposal")
            with live_project(home):
                v = validate.validate_patch(home.warehouse, proposal["sql"], live_model, home.repairs / repair_id / "recheck",
                                            proposal["history_loads"])
            if not v.ok:
                _set_open(state, "stale")
                save_state(home, state)
                log_event(home, "apply_refused", repair_id=repair_id, problems=v.problems)
                raise OpsError(f"{repair_id} no longer passes the gate on today's data, nothing was changed: " + "; ".join(v.problems))
        before = {"ack": json.loads(json.dumps(state["acknowledged"])), "last_good": state["last_good_load"]}
        version = state["model_version"]
        if outcome == "patch":
            shutil.copy(home.model, home.model_history / f"stg_orders.v{version}.sql")
            home.model.write_text(proposal["sql"], encoding="utf-8")
            state["model_version"] = version + 1
            shutil.copy(home.model, home.model_history / f"stg_orders.v{version + 1}.sql")
        state["acknowledged"] = _acknowledge_current(home)
        save_state(home, state)
        verdict = _build_locked(home)
        state = load_state(home)
        if verdict["status"] != "healthy":
            if outcome == "patch":
                home.model.write_text(live_model, encoding="utf-8")
                state["model_version"] = version
            state["acknowledged"], state["last_good_load"] = before["ack"], before["last_good"]
            _set_open(state, "failed_after_apply")
            save_state(home, state)
            _build_locked_quiet(home)
            log_event(home, "apply_undone", repair_id=repair_id, reasons=verdict["reasons"])
            raise OpsError(f"after applying {repair_id} the pipeline is still not healthy ({'; '.join(verdict['reasons'])}); "
                           f"the previous model is back in place")
        _set_open(state, "applied")
        save_state(home, state)
        _decision(home, repair_id, decision="approved", by=by, comment=comment, outcome=outcome)
        log_event(home, "apply", repair_id=repair_id, by=by, outcome=outcome, comment=comment)
        return {"applied": True, "repair_id": repair_id, "outcome": outcome, "model_version": state["model_version"],
                "status": verdict["status"], "mart_days": verdict["mart_days"], "mart_latest_day": verdict["mart_latest_day"]}


def _build_locked_quiet(home: Home) -> None:
    """Rebuild after an undo so the marts match the restored model. Its verdict is not interesting; the failure is already reported."""
    try:
        _build_locked(home)
    except OpsError:
        pass


def reject(home: Home, repair_id: str, by: str = "", comment: str = "") -> dict:
    by = by or getpass.getuser()
    with locked(home):
        state = load_state(home)
        _open_proposal(home, state, repair_id)
        _set_open(state, "rejected")
        save_state(home, state)
        _decision(home, repair_id, decision="rejected", by=by, comment=comment)
        log_event(home, "reject", repair_id=repair_id, by=by, comment=comment)
        return {"rejected": True, "repair_id": repair_id}


# ---- status --------------------------------------------------------------------------------------------------------------

def status(home: Home) -> dict:
    """Read-only and lock-free, so it can be asked while a repair waits on the model; counts it cannot read right now show as -1."""
    state = load_state(home)
    out = {"home": str(home.root), "next_day": state["next_day"], "loads": len(state["loads"]),
           "source_drift": ",".join(state["source_drift"]) or "none", "model_version": state["model_version"],
           "last_build": (state["last_build"] or {}).get("status", "never"), "last_good_load": state["last_good_load"] or "",
           "open_repair": (state["open_repair"] or {}).get("id", "none"),
           "open_repair_status": (state["open_repair"] or {}).get("status", ""), **_mart_summary(home), "raw_rows": -1}
    if home.warehouse.exists():
        try:
            con = duckdb.connect(str(home.warehouse), read_only=True)
            try:
                out["raw_rows"] = int(con.execute("select count(*) from raw.orders").fetchone()[0])
            finally:
                con.close()
        except duckdb.Error:
            pass
    return out


# ---- command line --------------------------------------------------------------------------------------------------------

def _print_result(cmd: str, res: dict) -> None:
    if cmd == "build":
        print(f"build: {res['status']} ({res['dbt_nodes']} dbt nodes; mart has {res['mart_days']} days, latest {res['mart_latest_day'] or '-'})")
        for r in res["reasons"]:
            print(f"  - {r}")
        for name, msg in res["dbt_messages"].items():
            print(f"  dbt message for {name}: {' '.join(msg.split())}")
        if res["drift_handled"]:
            print(f"  already handled by {res['open_repair']} ({res['open_repair_status']})")
    elif cmd == "repair":
        print(f"repair: outcome = {res['outcome']} ({res['repair_id'] or '-'})")
        print(f"  reason: {res['reason']}")
        if res.get("drift"):
            print(f"  drift: {res['drift']}")
        if res.get("diff"):
            print("  proposed change to stg_orders.sql:\n" + "\n".join("    " + l for l in res["diff"].splitlines()))
        if res.get("checks"):
            print("  gate: " + ", ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in res["checks"].items()))
        if res["needs_approval"]:
            print("  waiting for a person: nothing has been changed")
    else:
        print(f"{cmd}: " + ", ".join(f"{k}={v}" for k, v in res.items()))


def _outputs(res: dict) -> dict:
    """What a scheduler may branch on: flat strings and numbers (booleans become 'true' / 'false'). Long text stays in the log and in the repair folder."""
    keep: dict = {}
    for k, v in res.items():
        if k in ("diff", "dbt_messages", "checks"):
            continue
        if isinstance(v, (list, tuple)):
            v = "; ".join(str(x) for x in v)
        if isinstance(v, bool):
            keep[k] = "true" if v else "false"      # as text, so a flow compares strings and never depends on how a scheduler types JSON
        elif isinstance(v, (int, float)):
            keep[k] = v
        elif isinstance(v, str):
            keep[k] = " ".join(v.split())[:400]
    return keep


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mend_ops", description="Operate a live Mend pipeline.")
    p.add_argument("--home", help=f"state folder (default: $MEND_HOME or ./{DEFAULT_HOME})")
    p.add_argument("--kestra", action="store_true", help="also print the outputs line a Kestra script task picks up")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="create the home")
    s.add_argument("--reset", action="store_true", help="wipe it first")
    s = sub.add_parser("bootstrap", help="reset, load a healthy history, build")
    s.add_argument("--days", type=int, default=BOOTSTRAP_DAYS)
    s = sub.add_parser("source-change", help="demo: the source starts sending a changed feed")
    s.add_argument("drifts", nargs="*")
    s.add_argument("--clear", action="store_true", help="the source goes back to the original feed")
    s.add_argument("--list", action="store_true", help="list the drifts you can simulate")
    s = sub.add_parser("ingest", help="load the next daily batch")
    s.add_argument("--days", type=int, default=1)
    sub.add_parser("build", help="dbt build on the live warehouse, then judge it")
    s = sub.add_parser("repair", help="ask the agent for a proposal; changes nothing")
    s.add_argument("--config", default="evidence", choices=("schema", "evidence", "notes"))
    s.add_argument("--notes", default=None, help="release note for --config notes (default: the note of the simulated source change)")
    s.add_argument("--force", action="store_true", help="propose again even though one is waiting")
    s.add_argument("--proposal-file", help="answer from this JSON file {action, sql, reason} instead of calling the model")
    s.add_argument("--model", default="")
    for name in ("apply", "reject"):
        s = sub.add_parser(name, help=f"{name} a proposal")
        s.add_argument("repair_id")
        s.add_argument("--by", default="")
        s.add_argument("--comment", default="")
    s = sub.add_parser("status", help="where the pipeline stands")
    s.add_argument("--json", action="store_true")
    return p


def run(args: argparse.Namespace) -> dict:
    home = Home.at(args.home)
    cmd = args.cmd
    if cmd == "init":
        return init(home, reset=args.reset)
    if cmd == "bootstrap":
        init(home, reset=True)
        ingest(home, days=args.days)
        res = build(home)
        if res["status"] != "healthy":
            raise OpsError("the original feed did not build healthy: " + "; ".join(res["reasons"]))
        return {**{k: res[k] for k in ("status", "mart_days", "mart_latest_day")}, "days_loaded": args.days}
    if cmd == "source-change":
        if args.list:
            print("\n".join(f"{k:22s} {d.summary}" for k, d in drift_mod.DRIFTS.items()))
            return {}
        return source_change(home, args.drifts, clear=args.clear)
    if cmd == "ingest":
        return ingest(home, days=args.days)
    if cmd == "build":
        return build(home)
    if cmd == "repair":
        proposer = replay_proposer(args.proposal_file) if args.proposal_file else None
        notes = args.notes if args.notes is not None else (os.environ.get("MEND_RELEASE_NOTE") or None)     # env: no shell quoting of free text
        return repair(home, config=args.config, notes=notes, proposer=proposer, force=args.force, model=args.model)
    if cmd in ("apply", "reject"):
        comment = args.comment or os.environ.get("MEND_COMMENT", "")                                       # env: no shell quoting of free text
        return (apply if cmd == "apply" else reject)(home, args.repair_id, by=args.by, comment=comment)
    if cmd == "status":
        return status(home)
    raise AssertionError(cmd)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        res = run(args)
    except OpsError as exc:
        print(f"ERROR: {exc}")
        return 1
    if res:
        if args.cmd == "status" and args.json:
            print(json.dumps(res, indent=2))
        else:
            _print_result(args.cmd, res)
        if args.kestra:
            print("::" + json.dumps({"outputs": _outputs(res)}, separators=(",", ":")) + "::")
    return 0


if __name__ == "__main__":
    sys.exit(main())
