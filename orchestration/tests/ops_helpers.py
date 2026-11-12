"""Helpers shared by the ops tests: scripted model answers and copies of a prepared home."""
from __future__ import annotations

import shutil
from pathlib import Path

import mend_ops as ops

RENAME_AMOUNT_FIX = ("cast(coalesce(amount, total_amount) as decimal(12, 2)) as amount", "cast(amount as decimal(12, 2)) as amount")


class Scripted:
    """A proposer that gives the answers it was told to give, and remembers how often it was asked."""

    def __init__(self, *answers: tuple[str, str, str]):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, messages):
        from mend.agent import Proposal, ProposeResult
        action, sql, reason = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return ProposeResult(proposal=Proposal(action=action, sql=sql, reason=reason), model="scripted")


def fixed_model(home: ops.Home) -> str:
    """The live staging model with the rename of amount handled."""
    new, old = RENAME_AMOUNT_FIX
    return home.model.read_text(encoding="utf-8").replace(old, new)


def patch_answer(home: ops.Home) -> Scripted:
    return Scripted(("repair", fixed_model(home), "total_amount replaced amount; fall back to it."))


def clone(src: Path, dest: Path) -> ops.Home:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".lock"))
    return ops.Home.at(dest)
