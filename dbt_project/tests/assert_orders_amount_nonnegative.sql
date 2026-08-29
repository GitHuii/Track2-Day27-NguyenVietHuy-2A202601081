-- Singular test: stg_orders amount_usd must be >=0
select *
from {{ ref('stg_orders') }}
where amount_usd < 0
