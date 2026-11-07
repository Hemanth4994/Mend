select
    cast(timezone('UTC', placed_at) as date) as order_date,
    count(*) as orders,
    count(distinct customer_id) as customers,
    sum(case when status <> 'cancelled' then amount else 0 end) as revenue
from {{ ref('stg_orders') }}
group by 1
