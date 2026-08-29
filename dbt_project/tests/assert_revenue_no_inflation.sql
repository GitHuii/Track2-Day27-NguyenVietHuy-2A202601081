-- Singular business test: revenue should not be inflated by duplicate active customers.
-- Expected correct revenue is sum of completed order amount_usd per day.
-- If customer dimension has duplicate active rows, left join will inflate count and sum.
-- This test flags days where fct_daily_revenue exceeds the source sum by >1%.
-- Query returns rows if inflation detected.
with completed_orders as (
    select order_date, sum(amount_usd) as true_revenue, count(*) as true_rows
    from {{ ref('stg_orders') }}
    where status = 'completed'
    group by 1
)
select
    f.order_date,
    f.daily_revenue,
    c.true_revenue,
    f.completed_order_rows,
    c.true_rows,
    (f.daily_revenue - c.true_revenue) as revenue_diff
from {{ ref('fct_daily_revenue') }} f
join completed_orders c using (order_date)
where abs(f.daily_revenue - c.true_revenue) > 0.01
   or f.completed_order_rows != c.true_rows
