"""The flows cannot be run without Docker and Kestra, so what can be checked without them is checked here: that they parse, that they
use only the task types and properties verified against Kestra v2.0.10's source, and that every output a flow reads is one the
command really prints."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import mend_ops as ops
from ops_helpers import Scripted, clone, patch_answer

ORCH = Path(__file__).resolve().parents[1]
FLOWS = sorted((ORCH / "flows").glob("*.yml"))
# task types and the properties each may use, taken from Kestra v2.0.10 (core and plugin-scripts source); see orchestration/README.md
COMMON = {"id", "type", "description", "timeout", "disabled", "retry"}
TASKS = {
    "io.kestra.plugin.scripts.shell.Commands": COMMON | {"containerImage", "taskRunner", "commands", "env", "warningOnStdErr"},
    "io.kestra.plugin.core.log.Log": COMMON | {"message", "level"},
    "io.kestra.plugin.core.flow.If": COMMON | {"condition", "then", "else"},
    "io.kestra.plugin.core.flow.Pause": COMMON | {"onResume", "pauseDuration", "behavior"},
    "io.kestra.plugin.core.flow.Subflow": COMMON | {"namespace", "flowId", "inputs", "wait"},
    "io.kestra.plugin.core.execution.Fail": COMMON | {"errorMessage", "condition"},
}
RUNNER = {"type", "pullPolicy", "volumes"}
INPUT_TYPES = {"BOOL", "INT", "STRING", "SELECT"}
FLOW_KEYS = {"id", "namespace", "description", "labels", "inputs", "tasks", "triggers", "errors", "concurrency"}


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def walk(tasks):
    for t in tasks:
        yield t
        for key in ("then", "else"):
            yield from walk(t.get(key, []))


def all_tasks(flow):
    return list(walk(flow.get("tasks", []))) + list(walk(flow.get("errors", [])))


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.name)
def test_a_flow_uses_only_verified_types_and_properties(path):
    flow = load(path)
    assert set(flow) <= FLOW_KEYS, set(flow) - FLOW_KEYS
    assert flow["namespace"] == "mend" and flow["id"] == path.stem
    for task in all_tasks(flow):
        assert task["type"] in TASKS, f"{task['id']}: unverified task type {task['type']}"
        assert set(task) <= TASKS[task["type"]], f"{task['id']}: unverified properties {set(task) - TASKS[task['type']]}"
    for inp in flow.get("inputs", []):
        assert inp["type"] in INPUT_TYPES, inp
    for pause in (t for t in all_tasks(flow) if t["type"].endswith(".Pause")):
        assert {i["type"] for i in pause["onResume"]} <= INPUT_TYPES and "timeout" in pause     # an unanswered approval must end


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.name)
def test_every_step_runs_the_local_image_and_never_pulls(path):
    for task in (t for t in all_tasks(load(path)) if t["type"].endswith("shell.Commands")):
        assert task["containerImage"] == "mend-ops:local", task["id"]
        runner = task["taskRunner"]
        assert set(runner) <= RUNNER and runner["pullPolicy"] == "NEVER" and runner["volumes"] == ["mend_home:/mend_home"], task["id"]
        assert all(c.split()[0] == "mend-ops" and " --kestra " in c for c in task["commands"]), task["commands"]


def test_ids_are_unique_within_each_flow():
    for path in FLOWS:
        ids = [t["id"] for t in all_tasks(load(path))]
        assert len(ids) == len(set(ids)), (path.name, ids)


def test_only_the_repair_step_gets_the_model_key():
    for path in FLOWS:
        for task in (t for t in all_tasks(load(path)) if t["type"].endswith("shell.Commands")):
            has_key = "GOOGLE_API_KEY" in (task.get("env") or {})
            assert has_key == (task["id"] == "propose"), (path.name, task["id"])


def test_the_daily_schedule_is_defined_but_off():
    (trigger,) = load(ORCH / "flows" / "daily.yml")["triggers"]
    assert trigger["type"].endswith("trigger.Schedule") and trigger["disabled"] is True and trigger["timezone"] == "Asia/Kolkata"


def test_the_daily_flow_starts_the_repair_flow_without_waiting_for_it():
    (start,) = [t for t in all_tasks(load(ORCH / "flows" / "daily.yml")) if t["type"].endswith("Subflow")]
    assert (start["namespace"], start["flowId"], start["wait"]) == ("mend", "repair", False)
    assert "repair" in {p.stem for p in FLOWS}


def test_the_compose_file_builds_a_pinned_slim_kestra_and_publishes_nothing_but_the_ui_on_localhost():
    compose = yaml.safe_load((ORCH / "docker-compose.yml").read_text(encoding="utf-8"))
    kestra = compose["services"]["kestra"]
    assert kestra["image"] == "mend-kestra:local" and kestra["build"]["context"] == "./kestra"
    dockerfile = (ORCH / "kestra" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"(?m)^FROM kestra/kestra:v\d+\.\d+\.\d+-slim$", dockerfile), "pin an exact version, not latest"
    assert "/app/kestra plugins install io.kestra.plugin:plugin-script-shell:LATEST" in dockerfile   # the one plugin the flows need
    assert (ORCH / "kestra").iterdir() and [p.name for p in (ORCH / "kestra").iterdir()] == ["Dockerfile"]   # nothing else (.env) in the build context
    assert kestra["ports"] == ["127.0.0.1:8080:8080"]
    assert "postgres" not in str(compose["services"]["postgres"].get("ports", ""))
    assert compose["volumes"]["mend_home"]["name"] == "mend_home"


def test_every_task_type_the_flows_use_comes_from_kestra_itself_or_the_shell_plugin():
    """The slim image has no plugins except the one installed in kestra/Dockerfile."""
    used = {t["type"] for path in FLOWS for t in all_tasks(load(path))}
    triggers = {t["type"] for path in FLOWS for t in load(path).get("triggers", [])}
    assert {u for u in used | triggers if not u.startswith("io.kestra.plugin.core.")} == {"io.kestra.plugin.scripts.shell.Commands"}


def test_no_secret_is_committed_in_orchestration():
    for path in ORCH.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".yml", ".md", ".example", ".txt", ""} and ".env" != path.name and "__pycache__" not in str(path):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", text), path
            assert not re.search(r"(?m)^SECRET_GOOGLE_API_KEY=\S+", text), path


# ---- the contract between the flows and the command line ---------------------------------------------------------------------

def referenced_outputs():
    """{(task id, command word): {output keys the flows read from that task}}"""
    found: dict[str, set[str]] = {}
    for path in FLOWS:
        flow = load(path)
        text = path.read_text(encoding="utf-8")
        for task in all_tasks(flow):
            if task["type"].endswith("shell.Commands"):
                command = task["commands"][0].split("--kestra ", 1)[1].split()[0]
                keys = set(re.findall(rf"outputs\.{task['id']}\.vars\.(\w+)", text))
                found.setdefault(command, set()).update(keys)
    return found


def keys(res: dict) -> set[str]:
    return set(ops._outputs(res))


def test_every_output_a_flow_reads_is_printed_by_its_command(healthy_template, tmp_path):
    calm, rough = clone(healthy_template, tmp_path / "calm"), clone(healthy_template, tmp_path / "rough")
    seen: dict[str, list[set[str]]] = {"build": [], "repair": [], "apply": [], "ingest": [], "source-change": []}
    seen["build"].append(keys(ops.build(calm)))                                                               # healthy
    seen["repair"].append(keys(ops.repair(calm, proposer=Scripted(("escalate", "", "never asked")))))        # nothing to repair
    seen["source-change"].append(keys(ops.source_change(calm, ["drop_city"])))
    seen["ingest"].append(keys(ops.ingest(calm)))
    seen["repair"].append(keys(ops.repair(calm, proposer=Scripted(("escalate", "", "a person must look")))))  # escalation

    ops.source_change(rough, ["rename_amount"])
    ops.ingest(rough, days=2)
    seen["build"].append(keys(ops.build(rough)))                                                              # drift
    asked = patch_answer(rough)
    seen["repair"].append(keys(ops.repair(rough, proposer=asked)))                                            # a patch
    seen["repair"].append(keys(ops.repair(rough, proposer=asked)))                                            # the same one, pending
    seen["apply"].append(keys(ops.apply(rough, "r0001")))
    for command, wanted in referenced_outputs().items():
        for printed in seen.get(command, []):
            assert wanted <= printed, f"mend-ops {command}: flows read {sorted(wanted - printed)} but it printed only {sorted(printed)}"
    assert all(seen[c] for c in seen)


def test_bootstrap_prints_what_its_flow_reads(tmp_path, capsys):
    """bootstrap is the slowest command (it builds a home); it is run once here through the real command line."""
    assert ops.main(["--home", str(tmp_path / "h"), "--kestra", "bootstrap", "--days", "2"]) == 0
    printed = __import__("json").loads(re.search(r"::(\{.*})::", capsys.readouterr().out).group(1))["outputs"]
    assert referenced_outputs()["bootstrap"] <= set(printed)
