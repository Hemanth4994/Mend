"""The operations layer: detect, propose, wait for a person, apply or undo. The model is replaced by scripted answers; dlt, dbt, DuckDB
and the production gate are the real ones."""
from __future__ import annotations

import json
import os
import re
import time

import pytest

import mend_ops as ops
from ops_helpers import Scripted, fixed_model, patch_answer
from mend import sandbox, signals

# Kestra's own pattern for a script output line (core TaskLogLineMatcher.LOG_DATA_SYNTAX)
KESTRA_LINE = re.compile(r"^::(\{.*})::$")


def state(home):
    return ops.load_state(home)


# ---- a healthy pipeline ----------------------------------------------------------------------------------------------------

def test_bootstrap_builds_a_healthy_pipeline(healthy):
    s = state(healthy)
    assert s["next_day"] == ops.BOOTSTRAP_DAYS and len(s["loads"]) == ops.BOOTSTRAP_DAYS
    assert s["last_good_load"] == s["loads"][-1]["load_id"]
    out = ops.status(healthy)
    assert out["last_build"] == "healthy" and out["mart_days"] == ops.BOOTSTRAP_DAYS and out["raw_rows"] == 5 * 150


def test_more_days_of_the_original_feed_stay_healthy(healthy):
    ops.ingest(healthy, days=2)
    res = ops.build(healthy)
    assert res["status"] == "healthy" and res["reasons"] == [] and res["mart_days"] == ops.BOOTSTRAP_DAYS + 2


def test_repair_on_a_healthy_pipeline_does_not_call_the_model(healthy):
    asked = Scripted(("escalate", "", "should never be asked"))
    res = ops.repair(healthy, proposer=asked)
    assert res["outcome"] == "none" and asked.calls == 0 and state(healthy)["open_repair"] is None


# ---- detection -------------------------------------------------------------------------------------------------------------

def test_a_renamed_field_is_detected_and_the_mart_keeps_the_last_good_state(drifted):
    res = ops.build(drifted)
    assert res["status"] == "drift"
    assert any("not_null_stg_orders_amount" in r for r in res["reasons"])
    assert res["new_columns"] == ["total_amount"] and res["went_empty"] == ["amount"]
    assert res["mart_days"] == ops.BOOTSTRAP_DAYS          # dbt did not publish the bad days
    assert res["drift_handled"] is False
    assert state(drifted)["last_good_load"] == state(drifted)["loads"][ops.BOOTSTRAP_DAYS - 1]["load_id"]


def test_a_new_optional_column_is_a_drift_even_though_dbt_passes(healthy):
    ops.source_change(healthy, ["add_coupon"])
    ops.ingest(healthy)
    res = ops.build(healthy)
    assert res["status"] == "drift" and res["failing"] == {} and res["new_columns"] == ["coupon_code"]


def test_source_change_rejects_unknown_drifts_and_can_be_cleared(healthy):
    with pytest.raises(ops.OpsError, match="unknown drift"):
        ops.source_change(healthy, ["no_such_drift"])
    with pytest.raises(ops.OpsError, match="at least one"):
        ops.source_change(healthy, [])
    assert ops.source_change(healthy, ["rename_amount"])["release_note"].startswith("orders v2")
    assert ops.source_change(healthy, [], clear=True)["source_drift"] == "none"


# ---- the repair loop -------------------------------------------------------------------------------------------------------

def test_a_proposal_changes_nothing_until_a_person_approves(drifted):
    before = drifted.model.read_text(encoding="utf-8")
    res = ops.repair(drifted, proposer=patch_answer(drifted))
    assert res["outcome"] == "patch" and res["needs_approval"] and res["checks"] == {
        "static": True, "runs": True, "history": True, "filled": True, "dbt": True}
    assert "coalesce(amount, total_amount)" in res["diff"]
    assert drifted.model.read_text(encoding="utf-8") == before            # the live model is untouched
    assert ops.build(drifted)["status"] == "drift"                          # and so is the pipeline
    s = state(drifted)
    assert s["open_repair"] == {"id": "r0001", "status": "awaiting_approval", "outcome": "patch"}
    proposal = json.loads((drifted.repairs / "r0001" / "proposal.json").read_text(encoding="utf-8"))
    assert proposal["history_loads"] == ops.BOOTSTRAP_DAYS and proposal["base_model_sha"] == ops._sha(before)


def test_approving_applies_the_patch_and_the_marts_catch_up(drifted):
    ops.repair(drifted, proposer=patch_answer(drifted))
    out = ops.apply(drifted, "r0001", by="hemanth", comment="looks right")
    assert out["status"] == "healthy" and out["mart_days"] == ops.BOOTSTRAP_DAYS + 2
    assert "coalesce(amount, total_amount)" in drifted.model.read_text(encoding="utf-8")
    assert (drifted.model_history / "stg_orders.v0.sql").exists() and (drifted.model_history / "stg_orders.v1.sql").exists()
    assert "coalesce" not in (drifted.model_history / "stg_orders.v0.sql").read_text(encoding="utf-8")
    decision = json.loads((drifted.repairs / "r0001" / "decision.json").read_text(encoding="utf-8"))
    assert decision["decision"] == "approved" and decision["by"] == "hemanth" and decision["comment"] == "looks right"
    s = state(drifted)
    assert s["open_repair"]["status"] == "applied" and s["model_version"] == 1
    assert s["last_good_load"] == s["loads"][-1]["load_id"]
    assert any(e["kind"] == "apply" for e in map(json.loads, drifted.events.read_text().splitlines()))


