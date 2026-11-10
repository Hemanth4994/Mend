"""The evaluation runner: scoring end to end with stand-in models, resuming, the freeze guard and the report. No API key, no model."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "scripts" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

from mend.throttle import DailyQuotaExceeded   # noqa: E402

IDS = ["s01", "c01", "u01"]                       # one dev case of each kind of right answer: repair, leave alone, escalate


def go(cache, tmp_path, model="oracle", ids=IDS, **kw):
    out = tmp_path / f"{model}.jsonl"
    rows = run_eval.run_split("dev", "evidence", model, cache, out, ids=ids, log=lambda *_: None, **kw)
    return out, [r for r in rows if "id" in r]


def test_the_oracle_stand_in_gets_every_kind_of_case_right(cache, tmp_path):
    out, rows = go(cache, tmp_path)
    assert {r["id"]: r["label"] for r in rows} == {"s01": "repaired", "c01": "correct_noop", "u01": "correct_escalation"}
    head = json.loads(out.read_text(encoding="utf-8").splitlines()[0])["run"]
    assert head["split"] == "dev" and head["model"] == "oracle" and len(head["prompt_sha"]) == 12 and len(head["cases_sha"]) == 12
    s = rows[0]
    assert s["model_calls"] == 1 and s["failing_checks"] and "rejected" in s and s["correct"] and not s["silent"]


def test_the_lazy_stand_in_is_stopped_by_the_gate_when_the_failure_is_loud(cache, tmp_path):
    _, rows = go(cache, tmp_path, "lazy", ids=["s01"])
    r = rows[0]                                    # "no change" while dbt tests fail is rejected twice, then a person is asked
    assert r["outcome"] == "escalate" and r["label"] == "false_escalation" and r["attempts"] == 2 and not r["silent"]
    assert any("not_null" in p for p in r["rejected"][0])


def test_a_stopped_run_resumes_and_never_repeats_a_case(cache, tmp_path):
    out, first = go(cache, tmp_path, ids=["s01"])
    lines = out.read_text(encoding="utf-8").splitlines()

    def must_not_be_called(messages):
        raise AssertionError("the model was called for a case that was already answered")

    _, again = go(cache, tmp_path, ids=["s01"], proposer=must_not_be_called)
    assert out.read_text(encoding="utf-8").splitlines() == lines and again == first
    _, more = go(cache, tmp_path, ids=["s01", "c01"])                                  # a new case is added to the same file
    assert [r["id"] for r in more] == ["s01", "c01"]


def test_limit_runs_only_the_first_unanswered_cases(cache, tmp_path):
    _, rows = go(cache, tmp_path, ids=None, limit=1)
    assert [r["id"] for r in rows] == ["s01"]


def test_an_unexplained_failure_is_recorded_as_a_result_and_the_run_continues(cache, tmp_path):
    def broken(messages):
        raise RuntimeError("model API exploded")

    _, rows = go(cache, tmp_path, ids=["s01", "c01"], proposer=broken)
    assert [(r["id"], r["label"]) for r in rows] == [("s01", "error"), ("c01", "error")]
    assert "exploded" in rows[0]["error"] and not rows[0]["correct"] and not rows[0]["silent"]


def test_the_daily_quota_stops_the_run_cleanly(cache, tmp_path):
    messages = []

    def quota(_):
        raise DailyQuotaExceeded("daily limit")

    out = tmp_path / "quota.jsonl"
    rows = run_eval.run_split("dev", "evidence", "oracle", cache, out, ids=["s01", "c01"], proposer=quota, log=messages.append)
    assert [r for r in rows if "id" in r] == [] and any("run the same command again tomorrow" in m for m in messages)


def test_gemini_without_a_key_refuses_to_start(cache, tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        run_eval.run_split("dev", "evidence", "gemini", cache, tmp_path / "g.jsonl", ids=["s01"], log=lambda *_: None)
    assert "GOOGLE_API_KEY" in str(e.value) and not (tmp_path / "g.jsonl").exists()


# ---- the freeze guard ---------------------------------------------------------------------------------------------------

def test_the_test_split_needs_the_final_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_eval.py", "--split", "test", "--config", "evidence", "--model", "oracle"])
    with pytest.raises(SystemExit) as e:
        run_eval.main()
    assert "frozen" in str(e.value)


def test_the_freeze_check_fails_closed_without_the_tag(monkeypatch):
    def no_tag(*args):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(run_eval, "git", no_tag)
    with pytest.raises(SystemExit) as e:
        run_eval.check_freeze()
    assert "Cannot confirm the freeze" in str(e.value)


def test_the_freeze_check_fails_closed_when_a_frozen_file_changed(monkeypatch):
    answers = {"rev-list": "abc", "rev-parse": "def", "merge-base": "", "diff": "src/mend/agent.py", "status": ""}
    monkeypatch.setattr(run_eval, "git", lambda *args: answers[args[0]])
    with pytest.raises(SystemExit) as e:
        run_eval.check_freeze()
    assert "differ from tag" in str(e.value) and "src/mend/agent.py" in str(e.value)


def test_the_freeze_check_passes_when_nothing_changed(monkeypatch):
    answers = {"rev-list": "abc", "rev-parse": "def", "merge-base": "", "diff": "", "status": ""}
    monkeypatch.setattr(run_eval, "git", lambda *args: answers[args[0]])
    run_eval.check_freeze()


def test_the_report_flag_prints_a_summary_of_a_finished_run(cache, tmp_path, monkeypatch, capsys):
    out, _ = go(cache, tmp_path)
    monkeypatch.setattr("sys.argv", ["run_eval.py", "--report", str(out)])
    assert run_eval.main() == 0
    text = capsys.readouterr().out
    assert "silent corruption" in text and "structural" in text and "0 of 3" in text
