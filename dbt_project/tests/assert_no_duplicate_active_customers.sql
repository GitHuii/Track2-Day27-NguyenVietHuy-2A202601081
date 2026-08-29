-- Singular test: staging should not contain duplicate active customer_id (SCD enforcement)
-- If violated, fct_daily_revenue will inflate due to join fanout.
select customer_id, count(*) as active_count
from {{ ref('stg_customers') }}
where is_active = true
group by 1
having count(*) > 1
