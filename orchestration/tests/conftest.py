"""Fixtures for the ops tests. Building a healthy home takes seconds, so one is built per session and copied for each test."""
from __future__ import annotations

from pathlib import Path

import pytest

import mend_ops as ops
from ops_helpers import clone, patch_answer


@pytest.fixture(scope="session")
def healthy_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("healthy") / "home"
    home = ops.Home.at(path)
    ops.init(home, reset=True)
    ops.ingest(home, days=ops.BOOTSTRAP_DAYS)
    assert ops.build(home)["status"] == "healthy"
    return path


@pytest.fixture(scope="session")
def repaired_template(healthy_template, tmp_path_factory) -> Path:
    """A pipeline whose source renamed amount, whose repair a person approved, and which is healthy again."""
    home = clone(healthy_template, tmp_path_factory.mktemp("repaired") / "home")
    ops.source_change(home, ["rename_amount"])
    ops.ingest(home, days=2)
    assert ops.build(home)["status"] == "drift"
    res = ops.repair(home, proposer=patch_answer(home))
    assert res["outcome"] == "patch"
    ops.apply(home, res["repair_id"], by="fixture")
    return home.root


@pytest.fixture
def healthy(healthy_template, tmp_path) -> ops.Home:
    return clone(healthy_template, tmp_path / "home")


@pytest.fixture
def repaired(repaired_template, tmp_path) -> ops.Home:
    return clone(repaired_template, tmp_path / "home")


@pytest.fixture
def drifted(healthy) -> ops.Home:
    """Healthy history, then the source renames amount and two daily loads arrive."""
    ops.source_change(healthy, ["rename_amount"])
    ops.ingest(healthy, days=2)
    return healthy
