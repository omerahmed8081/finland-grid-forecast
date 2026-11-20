with source_rows as (
    select
        timezone('UTC', start_time) as interval_start_utc,
        timezone('UTC', end_time) as interval_end_utc,
        try_cast(value as double) as consumption_mwh,
        row_number() over (
            partition by start_time, end_time
            order by value desc nulls last
        ) as duplicate_rank
    from {{ source('raw', 'fingrid_124') }}
)
select
    timezone('UTC', interval_start_utc) as interval_start_utc,
    timezone('UTC', interval_end_utc) as interval_end_utc,
    consumption_mwh
from source_rows
where duplicate_rank = 1
  and interval_end_utc > interval_start_utc
