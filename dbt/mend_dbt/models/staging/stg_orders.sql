-- Staging contract: one row per order, canonical names and types.
-- This file is what the repair agent edits. Keep the source() reference.
select
    order_id,
    customer_id,
    placed_at,
    cast(amount as decimal(12, 2)) as amount,
    currency,
    status,
    city,
    state,
    channel,
    cast(item_count as integer) as item_count
from {{ source('raw', 'orders') }}
