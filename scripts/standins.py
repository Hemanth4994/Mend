"""Stand-in models for dry runs. They need no API key and exist to measure the SYSTEM around the model, above all the production gate.

    oracle      does the right thing for every case (it reads the gold repairs, which a real model never sees). Shows whether the gate lets
                every correct repair through, and what a perfect model scores.
    lazy        answers "no change" every time. Shows what the gate catches on its own and what it lets through silently.
    escalator   asks for a person every time. Safe and useless: the floor on silent corruption, the ceiling on human workload.

Scripts may import mend.gold; the package may not (tests/test_answer_key_is_hidden.py).
"""
from mend import gold
from mend.agent import Proposal, ProposeResult
from mend.cases import Case

NAMES = ("oracle", "lazy", "escalator")


def _answer(p: Proposal):
    return lambda messages: ProposeResult(p, "", 0, 0, "stand-in")


def make(name: str, case: Case):
    if name == "lazy":
        return _answer(Proposal(action="no_change", sql="", reason="stand-in: always says the model is fine"))
    if name == "escalator":
        return _answer(Proposal(action="escalate", sql="", reason="stand-in: always asks for a person"))
    if name == "oracle":
        fix = gold.repair_sql(case.drifts[0])
        if case.expected == "repair" and fix:
            return _answer(Proposal(action="repair", sql=fix, reason="stand-in: the hand-written correct repair"))
        if case.expected == "noop":
            return _answer(Proposal(action="no_change", sql="", reason="stand-in: nothing to fix"))
        return _answer(Proposal(action="escalate", sql="", reason="stand-in: the information is gone from the feed"))
    raise ValueError(f"unknown stand-in {name!r}; choose from {NAMES}")
