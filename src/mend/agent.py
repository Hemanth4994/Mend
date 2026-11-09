"""The Mend repair agent: a small LangGraph graph that decides what to do when a feed changes shape.

    gather -> propose -> check -+-> finish_patch      (repair that passed the repair gate)
                 ^              +-> finish_no_change  (the current model is still right, and nothing contradicts that)
                 |              +-> finish_escalate   (the model asked for a person, or the gate rejected every attempt)
                 +--- rejected, attempts left (one repair) ---+

The agent never touches the real project. It returns SQL; applying it (or asking a person first) is the orchestrator's job, decided by
a policy on how often gate-passing patches in that kind of change were wrong. The gate is the same code a production pipeline would
run (mend.validate) and never uses the answer key.

Three configurations differ ONLY in how much evidence is put in front of the model (mend.evidence): schema, evidence, notes.
The gate always sees everything, whatever the configuration, because it judges the patch, not the model.

The model is called at most MAX_ATTEMPTS times per case. It is injected as a plain function, so the whole graph is tested with a
scripted fake model and no API key.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from . import evidence as ev_mod
from . import sandbox, validate
from .feed import DRIFT_DAY

CONFIGS = ev_mod.CONFIGS
MAX_ATTEMPTS = 2                     # one proposal and one repair of it, then a person decides
DEFAULT_MODEL = "gemini-3.5-flash-lite"

SYSTEM_PROMPT = """You maintain a data pipeline. A daily feed of orders is loaded into the DuckDB table raw.orders (one load per day,
oldest first). A dbt staging model, stg_orders, turns raw.orders into a clean table with a fixed set of columns. The source team
sometimes changes how the feed is written without warning. Your job is to decide whether the staging model has to change and, if it
does, to rewrite it.

A change in how orders are written never changes what the orders are. After a correct repair, the new loads come out in exactly the
same form as the old loads.

How the loader (dlt) writes a change into raw.orders:
- A renamed field becomes a new column; the old column is simply empty for the new rows.
- A nested object is flattened into columns named parent__child.
- When a column's type changes, the new values go into a side column named <column>__v_<type> and the original column is empty for
  those rows.
- A column that disappears from the feed stays in the table, empty for the new rows.

Rules for a repaired model:
- One SELECT statement (a WITH clause is fine) that reads only {{ source('raw', 'orders') }}. Keep that source() reference exactly.
- Output exactly the contract columns with the contract types. No SELECT *.
- Loads from before the change must come out exactly as they do now. Where old rows and new rows use different columns, combine
  them, for example coalesce(old_column, new_column). Where a value changed meaning inside a column that kept its name, convert only
  the loads where it changed, using a date that you can read off the evidence.
- Never invent a value. If a contract column's information is not in the raw table at all any more, no correct repair exists.

Choose exactly one action:
- repair: put the complete new model in sql.
- no_change: the current model is still correct. A new column that the contract does not use is not a reason to change the model.
- escalate: a person is needed, because information is missing or you are not sure a fix is safe. Say what the person should look at.