def test_a_second_proposal_is_not_asked_for_while_one_is_waiting(drifted):
    asked = patch_answer(drifted)
    first = ops.repair(drifted, proposer=asked)
    again = ops.repair(drifted, proposer=asked)
    assert again["outcome"] == "pending" and again["repair_id"] == first["repair_id"] and asked.calls == 1
    assert again["needs_approval"] is False            # it must not open a second approval for the same proposal
    forced = ops.repair(drifted, proposer=asked, force=True)
    assert forced["repair_id"] == "r0002" and asked.calls == 2
    with pytest.raises(ops.OpsError, match="not the open repair"):
        ops.apply(drifted, "r0001")                                          # the old proposal was superseded


def test_rejecting_closes_the_proposal_and_the_pipeline_stays_in_drift(drifted):
    ops.repair(drifted, proposer=patch_answer(drifted))
    ops.reject(drifted, "r0001", by="hemanth", comment="not today")
    res = ops.build(drifted)
    assert res["status"] == "drift" and res["drift_handled"] and res["open_repair_status"] == "rejected"
    with pytest.raises(ops.OpsError, match="rejected"):
        ops.apply(drifted, "r0001")
    assert json.loads((drifted.repairs / "r0001" / "decision.json").read_text())["decision"] == "rejected"


def test_the_agents_escalation_waits_for_no_one_and_cannot_be_applied(healthy):
    ops.source_change(healthy, ["drop_city"])
    ops.ingest(healthy)
    res = ops.repair(healthy, proposer=Scripted(("escalate", "", "city is gone from the source; a person must decide")))
    assert res["outcome"] == "escalate" and res["needs_approval"] is False
    assert state(healthy)["open_repair"]["status"] == "escalated"
    with pytest.raises(ops.OpsError, match="escalated"):
        ops.apply(healthy, res["repair_id"])


def test_a_bad_proposal_is_stopped_by_the_gate_and_escalated(drifted):
    """The model's word is never enough: a patch that does not fix the failing check never reaches a person as a patch."""
    unchanged = drifted.model.read_text(encoding="utf-8")
    asked = Scripted(("repair", unchanged, "no idea, trying the same thing"))
    res = ops.repair(drifted, proposer=asked)
    assert res["outcome"] == "escalate" and asked.calls == 2                # one proposal, one correction, then a person
    assert "rejected the proposal" in res["reason"]


def test_a_patch_in_the_wrong_place_is_refused_before_it_is_proposed(drifted):
    res = ops.repair(drifted, proposer=Scripted(("repair", "select 1 as order_id", "wrong")))
    assert res["outcome"] == "escalate"


# ---- approval is for what the gate passed ----------------------------------------------------------------------------------

def test_apply_refuses_when_the_live_model_changed_after_the_proposal(drifted):
    ops.repair(drifted, proposer=patch_answer(drifted))
    drifted.model.write_text(drifted.model.read_text(encoding="utf-8") + "\n-- edited by hand\n", encoding="utf-8")
    with pytest.raises(ops.OpsError, match="live model changed"):
        ops.apply(drifted, "r0001")
    assert state(drifted)["open_repair"]["status"] == "stale"


def test_apply_refuses_when_new_data_breaks_the_patch(drifted):
    ops.repair(drifted, proposer=patch_answer(drifted))
    ops.source_change(drifted, ["rename_amount", "rename_status"])           # the source changes again before anyone approves
    ops.ingest(drifted)
    before = drifted.model.read_text(encoding="utf-8")
    with pytest.raises(ops.OpsError, match="no longer passes the gate"):
        ops.apply(drifted, "r0001")
    assert drifted.model.read_text(encoding="utf-8") == before and state(drifted)["open_repair"]["status"] == "stale"


def test_apply_puts_the_old_model_back_when_the_rebuild_is_not_healthy(drifted, monkeypatch):
    ops.repair(drifted, proposer=patch_answer(drifted))
    before = drifted.model.read_text(encoding="utf-8")
    ack_before = state(drifted)["acknowledged"]
    real = ops._dbt_build_live
    calls = {"n": 0}

    def flaky(home):
        calls["n"] += 1
        statuses, failing, messages = real(home)
        return (statuses, {"not_null_stg_orders_amount": 7}, messages) if calls["n"] == 1 else (statuses, failing, messages)

    monkeypatch.setattr(ops, "_dbt_build_live", flaky)
    with pytest.raises(ops.OpsError, match="previous model is back"):
        ops.apply(drifted, "r0001")
    s = state(drifted)
    assert drifted.model.read_text(encoding="utf-8") == before and s["model_version"] == 0
    assert s["acknowledged"] == ack_before and s["open_repair"]["status"] == "failed_after_apply"


