with hourly as (
    select
        timezone(
            'UTC',
            date_trunc('hour', timezone('UTC', interval_start_utc))
        ) as hour_utc,
        avg(consumption_mwh) as consumption_mwh
    from {{ ref('stg_fingrid_15m') }}
    where consumption_mwh is not null
    group by 1
)
select hour_utc, consumption_mwh
from hourly