If an earlier answer of yours is shown as rejected, the reasons come from automatic checks that run the model. Fix exactly those problems."""

CONTRACT_TEXT = """Output contract of stg_orders (exactly these columns), enforced by dbt tests:
  order_id     text, unique, not null
  customer_id  text, not null
  placed_at    timestamp with time zone, in UTC, not null, between 2026-01-01 and 2026-12-31
  amount       decimal(12,2) in rupees, not null, between 1 and 100000
  currency     text, for example INR
  status       one of placed, shipped, delivered, cancelled; not null
  city         text
  state        text
  channel      text, one of web, app, store
  item_count   integer"""


class Proposal(BaseModel):
    action: Literal["repair", "no_change", "escalate"] = Field(description="repair, no_change or escalate")
    sql: str = Field(description="The complete new staging model when action is repair; otherwise an empty string")
    reason: str = Field(description="One or two sentences: what changed in the feed and why this action. For escalate: what a person should look at")


@dataclass
class ProposeResult:
    proposal: Proposal | None
    error: str = ""                  # why there is no proposal (the model returned something unparseable)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""                  # the model that actually answered, as reported by the API


Proposer = Callable[[list[tuple[str, str]]], ProposeResult]


class State(TypedDict, total=False):
    db: str
    work: str
    config: str
    notes: str
    history_loads: int
    evidence: ev_mod.Evidence
    baseline: str
    context: str
    attempts: int
    input_tokens: int
    output_tokens: int
    models: list[str]
    proposal: dict | None
    error: str
    passed: bool
    problems: list[str]
    checks: dict
    rejected: list[dict]
    outcome: str                     # patch | no_change | escalate
    sql: str | None                  # the patch, only when outcome is patch and the gate passed it
    proposed_sql: str | None         # a rejected attempt, kept for the person who gets the escalation
    reason: str
    steps: list[dict]


def build_messages(context: str, baseline: str, rejected: list[dict]) -> list[tuple[str, str]]:
    parts = [CONTRACT_TEXT, f"Current staging model (models/staging/stg_orders.sql):\n{baseline.strip()}", context]
    for i, r in enumerate(rejected, start=1):
        shown = f"Action: {r['action']}\n" + (f"SQL:\n{r['sql'].strip()}\n" if r["sql"].strip() else "")
        parts.append(f"Your answer number {i} was rejected by the automatic checks.\n{shown}Problems:\n" +
                     "\n".join(f"- {p}" for p in r["problems"]) + "\nGive a corrected answer.")
    return [("system", SYSTEM_PROMPT), ("human", "\n\n".join(parts))]


def build_agent(proposer: Proposer, max_attempts: int = MAX_ATTEMPTS):
    def gather(state: State) -> State:
        if state["config"] not in CONFIGS:
            raise ValueError(f"config must be one of {CONFIGS}")
        ev = ev_mod.build_evidence(state["db"], Path(state["work"]) / "evidence")
        return {"evidence": ev, "baseline": sandbox.baseline_sql(), "context": ev_mod.render(ev, state["config"], state.get("notes", "")),
                "attempts": 0, "input_tokens": 0, "output_tokens": 0, "models": [], "proposal": None, "error": "", "passed": False,
                "problems": [], "checks": {}, "rejected": [], "outcome": "", "sql": None, "proposed_sql": None, "reason": "",
                "steps": [{"node": "gather", "failing": ev.failing, "schema_signals": ev.signals}]}

    def propose(state: State) -> State:
        res = proposer(build_messages(state["context"], state["baseline"], state["rejected"]))
        out: State = {"attempts": state["attempts"] + 1, "input_tokens": state["input_tokens"] + res.input_tokens,
                      "output_tokens": state["output_tokens"] + res.output_tokens,
                      "models": state["models"] + ([res.model] if res.model else [])}
        step = {"node": "propose", "attempt": out["attempts"], "model": res.model}
        if res.proposal is None:
            out.update(proposal=None, error=f"the reply was not a valid answer ({res.error or 'no structured output'})")
            step["error"] = out["error"]
        else:
            out.update(proposal=res.proposal.model_dump(), error="")
            step.update(action=res.proposal.action, reason=res.proposal.reason)
        out["steps"] = state["steps"] + [step]
        return out

    def check(state: State) -> State:
        """Judge the proposal with the production gate. A model's word is never enough to change the pipeline."""
        p, ev = state["proposal"], state["evidence"]
        checks: dict = {}
        if p is None:
            problems = [state["error"]]
        elif p["action"] == "repair":
            if not p["sql"].strip():
                problems = ["the action was repair but sql was empty"]
            else:
                v = validate.validate_patch(state["db"], p["sql"], state["baseline"], Path(state["work"]) / f"attempt{state['attempts']}",
                                            state.get("history_loads", DRIFT_DAY))
                problems, checks = v.problems, v.checks
        elif p["action"] == "no_change":
            v = validate.validate_no_change(ev.failing, ev.signals["went_null"])
            problems, checks = v.problems, v.checks
        else:                                                   # escalate: asking for a person is always allowed
            problems = []
        passed = not problems
        out: State = {"passed": passed, "problems": problems, "checks": checks}
        if not passed:
            out["rejected"] = state["rejected"] + [{"action": p["action"] if p else "none", "sql": (p or {}).get("sql", ""), "problems": problems}]
        out["steps"] = state["steps"] + [{"node": "check", "attempt": state["attempts"], "passed": passed, "checks": checks, "problems": problems}]
        return out

    def finish_patch(state: State) -> State:
        return {"outcome": "patch", "sql": state["proposal"]["sql"].strip() + "\n", "reason": state["proposal"]["reason"]}

    def finish_no_change(state: State) -> State:
        return {"outcome": "no_change", "sql": None, "reason": state["proposal"]["reason"]}

    def finish_escalate(state: State) -> State:
        if state["passed"]:                                     # the model itself asked for a person
            return {"outcome": "escalate", "sql": None, "proposed_sql": None, "reason": state["proposal"]["reason"]}
        last = state["rejected"][-1]
        reason = (f"The automatic checks rejected the proposal after {state['attempts']} attempt(s): " + "; ".join(last["problems"]))
        return {"outcome": "escalate", "sql": None, "reason": reason,
                "proposed_sql": last["sql"].strip() + "\n" if last["action"] == "repair" and last["sql"].strip() else None}

    def after_check(state: State) -> str:
        if state["passed"]:
            return {"repair": "finish_patch", "no_change": "finish_no_change", "escalate": "finish_escalate"}[state["proposal"]["action"]]
        return "propose" if state["attempts"] < max_attempts else "finish_escalate"

    g = StateGraph(State)
    for name, fn in (("gather", gather), ("propose", propose), ("check", check), ("finish_patch", finish_patch),
                     ("finish_no_change", finish_no_change), ("finish_escalate", finish_escalate)):
        g.add_node(name, fn)
    g.add_edge(START, "gather")
    g.add_edge("gather", "propose")
    g.add_edge("propose", "check")
    g.add_conditional_edges("check", after_check, {"finish_patch": "finish_patch", "finish_no_change": "finish_no_change",
                                                   "finish_escalate": "finish_escalate", "propose": "propose"})
    for end in ("finish_patch", "finish_no_change", "finish_escalate"):
        g.add_edge(end, END)
    return g.compile()


