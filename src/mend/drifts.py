"""The catalog of schema drifts a source system can introduce, and the release note that would describe each one.

A drift rewrites one raw order record (never the data's meaning). Several drifts can be composed. Each drift also carries the
note the source team would have published; the agent only sees it in the 'notes' configuration, the way Gloss only sees
definitions in its 'definitions' configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

Row = dict
Fn = Callable[[Row], Row]

IST = timedelta(hours=5, minutes=30)
STATUS_LETTERS = {"placed": "P", "shipped": "S", "delivered": "D", "cancelled": "C"}
STATUS_INTS = {"placed": 0, "shipped": 1, "delivered": 2, "cancelled": 3}
STATUS_RENAMED = {"placed": "created", "shipped": "shipped", "delivered": "fulfilled", "cancelled": "cancelled"}


@dataclass(frozen=True)
class Drift:
    id: str
    summary: str
    fn: Fn
    note: str = ""


DRIFTS: dict[str, Drift] = {}


def _reg(drift: Drift) -> Drift:
    assert drift.id not in DRIFTS, drift.id
    DRIFTS[drift.id] = drift
    return drift


def _utc(r: Row, key: str = "placed_at") -> datetime:
    return datetime.strptime(r[key], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---- building blocks -------------------------------------------------------------------------------------------------

def rename(old: str, new: str) -> Fn:
    return lambda r: {(new if k == old else k): v for k, v in r.items()}


def nest(parent: str, mapping: dict[str, str]) -> Fn:
    def f(r: Row) -> Row:
        r = dict(r)
        inner = {new: r.pop(old) for old, new in mapping.items()}
        r[parent] = inner
        return r
    return f


def pack_address(delim: str) -> Fn:
    def f(r: Row) -> Row:
        r = dict(r)
        r["shipping_address"] = f"{r.pop('city')}{delim}{r.pop('state')}"
        return r
    return f


def amount_text(style: str, key: str = "amount") -> Fn:
    fmt = {"plain": lambda a: f"{a:.2f}", "rupee": lambda a: f"₹{a:,.2f}", "comma": lambda a: f"{a:,.2f}"}[style]
    return lambda r: {**r, key: fmt(r[key])}


def item_text(r: Row) -> Row:
    return {**r, "item_count": str(r["item_count"])}


def paise(key: str = "amount", new: str | None = None) -> Fn:
    def f(r: Row) -> Row:
        r = dict(r)
        value = int(round(r.pop(key) * 100))
        r[new or key] = value
        return r
    return f


def amount_object(paise_units: bool = False) -> Fn:
    def f(r: Row) -> Row:
        r = dict(r)
        value = r.pop("amount")
        r["amount"] = {"value": int(round(value * 100)) if paise_units else value, "currency": r.pop("currency")}
        return r
    return f


def timestamp(style: str, key: str = "placed_at") -> Fn:
    fmt: dict[str, Callable[[datetime], object]] = {
        "epoch_s": lambda d: int(d.timestamp()),
        "epoch_ms": lambda d: int(d.timestamp() * 1000),
        "dmy": lambda d: d.strftime("%d/%m/%Y %H:%M:%S"),
        "ist_naive": lambda d: (d + IST).strftime("%Y-%m-%d %H:%M:%S"),
        "offset": lambda d: (d + IST).strftime("%Y-%m-%dT%H:%M:%S+05:30"),
    }[style]
    return lambda r: {**r, key: fmt(_utc(r, key))}


def status_map(fn: Callable[[str], object], key: str = "status") -> Fn:
    return lambda r: {**r, key: fn(r[key])}


def add(name: str, value: Callable[[Row], object]) -> Fn:
    return lambda r: {**r, name: value(r)}


def drop(*cols: str) -> Fn:
    return lambda r: {k: v for k, v in r.items() if k not in cols}


def compose(*fns: Fn) -> Fn:
    def f(r: Row) -> Row:
        for fn in fns:
            r = fn(r)
        return r
    return f


def _n(r: Row) -> int:
    return int(r["order_id"].split("-")[1])


# ---- structural: names and shapes change, the values do not ----------------------------------------------------------
for old, new in [("amount", "total_amount"), ("order_id", "id"), ("placed_at", "created_at"), ("customer_id", "cust_id"),
                 ("status", "order_status"), ("channel", "sales_channel"), ("item_count", "qty")]:
    _reg(Drift(f"rename_{old}", f"`{old}` is renamed `{new}`", rename(old, new), f"orders v2: field `{old}` renamed to `{new}`."))

_reg(Drift("nest_customer_id", "customer_id moves into a `customer` object", nest("customer", {"customer_id": "id"}),
           "orders v2: `customer_id` is now `customer.id`."))
_reg(Drift("nest_shipping", "city and state move into a `shipping` object", nest("shipping", {"city": "city", "state": "state"}),
           "orders v2: `city` and `state` are now under `shipping`."))
_reg(Drift("nest_amount", "amount and currency move into an `amount` object", amount_object(),
           "orders v2: `amount` is now an object with `value` and `currency`; the top-level `currency` field is gone."))
_reg(Drift("nest_customer_full", "customer id, city and state move into a `customer` object",
           nest("customer", {"customer_id": "id", "city": "city", "state": "state"}),
           "orders v2: customer fields are grouped under `customer` (`id`, `city`, `state`)."))
for tag, delim in [("comma", ", "), ("pipe", "|"), ("dash", " - ")]:
    _reg(Drift(f"pack_{tag}", f"city and state are packed into `shipping_address` with '{delim}'", pack_address(delim),
               f"orders v2: `city` and `state` replaced by one field, `shipping_address` (\"city{delim}state\")."))

# ---- value: the same column now carries a different representation ---------------------------------------------------
_reg(Drift("text_amount_plain", "amount arrives as a plain decimal string", amount_text("plain"), "orders v2: `amount` is now sent as a string, for example \"1249.50\"."))
_reg(Drift("text_amount_rupee", "amount arrives as a formatted rupee string", amount_text("rupee"), "orders v2: `amount` is now a display string, for example \"₹1,249.50\"."))
_reg(Drift("text_amount_comma", "amount arrives with thousands separators", amount_text("comma"), "orders v2: `amount` is now sent with thousands separators, for example \"1,249.50\"."))
_reg(Drift("text_item_count", "item_count arrives as a string", item_text, "orders v2: `item_count` is now sent as a string."))
_reg(Drift("paise", "amount is now an integer number of paise", paise(), "orders v2: `amount` is now an integer in paise (1/100 rupee), not rupees."))
_reg(Drift("ts_epoch_ms", "placed_at is epoch milliseconds", timestamp("epoch_ms"), "orders v2: `placed_at` is now a Unix epoch in milliseconds."))
_reg(Drift("ts_dmy", "placed_at is a day-first string", timestamp("dmy"), "orders v2: `placed_at` is now formatted DD/MM/YYYY HH:MM:SS (UTC)."))
_reg(Drift("ts_ist_naive", "placed_at is local time without an offset", timestamp("ist_naive"),
           "orders v2: `placed_at` is now local time in India (IST, UTC+05:30) with no offset in the text."))
_reg(Drift("status_letters", "status is a one-letter code", status_map(STATUS_LETTERS.get), "orders v2: `status` is now a one-letter code: P placed, S shipped, D delivered, C cancelled."))
_reg(Drift("status_ints", "status is a number", status_map(STATUS_INTS.get), "orders v2: `status` is now a code: 0 placed, 1 shipped, 2 delivered, 3 cancelled."))
_reg(Drift("status_upper", "status is upper case", status_map(str.upper), "orders v2: `status` values are now upper case."))
_reg(Drift("status_us_spelling", "cancelled is spelled canceled", status_map(lambda s: "canceled" if s == "cancelled" else s),
           "orders v2: spelling aligned to US English (`canceled`)."))
_reg(Drift("status_renamed", "two status values are renamed", status_map(STATUS_RENAMED.get),
           "orders v2: status `placed` is now `created` and `delivered` is now `fulfilled`."))
_reg(Drift("status_prefixed", "status values get an ORDER_ prefix", status_map(lambda s: "ORDER_" + s.upper()), "orders v2: status values are now `ORDER_<STATUS>` in upper case."))

# ---- compound: two changes in the same release ----------------------------------------------------------------------
_reg(Drift("rename_paise", "amount becomes amount_paise (integer paise)", paise(new="amount_paise"),
           "orders v2: `amount` replaced by `amount_paise`, an integer in paise (1/100 rupee)."))
_reg(Drift("nest_customer_letters", "customer object plus one-letter status",
           compose(nest("customer", {"customer_id": "id", "city": "city", "state": "state"}), status_map(STATUS_LETTERS.get)),
           "orders v2: customer fields are grouped under `customer`; `status` is now P/S/D/C (placed, shipped, delivered, cancelled)."))
_reg(Drift("pack_dmy", "packed address plus day-first timestamp", compose(pack_address(", "), timestamp("dmy")),
           "orders v2: `shipping_address` (\"city, state\") replaces `city` and `state`; `placed_at` is DD/MM/YYYY HH:MM:SS (UTC)."))
_reg(Drift("created_ist", "placed_at renamed created_at and sent as IST local time",
           compose(rename("placed_at", "created_at"), timestamp("ist_naive", "created_at")),
           "orders v2: `placed_at` renamed `created_at`; it is local time in India (IST, UTC+05:30) with no offset in the text."))
_reg(Drift("text_rename", "total_amount arrives as a rupee string",
           compose(rename("amount", "total_amount"), amount_text("rupee", "total_amount")),
           "orders v2: `amount` renamed `total_amount` and sent as a display string such as \"₹1,249.50\"."))
_reg(Drift("nest_shipping_upper", "shipping object plus upper-case status",
           compose(nest("shipping", {"city": "city", "state": "state"}), status_map(str.upper)),
           "orders v2: `city` and `state` are under `shipping`; `status` values are upper case."))
_reg(Drift("order_status_ints", "status renamed order_status and numeric",
           compose(rename("status", "order_status"), status_map(STATUS_INTS.get, "order_status")),
           "orders v2: `status` renamed `order_status`; codes are 0 placed, 1 shipped, 2 delivered, 3 cancelled."))
_reg(Drift("nest_amount_paise", "amount object in paise", amount_object(paise_units=True),
           "orders v2: `amount` is an object: `value` is an integer in paise (1/100 rupee) and `currency` is inside it; top-level `currency` is gone."))
_reg(Drift("two_renames", "customer_id and order_id both renamed", compose(rename("customer_id", "cust_id"), rename("order_id", "id")),
           "orders v2: `customer_id` renamed `cust_id`; `order_id` renamed `id`."))
_reg(Drift("pack_pipe_ints", "pipe-packed address plus numeric status", compose(pack_address("|"), status_map(STATUS_INTS.get)),
           "orders v2: `shipping_address` (\"city|state\") replaces `city` and `state`; `status` codes are 0 placed, 1 shipped, 2 delivered, 3 cancelled."))

# ---- controls: changes that need no repair ---------------------------------------------------------------------------
_reg(Drift("add_coupon", "optional coupon_code added", add("coupon_code", lambda r: f"SAVE{_n(r) % 5}" if _n(r) % 3 == 0 else None), "orders v2: new optional field `coupon_code`."))
_reg(Drift("add_device", "device_type added", add("device_type", lambda r: ["ios", "android", "desktop"][_n(r) % 3]), "orders v2: new field `device_type`."))
_reg(Drift("add_tier", "loyalty_tier added", add("loyalty_tier", lambda r: ["bronze", "silver", "gold"][_n(r) % 3]), "orders v2: new field `loyalty_tier`."))
_reg(Drift("add_discount", "discount_pct added", add("discount_pct", lambda r: float(_n(r) % 4 * 5)), "orders v2: new field `discount_pct`."))
_reg(Drift("add_meta", "nested meta object added", add("meta", lambda r: {"app_version": f"4.{_n(r) % 3}"}), "orders v2: new object `meta` with `app_version`."))
_reg(Drift("add_tags", "tags list added", add("tags", lambda r: ["gift"] if _n(r) % 4 == 0 else []), "orders v2: new list field `tags`."))
_reg(Drift("add_amount_tax", "lookalike column amount_tax added", add("amount_tax", lambda r: round(r["amount"] * 0.18, 2)),
           "orders v2: new field `amount_tax` (GST on the order). `amount` is unchanged."))
_reg(Drift("add_status_detail", "lookalike column status_detail added", add("status_detail", lambda r: "on_time"),
           "orders v2: new field `status_detail`. `status` is unchanged."))
_reg(Drift("case_order_id", "order_id is emitted as OrderID", rename("order_id", "OrderID"), "orders v2: field names are now emitted in PascalCase for `order_id`."))
_reg(Drift("case_amount_placed", "amount and placed_at emitted in PascalCase",
           compose(rename("amount", "Amount"), rename("placed_at", "PlacedAt")), "orders v2: `amount` and `placed_at` are now emitted as `Amount` and `PlacedAt`."))
_reg(Drift("ts_epoch_s", "placed_at is epoch seconds", timestamp("epoch_s"), "orders v2: `placed_at` is now a Unix epoch in seconds."))
_reg(Drift("ts_offset", "placed_at carries a +05:30 offset", timestamp("offset"), "orders v2: `placed_at` now carries an explicit +05:30 offset."))

# ---- unfixable: the information is gone ------------------------------------------------------------------------------
for col in ["order_id", "customer_id", "placed_at", "amount", "status", "city", "state", "channel", "item_count"]:
    _reg(Drift(f"drop_{col}", f"`{col}` is removed", drop(col), f"orders v2: field `{col}` removed from the feed."))
_reg(Drift("drop_city_state", "`city` and `state` are removed", drop("city", "state"), "orders v2: fields `city` and `state` removed from the feed."))


def apply_drifts(ids: list[str] | tuple[str, ...], rows: list[Row]) -> list[Row]:
    out = rows
    for i in ids:
        fn = DRIFTS[i].fn
        out = [fn(dict(r)) for r in out]
    return out


def notes_for(ids: list[str] | tuple[str, ...]) -> str:
    return " ".join(DRIFTS[i].note for i in ids)
