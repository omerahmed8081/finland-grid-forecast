with ordered_hours as (
    select
        hour_utc,
        lag(hour_utc) over (order by hour_utc) as previous_hour
    from {{ ref('features') }}
)
select hour_utc, previous_hour
from ordered_hours
where previous_hour is not null
  and epoch(hour_utc) - epoch(previous_hour) != 3600