def run_case(agent, db: str | Path, work: str | Path, config: str = "evidence", notes: str = "", case_id: str = "", split: str = "",
             model: str = "", history_loads: int = DRIFT_DAY) -> dict:
    """Run the agent on one raw database and return the final state plus the wall-clock time. Metadata makes the LangSmith trace searchable."""
    start = time.perf_counter()
    final = agent.invoke({"db": str(db), "work": str(work), "config": config, "notes": notes, "history_loads": history_loads},
                         config={"run_name": f"mend:{case_id or 'adhoc'}", "tags": ["mend", config] + ([split] if split else []),
                                 "metadata": {"case_id": case_id, "config": config, "split": split, "model": model}})
    final["latency_s"] = round(time.perf_counter() - start, 3)
    final["model_calls"] = final["attempts"]
    return final


def gemini_proposer(model: str | None = None, throttle=None) -> Proposer:
    """The real model. Needs GOOGLE_API_KEY. The returned function carries .model, the id that was requested."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = model or os.environ.get("MEND_MODEL", DEFAULT_MODEL)
    llm = ChatGoogleGenerativeAI(model=model).with_structured_output(Proposal, include_raw=True)

    def proposer(messages: list[tuple[str, str]]) -> ProposeResult:
        out = throttle.call(lambda: llm.invoke(messages)) if throttle else llm.invoke(messages)
        raw, parsed, err = out.get("raw"), out.get("parsed"), out.get("parsing_error")
        usage = getattr(raw, "usage_metadata", None) or {}
        meta = getattr(raw, "response_metadata", None) or {}
        return ProposeResult(proposal=parsed, error=str(err) if err else ("" if parsed else "no structured output"),
                             input_tokens=int(usage.get("input_tokens", 0)), output_tokens=int(usage.get("output_tokens", 0)),
                             model=str(meta.get("model_version") or meta.get("model_name") or model))

    proposer.model = model        # type: ignore[attr-defined]
    return proposer


def main() -> None:
    import argparse
    import tempfile

    from .cases import load_cases
    from .throttle import Throttle

    ap = argparse.ArgumentParser(description="Run the repair agent on one evaluation case (needs GOOGLE_API_KEY).")
    ap.add_argument("case", help="a case id from eval/cases.json, for example s01")
    ap.add_argument("--config", choices=CONFIGS, default="evidence")
    ap.add_argument("--cache", default=".mend_cache", help="where the raw databases are built (reused between runs)")
    args = ap.parse_args()
    case = {c.id: c for c in load_cases()}[args.case]
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    db = sandbox.build_case(case, cache)
    proposer = gemini_proposer(throttle=Throttle())
    with tempfile.TemporaryDirectory() as work:
        final = run_case(build_agent(proposer), db, work, args.config, case.notes, case.id, case.split, proposer.model)
    print(f"{case.id} ({case.group}): outcome={final['outcome']}\nreason: {final['reason']}")
    if final["sql"]:
        print("\npatch that passed the gate:\n" + final["sql"])
    elif final.get("proposed_sql"):
        print("\nrejected proposal, kept for a person:\n" + final["proposed_sql"])
    print(f"\nmodel_calls={final['model_calls']} tokens={final['input_tokens']}+{final['output_tokens']} latency={final['latency_s']}s "
          f"model={','.join(sorted(set(final['models']))) or proposer.model}")


if __name__ == "__main__":
    main()
