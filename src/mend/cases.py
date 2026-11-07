"""The 60 evaluation cases: which drifts hit the pipeline, what the right response is, and which split the case belongs to.

The case list is generated once by scripts/make_cases.py and stored in eval/cases.json, so it can be frozen like Gloss's questions.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .drifts import DRIFTS, notes_for

ROOT = Path(__file__).resolve().parents[2]
CASES_FILE = ROOT / "eval" / "cases.json"

GROUPS: dict[str, tuple[str, list[str]]] = {
    # group: (prefix, drift ids). The right response for the group is in EXPECTED.
    "structural": ("s", ["rename_amount", "rename_order_id", "rename_placed_at", "rename_customer_id", "rename_status", "rename_channel",
                         "rename_item_count", "nest_customer_id", "nest_shipping", "nest_amount", "nest_customer_full",
                         "pack_comma", "pack_pipe", "pack_dash"]),
    "value": ("v", ["text_amount_rupee", "text_amount_comma", "paise", "ts_epoch_ms", "ts_dmy", "ts_ist_naive", "status_letters",
                    "status_ints", "status_upper", "status_us_spelling", "status_renamed", "status_prefixed"]),
    "compound": ("m", ["rename_paise", "nest_customer_letters", "pack_dmy", "created_ist", "text_rename", "nest_shipping_upper",
                       "order_status_ints", "nest_amount_paise", "two_renames", "pack_pipe_ints"]),
    "control": ("c", ["add_coupon", "add_device", "add_tier", "add_discount", "add_meta", "add_tags", "add_amount_tax",
                      "add_status_detail", "case_order_id", "case_amount_placed", "ts_epoch_s", "ts_offset",
                      "text_amount_plain", "text_item_count"]),
    "unfixable": ("u", ["drop_order_id", "drop_customer_id", "drop_placed_at", "drop_amount", "drop_status", "drop_city", "drop_state",
                        "drop_channel", "drop_item_count", "drop_city_state"]),
}
EXPECTED = {"structural": "repair", "value": "repair", "compound": "repair", "control": "noop", "unfixable": "escalate"}


@dataclass(frozen=True)
class Case:
    id: str
    split: str            # dev | test
    group: str
    drifts: tuple[str, ...]
    expected: str         # repair | noop | escalate
    notes: str            # the source team's release note for this change

    @property
    def summary(self) -> str:
        return "; ".join(DRIFTS[d].summary for d in self.drifts)


def generate() -> list[Case]:
    """Within each group the cases alternate dev, test, dev, test, ..., so each split gets half of every kind."""
    out = []
    for group, (prefix, ids) in GROUPS.items():
        for k, drift_id in enumerate(ids, start=1):
            out.append(Case(f"{prefix}{k:02d}", "dev" if k % 2 == 1 else "test", group, (drift_id,), EXPECTED[group], notes_for([drift_id])))
    return out


def write_cases(path: Path = CASES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump([{**asdict(c), "drifts": list(c.drifts)} for c in generate()], f, indent=1, ensure_ascii=False)
        f.write("\n")


def load_cases(split: str | None = None, path: Path = CASES_FILE) -> list[Case]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cases = [Case(**{**c, "drifts": tuple(c["drifts"])}) for c in raw]
    return [c for c in cases if split in (None, c.split)]
