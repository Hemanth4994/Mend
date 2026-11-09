"""The repair agent's graph, tested with a scripted stand-in model: no API key, no network. Real dlt, real dbt, real gate.

What these tests pin down is the control flow, which is where an autonomous agent can go wrong:
the model's word is never enough (the gate decides), a rejected answer is shown back once, and the model is called at most twice.
"""
import itertools
import re
from types import SimpleNamespace

import pytest

from mend import agent, gold as gold_mod, sandbox
from mend.agent import ProposeResult, Proposal
from mend.cases import load_cases

BY_ID = {c.id: c for c in load_cases()}
BASE = sandbox.baseline_sql()
_runs = itertools.count()


class Scripted:
    """A stand-in model: returns its answers in order and records every message list it was shown."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, messages):
        self.seen.append(messages)
        a = self.answers.pop(0)
        if a is None:
            return ProposeResult(None, "not json", 10, 0, "fake-model")
        return ProposeResult(a, "", 100, 20, "fake-model")

    def human(self, i):
        return self.seen[i][1][1]


def repair(sql, reason="the feed changed"):
    return Proposal(action="repair", sql=sql, reason=reason)


def no_change(reason="nothing to fix"):
    return Proposal(action="no_change", sql="", reason=reason)


def escalate(reason="a person should look"):
    return Proposal(action="escalate", sql="", reason=reason)


def run(cache, case_id, model, config="evidence", max_attempts=agent.MAX_ATTEMPTS):
    case = BY_ID[case_id]
    db = sandbox.build_case(case, cache)
    work = cache / "agent" / f"{case_id}-{next(_runs)}"
    return agent.run_case(agent.build_agent(model, max_attempts), db, work, config, case.notes, case.id, case.split, "fake-model")


# ---- the three good endings ------------------------------------------------------------------------------------------------

def test_a_correct_repair_comes_back_as_a_patch(cache):
    m = Scripted(repair(gold_mod.repair_sql("rename_amount")))
    out = run(cache, "s01", m)
    assert out["outcome"] == "patch" and out["sql"].endswith("\n") and "source('raw', 'orders')" in out["sql"]
    assert out["attempts"] == out["model_calls"] == len(m.seen) == 1
    assert [s["node"] for s in out["steps"]] == ["gather", "propose", "check"]
    assert out["input_tokens"] == 100 and out["output_tokens"] == 20 and out["models"] == ["fake-model"]
    assert out["latency_s"] > 0


def test_no_change_is_accepted_when_nothing_is_failing(cache):
    out = run(cache, "c01", Scripted(no_change("only a new column the contract does not use")))
    assert out["outcome"] == "no_change" and out["sql"] is None and out["attempts"] == 1


def test_the_model_can_ask_for_a_person_and_is_not_second_guessed(cache):
    m = Scripted(escalate("the city information is gone from the feed"))
    out = run(cache, "u06", m)
    assert out["outcome"] == "escalate" and out["reason"] == "the city information is gone from the feed"
    assert out["sql"] is None and len(m.seen) == 1


# ---- the gate overrules the model --------------------------------------------------------------------------------------------

def test_a_rejected_answer_is_shown_back_and_the_second_try_can_pass(cache):
    m = Scripted(repair(BASE, "I think it is fine"), repair(gold_mod.repair_sql("rename_amount")))
    out = run(cache, "s01", m)                              # the first answer leaves the broken model alone, so the gate rejects it
    assert out["outcome"] == "patch" and out["attempts"] == 2 and len(out["rejected"]) == 1
    second = m.human(1)
    assert "was rejected by the automatic checks" in second and "Problems:\n- " in second
    assert "was rejected" not in m.human(0)


def test_two_rejections_end_in_an_escalation_and_the_model_is_never_called_a_third_time(cache):
    m = Scripted(repair(BASE), repair(BASE), repair(gold_mod.repair_sql("rename_amount")))
    out = run(cache, "s01", m)
    assert out["outcome"] == "escalate" and out["sql"] is None
    assert out["attempts"] == 2 and len(m.seen) == 2 and len(m.answers) == 1          # the third answer was never asked for
    assert out["reason"].startswith("The automatic checks rejected the proposal after 2 attempt(s)")
    assert out["proposed_sql"] == BASE.strip() + "\n"                                  # kept for the person who gets the escalation


def test_the_attempt_limit_is_a_setting(cache):
    m = Scripted(repair(BASE))
    out = run(cache, "s01", m, max_attempts=1)
    assert out["outcome"] == "escalate" and out["attempts"] == 1 and len(m.seen) == 1


def test_an_unparseable_reply_counts_as_an_attempt(cache):
    m = Scripted(None, repair(gold_mod.repair_sql("rename_amount")))
    out = run(cache, "s01", m)
    assert out["outcome"] == "patch" and out["attempts"] == 2
    assert "not a valid answer" in out["rejected"][0]["problems"][0] and "was rejected" in m.human(1)


def test_saying_no_change_on_a_broken_feed_is_rejected(cache):
    m = Scripted(no_change("looks fine to me"), escalate("tests are failing and I am not sure how to fix them"))
    out = run(cache, "s01", m)
    assert "not_null_stg_orders_amount" in out["rejected"][0]["problems"][0]           # the gate quotes the failing check
    assert out["outcome"] == "escalate" and out["attempts"] == 2 and out["sql"] is None


def test_inventing_values_for_a_column_that_is_gone_is_never_applied(cache):
    invented = repair(BASE.replace("    city,", "    'Delhi' as city,"), "fill the city with a default")
    out = run(cache, "u06", Scripted(invented, invented))
    assert out["outcome"] == "escalate" and out["sql"] is None
    assert "constant" in out["rejected"][0]["problems"][0] and "'Delhi'" in out["proposed_sql"]


# ---- what the model is shown ------------------------------------------------------------------------------------------------

def test_the_three_configs_differ_only_in_the_evidence_and_never_name_the_drift(cache):
    shown = {}
    for config in ("schema", "evidence", "notes"):
        m = Scripted(escalate())
        run(cache, "v03", m, config=config)
        shown[config] = m.human(0)
    assert "Schema signals" not in shown["schema"] and "Profile of each column" not in shown["schema"]
    assert "Schema signals" in shown["evidence"] and "Release note" not in shown["evidence"]
    assert "Release note" in shown["notes"]
    for config in ("schema", "evidence"):
        assert not re.search(r"paise|rename_|drop_", shown[config], re.I)
    assert all(s.startswith("Output contract of stg_orders") for s in shown.values())


def test_the_system_prompt_does_not_leak_the_answer_key():
    assert not re.search(r"paise|rename_|drop_|nest_|pack_", agent.SYSTEM_PROMPT)


def test_unknown_config_is_rejected(cache):
    with pytest.raises(ValueError):
        run(cache, "c01", Scripted(escalate()), config="everything")


# ---- the real-model adapter, with the Gemini client replaced by a fake ---------------------------------------------------

class FakeLLM:
    reply = None
    built = {}

    def __init__(self, model, **kw):
        FakeLLM.built = {"model": model, **kw}

    def with_structured_output(self, schema, include_raw=False):
        FakeLLM.built.update(schema=schema, include_raw=include_raw)
        return self

    def invoke(self, messages):
        return FakeLLM.reply


def test_the_gemini_adapter_reads_tokens_and_the_served_model(monkeypatch):
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", FakeLLM)
    monkeypatch.delenv("MEND_MODEL", raising=False)
    FakeLLM.reply = {"raw": SimpleNamespace(usage_metadata={"input_tokens": 1234, "output_tokens": 56},
                                            response_metadata={"model_version": "gemini-3.5-flash-lite-001"}),
                     "parsed": escalate("x"), "parsing_error": None}
    calls = []
    throttle = SimpleNamespace(call=lambda fn: (calls.append(1), fn())[1])
    p = agent.gemini_proposer(throttle=throttle)
    res = p([("system", "s"), ("human", "h")])
    assert p.model == agent.DEFAULT_MODEL == FakeLLM.built["model"] and FakeLLM.built["schema"] is Proposal and FakeLLM.built["include_raw"]
    assert res.proposal.action == "escalate" and (res.input_tokens, res.output_tokens) == (1234, 56)
    assert res.model == "gemini-3.5-flash-lite-001" and calls == [1]                   # every call went through the throttle


def test_the_gemini_adapter_reports_a_parse_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", FakeLLM)
    monkeypatch.setenv("MEND_MODEL", "some-other-model")
    FakeLLM.reply = {"raw": SimpleNamespace(usage_metadata=None, response_metadata=None), "parsed": None,
                     "parsing_error": ValueError("bad json")}
    p = agent.gemini_proposer()
    res = p([("human", "h")])
    assert p.model == "some-other-model" and res.proposal is None and "bad json" in res.error and res.model == "some-other-model"
