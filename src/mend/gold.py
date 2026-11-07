"""Hand-written correct repairs: the answer key for the evaluation.

These exist for two reasons: to prove that every repairable case really has a correct fix (the tests run each one and check the
result equals the answer key), and to give failure analysis something to compare a wrong repair with. The agent must never import
this module; a test enforces that.

A repair is the staging model with some columns re-expressed. History rows keep their old columns and drifted rows use the new
ones, so most repairs are coalesce(old, new). Two drifts change a column in place, so they need the date the change took effect.
"""
from __future__ import annotations

from . import feed

EFFECTIVE = feed.day_date(feed.DRIFT_DAY).isoformat()          # the first batch in the new shape

BASE = {
    "order_id": "order_id",
    "customer_id": "customer_id",
    "placed_at": "placed_at",
    "amount": "cast(amount as decimal(12, 2))",
    "currency": "currency",
    "status": "status",
    "city": "city",
    "state": "state",
    "channel": "channel",
    "item_count": "cast(item_count as integer)",
}


def _rename(old: str, new: str) -> dict[str, str]:
    expr = f"coalesce({old}, {new})"
    if old == "amount":
        expr = f"cast({expr} as decimal(12, 2))"
    if old == "item_count":
        expr = f"cast({expr} as integer)"
    return {old: expr}


def _split(delim: str) -> dict[str, str]:
    return {"city": f"coalesce(city, split_part(shipping_address, '{delim}', 1))",
            "state": f"coalesce(state, split_part(shipping_address, '{delim}', 2))"}


def _text_amount(col: str, strip: str) -> str:
    inner = col
    for ch in strip:
        inner = f"replace({inner}, '{ch}', '')"
    return f"cast(coalesce(amount, try_cast({inner} as double)) as decimal(12, 2))"


LETTERS = "case status when 'P' then 'placed' when 'S' then 'shipped' when 'D' then 'delivered' when 'C' then 'cancelled' else status end"
INTS = "case status when '0' then 'placed' when '1' then 'shipped' when '2' then 'delivered' when '3' then 'cancelled' else status end"
ORDER_STATUS_INTS = ("coalesce(status, case order_status when 0 then 'placed' when 1 then 'shipped' when 2 then 'delivered' "
                     "when 3 then 'cancelled' end)")
DMY = "strptime(placed_at__v_text, '%d/%m/%Y %H:%M:%S') at time zone 'UTC'"
IST = "interval '5 hours 30 minutes'"

O: dict[str, dict[str, str]] = {}
for _old, _new in [("amount", "total_amount"), ("order_id", "id"), ("placed_at", "created_at"), ("customer_id", "cust_id"),
                   ("status", "order_status"), ("channel", "sales_channel"), ("item_count", "qty")]:
    O[f"rename_{_old}"] = _rename(_old, _new)
O["nest_customer_id"] = {"customer_id": "coalesce(customer_id, customer__id)"}
O["nest_shipping"] = {"city": "coalesce(city, shipping__city)", "state": "coalesce(state, shipping__state)"}
O["nest_amount"] = {"amount": "cast(coalesce(amount, amount__value) as decimal(12, 2))", "currency": "coalesce(currency, amount__currency)"}
O["nest_customer_full"] = {"customer_id": "coalesce(customer_id, customer__id)", "city": "coalesce(city, customer__city)",
                           "state": "coalesce(state, customer__state)"}
O["pack_comma"], O["pack_pipe"], O["pack_dash"] = _split(", "), _split("|"), _split(" - ")

O["text_amount_rupee"] = {"amount": _text_amount("amount__v_text", "₹,")}
O["text_amount_comma"] = {"amount": _text_amount("amount__v_text", ",")}
O["paise"] = {"amount": f"cast(case when placed_at >= timestamptz '{EFFECTIVE} 00:00:00+00' then amount / 100.0 else amount end as decimal(12, 2))"}
O["ts_epoch_ms"] = {"placed_at": "coalesce(placed_at, to_timestamp(placed_at__v_bigint / 1000.0))"}
O["ts_dmy"] = {"placed_at": f"coalesce(placed_at, {DMY})"}
O["ts_ist_naive"] = {"placed_at": f"case when placed_at >= timestamptz '{EFFECTIVE} 00:00:00+00' then placed_at - {IST} else placed_at end"}
O["status_letters"] = {"status": LETTERS}
O["status_ints"] = {"status": INTS}
O["status_upper"] = {"status": "lower(status)"}
O["status_us_spelling"] = {"status": "replace(status, 'canceled', 'cancelled')"}
O["status_renamed"] = {"status": "case status when 'created' then 'placed' when 'fulfilled' then 'delivered' else status end"}
O["status_prefixed"] = {"status": "lower(replace(status, 'ORDER_', ''))"}

O["rename_paise"] = {"amount": "cast(coalesce(amount, amount_paise / 100.0) as decimal(12, 2))"}
O["nest_customer_letters"] = {**O["nest_customer_full"], "status": LETTERS}
O["pack_dmy"] = {**O["pack_comma"], **O["ts_dmy"]}
O["created_ist"] = {"placed_at": f"coalesce(placed_at, created_at - {IST})"}
O["text_rename"] = {"amount": f"cast(coalesce(amount, try_cast(replace(replace(total_amount, '₹', ''), ',', '') as double)) as decimal(12, 2))"}
O["nest_shipping_upper"] = {**O["nest_shipping"], **O["status_upper"]}
O["order_status_ints"] = {"status": ORDER_STATUS_INTS}
O["nest_amount_paise"] = {"amount": "cast(coalesce(amount, amount__value / 100.0) as decimal(12, 2))",
                          "currency": "coalesce(currency, amount__currency)"}
O["two_renames"] = {**O["rename_customer_id"], **O["rename_order_id"]}
O["pack_pipe_ints"] = {**O["pack_pipe"], "status": INTS}


def repair_sql(drift_id: str) -> str | None:
    """The correct staging model for a drift, or None when the information is gone (nothing correct exists)."""
    if drift_id not in O:
        return None
    cols = {**BASE, **O[drift_id]}
    body = ",\n    ".join(f"{expr} as {name}" for name, expr in cols.items())
    return f"select\n    {body}\nfrom {{{{ source('raw', 'orders') }}}}\n"