def test_approving_no_change_acknowledges_the_new_column_and_stops_the_alarm(healthy):
    ops.source_change(healthy, ["add_coupon"])
    ops.ingest(healthy)
    res = ops.repair(healthy, proposer=Scripted(("no_change", "", "coupon_code is optional and unused")))
    assert res["outcome"] == "no_change" and res["needs_approval"]
    out = ops.apply(healthy, res["repair_id"], by="hemanth")
    assert out["status"] == "healthy" and state(healthy)["model_version"] == 0
    ops.ingest(healthy)
    assert ops.build(healthy)["status"] == "healthy"                        # the same column does not raise the alarm again


def test_a_change_a_person_approved_is_not_shown_to_the_agent_again(repaired):
    """After the rename was repaired, `amount` stays empty in new loads. A later, different change must be judged on its own."""
    ops.source_change(repaired, ["rename_amount", "add_coupon"])
    ops.ingest(repaired)
    res = ops.build(repaired)
    assert res["status"] == "drift" and res["reasons"] == ["new columns: coupon_code"]
    seen = {}

    def spy(messages):
        seen["context"] = messages[-1][1]
        return Scripted(("no_change", "", "only an optional column was added"))(messages)

    out = ops.repair(repaired, proposer=spy)
    assert out["outcome"] == "no_change"                                    # the gate accepted it: nothing else is wrong
    assert "columns that were filled before and are empty now: none" in seen["context"]


# ---- the frozen code is left as it was found -------------------------------------------------------------------------------

def test_the_frozen_modules_point_back_at_the_repository_after_a_repair_even_when_it_fails(drifted):
    pristine = (signals.DBT_DIR, sandbox.STAGING_MODEL, signals.schema_signals)

    def broken(messages):
        raise RuntimeError("the API is down")

    with pytest.raises(RuntimeError, match="API is down"):
        ops.repair(drifted, proposer=broken)
    assert (signals.DBT_DIR, sandbox.STAGING_MODEL, signals.schema_signals) == pristine
    assert state(drifted)["open_repair"] is None                             # a failed call leaves no half-open repair


def test_without_a_key_the_error_is_clear_and_nothing_is_recorded(drifted, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ops.OpsError, match="GOOGLE_API_KEY"):
        ops.repair(drifted)
    s = state(drifted)
    assert s["repair_counter"] == 0 and s["open_repair"] is None


def test_no_repair_without_a_known_good_history(drifted):
    s = state(drifted)
    s["last_good_load"] = None
    ops.save_state(drifted, s)
    with pytest.raises(ops.OpsError, match="no healthy build"):
        ops.repair(drifted, proposer=patch_answer(drifted))


# ---- locking, the command line, the scheduler's contract -------------------------------------------------------------------

def test_a_second_command_waits_and_then_gives_up(healthy):
    with ops.locked(healthy):
        with pytest.raises(ops.OpsError, match="another Mend command"):
            with ops.locked(healthy, wait=0.3):
                pass
    with ops.locked(healthy, wait=0.3):                                      # released on exit
        pass


def test_a_lock_left_by_a_crashed_run_is_taken_over(healthy):
    healthy.lock.write_text("999 long ago")
    old = time.time() - 3 * 3600
    os.utime(healthy.lock, (old, old))
    with ops.locked(healthy, wait=0.3):
        pass


def test_the_outputs_line_is_what_kestra_parses(healthy, capsys):
    assert ops.main(["--home", str(healthy.root), "--kestra", "build"]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    match = KESTRA_LINE.match(last)
    assert match, last
    outputs = json.loads(match.group(1))["outputs"]
    assert outputs["status"] == "healthy" and outputs["drift_handled"] == "false"
    assert all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in outputs.values())


def test_repair_outputs_are_flat_and_carry_the_branch_keys(drifted, capsys):
    ops.repair(drifted, proposer=patch_answer(drifted), force=True)
    capsys.readouterr()
    # the same result through the command line, replaying a saved answer instead of calling the model
    answer = drifted.root.parent / "answer.json"
    answer.write_text(json.dumps({"action": "repair", "sql": fixed_model(drifted), "reason": "rename"}), encoding="utf-8")
    code = ops.main(["--home", str(drifted.root), "--kestra", "repair", "--force", "--proposal-file", str(answer)])
    assert code == 0
    out = capsys.readouterr().out
    outputs = json.loads(KESTRA_LINE.match(out.strip().splitlines()[-1]).group(1))["outputs"]
    assert outputs["outcome"] == "patch" and outputs["needs_approval"] == "true" and outputs["repair_id"] == "r0002"
    assert "diff" not in outputs and "checks" not in outputs


def test_errors_exit_with_one_and_say_why(healthy, capsys):
    assert ops.main(["--home", str(healthy.root), "apply", "r0001"]) == 1
    assert "not the open repair" in capsys.readouterr().out


def test_a_missing_home_says_how_to_create_it(tmp_path, capsys):
    assert ops.main(["--home", str(tmp_path / "nope"), "status"]) == 1
    assert "run `init`" in capsys.readouterr().out
