-- NOTE: This model is intentionally simple. If the customer dimension has more
-- than one active row per customer, the join can inflate revenue without a SQL
-- error. Students should add tests/unit tests that expose this failure mode.

with completed_orders as (
    select *
    from {{ ref('stg_orders') }}
    where status = 'completed'
),
active_customers as (
    -- Deduplicate SCD Type-2 dimension: exactly one active row per customer_id.
    -- Starter bug used `select *` causing join fanout (2 active rows -> revenue x2).
    -- Robust alternative is `row_number() over (partition by customer_id order by valid_from desc)=1`,
    -- here we use `group by customer_id` as minimal dedupe (keeps one per customer).
    -- Both prevent inflation: unit test `revenue_inflation_with_duplicate_active_customers` expects 2/170 not 4/340.
    select customer_id
    from {{ ref('stg_customers') }}
    where is_active = true
    group by customer_id
)
select
    o.order_date,
    count(*) as completed_order_rows,
    sum(o.amount_usd) as daily_revenue
from completed_orders o
left join active_customers c
    on o.customer_id = c.customer_id
group by 1
order by 1
